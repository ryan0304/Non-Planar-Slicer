"""Watertight STL export: contour stack -> solid triangle mesh -> STL bytes.

This app only ever prints single-wall vase mode: one continuous bead tracing
a 2D outline swept upward in Z. There is no toolpath representation of a
solid part. To let a user sculpt a shape here and then print it SOLID (walls
+ infill) in Orca, we need to hand Orca an actual mesh -- so this module
turns the same contour stack the wall generators already walk (see
:mod:`profile_stack`) into a closed, watertight solid: side walls between
consecutive contours, plus a centroid triangle-fan cap top and bottom.

Winding is the whole ballgame here: Orca (like any slicer) determines
inside-vs-outside by ray-casting against facet normals, so every triangle
must wind consistently CCW as seen from outside or the slicer's mesh repair
will guess wrong on some faces and leave holes or flipped shells. Contours
arriving here are assumed CCW-wound (the convention already enforced by
:func:`trident_gcode.mesh.slice_outer_loop` and by every parametric shape
function in :mod:`trident_gcode.paths`) and heights strictly increasing;
under those two assumptions the winding below is provably a closed 2-manifold
(every undirected edge shared by exactly 2 triangles, one in each direction).

Pure stdlib (struct only) -- no numpy/trimesh, matching the rest of this
codebase's fresh-Python-install constraint.
"""
from __future__ import annotations

import struct

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Triangle = tuple[Vec3, Vec3, Vec3]


def _centroid(contour) -> Vec2:
    n = len(contour)
    cx = sum(p[0] for p in contour) / n
    cy = sum(p[1] for p in contour) / n
    return (cx, cy)


def _cap_fan(ring, reverse: bool) -> list[Triangle]:
    """Triangle-fan cap from the centroid of an already-3D ring.

    ``reverse`` picks which winding gives an outward normal: a CCW contour's
    plain fan (centroid, p_j, p_j+1) faces +Z (right for a TOP cap, where the
    solid sits below); a BOTTOM cap needs the solid above it, so its outward
    normal must face -Z, which means reversing the last two vertices.

    The centroid takes its Z from the ring's own vertices rather than a single
    layer height, so a cap still closes cleanly when a point-edit modifier has
    pushed the rim's vertices to differing heights.
    """
    n = len(ring)
    cx = sum(p[0] for p in ring) / n
    cy = sum(p[1] for p in ring) / n
    cz = sum(p[2] for p in ring) / n
    centroid = (cx, cy, cz)
    tris: list[Triangle] = []
    for j in range(n):
        jn = (j + 1) % n
        a, b = ring[j], ring[jn]
        if reverse:
            tris.append((centroid, b, a))
        else:
            tris.append((centroid, a, b))
    return tris


def contours_to_mesh(contours, heights, *, cap_bottom=True, cap_top=True) -> list[tuple]:
    """Build a closed triangle mesh from a stack of closed 2D contours.

    ``contours``: list of closed outlines, each a list of (x, y) -- ALL the
    same length N, CCW-wound, centred near the origin (matches every contour
    stack this app already produces -- see :mod:`profile_stack`).
    ``heights``: Z for each contour, same length as ``contours``, strictly
    increasing (the normal direction derivation below assumes it).

    A contour's points may instead be (x, y, z) triples, in which case that per
    vertex Z wins over ``heights[i]``. Point-edit modifiers (FFD in particular)
    displace individual points in Z, so a single height per ring cannot describe
    the edited surface; ``heights`` is still required and still sets the layer
    ordering, it just stops being the last word on any given vertex.

    Side walls: between contour i and i+1, each of the N quads (one per
    vertex index) is split into two triangles sharing the diagonal from the
    lower-ring vertex to the next upper-ring vertex. Combined with
    :func:`_cap_fan` at the bottom (cap_bottom) and top (cap_top), every
    undirected edge in the result is shared by exactly two triangles -- a
    watertight 2-manifold -- as long as both caps are enabled; a stack with a
    cap disabled is intentionally left open at that end (e.g. to butt-join
    against another mesh) and will not pass a manifold check on its own.
    """
    if len(contours) != len(heights):
        raise ValueError(
            f"contours ({len(contours)}) and heights ({len(heights)}) "
            f"length mismatch"
        )
    if len(contours) < 2:
        raise ValueError("need at least two contours to build side walls")
    n = len(contours[0])
    if n < 3:
        raise ValueError("each contour needs at least 3 points")
    for c in contours:
        if len(c) != n:
            raise ValueError("all contours must have the same point count")

    # Normalise to one 3D vertex grid up front so the wall and cap code below
    # never has to care whether Z came from the ring or from ``heights``.
    verts = [
        [(p[0], p[1], p[2] if len(p) > 2 else heights[i]) for p in c]
        for i, c in enumerate(contours)
    ]

    tris: list[Triangle] = []

    for i in range(len(verts) - 1):
        r0, r1 = verts[i], verts[i + 1]
        for j in range(n):
            jn = (j + 1) % n
            # Two triangles per quad, sharing the p00-p11 diagonal. With a
            # CCW contour and z1 > z0 this winds both triangles with the
            # outward (radially-away-from-axis) normal -- see module
            # docstring for the cross-product derivation.
            tris.append((r0[j], r0[jn], r1[jn]))
            tris.append((r0[j], r1[jn], r1[j]))

    if cap_bottom:
        tris.extend(_cap_fan(verts[0], reverse=True))
    if cap_top:
        tris.extend(_cap_fan(verts[-1], reverse=False))

    return tris


def _facet_normal(v0: Vec3, v1: Vec3, v2: Vec3) -> Vec3:
    ux, uy, uz = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    vx, vy, vz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length <= 1e-15:
        return (0.0, 0.0, 0.0)          # degenerate facet: no defined normal
    return (nx / length, ny / length, nz / length)


def write_binary_stl(tris, name: bytes = b"") -> bytes:
    """Serialise a triangle list to the binary STL byte format.

    Layout: 80-byte header, uint32 triangle count, then per facet 3x float32
    normal + 3x3 float32 vertices + uint16 attribute (always 0). The normal is
    a real unit vector computed from the vertex winding (not left zeroed --
    some slicers/viewers trust the stored normal over recomputing it), falling
    back to (0,0,0) only for degenerate (zero-area) facets.
    """
    header = bytearray(80)
    header[: min(len(name), 80)] = name[:80]

    out = bytearray()
    out += bytes(header)
    out += struct.pack("<I", len(tris))
    for (v0, v1, v2) in tris:
        nx, ny, nz = _facet_normal(v0, v1, v2)
        out += struct.pack("<3f", nx, ny, nz)
        out += struct.pack("<3f", v0[0], v0[1], v0[2])
        out += struct.pack("<3f", v1[0], v1[1], v1[2])
        out += struct.pack("<3f", v2[0], v2[1], v2[2])
        out += struct.pack("<H", 0)
    return bytes(out)
