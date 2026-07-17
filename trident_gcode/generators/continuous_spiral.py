"""Turn a continuous spiral geometry into Klipper G-code on the bed."""
from __future__ import annotations

import math
from typing import Callable

from ..blobs import (BlobSpec, LoopSpec, blob_volume_at, compute_blob_sites,
                     compute_loop_sites)
from ..extrusion import blob_e_for_volume
from ..gcode import GcodeWriter
from ..paths import SpiralSpec, PathPoint, spiral_path, bounding_radius, circle
from .base_fill import (layered_base_and_brim, brim_outer_radius, blend_layer_z,
                        skirt_points, skirt_outer_radius)


def _overhang_flow(tilt: float | None, k: float) -> float:
    """Flow multiplier for overhang compensation. k=0 disables."""
    if k <= 0.0 or tilt is None:
        return 1.0
    boost = 1.0 + k * max(0.0, math.tan(max(tilt, 0.0)))
    return min(max(boost, 0.7), 1.6)


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
    blob_spec: BlobSpec | None = None,
    loop_spec: LoopSpec | None = None,
    overhang_flow_k: float = 0.0,
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

    With all adhesion/base/brim/flow_callback options at their defaults the emitted
    G-code is byte-identical to the plain continuous spiral (gap-aware wall only).
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
    skirt_r = skirt_outer_radius(shape, s0, skirt_loops, skirt_base_beyond)

    # ---- footprint (account for the brim reaching beyond the outline) --------
    radius = bounding_radius(pts)
    brim_r = brim_outer_radius(shape, s0, brim_loops)
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
    # (x, y, layer): layer k prints at base_z + k*lh.
    base_seq = layered_base_and_brim(
        shape, writer.line_width,
        first_layer_spacing=s0,
        base_layers=base_layers, brim_loops=brim_loops,
        points_per_turn=ppt, start_theta=0.0,
        base_style=base_style,
    ) if adhesion else []

    # -------- skirt: shield/purge loops printed FIRST, then retract+travel -----
    if skirt_loops > 0:
        skirt = skirt_points(shape, s0, skirt_loops, skirt_base_beyond,
                             points_per_turn=ppt, start_theta=0.0)
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

    # Blob placement: compute which indices get a blob deposit.
    blob_sites: set[int] = set()
    if blob_spec is not None and blob_spec.blobs_per_turn > 0:
        blob_sites = compute_blob_sites(total_pts, ppt, blob_spec)
        # Remove any sites in the first turn (first-layer zone).
        blob_sites -= set(range(ppt + 1))

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

    if not adhesion:
        # -------- plain spiral (byte-identical to the pre-adhesion generator) --
        skip_until = -1
        for i, p in enumerate(pts):
            if i <= skip_until:
                continue          # wall replaced by a loop strand here
            x, y, z = cx + p.x, cy + p.y, base_z + p.z
            speed = writer.first_layer_speed if i <= ppt else writer.print_speed
            if i == ppt:
                writer.set_fan(writer.fan_speed)
            t_frac = i / max(total_pts - 1, 1)
            cb_flow = flow_callback(t_frac) if flow_callback is not None else 1.0
            oh_flow = _overhang_flow(p.tilt, overhang_flow_k)
            writer.extrude_to(x, y, z, speed=speed, layer_height_override=p.gap,
                              flow_override=cb_flow * oh_flow)
            if i in blob_sites:
                vol = blob_volume_at(i / max(total_pts - 1, 1), blob_spec)
                e_mm = blob_e_for_volume(vol, writer.profile)
                writer.blob(e_mm, dwell_before_ms=0,
                            dwell_after_ms=blob_spec.dwell_after_ms)
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
            if base_layers > 0:
                # First layer is done once disk 0 is buried; fan on for the rest.
                writer.set_fan(writer.fan_speed)
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
        skip_until = -1
        for i, p in enumerate(pts):
            if i <= skip_until:
                continue          # wall replaced by a loop strand here
            x, y, z = cx + p.x, cy + p.y, wall_off + p.z
            # Squish/first-layer handling applies to the wall's first turn only
            # when the wall IS the first layer (no base under it).
            first_turn = (i <= ppt) and base_layers == 0
            if i > ppt and not _fan_on:
                writer.set_fan(writer.fan_speed)
                _fan_on = True
            if first_turn:
                speed = writer.first_layer_speed
                lh_over, flow_over = lh, first_layer_flow
            else:
                speed = writer.print_speed
                lh_over, flow_over = p.gap, 1.0
            t_frac = i / max(total_pts - 1, 1)
            cb_flow = flow_callback(t_frac) if (flow_callback is not None and not first_turn) else 1.0
            oh_flow = _overhang_flow(p.tilt, overhang_flow_k) if not first_turn else 1.0
            writer.extrude_to(x, y, z, speed=speed,
                              layer_height_override=lh_over,
                              flow_override=flow_over * cb_flow * oh_flow)
            if i in blob_sites:
                vol = blob_volume_at(i / max(total_pts - 1, 1), blob_spec)
                e_mm = blob_e_for_volume(vol, writer.profile)
                writer.blob(e_mm, dwell_before_ms=0,
                            dwell_after_ms=blob_spec.dwell_after_ms)
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
        "blob_count": writer.blob_count,
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
