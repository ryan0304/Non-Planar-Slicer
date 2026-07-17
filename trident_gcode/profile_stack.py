"""Contour-stack abstraction: unified input format for profile-spiral generation.

A contour stack is an ordered list of closed 2D contours (one per layer).
Both parametric shapes and sliced STL meshes produce this format, enabling
one generator (``profile_spiral``) to serve all modes.

All contours are CCW-wound, resampled to a fixed point count, and seam-aligned
so successive contours begin at the nearest point to the previous contour's
start -- keeping the spiral seam straight.
"""
from __future__ import annotations

import math
from typing import Callable, List, Tuple

# Type aliases
Contour = List[Tuple[float, float]]

# Re-use the mesh module's slicing / resampling helpers for STL mode.
from .mesh import (
    Triangle,
    mesh_bounds,
    slice_outer_loop,
    resample_closed,
    align_start,
)


def stack_from_shape(
    shape_fn: Callable[[float], float],
    radius: float,
    height: float,
    layer_height: float,
    points_per_turn: int,
    radius_envelope: Callable[[float], float] | None = None,
) -> list[Contour]:
    """Sample a parametric shape into a stack of contours (one per layer).

    *shape_fn* is a ``theta -> radius`` callable (like ``circle``, ``star``,
    ``superellipse`` from :mod:`paths`).  Each contour has *points_per_turn*
    vertices placed at equally-spaced angles around the profile, optionally
    scaled per-layer by *radius_envelope(t)* where *t* is the height fraction
    in [0, 1].

    Returns a list of *n_layers* contours, where ``n_layers = round(height /
    layer_height)``.
    """
    n_layers = max(1, int(round(height / layer_height)))
    contours: list[Contour] = []
    for i in range(n_layers):
        t = i / max(n_layers - 1, 1)  # 0..1
        scale = radius_envelope(t) if radius_envelope is not None else 1.0
        contour: Contour = []
        for j in range(points_per_turn):
            theta = 2.0 * math.pi * j / points_per_turn
            r = shape_fn(theta) * scale
            contour.append((r * math.cos(theta), r * math.sin(theta)))
        contours.append(contour)
    return contours


def stack_from_mesh(
    tris: list[Triangle],
    layer_height: float,
    points_per_turn: int,
) -> list[Contour]:
    """Slice an STL mesh into a contour stack.

    Wraps the existing :mod:`mesh` helpers: slices at mid-layer heights,
    recentres, resamples to *points_per_turn* points, and carries the seam
    alignment upward.  Layers that fail to slice (no intersection, too few
    points) are replaced by linear interpolation of their nearest valid
    neighbours instead of being dropped -- this keeps the stack length equal
    to the expected layer count and avoids index drift.
    """
    (minx, miny, minz), (maxx, maxy, maxz) = mesh_bounds(tris)
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    height = maxz - minz
    if height <= 0:
        raise ValueError("mesh has no height to slice")

    n_layers = max(1, int(round(height / layer_height)))

    # First pass: slice every layer, recording which ones succeeded.
    raw: list[Contour | None] = [None] * n_layers
    ref: tuple[float, float] = (max(maxx - mx, 1e-6), 0.0)

    for i in range(n_layers):
        h = minz + (i + 0.5) * layer_height
        loop = slice_outer_loop(tris, h)
        if loop is None or len(loop) < 4:
            continue
        loop = [(x - mx, y - my) for (x, y) in loop]
        rs = resample_closed(loop, points_per_turn)
        rs = align_start(rs, ref)
        ref = rs[0]
        raw[i] = rs

    # Second pass: fill gaps by interpolating nearest valid neighbours.
    contours: list[Contour] = []
    for i in range(n_layers):
        if raw[i] is not None:
            contours.append(raw[i])  # type: ignore[arg-type]
            continue
        # Find nearest valid below and above.
        below: Contour | None = None
        above: Contour | None = None
        bi, ai = i, i
        while bi >= 0:
            if raw[bi] is not None:
                below = raw[bi]
                break
            bi -= 1
        while ai < n_layers:
            if raw[ai] is not None:
                above = raw[ai]
                break
            ai += 1
        if below is not None and above is not None:
            span = ai - bi
            frac = (i - bi) / span if span > 0 else 0.5
            contours.append(interpolate_contours(below, above, frac))
        elif below is not None:
            contours.append(below)
        elif above is not None:
            contours.append(above)
        else:
            raise ValueError(
                "no printable cross-sections found -- is the STL watertight and Z-up?"
            )

    return contours


def contour_normals(contour: Contour) -> list[tuple[float, float]]:
    """Compute outward unit normals at each vertex of a CCW polygon.

    For vertex *i*, the normal is the average of the outward normals of the
    two adjacent edges (i-1,i) and (i,i+1).  Edge from *p* to *q* has
    direction ``(dx, dy)``; its left (outward for CCW) normal is ``(dy, -dx)``.
    """
    n = len(contour)
    normals: list[tuple[float, float]] = []
    for i in range(n):
        # Edge before: (i-1) -> i
        x0, y0 = contour[(i - 1) % n]
        x1, y1 = contour[i]
        dx0, dy0 = x1 - x0, y1 - y0
        # Left normal of edge (outward for CCW): (dy, -dx)
        nx0, ny0 = dy0, -dx0
        ln0 = math.hypot(nx0, ny0)
        if ln0 > 1e-12:
            nx0 /= ln0
            ny0 /= ln0

        # Edge after: i -> (i+1)
        x2, y2 = contour[(i + 1) % n]
        dx1, dy1 = x2 - x1, y2 - y1
        nx1, ny1 = dy1, -dx1
        ln1 = math.hypot(nx1, ny1)
        if ln1 > 1e-12:
            nx1 /= ln1
            ny1 /= ln1

        # Average and normalise
        ax, ay = nx0 + nx1, ny0 + ny1
        la = math.hypot(ax, ay)
        if la > 1e-12:
            normals.append((ax / la, ay / la))
        else:
            normals.append((nx0, ny0))  # degenerate: use one edge's normal
    return normals


def interpolate_contours(c0: Contour, c1: Contour, frac: float) -> Contour:
    """Linearly interpolate between two contours point-by-point.

    Both contours must have the same number of points.  *frac* = 0 returns
    *c0*, *frac* = 1 returns *c1*.
    """
    if len(c0) != len(c1):
        raise ValueError(
            f"contour length mismatch: {len(c0)} vs {len(c1)}"
        )
    inv = 1.0 - frac
    return [(inv * x0 + frac * x1, inv * y0 + frac * y1)
            for (x0, y0), (x1, y1) in zip(c0, c1)]
