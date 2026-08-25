"""Volumetric extrusion math and feedrate clamping.

The core idea of a continuous non-planar print is that the toolhead never lifts
between layers: Z rises smoothly along an XY path.  Two things must stay correct
for this to print well:

1. Extrusion volume per move must match the *3D* distance travelled (the bead is
   laid along the slanted path, not the flat projection).
2. The commanded feedrate must never push the Z axis past ``max_z_velocity``.
"""
from __future__ import annotations

import math

from .profile import PrinterProfile


def extrusion_for_segment(
    length_3d: float,
    line_width: float,
    layer_height: float,
    profile: PrinterProfile,
    flow_multiplier: float = 1.0,
) -> float:
    """Return mm of filament to extrude for a bead of given cross-section.

    Cross-section is modelled as ``line_width * layer_height`` (rectangular
    approximation, the same one slicers use for the bulk of a path).
    """
    volume = length_3d * line_width * layer_height * flow_multiplier
    return volume / profile.filament_area


def clamp_feedrate_for_z(
    dx: float,
    dy: float,
    dz: float,
    requested_mm_s: float,
    profile: PrinterProfile,
) -> float:
    """Lower the feedrate so the Z component stays within ``max_z_velocity``.

    Returns the allowed speed in mm/s.  Also caps to ``max_velocity``.
    """
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    speed = min(requested_mm_s, profile.max_velocity)
    if dist == 0.0:
        return speed

    # Fraction of the move's velocity that lands on the Z axis.
    z_fraction = abs(dz) / dist
    if z_fraction > 0.0:
        z_limited = profile.max_z_velocity / z_fraction
        speed = min(speed, z_limited)
    return speed
