"""Point Edit Modifiers -- post-slice deformation of the wall polyline.

Where every other knob in this codebase (radius_envelope, cage, r_pattern...)
shapes the wall WHILE the spiral is being generated, these operate on the
already-sliced path: ``spiral_path()`` runs first, then this module edits the
resulting list[PathPoint] in place, then gap/tilt are recomputed so the writer
still gets accurate per-point layer heights. This mirrors Clay Studio Pro's
"purple modifiers" (they work on sliced paths, not the source geometry).

Five modifiers, two of which are gates and three of which move geometry:

  Point Mask        -- image or procedural channel -> per-point exposure [0,1]
  Point Protection   -- protect top/bottom zones (with falloff) -> [0,1]
  Point FFD          -- point-level cage of (dx,dy,dz) mm displacements
  Point Smooth       -- neighbor-averaging along and across turns
  Point Radial Push  -- push/pull along the point's own radial direction

Mask and Protection multiply together into a single ``gate(theta, t)`` in
[0,1] that scales every deforming modifier's effect -- "expose and protect
print points before applying point edits", per the brief. All three deforming
modifiers are independently optional; enabling none of them (and passing no
mask/protection) leaves the input list untouched (same object, in fact),
so default output stays byte-identical.

Safety: this module intentionally does NOT re-implement the Z rate limiter
that spiral_path() applies to z_amp -- the real safety net is downstream and
already governs whatever this module produces: GcodeWriter.extrude_to() clamps
every layer_height_override to [0.25, 1.5]x nominal, and serve.py independently
re-verifies the emitted G-code with analyze_gcode() (probe hits, max Z rate,
footprint) before it is ever returned. FFD/Radial-Push displacement magnitude
is clamped at the spec-parsing boundary (see serve.py's _parse_point_*
helpers) purely to keep a malicious/buggy client far from that real ceiling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .paths import PathPoint, _tri

TWO_PI = 2.0 * math.pi


# --------------------------------------------------------------------------
# Point Protection Field -- protect top/bottom/falloff zones.
# --------------------------------------------------------------------------
@dataclass
class ProtectionSpec:
    protect_bottom: float = 0.0   # fraction of height [0,1], fully protected from the base
    protect_top: float = 0.0      # fraction of height [0,1], fully protected from the top
    falloff: float = 0.08         # fraction of height the effect ramps over


def protection_weight(spec: ProtectionSpec | None, t: float) -> float:
    """1.0 = fully editable, 0.0 = fully protected."""
    if spec is None:
        return 1.0
    w = 1.0
    fo = max(spec.falloff, 1e-6)
    if spec.protect_bottom > 0.0:
        edge = spec.protect_bottom
        if t < edge:
            w = min(w, 0.0)
        elif t < edge + fo:
            w = min(w, (t - edge) / fo)
    if spec.protect_top > 0.0:
        edge = 1.0 - spec.protect_top
        if t > edge:
            w = min(w, 0.0)
        elif t > edge - fo:
            w = min(w, (edge - t) / fo)
    return max(0.0, min(1.0, w))


# --------------------------------------------------------------------------
# Point Mask -- procedural channel or uploaded image -> per-point exposure.
# --------------------------------------------------------------------------
_CHANNELS: dict = {
    "checker":    lambda a, b: 1.0 if (math.floor(a / math.pi) + math.floor(b / math.pi)) % 2 == 0 else 0.0,
    "stripes_v":  lambda a, b: 0.5 + 0.5 * math.sin(a),
    "stripes_h":  lambda a, b: 0.5 + 0.5 * math.sin(b),
    "rings":      lambda a, b: 0.5 + 0.5 * math.sin(b),
    "diamond":    lambda a, b: 0.5 + 0.5 * (_tri(a + b) * _tri(a - b)),
    "gradient_v": lambda a, b: (b % TWO_PI) / TWO_PI,
    "gradient_h": lambda a, b: (a % TWO_PI) / TWO_PI,
    "radial":     lambda a, b: 0.5 + 0.5 * math.sin(a),
}
MASK_CHANNEL_NAMES = sorted(_CHANNELS.keys())


@dataclass
class MaskSpec:
    channel: str = "none"       # none | one of MASK_CHANNEL_NAMES
    scale_u: float = 8.0        # repeats around the perimeter (theta)
    scale_v: float = 6.0        # repeats over height (t)
    invert: bool = False
    # Uploaded grayscale grid: N rows (bottom->top) x M cols (azimuth), 0..1.
    # Takes precedence over ``channel`` when present.
    image: list | None = None


def _sample_image(image: list, theta: float, t: float) -> float:
    n = len(image)
    m = len(image[0])
    row = min(int(t * (n - 1) + 0.5), n - 1) if n > 1 else 0
    tau = (theta % TWO_PI) / TWO_PI * m
    col = int(tau) % m
    return image[row][col]


def mask_weight(spec: MaskSpec | None, theta: float, t: float) -> float:
    """1.0 = fully exposed to edits, 0.0 = fully protected."""
    if spec is None or (spec.channel == "none" and not spec.image):
        return 1.0
    if spec.image:
        v = _sample_image(spec.image, theta, t)
    else:
        fn = _CHANNELS.get(spec.channel)
        if fn is None:
            return 1.0
        a = theta * max(spec.scale_u, 0.001)
        b = TWO_PI * t * max(spec.scale_v, 0.001)
        v = fn(a, b)
    v = max(0.0, min(1.0, v))
    return (1.0 - v) if spec.invert else v


def gate_weight(mask: MaskSpec | None, protection: ProtectionSpec | None,
                theta: float, t: float) -> float:
    return mask_weight(mask, theta, t) * protection_weight(protection, t)


# --------------------------------------------------------------------------
# Point FFD -- point-level cage of (dx,dy,dz) mm displacements.
# --------------------------------------------------------------------------
FFD_MAX_MM = 4.0

# Point Smooth: max combined (theta + t) blend weight per point per
# iteration. Keeps the Jacobi-style neighbor average a convex combination
# (see apply_point_edits' Smooth block for why this is required for
# stability, not just quality).
_SMOOTH_STABILITY_CAP = 1.0


@dataclass
class FFDSpec:
    cage: list                  # N rows x M cols x [dx,dy,dz] mm, each clamped to +-FFD_MAX_MM
    strength: float = 1.0        # extra multiplier, clamped to [0,2] at parse time


def _ffd_sample(cage: list, theta: float, t: float) -> tuple[float, float, float]:
    n = len(cage)
    m = len(cage[0])
    ft = min(max(t, 0.0), 1.0) * (n - 1)
    i0 = min(int(ft), n - 2) if n > 1 else 0
    i1 = min(i0 + 1, n - 1)
    u = ft - i0
    u = (1.0 - math.cos(math.pi * u)) / 2.0
    tau = (theta % TWO_PI) / TWO_PI * m
    j0 = int(tau) % m
    j1 = (j0 + 1) % m
    v = tau - int(tau)
    v = (1.0 - math.cos(math.pi * v)) / 2.0

    def lerp3(p, q, f):
        return (p[0] + (q[0] - p[0]) * f,
                p[1] + (q[1] - p[1]) * f,
                p[2] + (q[2] - p[2]) * f)

    a = lerp3(cage[i0][j0], cage[i0][j1], v)
    b = lerp3(cage[i1][j0], cage[i1][j1], v)
    return lerp3(a, b, u)


# --------------------------------------------------------------------------
# Point Radial Push -- push/pull along the point's own radial direction.
# --------------------------------------------------------------------------
@dataclass
class RadialPushSpec:
    amp_mm: float = 1.0          # +push out / -pull in, clamped to +-5mm at parse time
    strength: float = 1.0        # extra multiplier, clamped to [0,2] at parse time


# --------------------------------------------------------------------------
# Point Smooth -- neighbor-averaging along (theta) and across (t) turns.
# --------------------------------------------------------------------------
@dataclass
class SmoothSpec:
    iterations: int = 2          # clamped to [1,12] at parse time
    theta_amount: float = 0.5    # blend weight toward same-turn neighbors, [0,1]
    t_amount: float = 0.5        # blend weight toward the turn above/below, [0,1]
    strength: float = 1.0        # extra multiplier, clamped to [0,2] at parse time


def _theta_t_at(i: int, total: int, ppt: int) -> tuple[float, float]:
    """Recover (theta, t) for point index i the same way spiral_path() derived
    them -- bed-anchored, independent of xy_twist/spine/ovality, matching the
    convention the existing cage already uses (`anchored to the BED, not the
    twisted shape`)."""
    t = i / max(total - 1, 1)
    theta = TWO_PI * ((i % ppt) / ppt)
    return theta, t


def apply_point_edits(
    pts: list[PathPoint],
    points_per_turn: int,
    *,
    mask: MaskSpec | None = None,
    protection: ProtectionSpec | None = None,
    ffd: FFDSpec | None = None,
    smooth: SmoothSpec | None = None,
    radial_push: RadialPushSpec | None = None,
) -> list[PathPoint]:
    """Post-slice point-edit stack. Returns ``pts`` unchanged (same object)
    when no deforming modifier is active, so default output stays
    byte-identical. Otherwise returns a NEW list of NEW PathPoints (the input
    is never mutated) with gap/tilt recomputed from the edited geometry.
    """
    if ffd is None and smooth is None and radial_push is None:
        return pts

    n = len(pts)
    ppt = max(1, points_per_turn)
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]

    # ---- gate: mask * protection, precomputed once per point --------------
    gates = [gate_weight(mask, protection, *_theta_t_at(i, n, ppt)) for i in range(n)]

    # ---- Point FFD: arbitrary (dx,dy,dz) displacement ----------------------
    if ffd is not None and ffd.cage:
        strength = ffd.strength
        for i in range(n):
            g = gates[i] * strength
            if g == 0.0:
                continue
            theta, t = _theta_t_at(i, n, ppt)
            dx, dy, dz = _ffd_sample(ffd.cage, theta, t)
            xs[i] += dx * g
            ys[i] += dy * g
            zs[i] += dz * g

    # ---- Point Radial Push: displace along the point's OWN radial dir -----
    if radial_push is not None and radial_push.amp_mm != 0.0:
        amt = radial_push.amp_mm * radial_push.strength
        for i in range(n):
            g = gates[i]
            if g == 0.0:
                continue
            r = math.hypot(xs[i], ys[i])
            if r < 1e-9:
                continue
            nx, ny = xs[i] / r, ys[i] / r
            push = amt * g
            xs[i] += nx * push
            ys[i] += ny * push

    # ---- Point Smooth: iterative neighbor-averaging ------------------------
    if smooth is not None and smooth.iterations > 0 and smooth.strength > 0.0:
        theta_amt = max(0.0, min(1.0, smooth.theta_amount)) * smooth.strength
        t_amt = max(0.0, min(1.0, smooth.t_amount)) * smooth.strength
        for _ in range(smooth.iterations):
            nxs, nys, nzs = xs[:], ys[:], zs[:]
            for i in range(n):
                g = gates[i]
                if g == 0.0:
                    continue
                turn0 = (i // ppt) * ppt
                turn_len = min(ppt, n - turn0)
                lo = i - 1 if i - 1 >= turn0 else turn0 + turn_len - 1
                hi = i + 1 if i + 1 < turn0 + turn_len else turn0
                bx = (xs[lo] + xs[hi]) / 2.0
                by = (ys[lo] + ys[hi]) / 2.0
                bz = (zs[lo] + zs[hi]) / 2.0
                has_t = i - ppt >= 0 and i + ppt < n
                if has_t:
                    bx2 = (xs[i - ppt] + xs[i + ppt]) / 2.0
                    by2 = (ys[i - ppt] + ys[i + ppt]) / 2.0
                    bz2 = (zs[i - ppt] + zs[i + ppt]) / 2.0
                f = theta_amt * g
                f2 = (t_amt * g) if has_t else 0.0
                # Stability: the update must stay a convex combination of
                # xs[i] and its neighbor blends (weights sum <= 1), or each
                # iteration extrapolates PAST the neighbor average and the
                # whole polyline blows up geometrically over successive
                # iterations. theta_amt/t_amt are each already clamped to
                # [0,1] but strength (up to 2x) multiplies both, so their sum
                # can reach 4 -- well past the point this stops being
                # "smoothing" and starts being divergent extrapolation.
                w = f + f2
                if w > _SMOOTH_STABILITY_CAP:
                    scale = _SMOOTH_STABILITY_CAP / w
                    f *= scale
                    f2 *= scale
                nxs[i] = xs[i] + (bx - xs[i]) * f
                nys[i] = ys[i] + (by - ys[i]) * f
                nzs[i] = zs[i] + (bz - zs[i]) * f
                if has_t:
                    nxs[i] += (bx2 - xs[i]) * f2
                    nys[i] += (by2 - ys[i]) * f2
                    nzs[i] += (bz2 - zs[i]) * f2
            xs, ys, zs = nxs, nys, nzs

    out = [PathPoint(xs[i], ys[i], zs[i]) for i in range(n)]
    for i, p in enumerate(out):
        if i >= ppt:
            prev = out[i - ppt]
            p.gap = p.z - prev.z
            dr = math.hypot(p.x, p.y) - math.hypot(prev.x, prev.y)
            vgap = max(p.gap, 0.01)
            p.tilt = math.atan2(dr, vgap)
        else:
            p.gap = None
            p.tilt = None
    return out
