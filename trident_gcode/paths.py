"""2D base profiles and continuous (seamless) spiral path generation.

A continuous thick print is, at heart, vase-mode on steroids: a single XY loop
that is swept upward without ever lifting the nozzle, while Z is free to wobble
non-planar.  This module builds the *geometry*; ``gcode.py`` turns it into moves.

Coordinates here are produced centred on (0,0) and then offset to the bed centre
by the caller, so the profile shapes stay easy to reason about.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------- base shapes
# Each base shape is a function theta -> radius(theta), theta in radians.
# This makes it trivial to compose organic outlines (stars, lobes, superellipse).

def circle(radius: float) -> Callable[[float], float]:
    return lambda theta: radius


def star(radius: float, points: int = 5, depth: float = 0.3) -> Callable[[float], float]:
    """Smooth star / gear-like lobed profile. ``depth`` 0..1 of the radius."""
    return lambda theta: radius * (1.0 - depth * 0.5 * (1 - math.cos(points * theta)))


def superellipse(radius: float, n: float = 4.0) -> Callable[[float], float]:
    """Squircle-ish profile. n=2 circle, larger n -> squarer."""
    def r(theta: float) -> float:
        c = abs(math.cos(theta)) ** n
        s = abs(math.sin(theta)) ** n
        return radius / max((c + s) ** (1.0 / n), 1e-9)
    return r


@dataclass
class SpiralSpec:
    """Parameters for a continuous non-planar spiral."""
    base_radius: float = 35.0          # mm
    height: float = 60.0               # mm total
    layer_height: float = 0.30         # mm rise per turn (spiral pitch)
    points_per_turn: int = 240         # XY resolution

    # XY twist: rotate the cross-section as it rises so lobes spiral up the
    # column (the classic "twisted vase" look). Measured in full turns over the
    # whole height. 0 = lobes run straight up.
    xy_twist_turns: float = 0.0

    # Non-planar Z modulation: z gets += z_amp * sin(z_waves*theta + z_phase*t)
    z_amp: float = 0.0                 # mm wave amplitude (0 = planar top)
    z_waves: int = 4                   # number of waves around the perimeter
    z_twist_turns: float = 0.0         # how much the wave pattern rotates over height
    # Fraction of total height over which the wave amplitude ramps 0 -> full.
    # Keeps the first layers planar so the bottom adheres and the nozzle never
    # dips below the bed.
    z_amp_ramp: float = 0.15

    # Radius modulation for organic bulging (function of height fraction t).
    radius_envelope: Callable[[float], float] | None = None  # t in [0,1] -> scale

    # Asymmetric "control cage": a grid of radius scale factors applied AFTER the
    # symmetric radius_envelope, letting users bulge/dent LOCAL regions of the
    # wall. N rows (height; bottom row = t 0, top row = t 1, evenly spaced) x M
    # columns (azimuth; column j at bed angle theta = 2*pi*j/M, periodic). The
    # cage is sampled with cosine-eased bilinear interpolation (see cage_scale)
    # and is anchored to the BED, not the twisted shape. None = symmetric.
    cage: list | None = None

    # Spine offset: maps height fraction t -> (dx, dy) XY displacement of the
    # cross-section CENTRE, breaking radial symmetry (leaning / bent vases).
    # Applied to the final x/y AFTER radius+texture+ovality. None = centred.
    spine_offset: Callable[[float], tuple[float, float]] | None = None

    # Ovality: turns the circular cross-section elliptical. Applied to the final
    # x/y AFTER radius+texture but BEFORE the spine offset, so textures stay
    # attached to the wall. Used value is clamped to [-0.4, 0.4].
    # x *= (1 + ovality); y *= (1 - ovality). 0 = circular.
    ovality: float = 0.0

    # Z-amplitude envelope: callable(t) -> scale multiplying z_amp.
    # None = current behaviour (ramp from z_amp_ramp, then full amplitude).
    # When provided, overrides the built-in ramp: z_mod = z_amp * envelope(t).
    z_amp_envelope: Callable[[float], float] | None = None

    # --- Radial surface texture (OGcode-style pattern library) ---------------
    # Displaces the RADIUS, not Z — so it costs zero Z-velocity budget and can
    # never strike the probe behind the nozzle. Aggressive textures are fine
    # here; the practical limit is overhang (radial change per turn).
    # Patterns: vwave | hwave | ripple | diamond | bubbles | pleats | hammered
    r_pattern: str | None = None
    r_amp: float = 1.0                 # mm radial displacement amplitude
    r_waves: int = 12                  # pattern repeats around the perimeter
    r_bands: float = 6.0               # pattern cycles over the full height
    r_twist_turns: float = 0.0         # rotate the pattern over the height
    r_phase: float = 0.0               # pattern offset (fraction of a cycle)
    r_fade_in: float = 0.10            # height fraction to fade the texture in
    r_fade_out: float = 0.0            # height fraction to fade it out at the top
    r_envelope: Callable[[float], float] | None = None  # overrides fades: t -> 0..1
    # Open-lattice mode: flip the pattern phase half a cycle on every other
    # turn. Successive turns then displace in OPPOSITE radial directions and
    # only bond where the zigzags cross — the wall becomes an open mesh of
    # strands bridging air between crossing nodes (fabric/chainmail vases).
    # Works best with vwave/pleats, amp 2-3mm, waves 15-40, slow print speed.
    r_alternate: bool = False


# Max change of the Z-wave amplitude per spiral turn, as a fraction of the
# layer height. Keeps the vertical gap between successive turns positive
# (worst case gap = (1 - _MAX_AMP_STEP) * layer_height at aligned crests), so
# the nozzle can never dive into the previous turn when the amplitude changes.
_MAX_AMP_STEP = 0.6


def _tri(x: float) -> float:
    """Triangle wave, period 2*pi, range [-1, 1]."""
    return 2.0 / math.pi * math.asin(math.sin(x))


# Each pattern maps (a, b) -> [-1, 1], where a = angular coordinate (radians,
# already includes waves/twist/phase) and b = vertical coordinate (radians,
# 2*pi per band). Positive values displace the wall outward.
_R_PATTERNS: dict = {
    # vertical ridges running straight up the wall
    "vwave":   lambda a, b: math.sin(a),
    # horizontal ripples wrapping the wall
    "hwave":   lambda a, b: math.sin(b),
    # diagonal ripple (classic candle-wax look)
    "ripple":  lambda a, b: math.sin(a + b),
    # diamond lattice from two crossing triangle waves
    "diamond": lambda a, b: _tri(a + b) * _tri(a - b),
    # rounded outward bumps on a grid
    "bubbles": lambda a, b: max(0.0, math.sin(a) * math.sin(b)) ** 1.5,
    # sharp vertical pleats (triangle ridges)
    "pleats":  lambda a, b: _tri(a),
    # pseudo-random hammered-metal facets (inward dimples, deterministic)
    "hammered": lambda a, b: -abs(
        (math.sin(a) * math.sin(b)
         + math.sin(1.618 * a + 2.4) * math.sin(0.7 * b + 1.3)
         + math.sin(0.5347 * a + 4.0) * math.sin(1.93 * b + 2.2)) / 1.8),
}

R_PATTERN_NAMES = sorted(_R_PATTERNS.keys())


def _fade_envelope(t: float, fade_in: float, fade_out: float) -> float:
    """0..1 envelope ramping in over ``fade_in`` and out over ``fade_out``."""
    e = 1.0
    if fade_in > 0.0 and t < fade_in:
        e = min(e, t / fade_in)
    if fade_out > 0.0 and t > 1.0 - fade_out:
        e = min(e, (1.0 - t) / fade_out)
    return max(0.0, e)


def cage_scale(cage, theta, t):
    """Smooth (cosine-eased bilinear) sample of the cage grid.

    Periodic in theta; clamped in t. cage: N rows x M cols, row 0 = bottom.
    """
    n = len(cage)
    m = len(cage[0])
    ft = min(max(t, 0.0), 1.0) * (n - 1)
    i0 = min(int(ft), n - 2)
    u = ft - i0
    u = (1.0 - math.cos(math.pi * u)) / 2.0
    tau = (theta % (2.0 * math.pi)) / (2.0 * math.pi) * m
    j0 = int(tau) % m
    v = tau - int(tau)
    v = (1.0 - math.cos(math.pi * v)) / 2.0
    j1 = (j0 + 1) % m
    a = cage[i0][j0] * (1.0 - v) + cage[i0][j1] * v
    b = cage[i0 + 1][j0] * (1.0 - v) + cage[i0 + 1][j1] * v
    return a * (1.0 - u) + b * u


@dataclass
class PathPoint:
    x: float
    y: float
    z: float
    # True local layer height: the vertical gap to the point one turn below
    # (z(i) - z(i - points_per_turn)). None on the first turn, where the bead
    # sits on the bed and the nominal layer height applies. With z twist/waves
    # or during the amplitude ramp this varies around each turn, so gap-aware
    # extrusion needs it per point.
    gap: float | None = None
    # Wall tilt angle (radians). Positive = leaning outward (overhang).
    # Computed from the radial change vs one turn below. None on the first turn.
    tilt: float | None = None


def spiral_path(
    spec: SpiralSpec,
    shape: Callable[[float], float] | None = None,
) -> list[PathPoint]:
    """Generate a continuous list of points for the whole print.

    The result is one unbroken polyline from base to top — exactly what a
    non-planar continuous print needs (no per-layer seams).
    """
    shape = shape or circle(spec.base_radius)
    turns = spec.height / spec.layer_height
    total_steps = max(2, int(round(turns * spec.points_per_turn)))

    ov = max(-0.4, min(0.4, spec.ovality))   # clamp elliptical exaggeration

    pts: list[PathPoint] = []
    _amps: list[float] = []          # rate-limited wave amplitude per point
    _offs: list[tuple[float, float]] = []   # spine (dx, dy) per point, for tilt
    for i in range(total_steps + 1):
        s = i / total_steps              # 0..1 over the whole print
        t = s                            # height fraction
        phi = turns * 2.0 * math.pi * s  # total swept angle
        theta = phi % (2.0 * math.pi)    # angle around this loop

        # Rotate the cross-section outline with height for a twisted column.
        # The point still sits at angle ``theta`` (so each loop is complete),
        # but the lobe pattern is sampled at a height-rotated angle.
        shape_angle = theta - spec.xy_twist_turns * 2.0 * math.pi * t
        r = shape(shape_angle)
        if spec.radius_envelope is not None:
            r *= spec.radius_envelope(t)
        if spec.cage is not None:
            r *= cage_scale(spec.cage, theta, t)

        # Radial surface texture: probe-safe (never moves Z). ``a`` follows the
        # twisted angular coordinate, ``b`` climbs 2*pi per band over height.
        if spec.r_pattern:
            fn = _R_PATTERNS.get(spec.r_pattern)
            if fn is None:
                raise ValueError(
                    f"unknown r_pattern '{spec.r_pattern}', expected one of "
                    f"{R_PATTERN_NAMES}")
            a = (spec.r_waves * theta
                 + 2.0 * math.pi * (spec.r_twist_turns * t + spec.r_phase))
            if spec.r_alternate and int(phi / (2.0 * math.pi)) % 2 == 1:
                a += math.pi          # odd turns swing the opposite way
            b = 2.0 * math.pi * spec.r_bands * t
            env = (spec.r_envelope(t) if spec.r_envelope is not None
                   else _fade_envelope(t, spec.r_fade_in, spec.r_fade_out))
            r += spec.r_amp * env * fn(a, b)

        x = r * math.cos(theta)
        y = r * math.sin(theta)

        # Ovality: stretch/squash the final wall coordinates into an ellipse.
        # Applied after texture so ridges ride the elliptical wall, before the
        # spine offset so the offset stays a pure centre translation.
        if ov != 0.0:
            x *= (1.0 + ov)
            y *= (1.0 - ov)

        # Spine offset: translate the whole cross-section centre in XY.
        if spec.spine_offset is not None:
            dx, dy = spec.spine_offset(t)
            x += dx
            y += dy
            _offs.append((dx, dy))
        else:
            _offs.append((0.0, 0.0))

        z = t * spec.height
        if spec.z_amp != 0.0:
            phase = spec.z_twist_turns * 2.0 * math.pi * t
            if spec.z_amp_envelope is not None:
                # Caller-supplied envelope fully controls the amplitude scale.
                ramp = spec.z_amp_envelope(t)
            elif spec.z_amp_ramp > 0.0:
                # Default: ramp amplitude in from the base so the first layers
                # stay planar (nozzle won't dip below the bed).
                ramp = min(1.0, t / spec.z_amp_ramp)
            else:
                ramp = 1.0
            amp = ramp * spec.z_amp
            # RATE LIMITER: the amplitude may only change by a fraction of the
            # layer height per turn. An abrupt amplitude step (z-amp ladder
            # bands, steep user amp curves) would otherwise let a new turn's
            # trough dive BELOW the previous turn's surface - the nozzle plows
            # into printed plastic and squeezes filament out.
            if i >= spec.points_per_turn:
                prev_amp = _amps[i - spec.points_per_turn]
                d = _MAX_AMP_STEP * spec.layer_height
                if amp > prev_amp + d:
                    amp = prev_amp + d
                elif amp < prev_amp - d:
                    amp = prev_amp - d
            _amps.append(amp)
            z += amp * math.sin(spec.z_waves * theta + phase)
        else:
            _amps.append(0.0)

        pts.append(PathPoint(x, y, z))

    # Compute the true local layer height per point: the vertical gap to the
    # point exactly one turn below. Analytically exact for this spiral (the
    # index offset is constant), and correct even under z twist/waves/ramp.
    ppt = spec.points_per_turn
    for i, p in enumerate(pts):
        if i >= ppt:
            prev = pts[i - ppt]
            p.gap = p.z - prev.z
            # Radial change relative to the (possibly moving) cross-section
            # centre: with a spine offset the wall radius is measured from the
            # offset centre at each point's own t, not from the world origin.
            ox_i, oy_i = _offs[i]
            ox_p, oy_p = _offs[i - ppt]
            dr = (math.hypot(p.x - ox_i, p.y - oy_i)
                  - math.hypot(prev.x - ox_p, prev.y - oy_p))
            vgap = max(p.gap, 0.01)
            p.tilt = math.atan2(dr, vgap)
        else:
            p.gap = None
            p.tilt = None
    return pts


def bounding_radius(pts: list[PathPoint]) -> float:
    return max((p.x * p.x + p.y * p.y) ** 0.5 for p in pts)
