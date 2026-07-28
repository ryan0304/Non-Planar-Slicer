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


# ----------------------------------------------------- compatibility check ---
def _point_in_polygon(pt: Vec2, poly: list[Vec2]) -> bool:
    """Ray-casting containment test. Points exactly on an edge are undefined."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xint = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < xint:
                inside = not inside
    return inside


def _is_star_convex(loop: list[Vec2]) -> bool:
    """True if ``loop`` is star-convex about its own centroid.

    A star-convex outline traversed in order sweeps its angle about the centre
    monotonically. Any backtrack means some ray from the centre crosses the
    outline more than once, which is exactly what breaks the ``radius(theta)``
    sampling the base/brim fill relies on.

    Winding-agnostic: raw stitched contours come out in either direction, so we
    take the sweep direction from the total and require every step to agree with
    it rather than assuming counter-clockwise.
    """
    n = len(loop)
    if n < 3:
        return False
    cx = sum(p[0] for p in loop) / n
    cy = sum(p[1] for p in loop) / n
    deltas = []
    for i in range(n):
        x0, y0 = loop[i][0] - cx, loop[i][1] - cy
        x1, y1 = loop[(i + 1) % n][0] - cx, loop[(i + 1) % n][1] - cy
        if (x0 == 0.0 and y0 == 0.0) or (x1 == 0.0 and y1 == 0.0):
            return False                           # centroid sits on the outline
        d = math.atan2(y1, x1) - math.atan2(y0, x0)
        deltas.append(math.atan2(math.sin(d), math.cos(d)))   # wrap to [-pi, pi]
    total = sum(deltas)
    if abs(abs(total) - 2.0 * math.pi) > 1e-3:
        return False              # centroid is outside, or the loop self-wraps
    sign = 1.0 if total >= 0.0 else -1.0
    return all(sign * d >= -1e-6 for d in deltas)  # tolerance beats float noise


def _slice_many(tris: list[Triangle], heights: list[float]) -> list[list[tuple[Vec2, Vec2]]]:
    """Slice at every height in ``heights`` in a single pass over the mesh.

    Calling :func:`slice_segments` once per height is O(triangles x heights),
    which is far too slow at our 500k-triangle ceiling. Each triangle only spans
    a contiguous run of sample planes, so we walk the mesh once and push each
    triangle's segment straight into the buckets it actually crosses. Assumes
    ``heights`` is sorted and evenly spaced.
    """
    k = len(heights)
    out: list[list[tuple[Vec2, Vec2]]] = [[] for _ in range(k)]
    if k == 0:
        return out
    h0 = heights[0]
    step = (heights[-1] - h0) / (k - 1) if k > 1 else 0.0

    for a, b, c in tris:
        zmin = a[2] if a[2] < b[2] else b[2]
        if c[2] < zmin:
            zmin = c[2]
        zmax = a[2] if a[2] > b[2] else b[2]
        if c[2] > zmax:
            zmax = c[2]
        if step > 0.0:
            i0 = int(math.ceil((zmin - h0) / step))
            i1 = int(math.floor((zmax - h0) / step))
            if i0 < 0:
                i0 = 0
            if i1 > k - 1:
                i1 = k - 1
        else:
            i0, i1 = 0, k - 1
        for i in range(i0, i1 + 1):
            h = heights[i]
            hits: list[Vec2] = []
            for p0, p1 in ((a, b), (b, c), (c, a)):
                if (p0[2] < h) != (p1[2] < h):
                    hits.append(_edge_cross(p0, p1, h))
            if len(hits) == 2:
                out[i].append((hits[0], hits[1]))
    return out


def analyze_vase_compatibility(
    tris: list[Triangle],
    *,
    samples: int = 24,
    area_eps: float = 0.01,
) -> dict:
    """Report what single-outer-contour slicing would silently discard.

    :func:`slice_outer_loop` keeps only the largest closed loop per layer, so
    interior holes and separate islands vanish without any warning and the print
    quietly becomes a different shape than the uploaded model. This samples the
    mesh at evenly spaced heights and classifies every extra contour it finds, so
    callers can say so up front instead of after a failed print.

    Holes and islands are reported separately because they fail differently:
    a hole gets paved over (the wall simply wraps the outside), whereas an island
    disappears from the print entirely.
    """
    (_minx, _miny, minz), (_maxx, _maxy, maxz) = mesh_bounds(tris)
    height = maxz - minz
    if height <= 0:
        raise ValueError("mesh has no height to slice")

    n = max(2, int(samples))
    # Inset the first/last samples so we never land exactly on the flat top or
    # bottom face, where the slice plane is coincident with the geometry.
    lo = minz + height * 0.02
    hi = maxz - height * 0.02
    heights = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    buckets = _slice_many(tris, heights)

    empty = 0
    hole_layers = 0
    island_layers = 0
    max_contours = 1
    worst_h = heights[0]
    worst_discard = 0.0
    base_loop: list[Vec2] | None = None

    for h, segs in zip(heights, buckets):
        if not segs:
            empty += 1
            continue
        loops = stitch_loops(segs)
        if not loops:
            empty += 1
            continue
        areas = [abs(_polygon_area(lp)) for lp in loops]
        big = max(areas)
        if big <= 0.0:
            empty += 1
            continue
        # Drop numerical slivers so tessellation noise isn't reported as a hole.
        keep = [(lp, ar) for lp, ar in zip(loops, areas) if ar >= area_eps * big]
        outer = max(keep, key=lambda pair: pair[1])[0]
        if base_loop is None:
            base_loop = outer

        if len(keep) > max_contours:
            max_contours = len(keep)
            worst_h = h

        has_hole = False
        has_island = False
        island_area = 0.0
        for lp, ar in keep:
            if lp is outer:
                continue
            if _point_in_polygon(lp[0], outer):
                has_hole = True
            else:
                has_island = True
                island_area += ar
        if has_hole:
            hole_layers += 1
        if has_island:
            island_layers += 1
            frac = island_area / (island_area + big)
            if frac > worst_discard:
                worst_discard = frac

    star_convex = _is_star_convex(base_loop) if base_loop else False
    compatible = (hole_layers == 0 and island_layers == 0)

    notes: list[str] = []
    if island_layers:
        notes.append(
            "%d of %d sampled layers contain separate islands - only the largest "
            "contour is printed, so those parts will be missing entirely."
            % (island_layers, n))
    if hole_layers:
        notes.append(
            "%d of %d sampled layers contain interior holes - the wall wraps the "
            "outer silhouette only, so holes will be paved over."
            % (hole_layers, n))
    if not star_convex:
        notes.append(
            "The bottom contour is not star-convex (some rays from its centre "
            "cross the outline twice). The wall follows the true outline, but the "
            "solid base and brim fill may be distorted.")
    if empty:
        notes.append(
            "%d of %d sampled heights produced no cross-section - the mesh may not "
            "be watertight or Z-up." % (empty, n))

    # A hollow/shelled model shows an inner+outer contour at every sampled
    # layer (hole_layers > 0), so max_contours == 1 with zero holes means the
    # upload is a solid, filled volume - our single bead can only trace its
    # outline, so the print comes out hollow no matter what the source was.
    solid_cross_section = (hole_layers == 0 and max_contours == 1)

    return {
        "compatible": compatible,
        "samples": n,
        "max_contours": max_contours,
        "hole_layers": hole_layers,
        "island_layers": island_layers,
        "empty_layers": empty,
        "worst_height_mm": round(worst_h, 2),
        "discarded_area_pct": round(worst_discard * 100.0, 1),
        "star_convex_base": star_convex,
        "solid_cross_section": solid_cross_section,
        "notes": notes,
    }
