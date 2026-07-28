"""Unified profile-spiral generator: contour stack -> continuous G-code.

Accepts an ordered list of closed 2D contours (one per layer) plus their
corresponding Z heights and wraps them into a single unbroken non-planar
spiral with optional Z-wave and radial-texture modulation.  Works identically
for parametric shapes and sliced STLs because both produce the same contour-
stack format (see :mod:`profile_stack`).

Emission pattern (header, safe_lift, travel, base/brim, wall spiral, footer)
mirrors :mod:`continuous_spiral` so the output is compatible with the same
analyse / preview tooling.
"""
from __future__ import annotations

import math
from typing import Callable

from ..blobs import (BlobSpec, LoopSpec, blob_volume_at, compute_blob_sites,
                     compute_loop_sites)
from ..extrusion import blob_e_for_volume
from ..gcode import GcodeWriter
from ..paths import _R_PATTERNS, _fade_envelope, _MAX_AMP_STEP, R_PATTERN_NAMES
from ..profile_stack import Contour, contour_normals, interpolate_contours
from .base_fill import layered_base_and_brim, brim_outer_radius, blend_layer_z
from .continuous_spiral import emit_loop, _overhang_fan, _fan_on_threshold


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _polygon_radius_fn(poly: Contour) -> Callable[[float], float]:
    """radius(theta) for a closed polygon centred near the origin.

    Identical to the one in mesh_spiral -- duplicated here so this module
    stays self-contained and we don't modify existing generators.
    """
    verts = [(x, y, math.atan2(y, x), math.hypot(x, y)) for (x, y) in poly]

    def r(theta: float) -> float:
        t = math.atan2(math.sin(theta), math.cos(theta))
        best_r = verts[0][3]
        best_d = 1e18
        for (_x, _y, a, rad) in verts:
            d = abs(math.atan2(math.sin(t - a), math.cos(t - a)))
            if d < best_d:
                best_d = d
                best_r = rad
        return best_r

    return r


def _ensure_fits(profile, cx: float, cy: float, radius: float) -> None:
    if (cx - radius < profile.print_min_x or cx + radius > profile.print_max_x
            or cy - radius < profile.print_min_y or cy + radius > profile.print_max_y):
        raise ValueError(
            f"Footprint radius {radius:.1f} at center ({cx:.0f},{cy:.0f}) "
            f"falls outside the safe print area "
            f"X[{profile.print_min_x}-{profile.print_max_x}] "
            f"Y[{profile.print_min_y}-{profile.print_max_y}]. "
            f"Shrink the contour or recenter."
        )


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def build_profile_spiral(
    writer: GcodeWriter,
    contours: list[Contour],
    heights: list[float],
    *,
    points_per_turn: int = 240,
    # Z-wave parameters
    z_amp: float = 0.0,
    z_waves: int = 4,
    z_twist_turns: float = 0.0,
    z_amp_ramp: float = 0.15,
    z_amp_envelope: Callable[[float], float] | None = None,
    # Radial texture parameters
    r_pattern: str | None = None,
    r_amp: float = 1.0,
    r_waves: int = 12,
    r_bands: float = 6.0,
    r_twist_turns: float = 0.0,
    r_phase: float = 0.0,
    r_fade_in: float = 0.10,
    r_fade_out: float = 0.0,
    r_envelope: Callable[[float], float] | None = None,
    r_alternate: bool = False,
    # XY twist
    xy_twist_turns: float = 0.0,
    # Placement / adhesion
    center: tuple[float, float] | None = None,
    base_z: float | None = None,
    travel_z_clearance: float = 5.0,
    first_layer_squish: float = 1.0,
    first_layer_spacing_factor: float = 1.0,
    first_layer_flow: float = 1.0,
    base_layers: int = 0,
    brim_loops: int = 0,
    blob_spec: BlobSpec | None = None,
    loop_spec: LoopSpec | None = None,
    overhang_flow_k: float = 0.0,
    fan_overhang_min: float | None = None,
    fan_overhang_max: float | None = None,
    fan_off_layers: int = 0,
    width_callback: Callable[[float], float] | None = None,
) -> dict:
    """Emit a complete profile-spiral and return a report dict.

    Parameters
    ----------
    writer : GcodeWriter
        Target G-code writer (already configured with profile / filament).
    contours : list[Contour]
        Ordered contour stack -- one closed polygon per layer, each with
        *points_per_turn* vertices, CCW wound.
    heights : list[float]
        Z height for each contour (same length as *contours*).
    width_callback : Callable[[float], float] | None
        If provided, called with the height fraction t in [0,1] for each wall
        point (same t used for z_amp_envelope/r_envelope); its return value
        multiplies the writer's nominal line_width for that move (passed
        through as ``line_width_override``). None (default) = no per-point
        override, and the emitted G-code is byte-identical to the generator
        without this parameter.
    """
    if len(contours) != len(heights):
        raise ValueError(
            f"contours ({len(contours)}) and heights ({len(heights)}) "
            f"length mismatch"
        )
    if not contours:
        raise ValueError("empty contour stack")

    n_contours = len(contours)
    n = points_per_turn

    # Validate radial texture pattern name up front.
    if r_pattern is not None and r_pattern not in _R_PATTERNS:
        raise ValueError(
            f"unknown r_pattern '{r_pattern}', expected one of {R_PATTERN_NAMES}"
        )

    profile = writer.profile
    cx, cy = center if center is not None else profile.bed_center
    lh = writer.layer_height

    # Overhang-adaptive fan: both bounds must be given to activate (None,None
    # is the default -- constant writer.fan_speed, byte-identical output).
    fan_overhang_active = fan_overhang_min is not None and fan_overhang_max is not None
    # A solid base already prints with no fan calls at all (nowhere in that
    # emission to turn it on) -- so a requested fan_off_layers count at or
    # below base_layers just means "turn on right after the base", same as
    # today's unconditional default.
    fan_immediate = base_layers > 0 and fan_off_layers <= base_layers

    # Adhesion package active?
    adhesion = (first_layer_squish < 1.0 or base_layers > 0 or brim_loops > 0)
    squish = first_layer_squish if adhesion else 1.0
    if base_z is None:
        base_z = squish * lh

    s0 = writer.line_width / max(squish, 1e-6) * max(first_layer_spacing_factor, 0.5)
    wall_off = base_z + (base_layers - 1) * lh if base_layers > 0 else base_z

    # ---- safety: footprint + top Z -----------------------------------------
    radius = max(math.hypot(x, y) for c in contours for (x, y) in c)

    bottom = contours[0]
    radius_fn = _polygon_radius_fn(bottom)

    brim_r = brim_outer_radius(radius_fn, s0, brim_loops) if adhesion else 0.0
    fit_radius = max(radius, brim_r)
    if loop_spec is not None:
        fit_radius = max(fit_radius, radius + loop_spec.out_mm)
    _ensure_fits(profile, cx, cy, fit_radius)

    # Compute top Z from the heights array + any z_amp headroom.
    max_height = max(heights)
    top_z = wall_off + max_height + abs(z_amp)
    if loop_spec is not None:
        top_z += loop_spec.up_mm
    if top_z > profile.z_max:
        raise ValueError(
            f"Print top Z {top_z:.1f} exceeds Z max {profile.z_max}. "
            f"Reduce height or layer count."
        )

    # ---- base / brim -------------------------------------------------------
    base_seq = layered_base_and_brim(
        radius_fn, writer.line_width,
        first_layer_spacing=s0,
        base_layers=base_layers, brim_loops=brim_loops,
        points_per_turn=n,
        start_theta=math.atan2(bottom[0][1], bottom[0][0]),
    ) if adhesion else []

    # ---- emit G-code -------------------------------------------------------
    writer.header()

    # Determine start position.
    if base_seq:
        s0x, s0y = cx + base_seq[0][0], cy + base_seq[0][1]
        sz = base_z + base_seq[0][2] * lh
    else:
        s0x, s0y = cx + bottom[0][0], cy + bottom[0][1]
        sz = wall_off + heights[0]
    writer.comment("move to profile spiral start")
    writer.safe_lift(sz + travel_z_clearance)
    writer.travel(s0x, s0y, sz + travel_z_clearance)
    writer.travel(s0x, s0y, sz)
    writer.unretract()

    # ---- base / brim emission ----------------------------------------------
    _fan_on = False
    if base_seq:
        writer.comment(
            f"solid base ({base_layers} layers) + brim ({brim_loops} loops)")
        base_zs = blend_layer_z(base_seq, base_z, lh)
        # The base/brim stack sits entirely below the wall's own t range (the
        # wall's t=0 is its first contour, printed after the base) -- so t=0.0
        # is the natural height fraction for every base/brim point here,
        # matching the same "start of the object" convention used elsewhere
        # (z_amp_ramp, r_fade_in) for t=0.
        cb_w_base = width_callback(0.0) if width_callback is not None else None
        lw_base = (writer.line_width * cb_w_base
                  if cb_w_base is not None else None)
        for (bx, by, k), bz in zip(base_seq, base_zs):
            first_disk = (k == 0)
            writer.extrude_to(
                cx + bx, cy + by, bz,
                speed=writer.first_layer_speed if first_disk else writer.print_speed,
                layer_height_override=lh,
                flow_override=first_layer_flow if first_disk else 1.0,
                line_width_override=lw_base,
            )
        if fan_immediate:
            writer.set_fan(fan_overhang_min if fan_overhang_active else writer.fan_speed)
            _fan_on = True

    # ---- wall spiral -------------------------------------------------------
    writer.comment("wall spiral")

    # Total-layer-count threshold (base + wall) at which the fan may turn on.
    # fan_off_layers<=0 reproduces today's default exactly: tilt is None for
    # step_idx < n regardless, so _overhang_fan(None,...) already returns
    # fan_min there whenever fan_immediate fired -- no separate cold-start
    # gate is needed the way continuous_spiral.py's two-branch structure
    # requires one.
    fan_on_threshold = _fan_on_threshold(fan_off_layers, base_layers, n)

    # Rate-limited z-wave amplitudes and emitted Z values, one per step,
    # flattened as (i * n + j).  Keeping the Z ring buffer lets us compute
    # the true gap to the point exactly one turn below.
    _amps: list[float] = []
    _zvals: list[float] = []
    _radii: list[float] = []

    total_steps = n_contours * n

    # Blob placement: compute which indices get a blob deposit.
    blob_sites: set[int] = set()
    if blob_spec is not None and blob_spec.blobs_per_turn > 0:
        blob_sites = compute_blob_sites(total_steps, n, blob_spec)
        # Remove any sites in the first turn (first-layer zone).
        blob_sites -= set(range(n + 1))

    loop_sites: set[int] = set()
    loop_skip = 2
    if loop_spec is not None and loop_spec.loops_per_turn > 0:
        loop_sites = compute_loop_sites(total_steps, n, loop_spec)
        loop_sites -= set(range(n + 1))
        seg_len = max(2.0 * math.pi * radius / n, 1e-6)
        loop_skip = max(2, int(round(loop_spec.rejoin_mm / seg_len)))
    # Pending teardrop: (rejoin_step, anchor_x, anchor_y, anchor_z, ux, uy).
    _loop_pending = None

    # Cache contour normals per layer to avoid recomputing every sub-step.
    # When blending, we compute normals on the blended contour only if
    # texture is active (relatively expensive but needed for accuracy).
    _cached_normals: dict[int, list[tuple[float, float]]] = {}

    for i in range(n_contours):
        for j in range(n):
            step_idx = i * n + j
            frac = j / n  # fraction within this turn
            # Height fraction t: 0..1 over the full print.
            t = (i + frac) / max(n_contours - 1, 1)
            t = min(1.0, max(0.0, t))

            # ---- interpolate contour (blend i and i+1 by frac) -------------
            i_next = min(i + 1, n_contours - 1)
            if i_next != i:
                blended = interpolate_contours(contours[i], contours[i_next], frac)
            else:
                blended = contours[i]

            # Point from the blended contour.
            px, py = blended[j]

            # ---- XY twist: rotate point with height ------------------------
            if xy_twist_turns != 0.0:
                angle = -xy_twist_turns * 2.0 * math.pi * t
                cos_a, sin_a = math.cos(angle), math.sin(angle)
                px, py = px * cos_a - py * sin_a, px * sin_a + py * cos_a

            # ---- radial texture displacement along outward normal ----------
            if r_pattern is not None:
                # Compute normals on the blended contour (cached per contour
                # index for non-blended cases).
                if i_next == i:
                    if i not in _cached_normals:
                        _cached_normals[i] = contour_normals(contours[i])
                    norms = _cached_normals[i]
                else:
                    norms = contour_normals(blended)

                nx, ny = norms[j]
                theta_tex = 2.0 * math.pi * j / n
                a = (r_waves * theta_tex
                     + 2.0 * math.pi * (r_twist_turns * t + r_phase))
                if r_alternate and i % 2 == 1:
                    a += math.pi      # odd turns swing the opposite way
                b = 2.0 * math.pi * r_bands * t
                env = (r_envelope(t) if r_envelope is not None
                       else _fade_envelope(t, r_fade_in, r_fade_out))
                pattern_fn = _R_PATTERNS[r_pattern]
                disp = r_amp * env * pattern_fn(a, b)
                px += disp * nx
                py += disp * ny

            # ---- Z computation ---------------------------------------------
            # Base Z from interpolating the heights array.
            if i_next != i:
                z_base = heights[i] + frac * (heights[i_next] - heights[i])
            else:
                z_base = heights[i]

            z = wall_off + z_base

            # Z-wave modulation.
            if z_amp != 0.0:
                theta_z = 2.0 * math.pi * j / n
                phase = z_twist_turns * 2.0 * math.pi * t
                if z_amp_envelope is not None:
                    ramp = z_amp_envelope(t)
                elif z_amp_ramp > 0.0:
                    ramp = min(1.0, t / z_amp_ramp)
                else:
                    ramp = 1.0
                amp = ramp * z_amp
                # Rate limiter: amplitude may only change by a fraction of
                # layer_height per turn.
                if step_idx >= n:
                    prev_amp = _amps[step_idx - n]
                    d = _MAX_AMP_STEP * lh
                    if amp > prev_amp + d:
                        amp = prev_amp + d
                    elif amp < prev_amp - d:
                        amp = prev_amp - d
                _amps.append(amp)
                z += amp * math.sin(z_waves * theta_z + phase)
            else:
                _amps.append(0.0)

            _zvals.append(z)
            cur_r = math.hypot(px, py)
            _radii.append(cur_r)

            # ---- gap-aware layer height ------------------------------------
            if i == 0 and base_layers == 0:
                gap = lh
                flow = first_layer_flow
                speed = writer.first_layer_speed
                oh_flow = 1.0
            else:
                if step_idx >= n:
                    gap = z - _zvals[step_idx - n]
                    dr = cur_r - _radii[step_idx - n]
                    vgap = max(gap, 0.01)
                    tilt = math.atan2(dr, vgap)
                else:
                    gap = lh
                    tilt = None
                if step_idx >= fan_on_threshold and not _fan_on:
                    writer.set_fan(_overhang_fan(tilt, fan_overhang_min, fan_overhang_max)
                                   if fan_overhang_active else writer.fan_speed)
                    _fan_on = True
                elif step_idx >= fan_on_threshold and fan_overhang_active:
                    writer.set_fan_if_changed(_overhang_fan(tilt, fan_overhang_min, fan_overhang_max))
                flow = 1.0
                speed = writer.print_speed
                if overhang_flow_k > 0.0 and tilt is not None and tilt > 0.0:
                    oh_flow = min(max(1.0 + overhang_flow_k * math.tan(max(tilt, 0.0)), 0.7), 1.6)
                else:
                    oh_flow = 1.0

            x_bed, y_bed = cx + px, cy + py
            if _loop_pending is not None:
                if step_idx < _loop_pending[0]:
                    continue      # wall replaced by the loop strand here
                # Arrived at the rejoin point: extrude the teardrop to here.
                _, ax, ay, az, lux, luy = _loop_pending
                emit_loop(writer, ax, ay, az, x_bed, y_bed, z,
                          lux, luy, loop_spec)
                _loop_pending = None
                continue
            cb_w = width_callback(t) if width_callback is not None else None
            writer.extrude_to(x_bed, y_bed, z, speed=speed,
                              layer_height_override=gap, flow_override=flow * oh_flow,
                              line_width_override=(writer.line_width * cb_w
                                                    if cb_w is not None else None))
            if step_idx in blob_sites:
                vol = blob_volume_at(step_idx / max(total_steps - 1, 1), blob_spec)
                e_mm = blob_e_for_volume(vol, writer.profile)
                writer.blob(e_mm, dwell_before_ms=0,
                            dwell_after_ms=blob_spec.dwell_after_ms)
            if step_idx in loop_sites and step_idx + loop_skip < total_steps - 1:
                nrm = math.hypot(px, py)
                lux, luy = (px / nrm, py / nrm) if nrm > 1e-9 else (1.0, 0.0)
                _loop_pending = (step_idx + loop_skip,
                                 x_bed, y_bed, z, lux, luy)

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
        "contours": n_contours,
        "points": total_steps,
        "base_layers": base_layers,
        "brim_loops": brim_loops,
        "footprint_radius_mm": round(fit_radius, 2),
        "top_z_mm": round(top_z, 2),
        "filament_mm": round(writer.total_filament_mm, 1),
        "max_z_rate_mm_s": round(writer.max_z_rate, 2),
        "layer_height_clamp_events": writer.layer_height_clamp_events,
        "width_clamp_events": writer.width_clamp_events,
        "blob_count": writer.blob_count,
        "loop_count": len(loop_sites),
        "bounds": writer.bounds,
    }
