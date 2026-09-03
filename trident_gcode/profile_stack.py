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
    Vec2,
    mesh_bounds,
    slice_outer_loop,
    slice_segments,
    stitch_loops,
    resample_closed,
    align_start,
    _is_star_convex,
    _point_in_polygon,
    _polygon_area,
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


# ---------------------------------------------------------------------------
# Mesh top surface -> seam ring (hybrid mode)
#
# The hybrid feature prints a solid, Orca-sliced planar base from the user's
# STL and then continues the non-planar parametric wall upward from the mesh's
# real top surface. The one ring where the two meet has to be the mesh's actual
# outline, sampled so that it is INDEX-COMPATIBLE with the parametric rings --
# that constraint is what the code below exists to satisfy.
# ---------------------------------------------------------------------------

def mesh_xy_midpoint(tris: list[Triangle]) -> tuple[float, float]:
    """XY midpoint of the mesh's bounding box -- the placement reference point.

    This is the same point :func:`stack_from_mesh` recentres its sliced contours
    on (``mx, my``), and the same point the solid base is placed by: the base is
    put on the bed by translating the mesh so this midpoint lands at bed centre,
    while the parametric wall is placed by its own ``center=(cx, cy)`` argument.

    Exported so the caller places the base with the identical value rather than
    recomputing a subtly different one (the polygon's centroid, say). If the two
    disagree, the seam ring is physically offset from the base underneath it.
    """
    (minx, miny, _minz), (maxx, maxy, _maxz) = mesh_bounds(tris)
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)


def _ray_radius(loop: list[Vec2], theta: float) -> float | None:
    """Distance from the origin to where the ray at *theta* leaves *loop*.

    *loop* is expressed relative to the ray origin, so the ray is
    ``s * (cos theta, sin theta)`` for ``s > 0``.  Returns ``None`` if no edge
    is crossed (the origin is outside the loop, or the loop is not closed).

    Callers must have established star-convexity about the origin first, in
    which case there is exactly one crossing.  We still take the largest ``s``
    of any hit: a ray passing exactly through a vertex reports the same point
    from both adjacent edges, and picking the max keeps that a no-op instead of
    depending on edge order.
    """
    dx = math.cos(theta)
    dy = math.sin(theta)
    best: float | None = None
    m = len(loop)
    for i in range(m):
        ax, ay = loop[i]
        bx, by = loop[(i + 1) % m]
        ex, ey = bx - ax, by - ay
        # Solve  s * d = A + u * e  for s (along the ray) and u (along the edge).
        denom = dx * ey - dy * ex
        if denom == 0.0:
            continue                      # edge parallel to the ray, or zero-length
        u = (ax * dy - ay * dx) / denom
        if u < -1e-9 or u > 1.0 + 1e-9:
            continue                      # crossing lies off the end of the edge
        s = (ax * ey - ay * ex) / denom
        if s > 0.0 and (best is None or s > best):
            best = s
    return best


def top_contour_from_mesh(
    tris: list[Triangle],
    points_per_turn: int,
    *,
    z: float,
    area_eps: float = 0.01,
) -> Contour:
    """The mesh's cross-section at height *z*, resampled BY ANGLE.

    Returns exactly *points_per_turn* points, CCW-wound, expressed relative to
    :func:`mesh_xy_midpoint` -- i.e. in the same origin-centred frame and with
    the same conventions as :func:`stack_from_shape`.

    Why by angle and not by arc length
    ----------------------------------
    :func:`stack_from_shape` places vertex *j* at ``theta = 2*pi*j/n`` exactly,
    and every per-index effect in ``build_profile_spiral`` (cage, ovality,
    radial texture, xy_twist) reads index *j* as meaning that angle.
    :func:`resample_closed` spaces points equally by ARC LENGTH, so on any
    non-circular outline its index *j* sits at a different azimuth. Lerping
    between two rings that disagree about what index *j* means would twist the
    wall as it leaves the mesh -- precisely on the non-circular mounts this
    feature exists for. So for each ``theta = 2*pi*j/n`` we cast a ray from the
    origin and take the radius where it leaves the outline. Index *j* then means
    the same azimuth on both rings by construction.

    *z* is a parameter, not a guess: the caller derives the transition height
    deterministically (it has to agree with the base actually sliced by Orca),
    so this function never infers it from the mesh's own ``maxz``.

    Fails loudly rather than approximating. This is the one ring that is
    supposed to be an EXACT match to the printed base beneath it, so the
    neighbour-interpolation fallback :func:`stack_from_mesh` uses for interior
    layers is deliberately not available here.
    """
    if not isinstance(points_per_turn, int) or isinstance(points_per_turn, bool):
        raise ValueError("points_per_turn must be an int")
    if points_per_turn < 3:
        raise ValueError(
            "points_per_turn must be at least 3, got %d" % points_per_turn)
    if not math.isfinite(z):
        raise ValueError(
            "transition height z must be a finite number -- a non-finite z "
            "passes every comparison in the slicer silently instead of "
            "tripping it, so it is rejected rather than clamped")
    if not math.isfinite(area_eps) or area_eps < 0.0:
        raise ValueError("area_eps must be a finite, non-negative number")

    origin = mesh_xy_midpoint(tris)
    ox, oy = origin

    # -- 1. Reject a genuinely AMBIGUOUS section at this height. --------------
    # slice_outer_loop() silently keeps only the largest loop. At the seam that
    # matters, but only for *islands*: two disjoint posts give no single answer
    # to "which outline does the wall continue from", and silently taking the
    # bigger one would print a shape the user did not model.
    #
    # HOLES are explicitly fine and must NOT be rejected. A mount with screw
    # holes (the motivating real-world case) has one unambiguous outer boundary;
    # the non-planar wall continues from that boundary, and the holes themselves
    # are preserved in the planar base because Orca slices the true solid --
    # which is the entire point of this feature. Rejecting them would refuse
    # exactly the parts it exists to support.
    # Sliver filtering mirrors analyze_vase_compatibility's area_eps so
    # tessellation noise is not counted as a second post.
    segs = slice_segments(tris, z)
    loops = stitch_loops(segs) if segs else []
    if loops:
        areas = [abs(_polygon_area(lp)) for lp in loops]
        big = max(areas)
        if big > 0.0:
            keep = [(lp, ar) for lp, ar in zip(loops, areas)
                    if ar >= area_eps * big]
            if len(keep) > 1:
                outer = max(keep, key=lambda pair: pair[1])[0]
                islands = 0
                for lp, _ar in keep:
                    if lp is outer:
                        continue
                    if not _point_in_polygon(lp[0], outer):
                        islands += 1
                if islands:
                    raise ValueError(
                        "the cross-section at z=%.3f mm has %d separate "
                        "island(s) outside the main outline; the wall traces "
                        "one outline, so there is no single shape to continue "
                        "from. Join the top surface into one closed outline in "
                        "CAD, or lower the transition height to a Z where the "
                        "section is a single connected region."
                        % (z, islands))

    # -- 2. The outline itself, via the shared helper (CCW-enforced). ---------
    loop = slice_outer_loop(tris, z)
    if loop is None or len(loop) < 4:
        got = "no intersection" if loop is None else "only %d points" % len(loop)
        raise ValueError(
            "no usable cross-section at z=%.3f mm (%s). The transition height "
            "must fall inside the mesh, and the mesh must be watertight and "
            "Z-up. This ring has to match the printed base exactly, so it is "
            "not interpolated from neighbouring layers." % (z, got))

    local = [(x - ox, y - oy) for (x, y) in loop]

    # -- 3. Star-convexity ABOUT THE RAY ORIGIN, which is the bbox midpoint. --
    # Not about the polygon's centroid: the rays come from the placement point,
    # so that is the point the property has to hold for.
    if not _is_star_convex(local, center=(0.0, 0.0)):
        raise ValueError(
            "the cross-section at z=%.3f mm is not star-convex about the "
            "placement origin (%.3f, %.3f) -- some ray from that point crosses "
            "the outline more than once, so one radius per angle is undefined. "
            "The seam ring must be sampled by angle to stay index-aligned with "
            "the parametric rings above it. Use a top surface that is "
            "star-convex about its bounding-box midpoint, or lower the "
            "transition height." % (z, ox, oy))

    # -- 4. Angle resample. ---------------------------------------------------
    contour: Contour = []
    for j in range(points_per_turn):
        theta = 2.0 * math.pi * j / points_per_turn
        r = _ray_radius(local, theta)
        if r is None or not math.isfinite(r):
            raise ValueError(
                "no boundary crossing at angle %.4f rad on the cross-section "
                "at z=%.3f mm -- the outline is open or does not enclose the "
                "placement origin (%.3f, %.3f)." % (theta, z, ox, oy))
        contour.append((r * math.cos(theta), r * math.sin(theta)))
    return contour


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


def _blend_weight(t: float, blend_intensity: float) -> float:
    """The interpolation weight for one blend layer, shared between
    ``blend_stack`` and any caller that needs to reproduce the same curve for
    a diagnostic (e.g. ``hybrid.py``'s ``blend_top_weight`` report field) --
    a second, independently-typed copy of this formula is exactly how the two
    could silently drift apart.

    ``blend_intensity`` (0..1) dials between a straight linear ramp (0.0) and
    the full smoothstep ease (1.0, ``w'(0) = w'(1) = 0``, see ``blend_stack``'s
    own docstring for why that avoids a crease ring). Linear interpolation
    between the two curves, not between two blend heights -- so a caller can
    ask for "the same span, gentler or harsher" independently of "how far up
    the wall the transition spans" (``blend_height``).

    ``blend_stack`` calls this with ``blend_intensity`` pinned to exactly 0.0
    ("chamfer") or exactly 1.0 ("fillet") -- the continuous dial stays here
    because it is still the shared curve definition, but the public knob is
    now the named style, not an arbitrary intensity value.
    """
    w_smooth = t * t * (3.0 - 2.0 * t)
    return t + blend_intensity * (w_smooth - t)


def blend_stack(
    mesh_ring: Contour,
    shape_fn: Callable[[float], float],
    radius: float,
    height: float,
    layer_height: float,
    points_per_turn: int,
    blend_height: float,
    radius_envelope: Callable[[float], float] | None = None,
    seam_style: str = "fillet",
    seam_coverage: float = 1.0,
) -> tuple[list[Contour], list[float]]:
    """Wall stack that starts at the mesh outline and eases into the shape.

    *mesh_ring* is the seam ring from :func:`top_contour_from_mesh` (already
    angle-resampled, so its index *j* means the same azimuth as the parametric
    rings').  The parametric rings are produced by :func:`stack_from_shape`
    itself, with *shape_fn*, *radius*, *height*, *layer_height*,
    *points_per_turn* and *radius_envelope* passed straight through, so the
    stack length and the rings above the blend are exactly what the pure
    parametric path would have produced.

    Returns ``(contours, heights)``:

    * ``contours[i]`` -- the ring for layer *i*, ``points_per_turn`` points, CCW.
    * ``heights[i]``  -- ``i * layer_height``, i.e. measured from the seam, NOT
      from the bed.  This matches ``hybrid.py``'s ``upper_heights`` convention:
      the absolute Z of the seam is passed to ``build_profile_spiral`` as
      ``base_z``, separately.

    Seam corner: chamfer vs. fillet
    --------------------------------
    Ring 0 is the mesh ring itself, unconditionally and exactly -- never a
    lerp of it, so the first wall layer lands on the base outline it was
    measured from. *blend_height* is the seam height available for the
    transition; *seam_coverage* (0..1) is the fraction of that seam height the
    chamfer/fillet actually spans:

        corner_extent = blend_height * seam_coverage

    Over ``[0, corner_extent]`` (measured from ring 0), ring *i* is
    ``interpolate_contours(mesh_ring, parametric[i], w)`` with

        t = (i * layer_height) / corner_extent        clamped to [0, 1]
        w = _blend_weight(t, 1.0 if seam_style == "fillet" else 0.0)

    ``seam_style = "fillet"`` pins the weight to the full smoothstep curve
    ``3t^2 - 2t^3`` (``w'(0) = w'(1) = 0``): the shape change per layer starts
    and ends at zero, so the wall leaves the mesh outline and reaches the
    parametric outline as a smooth round-over, with no kink at either end.
    ``seam_style = "chamfer"`` pins the weight to the pure linear ramp ``t``:
    slope-discontinuous at both ends, which is deliberate -- it is what
    produces a visible, straight beveled facet at the seam and again where the
    corner meets the parametric wall, instead of a curve. This is the
    "harsher/more abrupt, distinctly faceted" look, not a defect.

    At the defaults (``seam_style = "fillet"``, ``seam_coverage = 1.0``),
    ``corner_extent == blend_height`` and the weight is exactly the smoothstep
    curve -- this function's output is BYTE-IDENTICAL to before this seam-style
    split existed (when the equivalent was ``blend_intensity = 1.0``).

    Once ``t >= 1.0`` (i.e. above ``corner_extent``) the rings are the
    parametric ones verbatim (assigned, not lerped with ``w = 1``, so "pure
    parametric" is exact rather than float-dependent) -- this includes any
    remaining span between ``corner_extent`` and ``blend_height`` when
    ``seam_coverage < 1.0``: the corner finishes early and the wall becomes
    the plain parametric shape sooner, exactly as ``seam_coverage`` promises.

    Edge cases: ``blend_height == 0`` or ``seam_coverage == 0`` both give a
    hard seam -- ring 0 is the mesh ring, ring 1 onward is fully parametric.
    ``blend_height >= height`` makes the (uncovered) blend span the whole
    wall; if it is strictly greater, the topmost ring is still partway through
    the corner (when ``seam_coverage`` keeps ``corner_extent`` that large),
    which is what was asked for.
    """
    if len(mesh_ring) != points_per_turn:
        raise ValueError(
            "mesh_ring has %d points but points_per_turn is %d -- the seam ring "
            "and the parametric rings must share an index-to-angle mapping"
            % (len(mesh_ring), points_per_turn))
    for name, val in (("height", height), ("layer_height", layer_height),
                      ("blend_height", blend_height),
                      ("seam_coverage", seam_coverage)):
        if not math.isfinite(val):
            raise ValueError(
                "%s must be a finite number -- a non-finite value survives "
                "every clamp and comparison downstream, so it is rejected "
                "here rather than clamped" % name)
    if layer_height <= 0.0:
        raise ValueError("layer_height must be positive, got %r" % (layer_height,))
    if height <= 0.0:
        raise ValueError("height must be positive, got %r" % (height,))
    if blend_height < 0.0:
        raise ValueError("blend_height must not be negative, got %r" % (blend_height,))
    if seam_style not in ("fillet", "chamfer"):
        raise ValueError(
            "seam_style must be 'fillet' or 'chamfer', got %r" % (seam_style,))
    seam_coverage = max(0.0, min(seam_coverage, 1.0))
    intensity = 1.0 if seam_style == "fillet" else 0.0

    parametric = stack_from_shape(
        shape_fn, radius, height, layer_height, points_per_turn,
        radius_envelope=radius_envelope,
    )

    corner_extent = blend_height * seam_coverage

    contours: list[Contour] = []
    for i, para in enumerate(parametric):
        if i == 0:
            contours.append(list(mesh_ring))     # exact, always
            continue
        if corner_extent <= 0.0:
            t = 1.0                              # hard seam: no blend to be in
        else:
            t = (i * layer_height) / corner_extent
        if t >= 1.0:
            contours.append(list(para))          # exactly the parametric ring
        else:
            w = _blend_weight(t, intensity)
            contours.append(interpolate_contours(mesh_ring, para, w))

    heights = [i * layer_height for i in range(len(parametric))]
    return contours, heights
