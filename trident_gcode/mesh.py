"""Minimal, dependency-free STL loading and horizontal slicing.

Just enough geometry to drive continuous "vase-style" prints from a mesh: load
an STL (binary or ASCII), then at any height extract the **outer contour** of the
cross-section as a closed polygon. No numpy / trimesh / shapely required — handy
on fresh Python installs where those wheels may not exist yet.

Limitations (matching the continuous single-wall use case):
- We keep only the largest closed loop per slice (the outer silhouette), so
  interior holes/islands are ignored — exactly what vase-style wrapping wants.
- The mesh should be reasonably watertight; small gaps are tolerated by the
  endpoint-stitching epsilon.
"""
from __future__ import annotations

import math
import re
import struct
from collections import defaultdict

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]
Triangle = tuple[Vec3, Vec3, Vec3]


# ------------------------------------------------------------------ loading --
def load_stl(path: str) -> list[Triangle]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 84:
        raise ValueError("file too small to be an STL")

    # Trust the binary layout if the size matches exactly: 80B header + uint32
    # count + 50B per triangle. This is the only reliable binary/ASCII test.
    count = struct.unpack_from("<I", data, 80)[0]
    if len(data) == 84 + count * 50:
        return _load_binary(data, count)

    # Otherwise treat as ASCII.
    tris = _load_ascii(data.decode("utf-8", "replace"))
    if tris:
        return tris
    # Last resort: attempt binary anyway.
    return _load_binary(data, count)


def _load_binary(data: bytes, count: int) -> list[Triangle]:
    tris: list[Triangle] = []
    off = 84
    for _ in range(count):
        # 12 little-endian floats: normal(3) + v0(3) + v1(3) + v2(3), then 2B attr
        vals = struct.unpack_from("<12f", data, off)
        v0 = (vals[3], vals[4], vals[5])
        v1 = (vals[6], vals[7], vals[8])
        v2 = (vals[9], vals[10], vals[11])
        tris.append((v0, v1, v2))
        off += 50
    return tris


_VERTEX_RE = re.compile(
    r"vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)"
)


def _load_ascii(text: str) -> list[Triangle]:
    verts = [(float(a), float(b), float(c)) for a, b, c in _VERTEX_RE.findall(text)]
    tris: list[Triangle] = []
    for i in range(0, len(verts) - 2, 3):
        tris.append((verts[i], verts[i + 1], verts[i + 2]))
    return tris


# ------------------------------------------------------------------ bounds ---
def mesh_bounds(tris: list[Triangle]) -> tuple[Vec3, Vec3]:
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    for tri in tris:
        for v in tri:
            for k in range(3):
                lo[k] = min(lo[k], v[k])
                hi[k] = max(hi[k], v[k])
    return (tuple(lo), tuple(hi))  # type: ignore[return-value]


# ------------------------------------------------------------------ slicing --
def _edge_cross(p0: Vec3, p1: Vec3, h: float) -> Vec2:
    t = (h - p0[2]) / (p1[2] - p0[2])
    return (p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1]))


def slice_segments(tris: list[Triangle], h: float) -> list[tuple[Vec2, Vec2]]:
    """All intersection segments of the mesh with the plane z=h."""
    segs: list[tuple[Vec2, Vec2]] = []
    for a, b, c in tris:
        hits: list[Vec2] = []
        for p0, p1 in ((a, b), (b, c), (c, a)):
            below0 = p0[2] < h
            below1 = p1[2] < h
            if below0 != below1:                 # edge straddles the plane
                hits.append(_edge_cross(p0, p1, h))
        if len(hits) == 2:
            segs.append((hits[0], hits[1]))
    return segs


def _polygon_area(loop: list[Vec2]) -> float:
    a = 0.0
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def stitch_loops(segs: list[tuple[Vec2, Vec2]], eps: float = 1e-4) -> list[list[Vec2]]:
    """Connect intersection segments end-to-end into closed loops."""
    def key(p: Vec2) -> tuple[int, int]:
        return (round(p[0] / eps), round(p[1] / eps))

    endmap: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (p, q) in enumerate(segs):
        endmap[key(p)].append(i)
        endmap[key(q)].append(i)

    used = [False] * len(segs)
    loops: list[list[Vec2]] = []
    for start in range(len(segs)):
        if used[start]:
            continue
        used[start] = True
        p, q = segs[start]
        loop = [p, q]
        cur = q
        start_key = key(p)
        while True:
            k = key(cur)
            nxt = None
            for j in endmap[k]:
                if not used[j]:
                    nxt = j
                    break
            if nxt is None:
                break
            used[nxt] = True
            a, b = segs[nxt]
            cur = b if key(a) == k else a
            loop.append(cur)
            if key(cur) == start_key:
                break
        if len(loop) >= 4:
            loops.append(loop)
    return loops


def slice_outer_loop(tris: list[Triangle], h: float) -> list[Vec2] | None:
    """Return the largest closed contour at height ``h``, wound counter-clockwise."""
    segs = slice_segments(tris, h)
    if not segs:
        return None
    loops = stitch_loops(segs)
    if not loops:
        return None
    outer = max(loops, key=lambda lp: abs(_polygon_area(lp)))
    if _polygon_area(outer) < 0:        # enforce CCW winding
        outer = outer[::-1]
    return outer


# --------------------------------------------------------------- resampling --
def resample_closed(loop: list[Vec2], n: int) -> list[Vec2]:
    """Resample a closed polygon to exactly ``n`` points, equally spaced by arc length."""
    # Cumulative perimeter length.
    pts = loop
    m = len(pts)
    seg_len = []
    total = 0.0
    for i in range(m):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % m]
        d = math.hypot(x1 - x0, y1 - y0)
        seg_len.append(d)
        total += d
    if total == 0.0:
        return [pts[0]] * n

    out: list[Vec2] = []
    step = total / n
    target = 0.0
    i = 0
    acc = 0.0
    for _ in range(n):
        while acc + seg_len[i] < target and i < m - 1:
            acc += seg_len[i]
            i += 1
        # interpolate within segment i
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % m]
        local = (target - acc) / seg_len[i] if seg_len[i] > 0 else 0.0
        out.append((x0 + local * (x1 - x0), y0 + local * (y1 - y0)))
        target += step
    return out


def align_start(loop: list[Vec2], ref: Vec2) -> list[Vec2]:
    """Rotate the point list so it begins at the point nearest ``ref``.

    Keeps the spiral seam from wandering between successive layers.
    """
    best = 0
    best_d = math.inf
    for i, (x, y) in enumerate(loop):
        d = (x - ref[0]) ** 2 + (y - ref[1]) ** 2
        if d < best_d:
            best_d = d
            best = i
    return loop[best:] + loop[:best]
