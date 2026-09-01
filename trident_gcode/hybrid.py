"""Orchestrate a hybrid planar-base + non-planar-spiral print.

Solid planar layers (real walls + infill, sliced by OrcaSlicer) from z=0 up
to a chosen transition height, then the existing non-planar spiral continues
above that height to the top of the model -- v1's single-split scope for
combining planar and non-planar printing in one print.

Two entry points, differing only in where the planar base comes from and, as
a direct consequence, in what `height` means:

* :func:`build_hybrid_print` -- the base is a silhouette extrusion of the same
  parametric shape as the wall, and `height` is the WHOLE object (the base
  eats into it).
* :func:`build_mesh_hybrid_print` -- the base is the user's own imported STL,
  sliced by Orca as a true solid so holes and internal features survive, and
  `height` is ONLY the non-planar wall ABOVE the mesh (a 20 mm mount plus
  height=200 is a 220 mm object).

Both cross Orca's trust boundary through the single shared
:func:`_slice_replay_and_seam`, so neither can grow a weaker version of the
parse/replay/placement/retract sequence than the other.

Orca's own text output is never trusted or emitted verbatim: it is parsed
(orca_gcode_parser.py) and replayed (orca_replay.py) onto this app's own
GcodeWriter, so every existing safety check (_check_bounds, non-finite
rejection, PrinterProfile clamps) applies to the planar portion exactly as
it does to the non-planar portion. There is no silent fallback to a
non-planar-only print anywhere in this module -- every failure raises.
"""
from __future__ import annotations

import math
from typing import Callable

from .gcode import GcodeWriter
from .mesh import Triangle, mesh_bounds, slice_outer_loop
from .orca import FilamentSettings
from .orca_gcode_parser import parse_orca_gcode
from .orca_replay import replay_moves_onto_writer
from .orca_slice import (
    build_filament_json,
    build_machine_json,
    build_process_json,
    slice_stl_to_gcode,
)
from .profile_stack import (
    blend_stack,
    mesh_xy_midpoint,
    stack_from_shape,
    top_contour_from_mesh,
)
from .stl_export import contours_to_mesh, write_binary_stl
from .generators.profile_spiral import build_profile_spiral

_PLACEMENT_TOLERANCE_MM = 2.0

# How far the top of Orca's own extruding moves may sit from the seam height
# this module predicted before slicing, as a FRACTION OF ONE LAYER. Half a
# layer is the whole slack the layer-count rounding can produce, so anything
# beyond it means Orca added or dropped a whole layer -- which would leave the
# non-planar wall starting a layer above or below the surface it is supposed to
# land on. Expressed as a fraction of the caller's own layer_height rather than
# a millimetre figure: this is a rounding tolerance, not a machine limit, and
# it must scale with whatever layer height the caller asked for.
_SEAM_Z_TOLERANCE_LAYERS = 0.5


def _slice_replay_and_seam(
    writer: GcodeWriter,
    *,
    stl_bytes: bytes,
    layer_height: float,
    wall_count: int,
    infill_density: float,
    infill_pattern: str,
    orca_path: str,
    top_shell_layers: int = 0,
    bottom_shell_layers: int = 3,
    process_overrides: dict | None = None,
    filament_name: str | None,
    cx: float,
    cy: float,
    seam_z: float,
) -> tuple[dict, list]:
    """The one trust boundary between OrcaSlicer and this app's own output.

    Builds the machine/process/filament JSON from ``writer.profile``, slices
    *stl_bytes*, parses the result, emits the print's single header, replays
    every parsed move through ``GcodeWriter`` (so ``_check_bounds``,
    non-finite rejection and the ``PrinterProfile`` clamps apply to the planar
    portion exactly as they do to the non-planar portion), sanity-checks where
    Orca actually placed the base, retracts if the replay did not already end
    retracted, and writes the viewer's seam marker comment at *seam_z*.

    Returns ``(replay_report, moves)``. Both hybrid entry points -- the
    parametric :func:`build_hybrid_print` and the mesh-based
    :func:`build_mesh_hybrid_print` -- go through here, so neither can grow a
    weaker version of these checks than the other.

    Raises OrcaSliceError (from orca_slice.py), OrcaGcodeParseError (from
    orca_gcode_parser.py) or ValueError. Never falls back to anything.
    """
    profile = writer.profile

    # 1. Machine/process/filament JSON, entirely derived from *profile*.
    filament = FilamentSettings.from_orca(filament_name) if filament_name else None
    machine_json = build_machine_json(profile)
    process_json = build_process_json(
        profile, layer_height=layer_height, first_layer_height=layer_height,
        wall_count=wall_count, infill_density=infill_density,
        infill_pattern=infill_pattern, line_width=writer.line_width,
        top_shell_layers=top_shell_layers, bottom_shell_layers=bottom_shell_layers,
        # Caller-tunable process settings (speeds, adhesion, support). Passed
        # straight to build_process_json rather than re-validated here: that
        # function is the single place brim/speed/support values are checked
        # and clamped against the PrinterProfile, and a second copy of those
        # rules here is exactly the drift CLAUDE.md warns about. An unknown key
        # raises TypeError there rather than reaching the subprocess.
        **(process_overrides or {}),
    )
    filament_json = build_filament_json(filament, profile)

    # 2. Slice. Raises OrcaSliceError on any failure -- caught by the caller
    #    (serve.py), never swallowed here. OrcaSliceError already carries
    #    Orca's own captured stderr verbatim (orca_slice.py), which is the
    #    likeliest real-world failure for a hand-modelled mount: our own 2D
    #    slicer is more tolerant than Orca's 3D mesh repair.
    gcode_text = slice_stl_to_gcode(
        stl_bytes, machine_json=machine_json, process_json=process_json,
        filament_json=filament_json, orca_path=orca_path,
    )

    # 3. Parse. Raises OrcaGcodeParseError on anything not trusted.
    moves = parse_orca_gcode(gcode_text)

    # 4. The ONE header call for the whole hybrid print -- the wall above
    #    resumes rather than emitting its own.
    writer.header()

    # 5. Replay onto the writer -- every move still passes through
    #    GcodeWriter's own _check_bounds()/non-finite guards.
    replay_report = replay_moves_onto_writer(writer, moves, profile=profile)

    # 6. Placement sanity check: refuse to continue a hybrid print with a
    #    base Orca placed off-target, rather than silently printing it in
    #    the wrong spot under a correctly-centered wall.
    #
    #    Only EXTRUDING moves count: Orca's very first body line is
    #    typically a position-less retract (e.g. "G1 E-.6") emitted before
    #    any real travel has happened, which the parser necessarily reports
    #    at its (x=0, y=0) starting default (see orca_gcode_parser.py) --
    #    including that point here would drag the computed bounding box
    #    toward the origin regardless of where the actual geometry is.
    extrude_moves = [m for m in moves if m.e_delta is not None and m.e_delta > 0]
    if extrude_moves:
        xs = [m.x for m in extrude_moves]
        ys = [m.y for m in extrude_moves]
        placed_cx = (min(xs) + max(xs)) / 2.0
        placed_cy = (min(ys) + max(ys)) / 2.0
        if (abs(placed_cx - cx) > _PLACEMENT_TOLERANCE_MM
                or abs(placed_cy - cy) > _PLACEMENT_TOLERANCE_MM):
            raise ValueError(
                "OrcaSlicer placed the base off-target "
                f"(bounding-box center {placed_cx:.1f},{placed_cy:.1f} vs "
                f"intended {cx:.1f},{cy:.1f}); refusing to continue a "
                "hybrid print with a misaligned base."
            )

    # 7. Defensive retract, mirroring every existing generator's own
    #    pre-footer retract -- only if the replay didn't already end
    #    retracted (avoids a double retract if Orca's own gcode already
    #    ended with one, which it normally does).
    if not replay_report.get("ends_retracted", False):
        writer.retract()

    # Marker comment for the viewer's 3D preview -- it scans the served
    # gcode text client-side for this literal string and draws one reference
    # plane at this Z, so the hybrid seam is visible without plumbing new
    # per-move metadata through the API. writer.comment() already exists;
    # no new GcodeWriter capability needed.
    writer.comment(f"hybrid: non-planar wall begins here (z={seam_z:.4f})")

    return replay_report, moves


def build_hybrid_print(
    writer: GcodeWriter,
    *,
    shape_fn: Callable[[float], float],
    radius: float,
    height: float,
    transition_height: float,
    layer_height: float,
    points_per_turn: int,
    wall_count: int,
    infill_density: float,
    infill_pattern: str,
    orca_path: str,
    filament_name: str | None = None,
    radius_envelope: Callable[[float], float] | None = None,
    amp_envelope: Callable[[float], float] | None = None,
    center: tuple[float, float] | None = None,
    **profile_spiral_kwargs,
) -> dict:
    """Emit a hybrid print: an OrcaSlicer-sliced planar base up to
    *transition_height*, then the existing non-planar spiral wall for the
    rest of *height*. Returns a merged report dict.

    *shape_fn*/*radius*/*points_per_turn* are shared between the base and
    the wall so the seam is a per-vertex match (no visible step).

    *radius_envelope* and *amp_envelope* are the caller's WHOLE-OBJECT
    curves (height fraction t in [0,1], 0=bed 1=top -- the same meaning
    `radius_profile`/`amp_profile` already have everywhere else in the UI).
    This function -- not the caller -- rescales them onto each of the two
    local segments (base: [0, seam_t], wall: [seam_t, 1]) once
    achieved_base_height is known, since that value is only computed here
    (step 1, layer-snapped) and isn't available to a caller in advance.
    *profile_spiral_kwargs* must NOT include `z_amp_envelope` -- pass the
    whole-object curve via *amp_envelope* instead, so it gets the same
    rescaling treatment.

    Raises OrcaSliceError (from orca_slice.py) or ValueError on any
    failure -- there is never a silent fallback to a non-planar-only print.
    """
    profile = writer.profile

    # 1. Snap to a whole number of Orca layers; the ACHIEVED value (not the
    #    requested one) drives everything downstream. At least TWO layers --
    #    not one -- because contours_to_mesh() needs two distinct Z rings to
    #    build side walls at all; a single-layer "base" has no height
    #    difference between its top and bottom ring.
    n_base_layers = max(2, round(transition_height / layer_height))
    achieved_base_height = n_base_layers * layer_height
    seam_t = achieved_base_height / height if height > 0 else 0.0

    def _base_radius_env(t_local):
        return radius_envelope(t_local * seam_t)

    def _wall_radius_env(t_local):
        return radius_envelope(seam_t + t_local * (1.0 - seam_t))

    def _wall_amp_env(t_local):
        return amp_envelope(seam_t + t_local * (1.0 - seam_t))

    # 2. Base contour stack: silhouette only, same shape_fn as the wall.
    base_contours = stack_from_shape(
        shape_fn, radius, achieved_base_height, layer_height, points_per_turn,
        radius_envelope=(_base_radius_env if radius_envelope is not None else None),
    )
    base_heights = [i * layer_height for i in range(len(base_contours))]

    # 3. Absolute bed coordinates -- bed center, NOT origin-centered like
    #    contours_to_mesh's usual /api/export_stl convention. Intentional:
    #    Orca needs to place this where the whole print actually sits.
    cx, cy = center if center is not None else profile.bed_center
    translated = [[(x + cx, y + cy) for (x, y) in ring] for ring in base_contours]

    # 4. Watertight STL for Orca to slice.
    tris = contours_to_mesh(translated, base_heights, cap_bottom=True, cap_top=True)
    stl_bytes = write_binary_stl(tris)

    # 5. Slice -> parse -> header -> replay -> placement check -> retract ->
    #    seam marker. Shared verbatim with build_mesh_hybrid_print() so both
    #    paths cross Orca's trust boundary through the same code.
    replay_report, _moves = _slice_replay_and_seam(
        writer, stl_bytes=stl_bytes, layer_height=layer_height,
        wall_count=wall_count, infill_density=infill_density,
        infill_pattern=infill_pattern, orca_path=orca_path,
        filament_name=filament_name, cx=cx, cy=cy,
        seam_z=achieved_base_height,
    )

    # 6. Upper (non-planar) contour stack + wall, resuming from wherever
    #     the replay left the toolhead.
    upper_height = height - achieved_base_height
    upper_contours = stack_from_shape(
        shape_fn, radius, upper_height, layer_height, points_per_turn,
        radius_envelope=(_wall_radius_env if radius_envelope is not None else None),
    )
    upper_heights = [i * layer_height for i in range(len(upper_contours))]
    wall_report = build_profile_spiral(
        writer, upper_contours, upper_heights,
        points_per_turn=points_per_turn,
        resume=True, base_z=achieved_base_height, base_layers=0,
        center=(cx, cy),
        z_amp_envelope=(_wall_amp_env if amp_envelope is not None else None),
        **profile_spiral_kwargs,
    )

    return {
        "hybrid": True,
        "achieved_base_height_mm": achieved_base_height,
        "orca_base_layers": n_base_layers,
        **replay_report,
        **wall_report,
    }


# ---------------------------------------------------------------------------
# Mesh-based hybrid: the user's own STL is the planar base.
# ---------------------------------------------------------------------------

def _scaled(tris: list[Triangle], k: float) -> list[Triangle]:
    """Uniform vertex scale, mirroring serve.py's own mesh-scale step."""
    return [tuple((vx * k, vy * k, vz * k) for (vx, vy, vz) in t) for t in tris]


def _translated(tris: list[Triangle], dx: float, dy: float, dz: float) -> list[Triangle]:
    return [tuple((vx + dx, vy + dy, vz + dz) for (vx, vy, vz) in t) for t in tris]


def build_mesh_hybrid_print(
    writer: GcodeWriter,
    *,
    tris: list[Triangle],
    scale: float,
    layer_height: float,
    points_per_turn: int,
    shape_fn: Callable[[float], float],
    radius: float,
    height: float,
    blend_height: float,
    wall_count: int,
    infill_density: float,
    infill_pattern: str,
    orca_path: str,
    top_shell_layers: int = 3,
    bottom_shell_layers: int = 3,
    process_overrides: dict | None = None,
    center: tuple[float, float] | None = None,
    radius_envelope: Callable[[float], float] | None = None,
    amp_envelope: Callable[[float], float] | None = None,
    filament_name: str | None = None,
    **profile_spiral_kwargs,
) -> dict:
    """Emit a hybrid print whose planar base is the user's OWN solid STL.

    The imported mesh (a Fusion-modelled pendant-cord mount, say) is sliced by
    OrcaSlicer as a real solid -- walls, infill, holes, bosses and internal
    features all preserved, because the bytes handed to Orca are
    ``write_binary_stl(tris)`` on the user's actual triangles. There is NO
    ``contours_to_mesh`` round-trip here; that is the entire difference from
    :func:`build_hybrid_print`, whose base is a silhouette extrusion of the
    same parametric shape as its wall. Above the mesh, the existing non-planar
    spiral continues from the mesh's real top contour.

    HEIGHT SEMANTICS -- DELIBERATELY DIFFERENT FROM :func:`build_hybrid_print`
    -------------------------------------------------------------------------
    *height* here is ONLY the non-planar portion ABOVE the mesh. A 20 mm mount
    with ``height=200`` is a 220 mm object; in :func:`build_hybrid_print`,
    ``height=200`` would be the whole 200 mm object with the base eating into
    it. That is why *radius_envelope* and *amp_envelope* are passed straight
    through with their ``t in [0, 1]`` mapping over the WALL SEGMENT ALONE and
    get no height-fraction rescaling: there is no whole-object fraction to
    rescale onto, unlike ``build_hybrid_print``'s ``seam_t`` closures.

    Order of operations (each step exists for a reason)
    ---------------------------------------------------
    1. Uniform *scale* on the vertices (same step as serve.py's mesh path).
    2. ``achieved_base_height`` is computed UP FRONT and deterministically,
       before Orca is ever invoked, exactly as the parametric path does:
       ``n_base_layers = max(2, round(mesh_height / layer_height))``. It is
       never derived from the parsed moves afterwards, because the seam ring
       has to be sampled at the Z that will actually be printed at the seam --
       sample it anywhere else and the wall's first ring does not match the
       surface beneath it.
    3. Translate so :func:`mesh_xy_midpoint` (the exported one -- never a
       second, subtly different midpoint) lands on *center*, and ``minz`` -> 0
       so the mesh sits on the bed.
    4. Every cheap pre-flight check runs BEFORE the subprocess: footprint fits
       the bed, total height fits ``profile.z_max``, the mesh is sliceable and
       Z-up, and the seam contour at ``achieved_base_height`` exists, is a
       single loop and is star-convex about the placement origin
       (:func:`top_contour_from_mesh`'s own checks). ``slice_stl_to_gcode``
       has a 120 s timeout and runs with ``--arrange 0``, so a bad mesh must
       fail fast and clearly in-process rather than after a slow, opaque
       external failure.
    5. After slicing, the top of Orca's own extruding moves must agree with
       the predicted ``achieved_base_height`` to within half a layer -- if
       Orca silently added or dropped a layer this RAISES rather than leaving
       the seam at the wrong height.

    *blend_height*
    --------------
    Passed to :func:`blend_stack` verbatim -- it is NOT clamped or rewritten
    here. ``blend_stack`` already documents that a *blend_height* strictly
    greater than the span of the wall leaves the topmost ring partway through
    the blend, and having this function quietly disagree with that would give
    the same argument two meanings. Note the wall's topmost ring sits at
    ``(n_layers - 1) * layer_height``, not at *height*, so
    ``blend_height == height`` still leaves the top ring a fraction short of
    fully parametric. A caller that wants "fully parametric by the top ring"
    should pass ``blend_height = (n_layers - 1) * layer_height`` with
    ``n_layers = max(1, round(height / layer_height))``. The report says which
    of the two happened: ``blend_reaches_parametric`` and ``blend_top_weight``.

    Overhang
    --------
    Neither this function nor ``blend_stack`` applies a printable-overhang
    ceiling to the blend rate: that ceiling belongs to the selected
    ``PrinterProfile`` (CLAUDE.md -- no machine limit may be a module
    constant), and this module does not own the profile's quality policy. The
    blend's steepest layer-to-layer step is surfaced as DATA instead --
    ``blend_max_step_mm``, ``blend_max_slope`` (horizontal per vertical) and
    ``blend_max_overhang_deg`` -- so the server layer can compare it against
    ``slope_ceiling(profile)`` and warn or refuse with the machine's own
    number.

    Raises OrcaSliceError (from orca_slice.py, carrying Orca's captured
    stderr verbatim), OrcaGcodeParseError or ValueError on any failure --
    there is never a silent fallback to a non-planar-only print.
    """
    profile = writer.profile

    if "z_amp_envelope" in profile_spiral_kwargs:
        raise ValueError(
            "pass the wall's amplitude curve as amp_envelope=, not as "
            "z_amp_envelope= in profile_spiral_kwargs -- one name, one meaning"
        )
    for name, val in (("scale", scale), ("layer_height", layer_height),
                      ("height", height), ("blend_height", blend_height),
                      ("radius", radius)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) \
                or not math.isfinite(float(val)):
            raise ValueError(
                "%s must be a finite number, got %r -- a non-finite value "
                "passes every comparison downstream instead of tripping it, "
                "so it is rejected here rather than clamped" % (name, val))
    if scale <= 0.0:
        raise ValueError("scale must be positive, got %r" % (scale,))
    if layer_height <= 0.0:
        raise ValueError("layer_height must be positive, got %r" % (layer_height,))
    if height <= 0.0:
        raise ValueError(
            "height must be positive, got %r -- for this mesh hybrid it is the "
            "non-planar wall ABOVE the mesh, not the whole object" % (height,))
    if blend_height < 0.0:
        raise ValueError("blend_height must not be negative, got %r" % (blend_height,))
    if not tris:
        raise ValueError("no triangles to slice -- the imported mesh is empty")

    # 1. Uniform scale, mirroring serve.py's own mesh-scale step.
    if scale != 1.0:
        tris = _scaled(tris, scale)

    (minx, miny, minz), (maxx, maxy, maxz) = mesh_bounds(tris)
    mesh_height = maxz - minz
    if not (mesh_height > 0.0):
        raise ValueError(
            "the mesh has no Z extent (%.4f mm), so there is nothing to slice "
            "into a planar base. Export the model Z-up (the print direction is "
            "+Z) and check the units." % (mesh_height,))

    # 2. The seam height, decided BEFORE Orca runs.
    #
    #    It is the mesh's own top, NOT a layer-snapped multiple. The parametric
    #    path snaps (build_hybrid_print's max(2, round(...))) because THIS module
    #    generates that base's geometry and so gets to choose its height. Here
    #    the geometry is the user's file: Orca slices the solid it is given and
    #    adapts its own layer count to land on the model's true top. Snapping
    #    here is wrong in both directions -- measured against a real 5.000 mm
    #    mount at 0.3 mm layers, round() gave 5.1 mm (above the mesh, so the
    #    seam ring had no cross-section at all) and floor() gave 4.8 mm, which
    #    then tripped the drift check below because Orca had actually printed
    #    to 5.000 mm.
    achieved_base_height = mesh_height
    if achieved_base_height < 2.0 * layer_height:
        raise ValueError(
            "the mesh is only %.3f mm tall, which is under two %.3f mm layers, "
            "so there is no solid base to print before the non-planar wall "
            "starts. Use a taller model or a smaller layer height."
            % (mesh_height, layer_height))
    # Informational only (reported as orca_base_layers); Orca owns the real
    # layer count, so nothing downstream may depend on this figure.
    n_base_layers = max(1, int(round(mesh_height / layer_height)))

    # 3. Placement: the mesh's bounding-box midpoint (the EXPORTED one, so it
    #    cannot drift from the value top_contour_from_mesh measures its ring
    #    against) goes to *center*, and the mesh sits on the bed.
    mx, my = mesh_xy_midpoint(tris)
    cx, cy = center if center is not None else profile.bed_center
    tris = _translated(tris, cx - mx, cy - my, -minz)

    # 4. Pre-flight, all of it before the subprocess. ------------------------
    span_x, span_y = maxx - minx, maxy - miny
    lo_x, hi_x = cx - span_x / 2.0, cx + span_x / 2.0
    lo_y, hi_y = cy - span_y / 2.0, cy + span_y / 2.0
    if (lo_x < 0.0 or lo_y < 0.0
            or hi_x > profile.bed_size_x or hi_y > profile.bed_size_y):
        raise ValueError(
            "the mesh footprint %.1f x %.1f mm centered at (%.1f, %.1f) spans "
            "X[%.1f-%.1f] Y[%.1f-%.1f], outside this printer's %.0f x %.0f mm "
            "bed. Scale the model down or move it." % (
                span_x, span_y, cx, cy, lo_x, hi_x, lo_y, hi_y,
                profile.bed_size_x, profile.bed_size_y))

    total_height = achieved_base_height + height
    if total_height > profile.z_max:
        raise ValueError(
            "the mesh base (%.2f mm) plus the non-planar wall (%.2f mm) is "
            "%.2f mm tall, above this printer's %.0f mm Z limit. NOTE: for a "
            "mesh hybrid, height is the wall ABOVE the mesh, not the whole "
            "object." % (achieved_base_height, height, total_height,
                         profile.z_max))

    # Is it sliceable at all? A mesh with no printable cross-section has to
    # fail HERE, in-process and with a real message, not 120 s later inside an
    # opaque external slicer that was also told not to lay it flat
    # (--arrange 0, orca_slice.py).
    mid_loop = slice_outer_loop(tris, mesh_height / 2.0)
    if mid_loop is None or len(mid_loop) < 4:
        raise ValueError(
            "no printable cross-section at the mesh's own mid-height "
            "(z=%.3f mm) -- is the STL watertight and Z-up? Nothing was sent "
            "to OrcaSlicer." % (mesh_height / 2.0,))

    # The seam ring, extracted before slicing: top_contour_from_mesh raises on
    # a degenerate, multi-loop or non-star-convex section, and every one of
    # those is cheaper to discover now than after a 120 s slice.
    #
    #    Sampled at the MIDDLE of the last printed layer, not at its top face:
    #    stack_from_mesh already samples every layer at minz+(i+0.5)*layer_height
    #    for the same reason, and slicing exactly at a closed mesh's top face is
    #    degenerate (the plane touches it rather than cutting through it). Half a
    #    layer down is still the outline of the surface the wall lands on for any
    #    wall that is vertical over its last layer, and it is always strictly
    #    inside the solid -- including when the mesh height is an exact multiple
    #    of the layer height and achieved_base_height lands right on maxz.
    mesh_ring = top_contour_from_mesh(
        tris, points_per_turn, z=achieved_base_height - layer_height * 0.5)

    # 5. The user's TRUE solid, straight to Orca. No contours_to_mesh round
    #    trip: that would flatten the model to a silhouette extrusion and
    #    silently discard exactly the holes and internal features this
    #    feature exists to preserve.
    stl_bytes = write_binary_stl(tris)

    # 6. Slice -> parse -> header -> replay -> placement check -> retract ->
    #    seam marker. The same trust boundary the parametric path crosses.
    replay_report, moves = _slice_replay_and_seam(
        writer, stl_bytes=stl_bytes, layer_height=layer_height,
        wall_count=wall_count, infill_density=infill_density,
        infill_pattern=infill_pattern, orca_path=orca_path,
        top_shell_layers=top_shell_layers, bottom_shell_layers=bottom_shell_layers,
        process_overrides=process_overrides,
        filament_name=filament_name, cx=cx, cy=cy,
        seam_z=achieved_base_height,
    )

    # 7. Did Orca agree with the prediction? Same idiom as the placement
    #    sanity check above, and extruding moves only for the same reason
    #    (a position-less leading retract reports at the parser's own
    #    starting default). If Orca added or dropped a layer, the wall's
    #    first ring -- sampled from the mesh at achieved_base_height -- is
    #    no longer the outline of the surface it lands on, so this raises
    #    instead of printing a mismatched seam.
    extruded_z = [m.z for m in moves if m.e_delta is not None and m.e_delta > 0]
    if not extruded_z:
        raise ValueError(
            "OrcaSlicer produced no extruding moves for this mesh, so there "
            "is no planar base to continue from; refusing to print a "
            "non-planar wall floating at z=%.3f mm." % (achieved_base_height,))
    orca_top_z = max(extruded_z)
    tolerance = _SEAM_Z_TOLERANCE_LAYERS * layer_height
    if abs(orca_top_z - achieved_base_height) > tolerance:
        raise ValueError(
            "OrcaSlicer's base tops out at z=%.4f mm but the seam was planned "
            "for z=%.4f mm (tolerance %.4f mm, half a layer): Orca added or "
            "dropped a layer, so the wall's first ring no longer matches the "
            "surface under it. Refusing to continue." % (
                orca_top_z, achieved_base_height, tolerance))

    # 8. The wall: starts as the mesh's own top outline and eases into the
    #    parametric shape. radius_envelope/amp_envelope map over THIS segment
    #    alone (see the height-semantics note in the docstring) -- no
    #    whole-object rescaling closures here, deliberately.
    contours, heights = blend_stack(
        mesh_ring, shape_fn, radius, height, layer_height, points_per_turn,
        blend_height, radius_envelope=radius_envelope,
    )
    wall_report = build_profile_spiral(
        writer, contours, heights,
        points_per_turn=points_per_turn,
        resume=True, base_z=achieved_base_height, base_layers=0,
        center=(cx, cy),
        z_amp_envelope=amp_envelope,
        **profile_spiral_kwargs,
    )

    # Blend diagnostics, surfaced as data for the server layer to judge
    # against slope_ceiling(profile) -- never judged against a constant here.
    max_step = 0.0
    for i in range(1, len(contours)):
        for (x0, y0), (x1, y1) in zip(contours[i - 1], contours[i]):
            d = math.hypot(x1 - x0, y1 - y0)
            if d > max_step:
                max_step = d
    if len(contours) > 1:
        t_top = 1.0 if blend_height <= 0.0 else (
            (len(contours) - 1) * layer_height / blend_height)
        t_top = min(max(t_top, 0.0), 1.0)
        top_weight = t_top * t_top * (3.0 - 2.0 * t_top)
    else:
        top_weight = 0.0          # a one-ring wall IS the mesh ring

    return {
        "hybrid": True,
        "mesh_base": True,
        "achieved_base_height_mm": achieved_base_height,
        "orca_base_layers": n_base_layers,
        "orca_base_top_z_mm": orca_top_z,
        "mesh_height_mm": mesh_height,
        "wall_height_mm": height,
        "total_height_mm": total_height,
        "wall_top_z_mm": achieved_base_height + heights[-1],
        "blend_height_mm": blend_height,
        "blend_top_weight": top_weight,
        "blend_reaches_parametric": top_weight >= 1.0 - 1e-12,
        "blend_max_step_mm": max_step,
        "blend_max_slope": max_step / layer_height,
        "blend_max_overhang_deg": math.degrees(math.atan2(max_step, layer_height)),
        **replay_report,
        **wall_report,
    }
