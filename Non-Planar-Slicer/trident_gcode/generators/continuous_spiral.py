"""Turn a continuous spiral geometry into Klipper G-code on the bed."""
from __future__ import annotations

import math
from typing import Callable

from ..blobs import LoopSpec, compute_loop_sites
from ..gcode import GcodeWriter
from ..paths import (SpiralSpec, PathPoint, spiral_path, bounding_radius, circle,
                    cage_scale)
from ..point_edit import (MaskSpec, ProtectionSpec, FFDSpec, SmoothSpec,
                          RadialPushSpec, apply_point_edits)
from .base_fill import (layered_base_and_brim, brim_outer_radius, blend_layer_z,
                        skirt_points, skirt_outer_radius)


def _overhang_flow(tilt: float | None, k: float) -> float:
    """Flow multiplier for overhang compensation. k=0 disables."""
    if k <= 0.0 or tilt is None:
        return 1.0
    boost = 1.0 + k * max(0.0, math.tan(max(tilt, 0.0)))
    return min(max(boost, 0.7), 1.6)


# Wall tilt (radians) at which an overhang is considered "absolute" -- i.e.
# steep enough that the part-cooling fan should already be at fan_max. Chosen
# well short of 90 deg (fully horizontal) since by the time a wall leans this
# far it already has minimal support from the turn below and needs full
# cooling *before* it gets any steeper, not once it's flat.
#
# 45 deg, not 60: tilt here is measured from vertical, and 45 is the classic
# FDM limit past which a wall stops being self-supporting -- full cooling by
# then is the physically meaningful point. 60 also made fan_max effectively
# unreachable through the UI: a silhouette flared across the whole height
# tops out near 23 deg (only ~1/3 of the way to fan_max), so the "max" slider
# looked inert on every design except a sharp local flare.
OVERHANG_FAN_FULL_TILT = math.radians(45.0)


def _fan_on_threshold(fan_off_layers: int, base_layers: int, ppt: int) -> int:
    """Wall-loop point index at which the part-cooling fan is allowed to turn
    on, given a TOTAL layer count (base + wall combined, from the absolute
    start of the print) that should stay fan-off.

    ``fan_off_layers <= 0`` reproduces today's implicit default exactly (fan
    on immediately once any solid base finishes, else after the wall's own
    first turn) -- byte-identical output. A solid base already prints with no
    fan calls at all regardless of this setting (there is nowhere in that
    emission to turn it on), so a requested count at or below ``base_layers``
    just means "turn on right after the base", same as the default.
    """
    if fan_off_layers > 0:
        effective = fan_off_layers
    else:
        effective = base_layers if base_layers > 0 else 1
    return max(0, effective - base_layers) * ppt


def _overhang_fan(tilt: float | None, fan_min: float, fan_max: float) -> float:
    """Fan speed fraction (0..1) driven by wall tilt.

    Ramps linearly from ``fan_min`` at a vertical wall (tilt=0) up to
    ``fan_max`` at OVERHANG_FAN_FULL_TILT ("absolute overhang") and holds
    fan_max beyond that. Overhanging turns have less material underneath to
    carry heat away, so they need more airflow to solidify before the next
    turn arrives on top of them -- steeper leans need it sooner.
    """
    if tilt is None:
        return fan_min
    frac = min(max(tilt, 0.0), OVERHANG_FAN_FULL_TILT) / OVERHANG_FAN_FULL_TILT
    return fan_min + (fan_max - fan_min) * frac


_LOOP_SEGMENTS = 10   # bezier sampling of one teardrop excursion


def emit_loop(writer: GcodeWriter,
              x0: float, y0: float, z0: float,
              x1: float, y1: float, z1: float,
              ux: float, uy: float, spec: LoopSpec) -> None:
    """Emit one teardrop loop excursion from wall point A to wall point B.

    The nozzle extrudes CONTINUOUSLY along a quadratic bezier that leaves the
    wall at A, bulges ``out_mm`` outward along (ux, uy) (rising ``up_mm`` at
    the apex so the strand clears the bead below), and re-anchors into the
    wall at B, ``rejoin_mm`` further along the perimeter.  The unsupported
    middle of the strand sags under gravity into a hanging loop — the printer
    never re-traces its own strand, so the loop stays open.
    """
    # Quadratic bezier control point: apex reaches ~out_mm when the control
    # sits 2*out_mm out (B(0.5) = midpoint + control_offset/2).
    mx, my, mz = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0
    cx_ = mx + ux * 2.0 * spec.out_mm
    cy_ = my + uy * 2.0 * spec.out_mm
    cz_ = mz + 2.0 * spec.up_mm
    n = _LOOP_SEGMENTS
    for k in range(1, n + 1):
        t = k / n
        omt = 1.0 - t
        bx = omt * omt * x0 + 2.0 * omt * t * cx_ + t * t * x1
        by = omt * omt * y0 + 2.0 * omt * t * cy_ + t * t * y1
        bz = omt * omt * z0 + 2.0 * omt * t * cz_ + t * t * z1
        writer.extrude_to(bx, by, bz, speed=spec.speed_mm_s,
                          flow_override=spec.flow)
        if spec.dwell_ms > 0 and k == n // 2:
            writer.dwell(spec.dwell_ms)
    writer.comment("loop")


def _base_t0_footprint(spec: SpiralSpec, shape: Callable[[float], float]):
    """Radius function + point transform matching the WALL's OWN t=0 (bottom)
    cross-section, for building the base/brim/skirt.

    ``spiral_path`` (paths.py) builds each wall point from ``shape(theta)``
    scaled by the radius envelope and the cage -- both pure radial terms --
    and only THEN squashes the produced x/y into an ellipse (ovality) and
    finally translates the whole cross-section by the spine offset. Ovality
    and spine are affine operations on the already-computed point, not radius
    terms: folding them into a theta->radius function would change the
    projected angle, not just the length. base_fill.py's disk/brim/skirt
    builders assume exactly x=r*cos(theta), y=r*sin(theta) (see its
    ``_outline_xy``), so only the envelope+cage part can go into
    ``radius_fn``; ovality/spine have to be applied to the (x, y) points
    those builders return, in the same order spiral_path applies them.

    Before this, the base/brim/skirt were built from the raw ``shape``
    callable alone -- ignoring radius_envelope, cage, ovality and the spine
    entirely, while the wall (built via ``spiral_path(spec, shape)``) honours
    all of them. On a narrowing radius_profile ([[0, 0.5], [1, 1.0]], i.e. the
    wall starts at half radius and flares to full), that put the base disks
    at the FULL un-enveloped radius while the wall's first point sat at half
    of it: measured on real output (base_radius=30), base disks printed out
    to r=30.00 while the wall's t=0 point sat at r=15.09. The two were then
    joined by a single EXTRUDING travel move dragging E0.84190 (~19x a normal
    move's E) across that 15mm gap, straight over the top of the already-
    finished disk.

    Returns (radius_fn, transform, identity):
      radius_fn(theta) -> envelope/cage-scaled radius, suitable for
        layered_base_and_brim / skirt_points / brim_outer_radius /
        skirt_outer_radius (all of which assume a plain theta->radius
        callable).
      transform(x, y) -> (x, y) with ovality and the t=0 spine offset
        applied, for POST-processing points already produced by those
        functions from ``radius_fn``.
      identity -- True iff ``radius_fn`` IS ``shape`` itself (not a wrapper)
        and ``transform`` is None, so a caller can skip the point-transform
        step entirely -- guaranteeing byte-identical output (by construction,
        not by hoping the float math agrees) whenever nothing here is
        actually active. A LEANING design (spine_mm > 0) still lands on this
        fast path: the lean is a function of height fraction t, and at t=0 it
        is always (0, 0) -- so a purely leaning wall's base was already
        correct before this change and needs no adjustment now.

    Deliberately excludes the radial surface texture (``r_pattern``): the
    disk paver requires a star-convex outline (see base_fill.py's module
    docstring), and a textured radius function is not guaranteed to stay
    star-convex. Residual: with the default ``pattern_fade_in`` > 0 this is
    invisible (the texture has no effect yet at t=0), but a design that sets
    ``pattern_fade_in = 0`` has the wall's very first point already displaced
    by up to ``r_amp`` (renamed ``pattern_amp`` in the UI) of radial texture
    that the base does not, and cannot cleanly, follow.
    """
    env0 = spec.radius_envelope(0.0) if spec.radius_envelope is not None else 1.0
    ov = max(-0.4, min(0.4, spec.ovality))
    dx0, dy0 = (spec.spine_offset(0.0) if spec.spine_offset is not None
                else (0.0, 0.0))

    if env0 == 1.0 and spec.cage is None and ov == 0.0 and dx0 == 0.0 and dy0 == 0.0:
        return shape, None, True

    cage = spec.cage
    if cage is not None:
        def radius_fn(theta: float) -> float:
            return shape(theta) * env0 * cage_scale(cage, theta, 0.0)
    else:
        def radius_fn(theta: float) -> float:
            return shape(theta) * env0

    def transform(x: float, y: float) -> tuple[float, float]:
        if ov != 0.0:
            x *= (1.0 + ov)
            y *= (1.0 - ov)
        return (x + dx0, y + dy0)

    return radius_fn, transform, False


def _base_footprint_bound(spec: SpiralSpec, r: float) -> float:
    """Conservative upper bound on a base/brim/skirt scalar radius `r` (itself
    already computed from ``_base_t0_footprint``'s envelope/cage-scaled
    ``radius_fn``) once ovality and the t=0 spine offset are accounted for.

    The actual PATHS use the exact ``transform`` from ``_base_t0_footprint``;
    this is only for the scalar footprint checks that feed ``_ensure_fits``
    (bed-fit), where an outline that ovality turns into an ellipse or the
    spine shifts off-centre needs a radius no smaller than its true reach in
    any direction. Ovality is bounded by scaling up by ``1 + abs(ov)`` (the
    longer of the two elliptical axes) rather than the exact anisotropic
    scale, and the spine shift is added as its full magnitude regardless of
    direction -- both deliberately over-, never under-, estimate. A bed-fit
    check that under-estimates is a crash; one that over-estimates just
    rejects a design that might, by exact math, have technically fit.
    """
    ov = max(-0.4, min(0.4, spec.ovality))
    dx0, dy0 = (spec.spine_offset(0.0) if spec.spine_offset is not None
                else (0.0, 0.0))
    return r * (1.0 + abs(ov)) + math.hypot(dx0, dy0)


def _xform_xy_seq(seq, xform):
    """Apply an (x, y) -> (x, y) point transform to the leading x, y of every
    tuple in ``seq``, preserving any trailing fields (``layered_base_and_brim``
    returns (x, y, layer) triples; ``skirt_points`` returns (x, y) pairs).

    ``xform is None`` returns ``seq`` UNCHANGED (same list object, not even a
    rebuild) -- the no-op path ``_base_t0_footprint`` hands back for the
    byte-identical case.
    """
    if xform is None:
        return seq
    return [xform(x, y) + tuple(rest) for (x, y, *rest) in seq]


def build_continuous_spiral(
    writer: GcodeWriter,
    spec: SpiralSpec,
    shape: Callable[[float], float] | None = None,
    center: tuple[float, float] | None = None,
    base_z: float | None = None,
    travel_z_clearance: float = 5.0,
    first_layer_squish: float = 1.0,
    first_layer_spacing_factor: float = 1.0,
    first_layer_flow: float = 1.0,
    base_layers: int = 0,
    brim_loops: int = 0,
    base_style: str = "spiral",
    skirt_loops: int = 0,
    flow_callback: Callable[[float], float] | None = None,
    width_callback: Callable[[float], float] | None = None,
    loop_spec: LoopSpec | None = None,
    overhang_flow_k: float = 0.0,
    fan_overhang_min: float | None = None,
    fan_overhang_max: float | None = None,
    fan_off_layers: int = 0,
    point_mask: MaskSpec | None = None,
    point_protection: ProtectionSpec | None = None,
    point_ffd: FFDSpec | None = None,
    point_smooth: SmoothSpec | None = None,
    point_radial_push: RadialPushSpec | None = None,
) -> dict:
    """Emit a complete continuous non-planar spiral and return a small report.

    The path is generated centred on the origin, then translated to ``center``
    (defaults to the bed centre).  The whole thing is lifted by ``base_z`` so the
    bottom bead sits at the correct first-layer height.

    First-layer adhesion (active when ``first_layer_squish`` < 1, or a base/brim
    is requested): the first turn prints at ``squish*layer_height`` off the bed
    while extruding as if the full nominal layer height (squish presses plastic
    into the plate); a flat priming loop precedes the spiral; ``first_layer_flow``
    scales flow over the first turn.

    Solid base + brim: ``base_layers`` stacked disks (``base_style`` "spiral" =
    warped Archimedean, "concentric" = closed rings) and ``brim_loops`` outward
    rings are laid FIRST at the base Z, chained brim -> base -> priming loop ->
    wall as one travel-free bead.

    ``skirt_loops`` > 0 prints that many outline-following loops clear of the whole
    print (3mm beyond the brim) at first-layer height/speed BEFORE anything else,
    then retracts, travels to the main bead start, unretracts, and proceeds as
    usual — purging the nozzle and shielding drafts.

    ``flow_callback(t)`` -- if provided, called with the height fraction t in
    [0,1] for each wall-spiral point and its return value multiplies the flow for
    that move (applied after first_layer_flow).  Useful for flow-ladder calibration
    prints.  None (default) = no per-point override.

    ``width_callback(t)`` -- if provided, called with the same height fraction t
    in [0,1] for each wall-spiral point and its return value multiplies the
    writer's nominal line_width for that move (passed through as
    ``line_width_override``).  Like ``flow_callback``, suppressed during the
    first adhesion turn.  None (default) = no per-point override.

    With all adhesion/base/brim/flow_callback/width_callback options at their
    defaults the emitted G-code is byte-identical to the plain continuous
    spiral (gap-aware wall only).
    """
    profile = writer.profile
    cx, cy = center if center is not None else profile.bed_center

    # The spiral pitch IS the layer height — keep the writer's extrusion math in
    # sync with the geometry so E is never computed for the wrong layer height.
    writer.layer_height = spec.layer_height
    lh = writer.layer_height

    shape = shape or circle(spec.base_radius)
    pts = spiral_path(spec, shape)
    ppt = spec.points_per_turn

    # The base/brim/skirt must be built from the WALL's own t=0 footprint
    # (envelope + cage + ovality + spine), not the raw ``shape`` -- see
    # _base_t0_footprint's docstring for the E0.84190 scar this fixes.
    # (the third element, `identity`, is informational -- callers here decide
    # off `base_xform is None`, which _xform_xy_seq already handles)
    base_radius_fn, base_xform, _ = _base_t0_footprint(spec, shape)

    # Point Edit Modifiers: post-slice deformation of the already-generated
    # wall polyline (Point Mask / Protection / FFD / Smooth / Radial Push).
    # No-op (same list, byte-identical output) when none are active.
    pts = apply_point_edits(
        pts, ppt,
        mask=point_mask, protection=point_protection, ffd=point_ffd,
        smooth=point_smooth, radial_push=point_radial_push,
    )

    # Is any first-layer / base / brim feature active? If not, fall back to the
    # exact plain-spiral emission so default output stays byte-identical. Note:
    # first_layer_flow only parameterises the package when it is already on (via
    # squish/base/brim) — on its own it must not perturb the default output.
    adhesion = (first_layer_squish < 1.0 or base_layers > 0 or brim_loops > 0
                or skirt_loops > 0)

    squish = first_layer_squish if adhesion else 1.0
    if base_z is None:
        base_z = squish * lh

    # Effective first-layer bead spacing: a bead extruded for the full nominal
    # layer height but squished into squish*lh spreads to ~line_width/squish
    # wide. Layer-0 fill lines must be spaced at that EFFECTIVE width or the
    # excess plastic has nowhere to go and oozes up between lines.
    s0 = writer.line_width / max(squish, 1e-6) * max(first_layer_spacing_factor, 0.5)

    # The wall starts AT the top disk's level and helixes up from it (the
    # spiral's first turn rises 0 -> lh), exactly like vase-mode rising off a
    # priming loop — so the base -> wall hand-off is continuous in Z.
    wall_off = base_z + (base_layers - 1) * lh if base_layers > 0 else base_z

    # ---- skirt geometry (loops that shield/purge, clear of everything) -------
    # Innermost skirt ring sits 3mm beyond the brim's reach (brim_loops*s0 past
    # the outline); each further ring steps out by the first-layer spacing.
    SKIRT_GAP = 3.0
    skirt_base_beyond = brim_loops * s0 + SKIRT_GAP
    # skirt_r/brim_r must be computed from the SAME t=0 footprint the base is
    # built from (a flared silhouette grows the base, and the bed-fit check
    # below has to see the grown figure) -- then conservatively widened for
    # ovality/spine, since those are points-level ops _base_footprint_bound
    # cannot apply exactly to a single scalar radius (see its docstring).
    skirt_r = _base_footprint_bound(
        spec, skirt_outer_radius(base_radius_fn, s0, skirt_loops, skirt_base_beyond))

    # ---- footprint (account for the brim reaching beyond the outline) --------
    radius = bounding_radius(pts)
    brim_r = _base_footprint_bound(spec, brim_outer_radius(base_radius_fn, s0, brim_loops))
    fit_radius = max(radius, brim_r, skirt_r)
    if loop_spec is not None:
        # Loop excursions reach out_mm beyond the wall.
        fit_radius = max(fit_radius, radius + loop_spec.out_mm)
    _ensure_fits(profile, cx, cy, fit_radius)

    top_z = wall_off + max(p.z for p in pts)
    if loop_spec is not None:
        top_z += loop_spec.up_mm       # excursions climb above the wall
    if top_z > profile.z_max:
        raise ValueError(
            f"Print top Z {top_z:.1f} exceeds Z max {profile.z_max}. "
            f"Reduce height or layer count."
        )

    writer.header()

    # Build the brim+base path (empty unless requested). Entries are
    # (x, y, layer): layer k prints at base_z + k*lh. Built from base_radius_fn
    # (the wall's t=0 footprint), then ovality/spine applied to the produced
    # points via base_xform -- a no-op (same list) in the byte-identical case.
    base_seq = _xform_xy_seq(layered_base_and_brim(
        base_radius_fn, writer.line_width,
        first_layer_spacing=s0,
        base_layers=base_layers, brim_loops=brim_loops,
        points_per_turn=ppt, start_theta=0.0,
        base_style=base_style,
    ), base_xform) if adhesion else []

    # -------- skirt: shield/purge loops printed FIRST, then retract+travel -----
    if skirt_loops > 0:
        skirt = _xform_xy_seq(skirt_points(base_radius_fn, s0, skirt_loops, skirt_base_beyond,
                             points_per_turn=ppt, start_theta=0.0), base_xform)
        if skirt:
            k0x, k0y = cx + skirt[0][0], cy + skirt[0][1]
            k_lift = base_z + travel_z_clearance
            writer.comment(f"skirt ({skirt_loops} loops)")
            writer.safe_lift(k_lift)
            writer.travel(k0x, k0y, k_lift)
            writer.travel(k0x, k0y, base_z)
            writer.unretract()
            for (kx, ky) in skirt:
                writer.extrude_to(
                    cx + kx, cy + ky, base_z,
                    speed=writer.first_layer_speed,
                    layer_height_override=lh,      # squished, extrude full lh
                    flow_override=first_layer_flow,
                )
            writer.retract()

    # Move to the START of the whole bead.
    if base_seq:
        start_xy = (base_seq[0][0], base_seq[0][1])
        sz = base_z + base_seq[0][2] * lh
    else:
        start_xy = (pts[0].x, pts[0].y)
        sz = base_z + pts[0].z
    sx, sy = cx + start_xy[0], cy + start_xy[1]
    lift_z = max(sz + travel_z_clearance, base_z + travel_z_clearance)
    writer.comment("move to spiral start")
    writer.safe_lift(lift_z)              # climb straight up first (PRINT_START leaves us low)
    writer.travel(sx, sy, lift_z)
    writer.travel(sx, sy, sz)
    writer.unretract()

    total_pts = len(pts)

    # Loop placement: which indices get a hanging-loop excursion.
    loop_sites: set[int] = set()
    loop_skip = 2
    if loop_spec is not None and loop_spec.loops_per_turn > 0:
        loop_sites = compute_loop_sites(total_pts, ppt, loop_spec)
        loop_sites -= set(range(ppt + 1))
        # The teardrop re-anchors rejoin_mm further along the perimeter: skip
        # that many wall points so the bead is not re-traced over the strand.
        seg_len = max(2.0 * math.pi * radius / ppt, 1e-6)
        loop_skip = max(2, int(round(loop_spec.rejoin_mm / seg_len)))

    def _loop_dir(p: PathPoint) -> tuple[float, float]:
        n = math.hypot(p.x, p.y)
        return (p.x / n, p.y / n) if n > 1e-9 else (1.0, 0.0)

    # Overhang-adaptive fan: both bounds must be given to activate (None,None
    # is the default -- constant writer.fan_speed, byte-identical output).
    fan_overhang_active = fan_overhang_min is not None and fan_overhang_max is not None

    if not adhesion:
        # -------- plain spiral (byte-identical to the pre-adhesion generator) --
        fan_on_i = _fan_on_threshold(fan_off_layers, 0, ppt)
        skip_until = -1
        for i, p in enumerate(pts):
            if i <= skip_until:
                continue          # wall replaced by a loop strand here
            x, y, z = cx + p.x, cy + p.y, base_z + p.z
            speed = writer.first_layer_speed if i <= ppt else writer.print_speed
            if i == fan_on_i:
                writer.set_fan(_overhang_fan(p.tilt, fan_overhang_min, fan_overhang_max)
                               if fan_overhang_active else writer.fan_speed)
            elif i > fan_on_i and fan_overhang_active:
                writer.set_fan_if_changed(_overhang_fan(p.tilt, fan_overhang_min, fan_overhang_max))
            t_frac = i / max(total_pts - 1, 1)
            cb_flow = flow_callback(t_frac) if flow_callback is not None else 1.0
            cb_w = width_callback(t_frac) if width_callback is not None else None
            oh_flow = _overhang_flow(p.tilt, overhang_flow_k)
            writer.extrude_to(x, y, z, speed=speed, layer_height_override=p.gap,
                              flow_override=cb_flow * oh_flow,
                              line_width_override=(writer.line_width * cb_w
                                                    if cb_w is not None else None))
            if i in loop_sites and i + loop_skip < total_pts - 1:
                j = i + loop_skip
                pj = pts[j]
                ux, uy = _loop_dir(p)
                emit_loop(writer, x, y, z,
                          cx + pj.x, cy + pj.y, base_z + pj.z,
                          ux, uy, loop_spec)
                skip_until = j
    else:
        # -------- brim + solid base (stacked disks), one continuous bead -------
        _fan_on = False
        # A solid base already prints with no fan calls at all (nowhere in
        # that emission to turn it on) -- so a requested fan_off_layers count
        # at or below base_layers just means "turn on right after the base",
        # same as today's unconditional default.
        fan_immediate = base_layers > 0 and fan_off_layers <= base_layers
        if base_seq:
            writer.comment(
                f"solid base ({base_layers} layers) + brim ({brim_loops} loops)")
            base_zs = blend_layer_z(base_seq, base_z, lh)
            for (bx, by, k), bz in zip(base_seq, base_zs):
                first = (k == 0)
                writer.extrude_to(
                    cx + bx, cy + by, bz,
                    speed=writer.first_layer_speed if first else writer.print_speed,
                    layer_height_override=lh,      # disk 0: full volume, squished
                    flow_override=first_layer_flow if first else 1.0,
                )
            if fan_immediate:
                # First layer is done once disk 0 is buried; fan on for the rest.
                # (No wall point yet at this point in the sequence -- the wall's
                # own tilt-driven updates take over once its loop starts below.)
                writer.set_fan(fan_overhang_min if fan_overhang_active else writer.fan_speed)
                _fan_on = True

        # -------- flat priming loop (squish-only starts; a base replaces it) ---
        if base_layers == 0:
            writer.comment("flat priming loop")
            for p in pts[:ppt + 1]:
                writer.extrude_to(
                    cx + p.x, cy + p.y, base_z,
                    speed=writer.first_layer_speed,
                    layer_height_override=lh,      # squished but extrude full lh
                    flow_override=first_layer_flow,
                )

        # -------- the wall spiral (starts on top of the base stack) ------------
        writer.comment("wall spiral")
        # When the fan already came on immediately after the base, its own
        # tilt-adaptive tracking still only starts after the wall's first turn
        # (a pre-existing cold-start window, independent of fan_off_layers).
        # Otherwise the requested total off-layer count sets the threshold.
        fan_on_i = ppt if fan_immediate else _fan_on_threshold(fan_off_layers, base_layers, ppt)
        skip_until = -1
        for i, p in enumerate(pts):
            if i <= skip_until:
                continue          # wall replaced by a loop strand here
            x, y, z = cx + p.x, cy + p.y, wall_off + p.z
            # Squish/first-layer handling applies to the wall's first turn only
            # when the wall IS the first layer (no base under it).
            first_turn = (i <= ppt) and base_layers == 0
            if i > fan_on_i and not _fan_on:
                writer.set_fan(_overhang_fan(p.tilt, fan_overhang_min, fan_overhang_max)
                               if fan_overhang_active else writer.fan_speed)
                _fan_on = True
            elif i > fan_on_i and fan_overhang_active:
                writer.set_fan_if_changed(_overhang_fan(p.tilt, fan_overhang_min, fan_overhang_max))
            if first_turn:
                speed = writer.first_layer_speed
                lh_over, flow_over = lh, first_layer_flow
            else:
                speed = writer.print_speed
                lh_over, flow_over = p.gap, 1.0
            t_frac = i / max(total_pts - 1, 1)
            cb_flow = flow_callback(t_frac) if (flow_callback is not None and not first_turn) else 1.0
            cb_w = width_callback(t_frac) if (width_callback is not None and not first_turn) else None
            oh_flow = _overhang_flow(p.tilt, overhang_flow_k) if not first_turn else 1.0
            writer.extrude_to(x, y, z, speed=speed,
                              layer_height_override=lh_over,
                              flow_override=flow_over * cb_flow * oh_flow,
                              line_width_override=(writer.line_width * cb_w
                                                    if cb_w is not None else None))
            if i in loop_sites and i + loop_skip < total_pts - 1:
                j = i + loop_skip
                pj = pts[j]
                ux, uy = _loop_dir(p)
                emit_loop(writer, x, y, z,
                          cx + pj.x, cy + pj.y, wall_off + pj.z,
                          ux, uy, loop_spec)
                skip_until = j

    writer.retract()
    writer.footer()

    if writer.layer_height_clamp_events:
        writer.comment(
            f"WARNING: {writer.layer_height_clamp_events} moves had local layer "
            f"height clamped to [0.25,1.5]x nominal (steep non-planar geometry)"
        )
    if writer.width_clamp_events:
        writer.comment(
            f"WARNING: {writer.width_clamp_events} moves had local line width "
            f"clamped to [0.5,2.0]x nominal (width curve out of band)"
        )

    return {
        "points": len(pts),
        "base_layers": base_layers,
        "base_style": base_style,
        "brim_loops": brim_loops,
        "skirt_loops": skirt_loops,
        "footprint_radius_mm": round(fit_radius, 2),
        "top_z_mm": round(top_z, 2),
        "filament_mm": round(writer.total_filament_mm, 1),
        "max_z_rate_mm_s": round(writer.max_z_rate, 2),
        "layer_height_clamp_events": writer.layer_height_clamp_events,
        "width_clamp_events": writer.width_clamp_events,
        "loop_count": len(loop_sites),
        "bounds": writer.bounds,
    }


def _ensure_fits(profile, cx, cy, radius) -> None:
    minx, maxx = cx - radius, cx + radius
    miny, maxy = cy - radius, cy + radius
    if (minx < profile.print_min_x or maxx > profile.print_max_x
            or miny < profile.print_min_y or maxy > profile.print_max_y):
        raise ValueError(
            f"Footprint radius {radius:.1f} at center ({cx:.0f},{cy:.0f}) "
            f"falls outside the safe print area "
            f"X[{profile.print_min_x}-{profile.print_max_x}] "
            f"Y[{profile.print_min_y}-{profile.print_max_y}]. "
            f"Shrink base_radius or recenter."
        )
