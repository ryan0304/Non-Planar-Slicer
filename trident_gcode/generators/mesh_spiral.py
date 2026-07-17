"""Continuous non-planar spiral wrapped around an STL mesh silhouette.

Slices the mesh into per-layer outer contours and prints them as a single
unbroken spiral (vase-style), so an arbitrary shape becomes a continuous thick
wall. Optional non-planar Z waves ride on top, exactly like the parametric
generator. Reuses the same machine-safe :class:`GcodeWriter`.
"""
from __future__ import annotations

import math

from ..gcode import GcodeWriter
from ..mesh import (
    Triangle,
    mesh_bounds,
    slice_outer_loop,
    resample_closed,
    align_start,
)
from .base_fill import layered_base_and_brim, brim_outer_radius, blend_layer_z


def build_mesh_spiral(
    writer: GcodeWriter,
    tris: list[Triangle],
    *,
    points_per_turn: int = 200,
    scale: float = 1.0,
    z_amp: float = 0.0,
    z_waves: int = 4,
    z_amp_ramp: float = 0.15,
    center: tuple[float, float] | None = None,
    base_z: float | None = None,
    travel_z_clearance: float = 5.0,
    first_layer_squish: float = 1.0,
    first_layer_spacing_factor: float = 1.0,
    first_layer_flow: float = 1.0,
    base_layers: int = 0,
    brim_loops: int = 0,
) -> dict:
    profile = writer.profile
    cx, cy = center if center is not None else profile.bed_center
    lh = writer.layer_height

    # First-layer / base / brim package active? If not, keep byte-identical output.
    # first_layer_flow only parameterises the package when already on.
    adhesion = (first_layer_squish < 1.0 or base_layers > 0 or brim_loops > 0)
    squish = first_layer_squish if adhesion else 1.0
    if base_z is None:
        base_z = squish * lh

    if scale != 1.0:
        tris = [tuple((vx * scale, vy * scale, vz * scale) for (vx, vy, vz) in t)
                for t in tris]

    (minx, miny, minz), (maxx, maxy, maxz) = mesh_bounds(tris)
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0   # XY centre to recentre on bed
    height = maxz - minz
    if height <= 0:
        raise ValueError("mesh has no height to print")

    n_layers = max(1, int(round(height / lh)))

    # --- slice every layer into a recentred, resampled contour ---------------
    contours: list[list[tuple[float, float]]] = []
    ref = (max(maxx - mx, 1e-6), 0.0)   # seam reference: +X side of the part
    skipped = 0
    for i in range(n_layers):
        h = minz + (i + 0.5) * lh       # sample at mid-layer
        loop = slice_outer_loop(tris, h)
        if loop is None or len(loop) < 4:
            skipped += 1
            continue
        loop = [(x - mx, y - my) for (x, y) in loop]
        rs = resample_closed(loop, points_per_turn)
        rs = align_start(rs, ref)
        ref = rs[0]                      # carry the seam upward
        contours.append(rs)

    if not contours:
        raise ValueError(
            "no printable cross-sections found — is the STL watertight and Z-up?"
        )

    # Build a radius(theta) for the base/brim from the bottom contour polygon,
    # indexed by angle (star-convex assumption, same as the parametric shapes).
    bottom = contours[0]
    radius_fn = _polygon_radius_fn(bottom)

    # Effective first-layer spacing (squished bead spreads to ~lw/squish wide).
    # The wall starts AT the top disk's level and rises from it, so the
    # base -> wall hand-off is continuous in Z.
    s0 = writer.line_width / max(squish, 1e-6) * max(first_layer_spacing_factor, 0.5)
    wall_off = base_z + (base_layers - 1) * lh if base_layers > 0 else base_z

    # --- safety: footprint + top Z -------------------------------------------
    radius = max(math.hypot(x, y) for c in contours for (x, y) in c)
    brim_r = brim_outer_radius(radius_fn, s0, brim_loops) if adhesion else 0.0
    fit_radius = max(radius, brim_r)
    _ensure_fits(profile, cx, cy, fit_radius)
    top_z = wall_off + len(contours) * lh
    if top_z > profile.z_max:
        raise ValueError(
            f"Print top Z {top_z:.1f} exceeds Z max {profile.z_max}. Scale down or trim height."
        )

    # Brim + solid base path (empty unless requested). Entries are (x, y, layer):
    # layer k prints at base_z + k*lh. Seam ray = angle of the bottom contour's
    # start point so the last disk ends exactly at the wall's start.
    base_seq = layered_base_and_brim(
        radius_fn, writer.line_width,
        first_layer_spacing=s0,
        base_layers=base_layers, brim_loops=brim_loops,
        points_per_turn=points_per_turn,
        start_theta=math.atan2(bottom[0][1], bottom[0][0]),
    ) if adhesion else []

    # --- emit a single continuous spiral -------------------------------------
    writer.header()
    first = contours[0]
    if base_seq:
        s0x, s0y = cx + base_seq[0][0], cy + base_seq[0][1]
        sz = base_z + base_seq[0][2] * lh
    else:
        s0x, s0y = cx + first[0][0], cy + first[0][1]
        sz = base_z
    writer.comment("move to mesh spiral start")
    writer.safe_lift(sz + travel_z_clearance)
    writer.travel(s0x, s0y, sz + travel_z_clearance)
    writer.travel(s0x, s0y, sz)
    writer.unretract()

    _fan_on = False
    if base_seq:
        writer.comment(
            f"solid base ({base_layers} layers) + brim ({brim_loops} loops)")
        base_zs = blend_layer_z(base_seq, base_z, lh)
        for (bx, by, k), bz in zip(base_seq, base_zs):
            first_disk = (k == 0)
            writer.extrude_to(
                cx + bx, cy + by, bz,
                speed=writer.first_layer_speed if first_disk else writer.print_speed,
                layer_height_override=lh,          # disk 0: full volume, squished
                flow_override=first_layer_flow if first_disk else 1.0,
            )
        if base_layers > 0:
            writer.set_fan(writer.fan_speed)
            _fan_on = True

    n = points_per_turn
    n_done = len(contours)

    def _z_at(i: int, j: int) -> float:
        """Z of contour i at index j, including the non-planar wave."""
        z = wall_off + (i + j / n) * lh
        if z_amp != 0.0:
            ramp = min(1.0, (i / n_done) / z_amp_ramp) if z_amp_ramp > 0 else 1.0
            z += ramp * z_amp * math.sin(z_waves * 2.0 * math.pi * j / n)
        return z

    for i, contour in enumerate(contours):
        for j, (lx, ly) in enumerate(contour):
            x, y = cx + lx, cy + ly
            z = _z_at(i, j)
            # Gap-aware layer height: this contour's z at j minus the previous
            # contour's z at the same j (the point one turn below). Constant per
            # layer today, but correct once waves ramp/rotate.
            if i == 0 and base_layers == 0:
                # Wall IS the first layer: squish-compensated priming turn.
                gap = lh
                flow = first_layer_flow
                speed = writer.first_layer_speed
            else:
                if not _fan_on:
                    writer.set_fan(writer.fan_speed)
                    _fan_on = True
                gap = lh if i == 0 else z - _z_at(i - 1, j)
                flow = 1.0
                speed = writer.print_speed
            writer.extrude_to(x, y, z, speed=speed,
                              layer_height_override=gap, flow_override=flow)

    writer.retract()
    writer.footer()

    if writer.layer_height_clamp_events:
        writer.comment(
            f"WARNING: {writer.layer_height_clamp_events} moves had local layer "
            f"height clamped to [0.25,1.5]x nominal (steep non-planar geometry)"
        )

    return {
        "layers_printed": n_done,
        "layers_skipped": skipped,
        "points": n_done * n,
        "base_layers": base_layers,
        "brim_loops": brim_loops,
        "footprint_radius_mm": round(fit_radius, 2),
        "top_z_mm": round(top_z, 2),
        "filament_mm": round(writer.total_filament_mm, 1),
        "max_z_rate_mm_s": round(writer.max_z_rate, 2),
        "layer_height_clamp_events": writer.layer_height_clamp_events,
        "bounds": writer.bounds,
    }


def _polygon_radius_fn(poly: list[tuple[float, float]]):
    """radius(theta) for a closed polygon centred near the origin.

    Samples are taken by nearest vertex angle (the contour is densely and evenly
    resampled, so this is smooth enough for the base/brim warp). Star-convex
    outlines only — the same assumption the parametric shapes satisfy.
    """
    import math as _m
    verts = [(x, y, _m.atan2(y, x), _m.hypot(x, y)) for (x, y) in poly]

    def r(theta: float) -> float:
        # wrap theta to [-pi, pi] and find the vertex with the closest angle
        t = _m.atan2(_m.sin(theta), _m.cos(theta))
        best_r = verts[0][3]
        best_d = 1e18
        for (_x, _y, a, rad) in verts:
            d = abs(_m.atan2(_m.sin(t - a), _m.cos(t - a)))
            if d < best_d:
                best_d = d
                best_r = rad
        return best_r

    return r


def _ensure_fits(profile, cx, cy, radius) -> None:
    if (cx - radius < profile.print_min_x or cx + radius > profile.print_max_x
            or cy - radius < profile.print_min_y or cy + radius > profile.print_max_y):
        raise ValueError(
            f"Mesh footprint radius {radius:.1f} at center ({cx:.0f},{cy:.0f}) "
            f"falls outside the safe print area. Use --stl-scale to shrink it."
        )
