"""Non-planar surface-following: pave a height field with a continuous spiral.

Generates an Archimedean spiral over a disk (centre -> edge) and rides Z along a
:data:`~trident_gcode.surface.HeightField`, so the printed shell conforms to the
surface with no stair-stepping. Stacking several shells (alternating out/in to stay
continuous) builds a thick conformal wall - a continuous non-planar thick print.

Bed adhesion
------------
Until 2026-09-12 this was the one print mode in the app with no adhesion of any
kind: the shell began as a single thin spiral laid straight onto bare glass at
``first_layer_z``. It now carries the same package every other mode has --
``first_layer_squish``, an optional solid base disk (``base_layers``) and a
``brim_loops`` skirt of outward rings -- built by reusing
:mod:`trident_gcode.generators.base_fill` rather than new geometry. The shell's
footprint is a plain disk of ``radius``, which trivially satisfies that module's
star-convexity requirement (see its module docstring).

Every adhesion parameter defaults to a value that reproduces the pre-adhesion
output byte-for-byte; ``regression_ref/ref_surface_spiral.gcode`` locks that and
``regression_ref/ref_surface_spiral_adhesion.gcode`` locks the new geometry.
"""
from __future__ import annotations

import math

from ..gcode import GcodeWriter
from ..surface import HeightField
from .base_fill import blend_layer_z, brim_outer_radius, layered_base_and_brim

# Paving styles understood by base_fill.layered_base_and_brim().
BASE_STYLES = ("spiral", "concentric")


# --------------------------------------------------------------- boundary guards
def _finite(name: str, value) -> float:
    """Coerce to float and REJECT (never clamp) a non-finite value.

    Every comparison against NaN is False, so a NaN survives ``min()``/``max()``
    clamping untouched and sails through ``GcodeWriter._check_bounds``' X/Y test
    silently; ``json.loads`` will happily hand a server the bare tokens ``NaN``
    and ``Infinity``. This generator is a library boundary of its own -- it must
    not depend on serve.py having caught it first.
    """
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(
            f"{name} must be a finite number, got {value!r} "
            f"(non-finite values are rejected here, never clamped)")
    return v


def _count(name: str, value, *, minimum: int = 0) -> int:
    """Coerce to a whole number >= ``minimum``, rejecting anything else.

    Floats are accepted only when they are exactly integral, so a NaN/Infinity
    count is refused rather than silently truncated toward zero.
    """
    v = value
    if isinstance(v, bool):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if isinstance(v, float):
        if not math.isfinite(v) or v != int(v):
            raise ValueError(f"{name} must be a whole number, got {value!r}")
        v = int(v)
    if not isinstance(v, int):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if v < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {v}")
    return v


def _archimedean(radius: float, pitch: float, resolution: float) -> list[tuple[float, float]]:
    """Points along a spiral from centre to ``radius``; turns spaced by ``pitch``."""
    a = pitch / (2.0 * math.pi)          # r = a * theta
    theta_max = radius / a if a > 0 else 0.0
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    theta = 0.0
    # advance by ~constant arc length: dtheta = ds / r, with a floor near the centre
    while theta < theta_max:
        r = a * theta
        dtheta = resolution / max(r, pitch * 0.25)
        theta += dtheta
        r = a * theta
        pts.append((r * math.cos(theta), r * math.sin(theta)))
    return pts


def surface_spiral_geometry(
    writer: GcodeWriter,
    field: HeightField,
    radius: float,
    *,
    shells: int = 1,
    resolution: float = 0.4,
    first_layer_z: float | None = None,
    first_layer_squish: float = 1.0,
    base_layers: int = 0,
    brim_loops: int = 0,
) -> dict:
    """Resolve the spiral's sample points and every Z the print will use.

    Split out of :func:`build_surface_spiral` (which calls it) so a caller that
    has to pre-flight the toolpath -- serve.py's probe-slope check runs over the
    same sample points and the same Z the generator will emit -- cannot drift
    from the real geometry by re-deriving it by hand. There is exactly one
    implementation of this arithmetic; use it rather than copying it.

    Returns a dict with:
      ``base_pts``        the spiral's part-local (x, y) samples, centre -> edge.
      ``field_min`` / ``field_max``  the height field over those samples.
      ``first_layer_z``   Z of whatever touches the bed first (base disk 0 if
                          there is one, otherwise the shell itself).
      ``shell_floor_z``   Z of the shell's LOWEST point. Equals
                          ``first_layer_z`` with no base, and
                          ``first_layer_z + base_layers * layer_height`` with
                          one -- i.e. exactly one layer above the top base
                          disk (which sits at ``first_layer_z +
                          (base_layers - 1) * layer_height``), so the shell
                          neither re-prints into its own base nor floats above
                          it.
      ``z_offset``        the lift applied to the whole field
                          (``shell_floor_z - field_min``).
      ``top_z``           the highest Z the print reaches.
    """
    radius = _finite("radius", radius)
    resolution = _finite("resolution", resolution)
    squish = _finite("first_layer_squish", first_layer_squish)
    shells = _count("shells", shells, minimum=1)
    base_layers = _count("base_layers", base_layers, minimum=0)
    brim_loops = _count("brim_loops", brim_loops, minimum=0)
    if radius <= 0.0:
        raise ValueError(f"radius must be positive, got {radius}")
    if resolution <= 0.0:
        raise ValueError(
            f"resolution must be positive, got {resolution} "
            f"(arc length in mm between spiral samples; ~0.1-1.0 is sane)")
    if not (0.0 < squish <= 1.0):
        raise ValueError(
            f"first_layer_squish must be in (0, 1], got {squish}. Above 1.0 "
            f"would lift the first layer OFF the plate, not press it in.")

    lh = writer.layer_height

    # Is any first-layer / base / brim feature active? If not, every Z below
    # falls back to the pre-adhesion arithmetic exactly, so default output stays
    # byte-identical. Mirrors build_continuous_spiral's own `adhesion` flag --
    # including that first_layer_flow alone does NOT arm the package.
    adhesion = (squish < 1.0 or base_layers > 0 or brim_loops > 0)
    squish = squish if adhesion else 1.0

    if first_layer_z is None:
        # squish == 1.0 (the default) makes this exactly writer.layer_height,
        # the pre-adhesion default: 1.0 * x is exact for every finite float.
        first_layer_z = squish * lh
    else:
        first_layer_z = _finite("first_layer_z", first_layer_z)

    base_pts = _archimedean(radius, writer.line_width, resolution)
    if len(base_pts) < 2:
        raise ValueError("surface too small for the given line width")

    # Evaluate the field once per sample and reject any non-finite result HERE.
    # min()/max() over a sequence containing NaN return whichever value the
    # comparison order happens to keep, so a bad field would otherwise poison
    # z_offset without ever failing a test.
    zs: list[float] = []
    for (x, y) in base_pts:
        v = field(x, y)
        if not math.isfinite(v):
            raise ValueError(
                f"height field returned a non-finite value {v!r} at part-local "
                f"({x:.3f}, {y:.3f}); refusing to build a toolpath from it")
        zs.append(float(v))
    fmin, fmax = min(zs), max(zs)

    # The shell sits one full layer above the TOP base disk (disks occupy
    # first_layer_z + k*lh for k in 0..base_layers-1). Unlike a vase-mode wall,
    # whose helix starts AT the top disk and climbs lh over its first turn, a
    # conformal shell is a complete layer of its own -- starting it at the top
    # disk's Z would drive the nozzle back through material already laid.
    shell_floor_z = first_layer_z + base_layers * lh
    # Lift the whole field so its lowest point sits on that floor.
    z_offset = shell_floor_z - fmin

    top_z = z_offset + (shells - 1) * lh + fmax

    return {
        "base_pts": base_pts,
        "field_min": fmin,
        "field_max": fmax,
        "adhesion": adhesion,
        "first_layer_squish": squish,
        "first_layer_z": first_layer_z,
        "shell_floor_z": shell_floor_z,
        "z_offset": z_offset,
        "top_z": top_z,
    }


def build_surface_spiral(
    writer: GcodeWriter,
    field: HeightField,
    radius: float,
    *,
    shells: int = 1,
    resolution: float = 0.4,
    center: tuple[float, float] | None = None,
    first_layer_z: float | None = None,
    travel_z_clearance: float = 5.0,
    first_layer_squish: float = 1.0,
    first_layer_spacing_factor: float = 1.0,
    first_layer_flow: float = 1.0,
    base_layers: int = 0,
    brim_loops: int = 0,
    base_style: str = "spiral",
    base_points_per_turn: int = 240,
) -> dict:
    """Emit a conformal non-planar shell over ``field`` and return a report.

    Geometry
      ``radius``      footprint radius (mm) of the disk being paved.
      ``shells``      stacked conformal copies, each ``layer_height`` above the
                      last, alternating out/in so the bead never lifts.
      ``resolution``  arc length (mm) between spiral samples. Smaller = smoother
                      and slower; ~0.1-1.0 mm is the sane band. A real parameter,
                      not a constant: it is the knob that trades toolpath
                      fidelity against point count, and the caller owns the
                      budget (see serve.py's ``_check_surface_budget``).
      ``center``      bed XY of the part centre; defaults to the profile's.

    Bed adhesion (all default to a byte-identical no-op)
      ``first_layer_squish``  < 1.0 drops whatever touches the plate to
                      ``squish * layer_height`` while still extruding a full
                      nominal layer of plastic, pressing it into the sheet.
                      Same convention as ``build_continuous_spiral``.
      ``base_layers`` stacked solid disks under the shell, paved by
                      :mod:`.base_fill` as outline-warped spirals (or closed
                      rings, see ``base_style``).
      ``brim_loops``  outward rings beyond the footprint at the first-layer Z.
      ``first_layer_spacing_factor``  widens layer 0's bead spacing on top of
                      the ``line_width / squish`` effective width, so a squished
                      first layer does not overlap itself and ooze.
      ``first_layer_flow``  flow multiplier over layer 0. On its own it does NOT
                      arm the adhesion package (matching continuous_spiral), so
                      it cannot perturb default output.
      ``base_style``  "spiral" (default) or "concentric".
      ``base_points_per_turn``  angular sampling of the base/brim rings.

    ``first_layer_z`` is the Z of whatever touches the bed -- the base's first
    disk when there is one, otherwise the shell's own lowest point. Leave it at
    ``None`` to get ``first_layer_squish * layer_height``; passing an explicit
    value OVERRIDES the squish-derived height (exactly like
    ``build_continuous_spiral``'s ``base_z``), so a caller that wants the squish
    to take effect must pass ``None`` here.

    With a base present the shell's floor is ``first_layer_z + base_layers *
    layer_height`` -- one layer above the top disk. See
    :func:`surface_spiral_geometry`, which owns that arithmetic and which a
    caller pre-flighting the toolpath should call rather than re-deriving it.
    """
    profile = writer.profile
    cx, cy = center if center is not None else profile.bed_center
    lh = writer.layer_height

    travel_z_clearance = _finite("travel_z_clearance", travel_z_clearance)
    if travel_z_clearance < 0.0:
        raise ValueError(
            f"travel_z_clearance must be >= 0, got {travel_z_clearance}")
    spacing_factor = _finite("first_layer_spacing_factor", first_layer_spacing_factor)
    if spacing_factor <= 0.0:
        raise ValueError(
            f"first_layer_spacing_factor must be positive, got {spacing_factor}")
    first_layer_flow = _finite("first_layer_flow", first_layer_flow)
    if first_layer_flow <= 0.0:
        raise ValueError(f"first_layer_flow must be positive, got {first_layer_flow}")
    if base_style not in BASE_STYLES:
        raise ValueError(
            f"unknown base_style '{base_style}', expected one of {list(BASE_STYLES)}")
    base_points_per_turn = _count("base_points_per_turn", base_points_per_turn,
                                  minimum=3)

    geom = surface_spiral_geometry(
        writer, field, radius,
        shells=shells, resolution=resolution,
        first_layer_z=first_layer_z, first_layer_squish=first_layer_squish,
        base_layers=base_layers, brim_loops=brim_loops,
    )
    # Re-read the normalised values so everything below shares one source.
    shells = _count("shells", shells, minimum=1)
    base_layers = _count("base_layers", base_layers, minimum=0)
    brim_loops = _count("brim_loops", brim_loops, minimum=0)
    base_pts = geom["base_pts"]
    adhesion = geom["adhesion"]
    squish = geom["first_layer_squish"]
    first_layer_z = geom["first_layer_z"]
    shell_floor_z = geom["shell_floor_z"]
    z_offset = geom["z_offset"]
    top_z = geom["top_z"]

    # Effective first-layer bead spacing -- same formula as
    # build_continuous_spiral: a bead extruded for the full nominal layer height
    # but squished into squish*lh spreads to ~line_width/squish wide, and layer-0
    # fill lines must be spaced at that EFFECTIVE width or the excess plastic has
    # nowhere to go. Feeds base_fill's own `first_layer_spacing` contract.
    s0 = writer.line_width / max(squish, 1e-6) * max(spacing_factor, 0.5)

    # The brim reaches beyond the footprint, so the bed-fit check has to see the
    # grown figure. brim_outer_radius() returns 0.0 with no brim, leaving the
    # pre-adhesion check (plain `radius`) untouched.
    def _disk_radius(theta: float) -> float:
        return radius

    brim_r = brim_outer_radius(_disk_radius, s0, brim_loops)
    fit_radius = max(radius, brim_r)
    _ensure_fits(profile, cx, cy, fit_radius)

    if top_z > profile.z_max:
        raise ValueError(
            f"Surface top Z {top_z:.1f} exceeds Z max {profile.z_max}. "
            f"Reduce amplitude or shells."
        )

    # Print-ordered (x, y, layer) path for the brim + stacked base disks; []
    # unless something was asked for, which is the byte-identical default.
    base_seq = layered_base_and_brim(
        _disk_radius, writer.line_width,
        first_layer_spacing=s0,
        base_layers=base_layers, brim_loops=brim_loops,
        points_per_turn=base_points_per_turn, start_theta=0.0,
        base_style=base_style,
    ) if adhesion else []

    writer.header()

    n_base_pts = 0
    if base_seq:
        b0x, b0y = cx + base_seq[0][0], cy + base_seq[0][1]
        bz0 = first_layer_z + base_seq[0][2] * lh
        b_lift = min(bz0 + travel_z_clearance, profile.z_max)
        writer.comment("move to solid base start")
        writer.safe_lift(b_lift)
        writer.travel(b0x, b0y, b_lift)
        writer.travel(b0x, b0y, bz0)
        writer.unretract()
        writer.comment(
            f"solid base ({base_layers} layers) + brim ({brim_loops} loops)")
        for (bx, by, k), bz in zip(base_seq, blend_layer_z(base_seq, first_layer_z, lh)):
            first = (k == 0)
            writer.extrude_to(
                cx + bx, cy + by, bz,
                speed=writer.first_layer_speed if first else writer.print_speed,
                layer_height_override=lh,      # disk 0: full volume, squished
                flow_override=first_layer_flow if first else 1.0,
            )
            n_base_pts += 1

    n_pts = 0
    _fan_on = False
    if base_layers > 0 and base_seq:
        # A solid base has already buried the first layer, so the shell above it
        # is no longer a bed-adhesion layer: cool it. (Mirrors
        # build_continuous_spiral's `fan_immediate`; the base itself carries no
        # fan call, there is nowhere in that emission to put one.)
        writer.set_fan(writer.fan_speed)
        _fan_on = True

    first = base_pts[0]
    sx, sy = cx + first[0], cy + first[1]
    sz = z_offset + (field(first[0], first[1]))
    writer.comment("move to surface spiral start (centre)")
    if base_seq:
        # Already mid-print and parked on the outline: retract and climb clear of
        # the finished base before crossing back to the centre, rather than
        # dragging the nozzle across it. (safe_lift is deliberately NOT reused --
        # it is the "decouple from PRINT_START" move and was already emitted.)
        base_top_z = first_layer_z + max(base_layers - 1, 0) * lh
        lift_z = min(max(sz, base_top_z) + travel_z_clearance, profile.z_max)
        writer.retract()
        writer.travel(cx + base_seq[-1][0], cy + base_seq[-1][1], lift_z)
        writer.travel(sx, sy, lift_z)
        writer.travel(sx, sy, sz)
    else:
        writer.safe_lift(sz + travel_z_clearance)
        writer.travel(sx, sy, sz + travel_z_clearance)
        writer.travel(sx, sy, sz)
    writer.unretract()

    for s in range(shells):
        # alternate direction each shell so the path stays continuous (no travel)
        seq = base_pts if s % 2 == 0 else list(reversed(base_pts))
        if s == 1 and not _fan_on:
            writer.set_fan(writer.fan_speed)
            _fan_on = True
        # Shell 0 is the bed layer only when nothing was laid under it. With a
        # base it is an ordinary conformal layer on solid plastic, so the squish
        # package must not touch it.
        bed_layer = adhesion and s == 0 and base_layers == 0
        for j, (lx, ly) in enumerate(seq):
            x, y = cx + lx, cy + ly
            z = z_offset + s * lh + field(lx, ly)
            speed = writer.first_layer_speed if s == 0 else writer.print_speed
            # Gap between conformal shells is exactly the nominal layer height
            # (they are parallel copies offset by `lh`), so no gap-aware override
            # is needed here -- the writer's nominal layer_height is already right.
            writer.extrude_to(
                x, y, z, speed=speed,
                # Squished but extrude a FULL nominal layer, pressing plastic
                # into the plate -- build_continuous_spiral's own first-turn
                # convention. `lh` is what extrude_to uses when the override is
                # None, so the default path is bit-for-bit unchanged.
                layer_height_override=lh if bed_layer else None,
                flow_override=first_layer_flow if bed_layer else 1.0,
            )
            n_pts += 1
    # Single-shell surface: no second shell to trigger fan -- emit fan at end of shell 0
    if not _fan_on:
        writer.set_fan(writer.fan_speed)

    writer.retract()
    writer.footer()

    return {
        "shells": shells,
        "points": n_pts,
        "base_points": n_base_pts,
        "resolution_mm": round(resolution, 4),
        "base_layers": base_layers,
        "base_style": base_style,
        "brim_loops": brim_loops,
        "first_layer_squish": round(squish, 4),
        "first_layer_z_mm": round(first_layer_z, 4),
        "shell_floor_z_mm": round(shell_floor_z, 4),
        "footprint_radius_mm": round(fit_radius, 2),
        "top_z_mm": round(top_z, 2),
        "filament_mm": round(writer.total_filament_mm, 1),
        "max_z_rate_mm_s": round(writer.max_z_rate, 2),
        "layer_height_clamp_events": writer.layer_height_clamp_events,
        "bounds": writer.bounds,
    }


def _ensure_fits(profile, cx, cy, radius) -> None:
    if (cx - radius < profile.print_min_x or cx + radius > profile.print_max_x
            or cy - radius < profile.print_min_y or cy + radius > profile.print_max_y):
        raise ValueError(
            f"Surface footprint radius {radius:.1f} at center ({cx:.0f},{cy:.0f}) "
            f"falls outside the safe print area. Shrink --radius, --surface-scale "
            f"or the brim."
        )
