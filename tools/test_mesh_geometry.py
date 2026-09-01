#!/usr/bin/env python3
"""Tests for the mesh-top-surface geometry in trident_gcode/profile_stack.py.

Plain ``python tools/test_mesh_geometry.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Stdlib only, in the style of tools/test_mesh.py and
tools/test_printer_import.py.

Subject: ``top_contour_from_mesh`` (the seam ring where an Orca-sliced solid
base hands off to the non-planar parametric wall) and ``blend_stack`` (how that
ring eases into the parametric shape).

The property under the most pressure here is ANGULAR CORRESPONDENCE.
``stack_from_shape`` puts ring vertex *j* at ``theta = 2*pi*j/n`` exactly, and
``build_profile_spiral``'s per-index effects (cage, ovality, radial texture,
xy_twist) all read index *j* as that angle. ``mesh.resample_closed`` spaces
points equally by ARC LENGTH, so on any non-circular outline its index *j* sits
somewhere else. Lerping two rings that disagree about what index *j* means
twists the wall right where it leaves the mesh -- on exactly the non-circular
mounts the feature exists for. So several cases below assert
``atan2(y, x) == 2*pi*j/n`` directly, and one of them shows that arc-length
resampling fails that same assertion, i.e. the test has teeth.

The second property is the ORIGIN. The base is placed on the bed by moving the
mesh's XY bounding-box midpoint to bed centre, so the rays must come from that
midpoint and the ring must be expressed relative to it. The polygon's own
centroid is a different point on any asymmetric outline, and using it would
offset the seam from the base printed underneath it. The off-centre D case
below pins that down.

Meshes are built in code (side walls only -- horizontal cap facets never
straddle a slice plane, so they cannot change any result here).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.mesh import (
    _is_star_convex,
    _point_in_polygon,
    resample_closed,
    slice_outer_loop,
)
from trident_gcode.paths import circle, superellipse
from trident_gcode.profile_stack import (
    blend_stack,
    mesh_xy_midpoint,
    stack_from_shape,
    top_contour_from_mesh,
)

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _expect_value_error(fn, label: str, needle: str) -> None:
    """``fn()`` must raise ValueError whose message contains ``needle``."""
    try:
        fn()
    except ValueError as exc:
        msg = str(exc)
        check(True, f"{label}: raises ValueError")
        check(needle in msg, f"{label}: message explains it ({needle!r})", msg)
        check(msg.isascii(), f"{label}: message is ASCII-only", msg)
        return
    except Exception as exc:                      # noqa: BLE001 - report, don't mask
        check(False, f"{label}: raises ValueError",
              f"raised {type(exc).__name__}: {exc}")
        return
    check(False, f"{label}: raises ValueError", "no exception raised")


# --------------------------------------------------------------- mesh builders
def _prism(poly: list[tuple[float, float]], z0: float, z1: float) -> list:
    """Extrude a closed 2D polygon into prism side walls.

    Caps are omitted on purpose: a horizontal facet has all three vertices at
    one Z, so ``slice_segments``' ``below0 != below1`` test never fires on its
    edges and it contributes nothing to any cross-section.
    """
    tris = []
    m = len(poly)
    for i in range(m):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % m]
        a = (x0, y0, z0)
        b = (x1, y1, z0)
        c = (x1, y1, z1)
        d = (x0, y0, z1)
        tris.append((a, b, c))
        tris.append((a, c, d))
    return tris


def _circle_poly(r: float, n: int, cx: float = 0.0, cy: float = 0.0):
    return [(cx + r * math.cos(2.0 * math.pi * i / n),
             cy + r * math.sin(2.0 * math.pi * i / n)) for i in range(n)]


def _square_poly(half: float, cx: float = 0.0, cy: float = 0.0):
    return [(cx - half, cy - half), (cx + half, cy - half),
            (cx + half, cy + half), (cx - half, cy + half)]


def _star_poly(r_out: float, r_in: float, points: int,
               cx: float = 0.0, cy: float = 0.0):
    """A 2*points-vertex star. Vertex i sits at angle i*pi/points."""
    pts = []
    for i in range(2 * points):
        ang = math.pi * i / points
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


# An off-centre D: the disk of radius D_R about the world origin, cut by the
# half-plane x >= D_CUT. Convex (so star-convex about every interior point),
# but its bbox midpoint and its vertex centroid are far apart -- the shape that
# tells the two candidate ray origins apart.
D_R = 10.0
D_CUT = -4.0


def _d_poly(n_arc: int = 2000):
    a = math.acos(D_CUT / D_R)          # half-angle of the surviving arc
    return [(D_R * math.cos(-a + 2.0 * a * i / (n_arc - 1)),
             D_R * math.sin(-a + 2.0 * a * i / (n_arc - 1)))
            for i in range(n_arc)]


def _d_radius(theta: float, ox: float, oy: float) -> float:
    """Exact distance from (ox, oy) to where the ray at *theta* leaves the D."""
    dx, dy = math.cos(theta), math.sin(theta)
    cx, cy = -ox, -oy                   # disk centre, relative to the ray origin
    b = dx * cx + dy * cy
    c = cx * cx + cy * cy - D_R * D_R
    s = b + math.sqrt(b * b - c)        # positive root: where the ray exits
    if dx < 0.0:                        # ... unless the flat cut comes first
        s_cut = (D_CUT - ox) / dx
        if 0.0 < s_cut < s:
            s = s_cut
    return s


# An L: its bbox midpoint (15, 15) is OUTSIDE the solid.
def _l_poly():
    return [(0.0, 0.0), (30.0, 0.0), (30.0, 10.0),
            (10.0, 10.0), (10.0, 30.0), (0.0, 30.0)]


# A notched square: bbox midpoint (0, 0) is INSIDE, but the ray at -30 deg
# crosses the outline twice (the notch's inner face and the outer wall).
def _notched_square_poly():
    return [(-10.0, -10.0), (10.0, -10.0), (10.0, -3.0), (2.0, -3.0),
            (2.0, 3.0), (10.0, 3.0), (10.0, 10.0), (-10.0, 10.0)]


def _centroid_biased_l_poly(k: int = 30):
    """The same thin L, with extra COLLINEAR vertices near its inner corner.

    The extra points sit exactly on existing edges, so the outline is
    unchanged -- but they drag the vertex-average centroid to about
    (3.5, 8.4), which is inside the solid and about which the L *is*
    star-convex. The bounding-box midpoint is still (15, 15), out in the
    notch. So this is a mesh that a centroid-based star-convexity check
    accepts and a bbox-midpoint-based one rejects: the two origins give
    opposite answers about the same outline, which is the whole reason the
    check has to be told which point the rays come from.
    """
    return ([(10.0 * i / k, 0.0) for i in range(k)]
            + [(30.0, 0.0), (30.0, 10.0), (10.0, 10.0),
               (10.0, 30.0), (0.0, 30.0)]
            + [(0.0, 30.0 - 30.0 * i / k) for i in range(k)])


Z_LO, Z_HI = 0.0, 20.0
Z_CUT = 6.3          # a slice height that is neither an end nor a round number


def _angle_error(x: float, y: float, want: float) -> float:
    """|atan2(y, x) - want| wrapped into [0, pi]."""
    d = math.atan2(y, x) - want
    return abs(math.atan2(math.sin(d), math.cos(d)))


# ---------------------------------------------------------------------------
# 1. Circular prism: the ring must reproduce the analytic circle at every index.
# ---------------------------------------------------------------------------
def test_circle_prism_matches_analytic_circle():
    n = 240
    tris = _prism(_circle_poly(10.0, 720, cx=7.0, cy=-3.0), Z_LO, Z_HI)

    ox, oy = mesh_xy_midpoint(tris)
    check(abs(ox - 7.0) < 1e-9 and abs(oy + 3.0) < 1e-9,
          "circle: bbox midpoint is the circle centre", f"({ox}, {oy})")

    ring = top_contour_from_mesh(tris, n, z=Z_CUT)
    check(len(ring) == n, "circle: ring has exactly points_per_turn points",
          f"got {len(ring)}")

    worst = 0.0
    for j, (x, y) in enumerate(ring):
        theta = 2.0 * math.pi * j / n
        worst = max(worst, math.hypot(x - 10.0 * math.cos(theta),
                                      y - 10.0 * math.sin(theta)))
    check(worst < 1e-3,
          "circle: every index matches the analytic circle (origin-centred)",
          f"worst deviation {worst:.3e} mm")

    # CCW winding, like stack_from_shape's output.
    area = 0.0
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    check(area > 0.0, "circle: ring is CCW-wound", f"signed area {area:.3f}")


# ---------------------------------------------------------------------------
# 2. Square prism: the angular-correspondence property, stated directly.
#    This is the assertion arc-length resampling would fail (proved below).
# ---------------------------------------------------------------------------
def test_square_prism_index_means_angle():
    n = 64
    tris = _prism(_square_poly(10.0), Z_LO, Z_HI)
    ring = top_contour_from_mesh(tris, n, z=Z_CUT)

    worst_ang = 0.0
    worst_r = 0.0
    for j, (x, y) in enumerate(ring):
        theta = 2.0 * math.pi * j / n
        worst_ang = max(worst_ang, _angle_error(x, y, theta))
        want_r = 10.0 / max(abs(math.cos(theta)), abs(math.sin(theta)))
        worst_r = max(worst_r, abs(math.hypot(x, y) - want_r))
    check(worst_ang < 1e-9,
          "square: atan2(y, x) of index j is exactly 2*pi*j/n",
          f"worst angular error {worst_ang:.3e} rad")
    check(worst_r < 1e-9,
          "square: radius at each index matches the analytic square",
          f"worst radial error {worst_r:.3e} mm")


def test_arc_length_resampling_would_fail_that_assertion():
    """The discriminating case: swap in resample_closed and the test must break.

    If this ever reports a small angular error, arc-length and angular
    resampling have stopped being distinguishable and the test above has lost
    its teeth -- not a licence to use resample_closed for the seam ring.
    """
    n = 64
    tris = _prism(_square_poly(10.0), Z_LO, Z_HI)
    ox, oy = mesh_xy_midpoint(tris)
    loop = slice_outer_loop(tris, Z_CUT)
    arc = resample_closed([(x - ox, y - oy) for (x, y) in loop], n)

    worst_ang = 0.0
    for j, (x, y) in enumerate(arc):
        worst_ang = max(worst_ang, _angle_error(x, y, 2.0 * math.pi * j / n))
    check(worst_ang > 0.05,
          "square: arc-length resampling DOES violate index-means-angle",
          f"worst angular error {worst_ang:.3e} rad (wanted a large one)")


# ---------------------------------------------------------------------------
# 3. Star prism: same property on a strongly non-convex (but star-convex)
#    outline, plus the radii at the star's own vertex angles.
# ---------------------------------------------------------------------------
def test_star_prism_index_means_angle():
    n = 96
    points = 6
    r_out, r_in = 12.0, 5.0
    tris = _prism(_star_poly(r_out, r_in, points, cx=4.0, cy=9.0), Z_LO, Z_HI)

    ox, oy = mesh_xy_midpoint(tris)
    check(abs(ox - 4.0) < 1e-9 and abs(oy - 9.0) < 1e-9,
          "star: bbox midpoint is the star centre", f"({ox}, {oy})")

    ring = top_contour_from_mesh(tris, n, z=Z_CUT)

    worst_ang = 0.0
    for j, (x, y) in enumerate(ring):
        worst_ang = max(worst_ang, _angle_error(x, y, 2.0 * math.pi * j / n))
    check(worst_ang < 1e-9,
          "star: atan2(y, x) of index j is exactly 2*pi*j/n",
          f"worst angular error {worst_ang:.3e} rad")

    # n / (2*points) = 8, so ring index 8*i lands exactly on star vertex i,
    # which alternates outer tip / inner valley.
    worst_r = 0.0
    for i in range(2 * points):
        x, y = ring[8 * i]
        want = r_out if i % 2 == 0 else r_in
        worst_r = max(worst_r, abs(math.hypot(x, y) - want))
    check(worst_r < 1e-6,
          "star: indices landing on star vertices have the tip/valley radius",
          f"worst radial error {worst_r:.3e} mm")


# ---------------------------------------------------------------------------
# 4. THE ORIGIN. An off-centre D whose vertex centroid is nowhere near its
#    bounding-box midpoint. The rays must come from the bbox midpoint, because
#    that is the point the base is placed by.
# ---------------------------------------------------------------------------
def test_offcentre_shape_uses_bbox_midpoint_not_centroid():
    n = 128
    tris = _prism(_d_poly(), Z_LO, Z_HI)
    ox, oy = mesh_xy_midpoint(tris)

    loop = slice_outer_loop(tris, Z_CUT)
    gx = sum(p[0] for p in loop) / len(loop)
    gy = sum(p[1] for p in loop) / len(loop)
    sep = math.hypot(gx - ox, gy - oy)
    check(sep > 1.0,
          "D-shape: bbox midpoint and polygon centroid really are different",
          f"bbox ({ox:.4f}, {oy:.4f}) vs centroid ({gx:.4f}, {gy:.4f}), "
          f"{sep:.4f} mm apart")

    ring = top_contour_from_mesh(tris, n, z=Z_CUT)

    worst_bbox = 0.0
    worst_centroid = 0.0
    for j, (x, y) in enumerate(ring):
        theta = 2.0 * math.pi * j / n
        worst_bbox = max(worst_bbox,
                         abs(math.hypot(x, y) - _d_radius(theta, ox, oy)))
        worst_centroid = max(worst_centroid,
                             abs(math.hypot(x, y) - _d_radius(theta, gx, gy)))
    check(worst_bbox < 1e-3,
          "D-shape: ring is the exact outline as seen from the BBOX MIDPOINT",
          f"worst radial error {worst_bbox:.3e} mm")
    check(worst_centroid > 1.0,
          "D-shape: ring is NOT the outline as seen from the centroid "
          "(so the origin choice is pinned, not incidental)",
          f"worst radial error {worst_centroid:.3e} mm")

    # Same property stated in placement terms: the ring is expressed relative
    # to the bbox midpoint, so translating the mesh on the bed must not change
    # the ring at all.
    moved = _prism([(x + 137.5, y - 42.25) for (x, y) in _d_poly()], Z_LO, Z_HI)
    ring2 = top_contour_from_mesh(moved, n, z=Z_CUT)
    worst_move = max(math.hypot(a[0] - b[0], a[1] - b[1])
                     for a, b in zip(ring, ring2))
    check(worst_move < 1e-9,
          "D-shape: ring is translation-invariant (expressed about the origin "
          "that gets moved to bed centre)",
          f"worst deviation {worst_move:.3e} mm")


# ---------------------------------------------------------------------------
# 5. Fail-fast cases. Each must raise instead of guessing -- this is the one
#    ring that has to be an exact match to the base printed underneath it.
# ---------------------------------------------------------------------------
def test_two_posts_rejected():
    """Two disjoint posts: slice_outer_loop would silently keep one of them.

    ISLANDS are the genuinely ambiguous case -- there is no single answer to
    "which outline does the wall continue from" -- so this must raise.
    """
    tris = (_prism(_square_poly(5.0, cx=-20.0, cy=0.0), Z_LO, Z_HI)
            + _prism(_square_poly(5.0, cx=20.0, cy=0.0), Z_LO, Z_HI))
    _expect_value_error(lambda: top_contour_from_mesh(tris, 64, z=Z_CUT),
                        "two_posts", "island")


def test_hole_accepted():
    """An annulus MUST be accepted, tracing the OUTER outline.

    A hole is not ambiguous: the outer boundary is unique, the wall continues
    from it, and the hole itself survives in the planar base because Orca
    slices the user's true solid. The motivating real-world part -- a mount
    with screw holes -- is exactly this shape, so rejecting it would refuse
    the case the feature exists for. (This deliberately replaces an earlier
    test that asserted the opposite; that behaviour was wrong and was found
    by running the real part through the real UI.)
    """
    tris = (_prism(_circle_poly(12.0, 180), Z_LO, Z_HI)
            + _prism(_circle_poly(5.0, 180), Z_LO, Z_HI))
    ring = top_contour_from_mesh(tris, 64, z=Z_CUT)
    check(len(ring) == 64, "hole: annulus accepted, ring has n points",
          "got %d" % len(ring))
    radii = [math.hypot(x, y) for (x, y) in ring]
    check(all(abs(r - 12.0) < 0.05 for r in radii),
          "hole: ring traces the OUTER boundary (r=12), not the hole",
          "radii %.3f..%.3f" % (min(radii), max(radii)))


def test_origin_outside_rejected():
    """L-shape: the bbox midpoint (15, 15) is not inside the solid at all."""
    poly = _l_poly()
    check(not _point_in_polygon((15.0, 15.0), poly),
          "L-shape: bbox midpoint (15, 15) is outside the outline")
    tris = _prism(poly, Z_LO, Z_HI)
    _expect_value_error(lambda: top_contour_from_mesh(tris, 64, z=Z_CUT),
                        "l_shape", "star-convex")


def test_non_star_convex_rejected():
    """Notched square: origin IS inside, but a ray still crosses twice."""
    poly = _notched_square_poly()
    check(_point_in_polygon((0.0, 0.0), poly),
          "notched: bbox midpoint (0, 0) IS inside the outline "
          "(so this is the two-crossings case, not the outside case)")
    check(not _is_star_convex(poly, center=(0.0, 0.0)),
          "notched: outline is not star-convex about (0, 0)")
    tris = _prism(poly, Z_LO, Z_HI)
    _expect_value_error(lambda: top_contour_from_mesh(tris, 64, z=Z_CUT),
                        "notched", "star-convex")


def test_slice_outside_mesh_rejected():
    tris = _prism(_square_poly(10.0), Z_LO, Z_HI)
    _expect_value_error(lambda: top_contour_from_mesh(tris, 64, z=Z_HI + 5.0),
                        "z_above_mesh", "no usable cross-section")
    _expect_value_error(lambda: top_contour_from_mesh(tris, 64, z=Z_LO - 5.0),
                        "z_below_mesh", "no usable cross-section")


def test_non_finite_and_bad_arguments_rejected():
    tris = _prism(_square_poly(10.0), Z_LO, Z_HI)
    for label, bad in (("nan", float("nan")), ("inf", float("inf")),
                       ("-inf", float("-inf"))):
        _expect_value_error(
            lambda bad=bad: top_contour_from_mesh(tris, 64, z=bad),
            f"z_{label}", "finite")
    _expect_value_error(lambda: top_contour_from_mesh(tris, 2, z=Z_CUT),
                        "ppt_too_small", "at least 3")


# ---------------------------------------------------------------------------
# 6. blend_stack.
# ---------------------------------------------------------------------------
def _mesh_ring(n: int):
    tris = _prism(_circle_poly(10.0, 720, cx=7.0, cy=-3.0), Z_LO, Z_HI)
    return top_contour_from_mesh(tris, n, z=Z_CUT)


def test_blend_endpoints():
    n = 64
    shape = superellipse(30.0, 6.0)     # deliberately not a circle
    height, lh, blend = 10.0, 0.5, 3.0
    ring = _mesh_ring(n)

    contours, heights = blend_stack(ring, shape, 30.0, height, lh, n, blend)
    para = stack_from_shape(shape, 30.0, height, lh, n)

    check(len(contours) == len(para),
          "blend: stack length equals the pure parametric stack",
          f"{len(contours)} vs {len(para)}")
    check(heights == [i * lh for i in range(len(para))],
          "blend: heights are i * layer_height, measured from the seam",
          str(heights[:4]))

    check(contours[0] == ring,
          "blend: ring 0 is the mesh ring EXACTLY (not a lerp of it)")

    end = int(round(blend / lh))        # 6 -> exactly at blend_height
    check(contours[end] == para[end],
          "blend: the ring at the end of the blend is exactly the parametric "
          "ring")
    check(all(contours[i] == para[i] for i in range(end, len(para))),
          "blend: every ring above the blend is exactly parametric")
    check(all(contours[i] != para[i] and contours[i] != ring
              for i in range(1, end)),
          "blend: rings inside the blend are strictly between the two")


def test_blend_uses_smoothstep():
    """Recover the weight from a circle-to-circle blend and check the formula."""
    n = 64
    height, lh, blend = 10.0, 0.5, 3.0
    tris = _prism(_circle_poly(10.0, 2000), Z_LO, Z_HI)
    ring = top_contour_from_mesh(tris, n, z=Z_CUT)      # radius 10 about origin
    contours, _h = blend_stack(ring, circle(30.0), 30.0, height, lh, n, blend)

    worst = 0.0
    for i in range(1, int(round(blend / lh))):
        t = (i * lh) / blend
        want = t * t * (3.0 - 2.0 * t)
        got = (math.hypot(*contours[i][0]) - 10.0) / 20.0
        worst = max(worst, abs(got - want))
    check(worst < 1e-3, "blend: weight follows smoothstep 3t^2 - 2t^3",
          f"worst deviation {worst:.3e}")

    # Slope-continuity at both ends is the reason for smoothstep: the per-layer
    # shape change starts and ends at (near) zero, unlike a linear ramp.
    r = [math.hypot(*c[0]) for c in contours]
    first_step = r[1] - r[0]
    mid_step = r[3] - r[2]
    check(first_step < 0.35 * mid_step,
          "blend: first layer's shape change is much smaller than mid-blend "
          "(no kink leaving the mesh)",
          f"first {first_step:.4f} mm vs mid {mid_step:.4f} mm")


def test_blend_height_zero_is_a_hard_seam():
    n = 64
    shape = superellipse(30.0, 6.0)
    height, lh = 10.0, 0.5
    ring = _mesh_ring(n)

    contours, _h = blend_stack(ring, shape, 30.0, height, lh, n, 0.0)
    para = stack_from_shape(shape, 30.0, height, lh, n)

    check(contours[0] == ring,
          "blend_height=0: ring 0 is still exactly the mesh ring")
    check(all(contours[i] == para[i] for i in range(1, len(para))),
          "blend_height=0: ring 1 onward is fully parametric (hard seam)")


def test_blend_height_spanning_the_whole_wall():
    n = 64
    shape = superellipse(30.0, 6.0)
    height, lh = 10.0, 0.5
    ring = _mesh_ring(n)
    para = stack_from_shape(shape, 30.0, height, lh, n)

    # blend_height == height: rings run 0 .. (n_layers-1)*lh = 9.5, so the top
    # ring is at t = 0.95 -- still inside the blend, which is what was asked.
    contours, _h = blend_stack(ring, shape, 30.0, height, lh, n, height)
    check(contours[0] == ring, "blend_height=height: ring 0 is the mesh ring")
    check(contours[-1] != para[-1],
          "blend_height=height: the blend really does span the whole wall "
          "(top ring not yet fully parametric)")
    check(all(c != ring for c in contours[1:]),
          "blend_height=height: no ring above 0 is stuck on the mesh shape")

    # blend_height >> height: the whole wall is still near the mesh shape.
    contours2, _h2 = blend_stack(ring, shape, 30.0, height, lh, n, 100.0)
    top_dev = max(math.hypot(a[0] - b[0], a[1] - b[1])
                  for a, b in zip(contours2[-1], ring))
    full_dev = max(math.hypot(a[0] - b[0], a[1] - b[1])
                   for a, b in zip(para[-1], ring))
    check(0.0 < top_dev < 0.1 * full_dev,
          "blend_height >> height: top ring has barely left the mesh shape",
          f"moved {top_dev:.4f} mm of a possible {full_dev:.4f} mm")


def test_blend_rejects_bad_arguments():
    n = 64
    ring = _mesh_ring(n)
    shape = circle(30.0)
    _expect_value_error(
        lambda: blend_stack(ring, shape, 30.0, 10.0, 0.5, n + 1, 3.0),
        "blend_length_mismatch", "points_per_turn")
    for label, bad in (("nan", float("nan")), ("inf", float("inf"))):
        _expect_value_error(
            lambda bad=bad: blend_stack(ring, shape, 30.0, 10.0, 0.5, n, bad),
            f"blend_height_{label}", "finite")
        _expect_value_error(
            lambda bad=bad: blend_stack(ring, shape, 30.0, bad, 0.5, n, 3.0),
            f"height_{label}", "finite")
    _expect_value_error(
        lambda: blend_stack(ring, shape, 30.0, 10.0, 0.0, n, 3.0),
        "layer_height_zero", "positive")
    _expect_value_error(
        lambda: blend_stack(ring, shape, 30.0, 10.0, 0.5, n, -1.0),
        "blend_height_negative", "negative")


# ---------------------------------------------------------------------------
# 7. The additive `center` argument on mesh._is_star_convex must not have
#    changed what the no-argument call answers -- analyze_vase_compatibility
#    still relies on the centroid default.
# ---------------------------------------------------------------------------
def test_is_star_convex_default_unchanged():
    loops = [_circle_poly(10.0, 64, cx=7.0, cy=-3.0), _square_poly(10.0),
             _star_poly(12.0, 5.0, 6), _l_poly(), _notched_square_poly(),
             _d_poly(64)]
    for i, loop in enumerate(loops):
        cx = sum(p[0] for p in loop) / len(loop)
        cy = sum(p[1] for p in loop) / len(loop)
        check(_is_star_convex(loop) == _is_star_convex(loop, center=(cx, cy)),
              f"_is_star_convex: default still means the vertex centroid "
              f"(loop {i})")
    # And it is genuinely a two-argument question: one outline, two origins,
    # opposite answers.
    biased = _centroid_biased_l_poly()
    check(_is_star_convex(biased),
          "_is_star_convex: biased L IS star-convex about its centroid")
    check(not _is_star_convex(biased, center=(15.0, 15.0)),
          "_is_star_convex: biased L is NOT star-convex about its bbox "
          "midpoint (so the origin must be passed, not assumed)")


def test_star_convexity_is_checked_about_the_bbox_midpoint():
    """End-to-end: the outline a centroid-based check would wrongly accept.

    top_contour_from_mesh casts its rays from the bbox midpoint, so that is the
    point star-convexity has to hold about. If the check ever reverts to
    _is_star_convex's centroid default, this mesh sails through and the ring
    comes out with a wrong radius wherever a ray crosses twice.
    """
    poly = _centroid_biased_l_poly()
    tris = _prism(poly, Z_LO, Z_HI)
    ox, oy = mesh_xy_midpoint(tris)
    check(abs(ox - 15.0) < 1e-9 and abs(oy - 15.0) < 1e-9,
          "biased L: bbox midpoint is (15, 15)", f"({ox}, {oy})")
    check(_is_star_convex(poly),
          "biased L: a centroid-based check would ACCEPT this mesh")
    _expect_value_error(lambda: top_contour_from_mesh(tris, 64, z=Z_CUT),
                        "biased_l", "star-convex")


def main() -> int:
    test_circle_prism_matches_analytic_circle()
    test_square_prism_index_means_angle()
    test_arc_length_resampling_would_fail_that_assertion()
    test_star_prism_index_means_angle()
    test_offcentre_shape_uses_bbox_midpoint_not_centroid()
    test_two_posts_rejected()
    test_hole_accepted()
    test_origin_outside_rejected()
    test_non_star_convex_rejected()
    test_star_convexity_is_checked_about_the_bbox_midpoint()
    test_slice_outside_mesh_rejected()
    test_non_finite_and_bad_arguments_rejected()
    test_blend_endpoints()
    test_blend_uses_smoothstep()
    test_blend_height_zero_is_a_hard_seam()
    test_blend_height_spanning_the_whole_wall()
    test_blend_rejects_bad_arguments()
    test_is_star_convex_default_unchanged()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILED:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
