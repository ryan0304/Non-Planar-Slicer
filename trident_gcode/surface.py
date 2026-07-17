"""Height fields for non-planar surface-following.

A *surface* here is just a function ``f(x, y) -> z`` (mm), evaluated in part-local
coordinates centred on the origin. The surface-following generator paves it with a
continuous spiral whose Z rides the field, so the print conforms to the shape
instead of stepping flat — the essence of non-planar printing.

Two sources are provided:
- parametric fields (dome, ripple, saddle, waves) for clean demos, and
- :func:`surface_from_stl`, which samples the *top* surface of a mesh into a grid
  so you can drape a conformal shell over a real model.
"""
from __future__ import annotations

import math
from typing import Callable

from .mesh import Triangle, mesh_bounds

HeightField = Callable[[float, float], float]


# ----------------------------------------------------------- parametric fields
def dome(radius: float, height: float) -> HeightField:
    """Hemispherical-ish cap: peak ``height`` at centre, 0 at ``radius``."""
    def f(x: float, y: float) -> float:
        r = math.hypot(x, y) / radius
        if r >= 1.0:
            return 0.0
        return height * math.sqrt(1.0 - r * r)
    return f


def ripple(amp: float, wavelength: float) -> HeightField:
    """Concentric rings: smooth cosine waves radiating from the centre (0..amp)."""
    k = 2.0 * math.pi / wavelength
    return lambda x, y: amp * 0.5 * (1.0 + math.cos(k * math.hypot(x, y)))


def saddle(amp: float, scale: float) -> HeightField:
    """Pringle/saddle surface; ``amp`` is the corner height, ``scale`` the span."""
    s = scale * scale
    return lambda x, y: amp * (x * x - y * y) / s


def waves(amp: float, wavelength: float) -> HeightField:
    """Egg-crate: sinusoidal in both X and Y (0..amp)."""
    k = 2.0 * math.pi / wavelength
    return lambda x, y: amp * 0.25 * (2.0 + math.sin(k * x) + math.sin(k * y))


PARAMETRIC = {"dome", "ripple", "saddle", "waves"}


def make_parametric(name: str, amp: float, radius: float, wavelength: float) -> HeightField:
    if name == "dome":
        return dome(radius, amp)
    if name == "ripple":
        return ripple(amp, wavelength)
    if name == "saddle":
        return saddle(amp, radius)
    if name == "waves":
        return waves(amp, wavelength)
    raise ValueError(f"unknown surface '{name}', expected one of {sorted(PARAMETRIC)}")


# ----------------------------------------------------------- STL top sampling
def surface_from_stl(tris: list[Triangle], res: int = 200, scale: float = 1.0):
    """Sample a mesh's upper surface into a grid and return ``(field, radius)``.

    ``field(x, y)`` gives the top Z at a part-local XY (centred on the mesh's XY
    bbox). ``radius`` is the half-diagonal of the footprint, for the fit check.
    """
    if scale != 1.0:
        tris = [tuple((vx * scale, vy * scale, vz * scale) for (vx, vy, vz) in t)
                for t in tris]
    (minx, miny, minz), (maxx, maxy, maxz) = mesh_bounds(tris)
    w = maxx - minx
    h = maxy - miny
    if w <= 0 or h <= 0:
        raise ValueError("mesh has no XY extent")

    neg = -1e30
    grid = [[neg] * res for _ in range(res)]

    def gx(x):  # world X -> grid col (float)
        return (x - minx) / w * (res - 1)

    def gy(y):
        return (y - miny) / h * (res - 1)

    # Rasterise each triangle's top: for cells under its bbox, keep the max Z.
    for a, b, c in tris:
        c0 = max(0, int(math.floor(min(gx(a[0]), gx(b[0]), gx(c[0])))))
        c1 = min(res - 1, int(math.ceil(max(gx(a[0]), gx(b[0]), gx(c[0])))))
        r0 = max(0, int(math.floor(min(gy(a[1]), gy(b[1]), gy(c[1])))))
        r1 = min(res - 1, int(math.ceil(max(gy(a[1]), gy(b[1]), gy(c[1])))))
        if c1 < c0 or r1 < r0:
            continue
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = a, b, c
        denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denom) < 1e-12:
            continue
        for gr in range(r0, r1 + 1):
            wy = miny + gr / (res - 1) * h
            for gc in range(c0, c1 + 1):
                wx = minx + gc / (res - 1) * w
                l1 = ((by - cy) * (wx - cx) + (cx - bx) * (wy - cy)) / denom
                l2 = ((cy - ay) * (wx - cx) + (ax - cx) * (wy - cy)) / denom
                l3 = 1.0 - l1 - l2
                if l1 < -1e-6 or l2 < -1e-6 or l3 < -1e-6:
                    continue
                z = l1 * az + l2 * bz + l3 * cz
                if z > grid[gr][gc]:
                    grid[gr][gc] = z

    # Fill any unsampled cells with the floor so queries never return -inf.
    for gr in range(res):
        for gc in range(res):
            if grid[gr][gc] == neg:
                grid[gr][gc] = minz

    mx = (minx + maxx) / 2.0
    my = (miny + maxy) / 2.0

    def field(x: float, y: float) -> float:
        # x,y are part-local (centred). Bilinear sample, clamped to the grid.
        fx = min(max(gx(x + mx), 0.0), res - 1.0)
        fy = min(max(gy(y + my), 0.0), res - 1.0)
        x0, y0 = int(fx), int(fy)
        x1, y1 = min(x0 + 1, res - 1), min(y0 + 1, res - 1)
        tx, ty = fx - x0, fy - y0
        top = grid[y0][x0] * (1 - tx) + grid[y0][x1] * tx
        bot = grid[y1][x0] * (1 - tx) + grid[y1][x1] * tx
        return top * (1 - ty) + bot * ty

    radius = 0.5 * math.hypot(w, h)
    return field, radius
