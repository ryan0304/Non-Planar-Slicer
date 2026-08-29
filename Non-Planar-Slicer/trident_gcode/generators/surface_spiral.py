"""Non-planar surface-following: pave a height field with a continuous spiral.

Generates an Archimedean spiral over a disk (centre -> edge) and rides Z along a
:data:`~trident_gcode.surface.HeightField`, so the printed shell conforms to the
surface with no stair-stepping. Stacking several shells (alternating out/in to stay
continuous) builds a thick conformal wall — a continuous non-planar thick print.
"""
from __future__ import annotations

import math

from ..gcode import GcodeWriter
from ..surface import HeightField


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
) -> dict:
    profile = writer.profile
    cx, cy = center if center is not None else profile.bed_center
    if first_layer_z is None:
        first_layer_z = writer.layer_height
    lh = writer.layer_height

    _ensure_fits(profile, cx, cy, radius)

    base_pts = _archimedean(radius, writer.line_width, resolution)
    if len(base_pts) < 2:
        raise ValueError("surface too small for the given line width")

    # Lift the whole field so its lowest point sits at the first-layer height.
    fmin = min(field(x, y) for (x, y) in base_pts)
    z_offset = first_layer_z - fmin

    top_z = z_offset + (shells - 1) * lh + max(field(x, y) for (x, y) in base_pts)
    if top_z > profile.z_max:
        raise ValueError(
            f"Surface top Z {top_z:.1f} exceeds Z max {profile.z_max}. "
            f"Reduce amplitude or shells."
        )

    writer.header()
    first = base_pts[0]
    sx, sy = cx + first[0], cy + first[1]
    sz = z_offset + (field(first[0], first[1]))
    writer.comment("move to surface spiral start (centre)")
    writer.safe_lift(sz + travel_z_clearance)
    writer.travel(sx, sy, sz + travel_z_clearance)
    writer.travel(sx, sy, sz)
    writer.unretract()

    n_pts = 0
    _fan_on = False
    for s in range(shells):
        # alternate direction each shell so the path stays continuous (no travel)
        seq = base_pts if s % 2 == 0 else list(reversed(base_pts))
        if s == 1 and not _fan_on:
            writer.set_fan(writer.fan_speed)
            _fan_on = True
        for j, (lx, ly) in enumerate(seq):
            x, y = cx + lx, cy + ly
            z = z_offset + s * lh + field(lx, ly)
            speed = writer.first_layer_speed if s == 0 else writer.print_speed
            # Gap between conformal shells is exactly the nominal layer height
            # (they are parallel copies offset by `lh`), so no gap-aware override
            # is needed here — the writer's nominal layer_height is already right.
            writer.extrude_to(x, y, z, speed=speed)
            n_pts += 1
    # Single-shell surface: no second shell to trigger fan — emit fan at end of shell 0
    if not _fan_on:
        writer.set_fan(writer.fan_speed)

    writer.retract()
    writer.footer()

    return {
        "shells": shells,
        "points": n_pts,
        "footprint_radius_mm": round(radius, 2),
        "top_z_mm": round(top_z, 2),
        "filament_mm": round(writer.total_filament_mm, 1),
        "max_z_rate_mm_s": round(writer.max_z_rate, 2),
        "bounds": writer.bounds,
    }


def _ensure_fits(profile, cx, cy, radius) -> None:
    if (cx - radius < profile.print_min_x or cx + radius > profile.print_max_x
            or cy - radius < profile.print_min_y or cy + radius > profile.print_max_y):
        raise ValueError(
            f"Surface footprint radius {radius:.1f} at center ({cx:.0f},{cy:.0f}) "
            f"falls outside the safe print area. Shrink --radius or --surface-scale."
        )
