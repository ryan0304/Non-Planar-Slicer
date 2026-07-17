"""Solid base disk + brim as one continuous bead.

Both are built as *outline-warped Archimedean spirals*: a radial scale ``s`` sweeps
continuously across many turns while the point rides the part outline ``r(theta)``,
so a star-convex closed shape (all our parametric outlines, and mesh bottom contours
via their resampled polygon treated as ``r`` indexed by angle) gets paved with a
gap-free radial spiral that never lifts the nozzle.

The base fills the interior (centre <-> outline). The brim adds a few outward loops
beyond the outline at the same Z. Chained brim -> base -> wall, the whole thing is a
single unbroken path: the brim spirals inward to the outline, the base fills, and the
last base pass ends exactly at the outline seam where the wall spiral begins.

Everything here is produced centred on the origin (like ``spiral_path``); the caller
adds the bed centre and the base Z.
"""
from __future__ import annotations

import math
from typing import Callable


def _outline_xy(radius_fn: Callable[[float], float], theta: float, scale: float
                ) -> tuple[float, float]:
    r = radius_fn(theta) * scale
    return (r * math.cos(theta), r * math.sin(theta))


def _mean_radius(radius_fn: Callable[[float], float], samples: int = 180) -> float:
    return sum(radius_fn(2.0 * math.pi * k / samples)
               for k in range(samples)) / samples


def base_disk_points(
    radius_fn: Callable[[float], float],
    line_width: float,
    *,
    points_per_turn: int = 240,
    outward: bool = True,
    start_theta: float = 0.0,
) -> list[tuple[float, float]]:
    """One solid base disk as a warped Archimedean spiral.

    ``outward`` True sweeps centre -> outline; False sweeps outline -> centre.
    Radial spacing between successive loops equals ``line_width``. The path
    starts/ends on the seam ray ``start_theta`` so passes chain cleanly.

    The inward pass is the OUTWARD spiral traversed in reverse (not a re-phased
    parameterisation), so stacked disks land on radially coincident loops at every
    angle — otherwise reversing the radial sweep alone shifts the loops by half a
    line width off the seam and the fill spacing halves. When sweeping outward the
    path ends ON the outline at ``start_theta`` (scale 1.0) — exactly where the
    wall spiral begins, giving a travel-free hand-off.
    """
    r_mean = max(_mean_radius(radius_fn), 1e-6)
    n_loops = max(1, int(math.ceil(r_mean / max(line_width, 1e-6))))
    total = n_loops * points_per_turn

    pts: list[tuple[float, float]] = []
    for i in range(total + 1):
        f = i / total                       # 0 -> 1: centre -> outline
        theta = start_theta + 2.0 * math.pi * n_loops * f
        pts.append(_outline_xy(radius_fn, theta, f))
    if not outward:
        pts.reverse()                       # outline -> centre, same loop radii
    return pts


def base_disk_concentric(
    radius_fn: Callable[[float], float],
    line_width: float,
    *,
    points_per_turn: int = 240,
    outward: bool = True,
    start_theta: float = 0.0,
) -> list[tuple[float, float]]:
    """One solid base disk as CONCENTRIC closed rings (outline in to centre).

    Unlike the Archimedean-spiral disk, this lays a set of complete closed loops
    that each follow the part outline ``r(theta) * scale``. Ring scales step
    inward by ``line_width`` from the outline (scale 1.0) toward the centre, so
    the radial spacing between rings equals ``line_width`` — matching the spiral
    disk's fill density and loop radii, so stacked disks of either style land on
    radially coincident loops.

    Rings are chained centre -> outline (``outward`` True), each ring starting on
    the seam ray ``start_theta`` and consecutive rings joined by a short radial
    hop at the seam. The final ring is the outline at scale 1.0, ending exactly on
    the seam where the wall spiral begins. ``outward`` False traverses the same
    rings in reverse (outline -> centre).

    Each ring's point count scales with its radius (the outermost/outline ring
    gets the full ``points_per_turn``; inner rings get proportionally fewer, with
    a small floor). A fixed count would make the tiny near-centre rings out of
    ~0.02 mm tangential steps, and a radial hop landing in that dwell during the
    disk's Z-blend produces a huge finite-difference Z-accel demand that the
    machine (and analyzer) reject. Proportional sampling keeps every segment near
    the outline's length, so the toolpath stays smooth like the spiral disk while
    the rings remain radially coincident across stacked disks (same scale -> same
    count).
    """
    r_mean = max(_mean_radius(radius_fn), 1e-6)
    n_loops = max(1, int(math.ceil(r_mean / max(line_width, 1e-6))))
    step = line_width / r_mean               # scale step between rings

    pts: list[tuple[float, float]] = []
    # Ring r = 0 (innermost) .. n_loops-1 (outermost = outline at scale 1.0).
    for r in range(n_loops):
        scale = 1.0 - (n_loops - 1 - r) * step
        if scale <= 1e-6:
            continue                         # skip a degenerate centre ring
        n_pts = max(12, min(points_per_turn, int(round(points_per_turn * scale))))
        for j in range(n_pts + 1):
            theta = start_theta + 2.0 * math.pi * (j / n_pts)
            pts.append(_outline_xy(radius_fn, theta, scale))
    if not outward:
        pts.reverse()                        # outline -> centre, same ring radii
    return pts


def brim_points(
    radius_fn: Callable[[float], float],
    spacing: float,
    loops: int,
    *,
    points_per_turn: int = 240,
    start_theta: float = 0.0,
    outward: bool = False,
) -> list[tuple[float, float]]:
    """``loops`` brim rings beyond the outline.

    Ring k (k = 1..loops) sits at ``r(theta) * (1 + k*spacing/r_mean)`` — the
    innermost ring is one bead OUTSIDE the outline, so the brim never re-traces
    the outline bead laid by the base disk / priming loop.

    ``outward`` False (default): spiral from the outermost ring inward, ending
    one bead outside the outline at ``start_theta``. True: start there and
    spiral out (used when the brim prints after the base disks).
    Returns [] if ``loops`` <= 0.
    """
    if loops <= 0:
        return []
    r_mean = max(_mean_radius(radius_fn), 1e-6)
    k_out = spacing / r_mean                 # scale step per brim loop
    total = loops * points_per_turn

    pts: list[tuple[float, float]] = []
    for i in range(total + 1):
        f = i / total                        # 0 -> 1 along the print path
        u = f if outward else (1.0 - f)      # 0 = innermost ring, 1 = outermost
        scale = 1.0 + k_out * (1.0 + (loops - 1) * u)
        theta = start_theta + 2.0 * math.pi * loops * f
        pts.append(_outline_xy(radius_fn, theta, scale))
    return pts


def brim_outer_radius(radius_fn: Callable[[float], float], spacing: float,
                      loops: int) -> float:
    """Max radius the brim reaches, for the footprint check (0 if no brim)."""
    if loops <= 0:
        return 0.0
    r_mean = max(_mean_radius(radius_fn), 1e-6)
    scale = 1.0 + loops * (spacing / r_mean)
    samples = 360
    return max(radius_fn(2.0 * math.pi * k / samples) * scale
               for k in range(samples))


def skirt_points(
    radius_fn: Callable[[float], float],
    line_width: float,
    loops: int,
    base_beyond: float,
    *,
    points_per_turn: int = 240,
    start_theta: float = 0.0,
) -> list[tuple[float, float]]:
    """``loops`` outline-following skirt rings, clear of the whole print.

    The innermost skirt ring sits ``base_beyond`` mm outside the outline (caller
    passes brim reach + a clearance gap); each further ring steps out by
    ``line_width``. Built as a single continuous spiral from the OUTERMOST ring
    inward, so it ends nearest the part — the caller then retracts and travels to
    the main bead start. Returns [] if ``loops`` <= 0.
    """
    if loops <= 0:
        return []
    r_mean = max(_mean_radius(radius_fn), 1e-6)
    scale_in = 1.0 + base_beyond / r_mean
    scale_out = 1.0 + (base_beyond + (loops - 1) * line_width) / r_mean
    total = loops * points_per_turn
    pts: list[tuple[float, float]] = []
    for i in range(total + 1):
        f = i / total                        # 0 -> 1: outermost -> innermost
        scale = scale_out + (scale_in - scale_out) * f
        theta = start_theta + 2.0 * math.pi * loops * f
        pts.append(_outline_xy(radius_fn, theta, scale))
    return pts


def skirt_outer_radius(radius_fn: Callable[[float], float], line_width: float,
                       loops: int, base_beyond: float) -> float:
    """Max radius the skirt reaches, for the footprint check (0 if no skirt)."""
    if loops <= 0:
        return 0.0
    r_mean = max(_mean_radius(radius_fn), 1e-6)
    scale = 1.0 + (base_beyond + (loops - 1) * line_width) / r_mean
    samples = 360
    return max(radius_fn(2.0 * math.pi * k / samples) * scale
               for k in range(samples))


def layered_base_and_brim(
    radius_fn: Callable[[float], float],
    line_width: float,
    *,
    first_layer_spacing: float | None = None,
    base_layers: int,
    brim_loops: int,
    points_per_turn: int = 240,
    start_theta: float = 0.0,
    base_style: str = "spiral",
) -> list[tuple[float, float, int]]:
    """Print-ordered (x, y, layer) path for the brim + stacked base disks.

    ``layer`` is the disk index: emit at ``z = first_layer_z + layer*lh``. The
    brim is tagged layer 0. Layer 0 (brim + first disk) uses
    ``first_layer_spacing`` — the EFFECTIVE bead width after squishing
    (line_width / squish), so a squished first layer doesn't overlap and ooze —
    while upper disks use the nominal ``line_width``.

    Print order and continuity (never a travel):
      1. Base disks bottom-up, alternating sweep direction. Directions are chosen
         so the LAST disk sweeps outward and ends ON the outline seam at
         ``start_theta`` — exactly where the wall spiral begins.
      2. The brim (if any) then spirals outline -> outward at layer 0. The wall
         hand-off crosses back over the brim as one thin extruded strand, which
         tears away with the brim. With no base disks, the brim instead spirals
         inward and ends one bead outside the outline, handing to the priming
         loop / wall with a tiny radial step.

    ``base_style`` selects how each disk is paved: ``"spiral"`` (default) uses the
    outline-warped Archimedean spiral; ``"concentric"`` uses stacked closed rings
    stepping inward from the outline. Both honour the same outward/inward chaining
    so the final disk still ends on the outline seam.

    Returns [] if there's nothing to lay.
    """
    if base_layers <= 0 and brim_loops <= 0:
        return []

    s0 = first_layer_spacing if first_layer_spacing else line_width
    seq: list[tuple[float, float, int]] = []

    def _extend(part: list[tuple[float, float]], layer: int) -> None:
        # Drop a duplicated junction point when parts chain exactly.
        if not part:
            return
        if seq and _close((seq[-1][0], seq[-1][1]), part[0]):
            part = part[1:]
        seq.extend((x, y, layer) for (x, y) in part)

    disk_builder = (base_disk_concentric if base_style == "concentric"
                    else base_disk_points)

    if base_layers > 0:
        # Disk k sweeps outward iff (base_layers-1-k) is even, so the final disk
        # always ends on the outline seam. Layer 0 uses the squished spacing.
        for k in range(base_layers):
            outward = ((base_layers - 1 - k) % 2 == 0)
            spacing = s0 if k == 0 else line_width
            _extend(disk_builder(radius_fn, spacing,
                                 points_per_turn=points_per_turn,
                                 outward=outward, start_theta=start_theta), k)
        # Brim after the disks: outline -> outward at layer 0.
        _extend(brim_points(radius_fn, s0, brim_loops,
                            points_per_turn=points_per_turn,
                            start_theta=start_theta, outward=True), 0)
    else:
        # Brim only: spiral inward, ending one bead outside the outline.
        _extend(brim_points(radius_fn, s0, brim_loops,
                            points_per_turn=points_per_turn,
                            start_theta=start_theta, outward=False), 0)

    return seq


def blend_layer_z(
    seq: list[tuple[float, float, int]],
    first_layer_z: float,
    layer_height: float,
    lead_mm: float = 10.0,
) -> list[float]:
    """Per-point Z for a layered base path, with helical layer transitions.

    A hard Z step at a layer change is a near-vertical extruding hop — huge
    Z-accel demand (Klipper throttles it) and a blob at the junction. Instead,
    blend Z linearly over ``lead_mm`` of PATH LENGTH after each layer change.
    Distance-based (not point-based) because disk junctions sit at the spiral
    centre, where per-point XY steps shrink to nothing — a point-count blend
    would still climb near-vertically there.
    """
    zs: list[float] = []
    if not seq:
        return zs
    prev_k = seq[0][2]
    z_from = first_layer_z + prev_k * layer_height
    cum = lead_mm                    # start settled on the first layer
    px, py = seq[0][0], seq[0][1]
    for (x, y, k) in seq:
        d = math.hypot(x - px, y - py)
        px, py = x, y
        z_to = first_layer_z + k * layer_height
        if k != prev_k:
            z_from = first_layer_z + prev_k * layer_height
            cum = 0.0
            prev_k = k
        if cum < lead_mm:
            cum += d
            f = min(1.0, cum / lead_mm)
            zs.append(z_from + (z_to - z_from) * f)
        else:
            zs.append(z_to)
    return zs


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-6) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
