#!/usr/bin/env python3
"""Functional (non-byte-exact) tests for hybrid.build_mesh_hybrid_print().

Plain ``python tools/test_mesh_hybrid.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors tools/test_hybrid.py's style exactly,
including its monkeypatched ``slice_stl_to_gcode`` (tools/test_hybrid.py:42-51)
so no live OrcaSlicer install is needed -- the checked-in
tools/fixtures/orca_gcode/sample_base.gcode stands in for "whatever Orca would
have produced".

The byte-exact half of this feature's coverage lives in
tools/check_regression.py (ref_mesh_hybrid_print.gcode); this file checks that
the function completes, raises where it must, and returns sane values.
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCA_FIXTURE = ROOT / "tools" / "fixtures" / "orca_gcode" / "sample_base.gcode"
MESH_FIXTURE = ROOT / "tools" / "fixtures" / "meshes" / "hex_mount.stl"
sys.path.insert(0, str(ROOT))

import trident_gcode.hybrid as hybrid
from trident_gcode.blobs import LoopSpec
from trident_gcode.gcode import GcodeWriter
from trident_gcode.mesh import load_stl
from trident_gcode.paths import circle, ZoneOverride
from trident_gcode.profile import PrinterProfile
from trident_gcode.profile_stack import (
    mesh_ring_radius_at, stack_from_shape, top_contour_from_mesh)


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class FakeOrca:
    """Stands in for the slice subprocess AND records whether it ever ran.

    ``calls == 0`` after an expected failure is the proof that a pre-flight
    check fired BEFORE the 120s external slice, which is the whole point of
    doing those checks in-process (orca_slice.py:232, and --arrange 0 at :268
    means a badly-oriented mesh is not quietly laid flat for us either).
    """

    def __init__(self, text: str | None = None):
        self.calls = 0
        self.stl_bytes: bytes | None = None
        self.text = text if text is not None else ORCA_FIXTURE.read_text(encoding="utf-8")

    def __call__(self, stl_bytes, *, machine_json, process_json, filament_json,
                 orca_path, **kw):
        self.calls += 1
        self.stl_bytes = stl_bytes
        return self.text


class CapturedWall:
    """Wraps build_profile_spiral to record the contour stack it was handed,
    and (via _run's own _nearest_ring_index trace, not this class) the seam
    angle build_mesh_hybrid_print re-anchored the wall to -- see seam_k's own
    docstring in _run below for why any test that independently recomputes a
    "what the wall SHOULD look like" reference needs that same rotation.
    """

    def __init__(self, real):
        self.real = real
        self.contours = None
        self.heights = None
        self.kwargs = None
        self.seam_k = None

    def __call__(self, writer, contours, heights, **kwargs):
        self.contours = contours
        self.heights = heights
        self.kwargs = kwargs
        return self.real(writer, contours, heights, **kwargs)


def _new_writer(profile, layer_height=0.2):
    return GcodeWriter(
        profile=profile, line_width=0.45, layer_height=layer_height,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def _run(fake, *, tris=None, capture_wall=False, **overrides):
    """Call build_mesh_hybrid_print with the standard fixture arguments.

    scale=0.1 shrinks the 25 mm / 4.5 mm checked-in hexagonal mount to a
    2.5 mm / 0.45 mm one. The seam is the mesh's own top (0.45 mm -- this path
    does NOT layer-snap, see build_mesh_hybrid_print step 2), and
    sample_base.gcode's own extruding moves top out at 0.40 mm, which is inside
    the step-7 drift tolerance of half a layer (0.10 mm) -- so that check is
    satisfied by construction rather than by luck.
    center=(102.5, 102.5) is load-bearing: it must match the Orca fixture's own
    footprint or the placement sanity check raises.

    ``wall.seam_k`` (always populated, regardless of ``capture_wall``): the
    index build_mesh_hybrid_print's own seam-alignment step (hybrid.py's
    "7c. Seam alignment", added by the "Fix hybrid seam alignment" commit)
    rotated the mesh ring and the parametric theta0 grid by, so that the wall
    resumes from wherever the base replay's toolhead actually ended up rather
    than always the mesh ring's fixed index 0. It depends only on the fixed
    Orca fixture text, scale and center -- never on wall-side args like
    blend_height/seam_style -- so it is the SAME value across every _run()
    call in a given test that shares those three. A test that independently
    recomputes a "what the wall SHOULD look like" reference (via
    top_contour_from_mesh/stack_from_shape, both theta0=0 by default) MUST
    rotate that reference by this same index/angle first, or it is comparing
    the correct, intentionally-rotated real output against a stale,
    unrotated guess -- see test_ring0_is_mesh_top_contour's and
    test_blend_height_edges's own comments for exactly this fix.
    """
    profile = PrinterProfile()
    writer = _new_writer(profile, overrides.get("layer_height", 0.2))
    args = dict(
        tris=(tris if tris is not None else load_stl(str(MESH_FIXTURE))),
        scale=0.1, layer_height=0.2, points_per_turn=60,
        shape_fn=circle(2.5), radius=2.5, height=3.0, blend_height=1.0,
        wall_count=2, infill_density=0.2, infill_pattern="grid",
        orca_path="unused-because-monkeypatched",
        center=(102.5, 102.5), z_amp=0.3, z_waves=3,
    )
    args.update(overrides)
    real_slice = hybrid.slice_stl_to_gcode
    real_wall = hybrid.build_profile_spiral
    real_nri = hybrid._nearest_ring_index
    wall = CapturedWall(real_wall)

    def _traced_nri(ring, target):
        k = real_nri(ring, target)
        wall.seam_k = k
        return k

    hybrid.slice_stl_to_gcode = fake
    hybrid._nearest_ring_index = _traced_nri
    if capture_wall:
        hybrid.build_profile_spiral = wall
    try:
        report = hybrid.build_mesh_hybrid_print(writer, **args)
    finally:
        hybrid.slice_stl_to_gcode = real_slice
        hybrid.build_profile_spiral = real_wall
        hybrid._nearest_ring_index = real_nri
    return report, writer, wall


_DEFAULT_LOOP_SPEC = LoopSpec(loops_per_turn=24, row_mm=0.5, up_mm=0.8,
                              stitch_mode="dip")


def _run_loop(fake, *, tris=None, loop_spec=None, **overrides):
    """Call build_mesh_loop_hybrid_print (Phase 2) with the same standard
    fixture arguments as _run(), plus a LoopSpec for the wall instead of
    z_amp/z_waves -- mirrors _run()'s own docstring/rationale exactly (same
    mesh fixture, same scale/seam-height-agreement arithmetic, same
    load-bearing center)."""
    profile = PrinterProfile()
    writer = _new_writer(profile, overrides.get("layer_height", 0.2))
    args = dict(
        tris=(tris if tris is not None else load_stl(str(MESH_FIXTURE))),
        scale=0.1, layer_height=0.2, points_per_turn=60,
        shape_fn=circle(2.5), radius=2.5, height=3.0, blend_height=1.0,
        loop_spec=(loop_spec if loop_spec is not None else _DEFAULT_LOOP_SPEC),
        wall_count=2, infill_density=0.2, infill_pattern="grid",
        orca_path="unused-because-monkeypatched",
        center=(102.5, 102.5),
    )
    args.update(overrides)
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = fake
    try:
        report = hybrid.build_mesh_loop_hybrid_print(writer, **args)
    finally:
        hybrid.slice_stl_to_gcode = real_slice
    return report, writer


# ---------------------------------------------------------------------------
# Procedural adversarial meshes (built here, not checked in -- each is one
# deliberate counter-example, mirroring tools/fixtures/meshes/README.md's rule
# that every rejection needs a fixture proving it actually fires).
# ---------------------------------------------------------------------------
def _prism(poly, height, zbot=0.0):
    n = len(poly)
    tris = []
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        a = (x0, y0, zbot)
        b = (x1, y1, zbot)
        c = (x1, y1, zbot + height)
        d = (x0, y0, zbot + height)
        tris += [(a, b, c), (a, c, d)]
    for i in range(1, n - 1):
        p0, pi, pj = poly[0], poly[i], poly[i + 1]
        tris.append(((p0[0], p0[1], zbot), (pj[0], pj[1], zbot), (pi[0], pi[1], zbot)))
        tris.append(((p0[0], p0[1], zbot + height),
                     (pi[0], pi[1], zbot + height), (pj[0], pj[1], zbot + height)))
    return tris


def _square(cx, cy, half):
    return [(cx - half, cy - half), (cx + half, cy - half),
            (cx + half, cy + half), (cx - half, cy + half)]


def _two_posts():
    """Two disjoint posts: the top cross-section is TWO loops, so the seam ring
    is undefined (the wall traces one outline)."""
    return _prism(_square(-15.0, 0.0, 5.0), 4.5) + _prism(_square(15.0, 0.0, 5.0), 4.5)


def _flat_mesh():
    """Zero Z extent: nothing to slice into a planar base at all."""
    return [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 8.0, 0.0)),
            ((0.0, 0.0, 0.0), (10.0, 8.0, 0.0), (0.0, 8.0, 0.0))]


def _open_wall():
    """A single vertical triangle: has Z extent but no closed cross-section --
    the not-watertight / not-really-a-solid case."""
    return [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 0.0, 4.5))]


# ---------------------------------------------------------------------------
# 1. Happy path.
# ---------------------------------------------------------------------------
def test_happy_path():
    fake = FakeOrca()
    tris = load_stl(str(MESH_FIXTURE))
    report, writer, _ = _run(fake, tris=tris)

    check(report["hybrid"] is True and report["mesh_base"] is True,
          "happy path: report marks hybrid=True, mesh_base=True")
    check(abs(report["achieved_base_height_mm"] - 0.45) < 1e-12,
          "happy path: achieved_base_height_mm is the mesh's own top (NOT layer-snapped)",
          str(report["achieved_base_height_mm"]))
    check(report["orca_base_layers"] == 2,
          "happy path: orca_base_layers == 2", str(report["orca_base_layers"]))
    check(report["extrude_count"] > 0,
          "happy path: base replay extruded something", str(report["extrude_count"]))
    check("contours" in report,
          "happy path: wall report merged in (has 'contours')", str(list(report.keys())))
    text = writer.text()
    check(text.count("PRINT_START") == 1,
          "happy path: exactly one PRINT_START in the whole print",
          str(text.count("PRINT_START")))
    check(text.count("PRINT_END") == 1,
          "happy path: exactly one PRINT_END in the whole print",
          str(text.count("PRINT_END")))
    check("hybrid: non-planar wall begins here (z=0.4500)" in text,
          "happy path: seam marker comment written at the predicted seam Z")

    # The whole point of the mesh path: Orca slices the user's TRUE solid.
    # A contours_to_mesh round trip would have replaced the fixture's 20
    # triangles with a 60-point-per-ring silhouette extrusion (hundreds).
    n_stl_tris = struct.unpack_from("<I", fake.stl_bytes, 80)[0]
    check(n_stl_tris == len(tris),
          "happy path: the STL handed to Orca is the user's own triangles "
          "(no contours_to_mesh round trip)",
          f"{n_stl_tris} vs {len(tris)}")


# ---------------------------------------------------------------------------
# 2. Height semantics: height is the WALL ABOVE THE MESH, not the whole object.
# ---------------------------------------------------------------------------
def test_height_semantics():
    report, _writer, _ = _run(FakeOrca(), height=3.0)
    check(abs(report["total_height_mm"]
              - (report["achieved_base_height_mm"] + report["wall_height_mm"])) < 1e-12,
          "height semantics: total == mesh base height + design height",
          str((report["total_height_mm"], report["achieved_base_height_mm"],
               report["wall_height_mm"])))
    check(report["total_height_mm"] > report["wall_height_mm"],
          "height semantics: the mesh ADDS to the design height (it is not "
          "carved out of it, unlike build_hybrid_print)",
          str((report["total_height_mm"], report["wall_height_mm"])))
    # The wall's topmost ring sits at base_z + (n_layers-1)*layer_height, one
    # layer short of base_z + height -- documented, and asserted here so a
    # future change to blend_stack's stack length cannot pass unnoticed.
    n_layers = max(1, round(3.0 / 0.2))
    check(abs(report["wall_top_z_mm"]
              - (report["achieved_base_height_mm"] + (n_layers - 1) * 0.2)) < 1e-12,
          "height semantics: wall_top_z_mm == base_z + (n_layers-1)*layer_height",
          str(report["wall_top_z_mm"]))

    # Doubling the design height must not change the base at all.
    tall, _w, _c = _run(FakeOrca(), height=6.0)
    check(abs(tall["achieved_base_height_mm"]
              - report["achieved_base_height_mm"]) < 1e-12,
          "height semantics: the base height is independent of the wall height")

    # amp_envelope/radius_envelope map over the wall segment alone: they must
    # reach build_profile_spiral as the caller's own object, NOT wrapped in a
    # whole-object rescaling closure like build_hybrid_print's _wall_amp_env.
    amp = lambda t: 0.2 + 0.1 * t
    _r, _w, wall = _run(FakeOrca(), amp_envelope=amp, capture_wall=True)
    check(wall.kwargs.get("z_amp_envelope") is amp,
          "height semantics: amp_envelope reaches the wall un-rescaled "
          "(t in [0,1] spans the wall segment alone)",
          repr(wall.kwargs.get("z_amp_envelope")))


# ---------------------------------------------------------------------------
# 3. Ring 0 is the mesh's own top contour, exactly.
# ---------------------------------------------------------------------------
def test_ring0_is_mesh_top_contour():
    tris = load_stl(str(MESH_FIXTURE))
    scaled = [tuple((x * 0.1, y * 0.1, z * 0.1) for (x, y, z) in t) for t in tris]
    expected = top_contour_from_mesh(scaled, 60, z=0.45 - 0.45 * 0.5)

    _report, _writer, wall = _run(FakeOrca(), capture_wall=True)
    # top_contour_from_mesh always samples index 0 at its own fixed theta=0
    # ray. The real wall re-anchors index 0 to wherever the base replay's
    # seam-alignment step (hybrid.py's "7c. Seam alignment") actually left
    # the toolhead -- rolling the array by wall.seam_k, exactly as
    # build_mesh_hybrid_print itself does to mesh_ring before handing it to
    # blend_stack. Without this roll, "expected" is a real ring compared
    # against itself at the wrong starting index -- see _run's own
    # docstring for why this must be the SAME seam_k on both sides.
    expected = expected[wall.seam_k:] + expected[:wall.seam_k]
    got = wall.contours[0]
    worst = max(math.hypot(a[0] - b[0], a[1] - b[1])
                for a, b in zip(got, expected))
    # Not bit-exact, and cannot be: the real path samples the ring AFTER
    # translating the mesh to bed centre, so every coordinate is ~102.5 +- 2.5
    # before the midpoint is subtracted back off -- textbook cancellation at
    # the 1e-15 level. 1e-9 mm is four orders of magnitude below anything a
    # nozzle can express, so the tolerance is not hiding a real offset.
    check(len(got) == 60 and worst < 1e-9,
          "ring 0: the wall's first ring IS the mesh's top contour "
          "(to float noise)", f"worst deviation {worst}")
    check(wall.heights[0] == 0.0 and wall.kwargs["base_z"] == 0.45,
          "ring 0: heights are seam-relative and base_z carries the absolute Z",
          str((wall.heights[0], wall.kwargs.get("base_z"))))
    # Not a circle: proves the seam really came from the hexagon, not from
    # shape_fn (a bug that would make this test vacuous).
    radii = [math.hypot(x, y) for (x, y) in got]
    check(max(radii) - min(radii) > 0.1,
          "ring 0: the seam ring is the hexagon's outline, not the parametric "
          "circle", f"radius spread {max(radii) - min(radii)}")


# ---------------------------------------------------------------------------
# 4. blend_height edge cases (blend_stack's own documented semantics, passed
#    through verbatim -- see build_mesh_hybrid_print's docstring).
# ---------------------------------------------------------------------------
def test_blend_height_edges():
    # blend_height == 0: hard seam. Ring 0 is still the mesh ring; ring 1
    # onward is fully parametric.
    report, _w, wall = _run(FakeOrca(), blend_height=0.0, capture_wall=True)
    # theta0 must match the SAME seam-alignment rotation build_mesh_hybrid_
    # print applied internally (wall.seam_k, constant across every _run()
    # call below -- it depends only on the fixed Orca fixture/scale/center,
    # never on blend_height/seam_style -- see _run's own docstring), or this
    # "parametric" reference is comparing the correctly-rotated real output
    # against a stale, unrotated guess.
    theta0 = 2.0 * math.pi * wall.seam_k / 60
    parametric = stack_from_shape(circle(2.5), 2.5, 3.0, 0.2, 60, theta0=theta0)
    hard = max(math.hypot(a[0] - b[0], a[1] - b[1])
               for a, b in zip(wall.contours[1], parametric[1]))
    check(hard == 0.0,
          "blend_height=0: ring 1 is fully parametric (hard seam)", str(hard))
    check(report["blend_reaches_parametric"] is True
          and report["blend_top_weight"] == 1.0,
          "blend_height=0: report says the wall reaches the parametric shape",
          str((report["blend_top_weight"], report["blend_reaches_parametric"])))

    # blend_height == height: the topmost ring sits at (n_layers-1)*lh = 2.8,
    # NOT at height=3.0, so it is still ~0.99 of the way through the blend --
    # not fully parametric. Reported honestly rather than silently clamped.
    report, _w, wall = _run(FakeOrca(), blend_height=3.0, capture_wall=True)
    check(report["blend_reaches_parametric"] is False
          and 0.9 < report["blend_top_weight"] < 1.0,
          "blend_height==height: the top ring is NOT fully parametric, and the "
          "report says so (no silent clamp)",
          str((report["blend_top_weight"], report["blend_reaches_parametric"])))
    top_gap = max(math.hypot(a[0] - b[0], a[1] - b[1])
                  for a, b in zip(wall.contours[-1], parametric[-1]))
    check(top_gap > 0.0,
          "blend_height==height: the top ring really does still differ from "
          "the parametric ring", str(top_gap))

    # The documented remedy: blend_height = (n_layers-1)*layer_height.
    n_layers = max(1, round(3.0 / 0.2))
    report, _w, wall = _run(FakeOrca(), blend_height=(n_layers - 1) * 0.2,
                            capture_wall=True)
    fixed_gap = max(math.hypot(a[0] - b[0], a[1] - b[1])
                    for a, b in zip(wall.contours[-1], parametric[-1]))
    check(report["blend_reaches_parametric"] is True and fixed_gap == 0.0,
          "blend_height=(n_layers-1)*layer_height: top ring is exactly the "
          "parametric ring (the documented way to get 'fully parametric')",
          str((report["blend_top_weight"], fixed_gap)))

    # blend_height > height: still accepted, still short of parametric.
    report, _w, _c = _run(FakeOrca(), blend_height=30.0)
    check(report["blend_reaches_parametric"] is False,
          "blend_height >> height: accepted, and reported as not fully "
          "parametric", str(report["blend_top_weight"]))

    # Overhang is surfaced as DATA, never gated against a module constant.
    report, _w, _c = _run(FakeOrca(), blend_height=0.0)
    check(report["blend_max_slope"] > 0.0
          and 0.0 < report["blend_max_overhang_deg"] < 90.0,
          "blend diagnostics: a hard seam surfaces its step/slope/overhang as "
          "data for the server layer to judge against slope_ceiling(profile)",
          str((report["blend_max_step_mm"], report["blend_max_slope"],
               report["blend_max_overhang_deg"])))


# ---------------------------------------------------------------------------
# 4b. seam_style/seam_coverage: SEPARATE knobs from blend_height. seam_style
#     picks the transition CURVE over the same span; seam_coverage is what
#     FRACTION of blend_height the curve actually spans before the wall
#     becomes pure parametric.
# ---------------------------------------------------------------------------
def test_seam_style_coverage_edges():
    # Omitting seam_style/seam_coverage must reproduce EXACTLY what explicit
    # seam_style="fillet", seam_coverage=1.0 produces -- this is the whole
    # safety net for check_regression.py's ref_mesh_hybrid_print.gcode staying
    # byte-identical.
    _r1, _w1, wall_default = _run(FakeOrca(), blend_height=1.0, capture_wall=True)
    # Same seam-alignment rotation build_mesh_hybrid_print applied internally
    # (wall_default.seam_k, constant across every _run() call in this test --
    # see _run's own docstring and test_blend_height_edges's identical fix).
    theta0 = 2.0 * math.pi * wall_default.seam_k / 60
    parametric = stack_from_shape(circle(2.5), 2.5, 3.0, 0.2, 60, theta0=theta0)
    _r2, _w2, wall_explicit = _run(
        FakeOrca(), blend_height=1.0, seam_style="fillet", seam_coverage=1.0,
        capture_wall=True)
    for i in (1, 2, 3):
        diff = max(math.hypot(a[0] - b[0], a[1] - b[1])
                   for a, b in zip(wall_default.contours[i], wall_explicit.contours[i]))
        check(diff == 0.0,
              f"seam_style/coverage omitted == explicit fillet/1.0, ring {i}", diff)

    # seam_style="chamfer" at seam_coverage=1.0: a pure LINEAR ramp (w=t), not
    # the default smoothstep curve. Checked at ring 1 (t=0.2, well off the
    # t=0.5 point where linear and smoothstep happen to coincide) against a
    # hand-computed lerp of the mesh ring and the parametric ring -- proves
    # the actual geometry, not just that _blend_weight agrees with itself.
    _r3, _w3, wall_lin = _run(
        FakeOrca(), blend_height=1.0, seam_style="chamfer", seam_coverage=1.0,
        capture_wall=True)
    mesh_ring = wall_lin.contours[0]
    i, t = 1, 1 * 0.2 / 1.0
    expected_linear = [(mx + (px - mx) * t, my + (py - my) * t)
                       for (mx, my), (px, py) in zip(mesh_ring, parametric[i])]
    got = wall_lin.contours[i]
    diff = max(math.hypot(a[0] - b[0], a[1] - b[1])
               for a, b in zip(got, expected_linear))
    check(diff < 1e-9,
          "seam_style=chamfer: ring matches a pure linear lerp(mesh, "
          "parametric, t), not the smoothstep curve", diff)

    divergence = max(math.hypot(a[0] - b[0], a[1] - b[1])
                     for a, b in zip(got, wall_default.contours[i]))
    check(divergence > 1e-3,
          "seam_style=chamfer ring genuinely differs from the default "
          "(fillet) ring at the same layer", divergence)

    # seam_coverage scales the corner extent against blend_height:
    # coverage=0.5 with blend_height=10 must behave IDENTICALLY to
    # blend_height=5 with coverage=1.0 (corner_extent = blend_height *
    # seam_coverage = 5 either way) -- proves seam_coverage is a fraction of
    # blend_height, not an independent mm value.
    _r8, _w8, wall_half_cov = _run(
        FakeOrca(), blend_height=10.0, seam_coverage=0.5, capture_wall=True)
    _r9, _w9, wall_half_span = _run(
        FakeOrca(), blend_height=5.0, seam_coverage=1.0, capture_wall=True)
    for ring_i in range(len(wall_half_cov.contours)):
        diff = max(math.hypot(a[0] - b[0], a[1] - b[1])
                   for a, b in zip(wall_half_cov.contours[ring_i],
                                    wall_half_span.contours[ring_i]))
        check(diff < 1e-9,
              f"seam_coverage=0.5 * blend_height=10 == blend_height=5 * "
              f"coverage=1.0, ring {ring_i}", diff)

    # seam_coverage=0.0 collapses to the existing hard-seam case: ring 0 is
    # the mesh ring, ring 1 onward is exactly the parametric ring.
    _r10, _w10, wall_zero_cov = _run(
        FakeOrca(), blend_height=1.0, seam_coverage=0.0, capture_wall=True)
    diff = max(math.hypot(a[0] - b[0], a[1] - b[1])
               for a, b in zip(wall_zero_cov.contours[1], parametric[1]))
    check(diff == 0.0,
          "seam_coverage=0.0: ring 1 is exactly the parametric ring (hard seam)",
          diff)

    # Not a machine limit -- a fixed [0,1] clamp is correct here (CLAUDE.md's
    # "no machine limit may be a module constant" is about PRINTER ceilings;
    # this is a slicer-cosmetic curve shape).
    _r4, _w4, wall_over = _run(
        FakeOrca(), blend_height=1.0, seam_coverage=1.5, capture_wall=True)
    diff = max(math.hypot(a[0] - b[0], a[1] - b[1])
               for a, b in zip(wall_over.contours[i], wall_default.contours[i]))
    check(diff == 0.0, "seam_coverage=1.5 clamps to 1.0 (same as default)", diff)

    _r5, _w5, wall_under = _run(
        FakeOrca(), blend_height=1.0, seam_coverage=-2.0, capture_wall=True)
    diff = max(math.hypot(a[0] - b[0], a[1] - b[1])
               for a, b in zip(wall_under.contours[i], parametric[i]))
    check(diff == 0.0,
          "seam_coverage=-2.0 clamps to 0.0 (hard seam, same as coverage=0.0)",
          diff)

    # Invalid seam_style is rejected, not silently coerced.
    try:
        _run(FakeOrca(), blend_height=1.0, seam_style="round")
        check(False, "seam_style='round': raises ValueError", "no exception raised")
    except ValueError as e:
        check("seam_style" in str(e), "seam_style='round': raises ValueError",
              str(e)[:120])

    # blend_reaches_parametric / blend_top_weight (the shared-formula
    # diagnostic in hybrid.py) must track whichever curve was actually used --
    # needs blend_height >> height so the topmost ring's t stays strictly
    # inside (0, 1); at t=1 every curve returns exactly 1.0 regardless of
    # style (see _blend_weight), which would make this comparison vacuous.
    r_smooth_top, _w6, _c6 = _run(FakeOrca(), blend_height=30.0, seam_style="fillet")
    r_linear_top, _w7, _c7 = _run(FakeOrca(), blend_height=30.0, seam_style="chamfer")
    check(0.0 < r_smooth_top["blend_top_weight"] < 1.0
          and 0.0 < r_linear_top["blend_top_weight"] < 1.0,
          "sanity: both top weights are mid-blend (not clamped to 0 or 1)",
          str((r_smooth_top["blend_top_weight"], r_linear_top["blend_top_weight"])))
    check(r_smooth_top["blend_top_weight"] != r_linear_top["blend_top_weight"],
          "blend_top_weight differs between seam_style=chamfer and fillet "
          "(same blend_height, different curve)",
          str((r_linear_top["blend_top_weight"], r_smooth_top["blend_top_weight"])))


# ---------------------------------------------------------------------------
# 5. A mesh whose top is two separate loops is refused (the wall traces ONE
#    outline, so the seam would print a shape that is not the model).
# ---------------------------------------------------------------------------
def test_multi_loop_top_is_refused():
    fake = FakeOrca()
    try:
        _run(fake, tris=_two_posts(), scale=1.0, radius=6.0,
             shape_fn=circle(6.0))
        check(False, "multi-loop top: raises ValueError", "no exception raised")
    except ValueError as e:
        check("island" in str(e),
              "multi-loop top: raises ValueError", str(e)[:160])
    check(fake.calls == 0,
          "multi-loop top: refused BEFORE the slice subprocess ran",
          f"slicer called {fake.calls} time(s)")


# ---------------------------------------------------------------------------
# 6. Unsliceable / non-Z-up meshes are refused BEFORE any slice call.
#    fake.calls == 0 is the assertion that proves the pre-flight ordering.
# ---------------------------------------------------------------------------
def test_unsliceable_mesh_refused_before_slicing():
    fake = FakeOrca()
    try:
        _run(fake, tris=_flat_mesh(), scale=1.0)
        check(False, "flat mesh: raises ValueError", "no exception raised")
    except ValueError as e:
        check("no Z extent" in str(e), "flat mesh: raises ValueError", str(e)[:160])
    check(fake.calls == 0,
          "flat mesh: refused BEFORE the slice subprocess ran",
          f"slicer called {fake.calls} time(s)")

    fake = FakeOrca()
    try:
        _run(fake, tris=_open_wall(), scale=1.0)
        check(False, "open (non-watertight) mesh: raises ValueError",
              "no exception raised")
    except ValueError as e:
        check("no printable cross-section" in str(e),
              "open (non-watertight) mesh: raises ValueError", str(e)[:160])
    check(fake.calls == 0,
          "open (non-watertight) mesh: refused BEFORE the slice subprocess ran",
          f"slicer called {fake.calls} time(s)")

    # And the same for a footprint that does not fit the bed.
    fake = FakeOrca()
    try:
        _run(fake, scale=20.0)
        check(False, "oversize footprint: raises ValueError", "no exception raised")
    except ValueError as e:
        check("outside this printer" in str(e),
              "oversize footprint: raises ValueError", str(e)[:160])
    check(fake.calls == 0,
          "oversize footprint: refused BEFORE the slice subprocess ran",
          f"slicer called {fake.calls} time(s)")


# ---------------------------------------------------------------------------
# 7. Orca disagreeing with the predicted seam height RAISES -- a silently
#    added or dropped layer would leave the wall's first ring on a surface it
#    does not match.
# ---------------------------------------------------------------------------
def test_orca_z_drift_raises():
    # scale=1.0 -> a 4.5 mm mesh -> a predicted seam at the mesh's own top,
    # 4.5 mm (Orca adapts its layers to land on the model height; this path
    # deliberately does NOT layer-snap -- see build_mesh_hybrid_print step 2),
    # while the Orca fixture's own extruding moves top out at 0.4 mm.
    fake = FakeOrca()
    try:
        _run(fake, scale=1.0, radius=25.0, shape_fn=circle(25.0))
        check(False, "Orca Z drift: raises ValueError", "no exception raised")
    except ValueError as e:
        check("tops out" in str(e) and "4.5" in str(e),
              "Orca Z drift: raises ValueError naming both heights", str(e)[:200])
    check(fake.calls == 1,
          "Orca Z drift: detected AFTER slicing (the slice did run)",
          f"slicer called {fake.calls} time(s)")

    # A base with no extruding moves at all is refused rather than printing a
    # wall floating in mid-air.
    fake = FakeOrca(text=";LAYER_CHANGE\n;Z:0.20\nM83\nG92 E0\nG1 X100 Y100 Z0.2 F600\n")
    try:
        _run(fake)
        check(False, "empty Orca base: raises ValueError", "no exception raised")
    except ValueError as e:
        check("no extruding moves" in str(e),
              "empty Orca base: raises ValueError", str(e)[:160])


# ---------------------------------------------------------------------------
# 8. Boundary rejection, not clamping (CLAUDE.md).
# ---------------------------------------------------------------------------
def test_non_finite_inputs_are_rejected():
    for name, override in (("scale", {"scale": float("nan")}),
                           ("height", {"height": float("inf")}),
                           ("blend_height", {"blend_height": float("nan")}),
                           ("seam_coverage", {"seam_coverage": float("nan")}),
                           ("layer_height", {"layer_height": float("inf")})):
        fake = FakeOrca()
        try:
            _run(fake, **override)
            check(False, f"non-finite {name}: rejected at the boundary",
                  "no exception raised")
        except ValueError as e:
            check("finite" in str(e), f"non-finite {name}: rejected at the boundary",
                  str(e)[:120])
        check(fake.calls == 0,
              f"non-finite {name}: rejected before the slice subprocess ran",
              f"slicer called {fake.calls} time(s)")


# ---------------------------------------------------------------------------
# 6. Zone Overrides reach the wall above a mesh-hybrid base (same
#    **profile_spiral_kwargs passthrough as the parametric hybrid path --
#    build_mesh_hybrid_print itself needed no signature change) but never
#    the mesh base / seam portion below it.
# ---------------------------------------------------------------------------
def test_zone_overrides_reach_the_wall_not_the_mesh_base():
    zone = ZoneOverride(t_lo=0.3, t_hi=0.6, blend=0.02, r_pattern="vwave", r_amp=1.0)
    _r1, w1, _c1 = _run(FakeOrca(), zones=None)
    text1 = w1.text()
    _r2, w2, _c2 = _run(FakeOrca(), zones=[zone])
    text2 = w2.text()

    check(text1 != text2,
          "zone overrides: a real texture zone changes the mesh-hybrid "
          "wall's G-code")
    base1 = text1.split("; wall spiral")[0]
    base2 = text2.split("; wall spiral")[0]
    check(base1 == base2,
          "zone overrides: the mesh base/seam portion (before the wall "
          "spiral) is byte-identical whether or not a wall zone is set")


# ---------------------------------------------------------------------------
# fan_off_layers is a TOTAL count from the absolute start of the print (mesh
# base + wall combined), not restarted at the wall's own layer 0 -- same fix
# as tools/test_hybrid.py's identically-named test, for
# build_mesh_hybrid_print(). The default fixture (scale=0.1 -> 0.45 mm mesh,
# layer_height=0.2) gives n_base_layers = max(1, round(0.45/0.2)) = 2.
# ---------------------------------------------------------------------------
def test_fan_off_layers_counts_from_the_base():
    seam_marker = "; hybrid: non-planar wall begins here"

    def _fan_on_lands_before_wall_spiral(text):
        after_seam = text.split(seam_marker, 1)[1]
        m_idx = after_seam.find("M106")
        spiral_idx = after_seam.find("; wall spiral")
        return m_idx != -1 and (spiral_idx == -1 or m_idx < spiral_idx)

    def _wall_points_before_fan_on(text):
        wall_text = text.split("; wall spiral", 1)[1]
        m_idx = wall_text.find("M106")
        if m_idx == -1:
            return None
        before = wall_text[:m_idx]
        return sum(1 for ln in before.splitlines()
                   if ln.startswith("G1") and " E" in ln)

    # fan_off_layers=0 (unset/default): the mesh base (2 layers) already
    # satisfies "0 layers off" -- fan must be on immediately at the seam.
    _r0, w0, _c0 = _run(FakeOrca(), fan_off_layers=0)
    text0 = w0.text()
    check(_fan_on_lands_before_wall_spiral(text0),
          "fan_off_layers=0: fan turns on immediately at the mesh seam, "
          "not one wall turn later")

    # fan_off_layers=1: still <= n_base_layers(2) -- also immediate.
    _r1, w1, _c1 = _run(FakeOrca(), fan_off_layers=1)
    text1 = w1.text()
    check(_fan_on_lands_before_wall_spiral(text1),
          "fan_off_layers=1 (<= the mesh base's own 2 layers): fan still "
          "turns on immediately at the seam")

    # fan_off_layers=5: n_base_layers(2) + 3 -- the mesh base already covers
    # 2 of the 5 requested, so the wall should wait exactly 3 more full
    # turns (3 * points_per_turn = 180 points), not all 5.
    _r2, w2, _c2 = _run(FakeOrca(), fan_off_layers=5)
    text2 = w2.text()
    n_before = _wall_points_before_fan_on(text2)
    check(n_before == 3 * 60,
          "fan_off_layers=5 with a 2-layer mesh base: fan turns on exactly "
          "3 wall turns (180 points) into the wall, not 5",
          str(n_before))

    # The mesh base/seam portion itself never carries a fan call, and is
    # identical regardless of fan_off_layers.
    base0 = text0.split(seam_marker, 1)[0]
    base2 = text2.split(seam_marker, 1)[0]
    check(base0 == base2,
          "fan_off_layers: the mesh base/seam portion (before the seam "
          "marker) is byte-identical regardless of the fan setting")
    check("M106" not in base0,
          "fan_off_layers: the mesh base portion itself never carries a "
          "fan-on call of its own")


# ---------------------------------------------------------------------------
# Phase 2: build_mesh_loop_hybrid_print -- a real Orca-sliced MESH planar
# base RESUMED into a Loop Fabric wall, blended in from the mesh's own seam
# outline (loop_fabric.py's new blend_ring/blend_height/seam_style/
# seam_coverage kwargs). Mirrors tools/test_hybrid.py's own Loop Fabric
# hybrid tests (test_loop_hybrid_*), adapted to this file's mesh-fixture
# harness.
# ---------------------------------------------------------------------------
def test_mesh_loop_happy_path():
    fake = FakeOrca()
    report, writer = _run_loop(fake)

    check(report["hybrid"] is True and report["loop_hybrid"] is True
          and report["mesh_base"] is True,
          "mesh loop happy path: report marks hybrid=True, loop_hybrid=True, "
          "mesh_base=True", str(report))
    check(abs(report["achieved_base_height_mm"] - 0.45) < 1e-12,
          "mesh loop happy path: achieved_base_height_mm is the mesh's own "
          "top (NOT layer-snapped)", str(report["achieved_base_height_mm"]))
    check(report["orca_base_layers"] == 2,
          "mesh loop happy path: orca_base_layers == 2",
          str(report["orca_base_layers"]))
    check(report["extrude_count"] > 0,
          "mesh loop happy path: base replay extruded something",
          str(report["extrude_count"]))
    check("fabric_rows" in report and report["fabric_rows"] > 0,
          "mesh loop happy path: wall report merged in (fabric_rows > 0)",
          str(report.get("fabric_rows")))
    check(report["points"] > 0,
          "mesh loop happy path: wall's own points field is honest and "
          "positive (no cuff points counted -- resume=True skips the cuff)",
          str(report["points"]))
    check(abs(report["total_height_mm"]
              - (report["achieved_base_height_mm"] + report["wall_height_mm"])) < 1e-12,
          "mesh loop happy path: total == mesh base height + wall height "
          "(the mesh-hybrid height convention, not build_loop_hybrid_print's)",
          str((report["total_height_mm"], report["achieved_base_height_mm"],
               report["wall_height_mm"])))

    text = writer.text()
    check(text.count("PRINT_START") == 1,
          "mesh loop happy path: exactly one PRINT_START in the whole print",
          str(text.count("PRINT_START")))
    check(text.count("PRINT_END") == 1,
          "mesh loop happy path: exactly one PRINT_END in the whole print",
          str(text.count("PRINT_END")))
    check("anchor cuff" not in text,
          "mesh loop happy path: no solid cuff is printed (the mesh planar "
          "base anchors row 0 instead)")
    seam_marker = "; hybrid: non-planar wall begins here"
    check(seam_marker in text,
          "mesh loop happy path: seam marker present in the output")
    check("; loop fabric (" in text.split(seam_marker, 1)[1],
          "mesh loop happy path: real loop-fabric stitch content follows "
          "the seam marker")

    n_stl_tris = struct.unpack_from("<I", fake.stl_bytes, 80)[0]
    tris = load_stl(str(MESH_FIXTURE))
    check(n_stl_tris == len(tris),
          "mesh loop happy path: the STL handed to Orca is the user's own "
          "triangles (no contours_to_mesh round trip)",
          f"{n_stl_tris} vs {len(tris)}")


# ---------------------------------------------------------------------------
# Placement mismatch still refuses -- shared _slice_replay_and_seam, same
# check as the parametric mesh-hybrid wall's own.
# ---------------------------------------------------------------------------
def test_mesh_loop_placement_mismatch_is_refused():
    fake = FakeOrca()
    profile = PrinterProfile()
    try:
        _run_loop(fake, center=profile.bed_center)  # far from the fixture's ~(102.5, 102.5)
        check(False, "mesh loop placement mismatch: raises ValueError",
              "no exception raised")
    except ValueError as e:
        check("off-target" in str(e),
              "mesh loop placement mismatch: raises ValueError", str(e)[:160])


# ---------------------------------------------------------------------------
# Orca disagreeing with the predicted seam height RAISES -- same check as
# test_orca_z_drift_raises() above, exercised through the loop-fabric entry
# point instead of the parametric one.
# ---------------------------------------------------------------------------
def test_mesh_loop_seam_drift_raises():
    fake = FakeOrca()
    try:
        _run_loop(fake, scale=1.0, radius=25.0, shape_fn=circle(25.0))
        check(False, "mesh loop Orca Z drift: raises ValueError",
              "no exception raised")
    except ValueError as e:
        check("tops out" in str(e) and "4.5" in str(e),
              "mesh loop Orca Z drift: raises ValueError naming both heights",
              str(e)[:200])
    check(fake.calls == 1,
          "mesh loop Orca Z drift: detected AFTER slicing (the slice did run)",
          f"slicer called {fake.calls} time(s)")


# ---------------------------------------------------------------------------
# The fan turns on immediately at the seam -- same rationale as
# build_loop_hybrid_print's own "5b. Fan" step (Loop Fabric has no
# fan_off_layers ramp of its own), reused verbatim by build_mesh_loop_
# hybrid_print.
# ---------------------------------------------------------------------------
def test_mesh_loop_fan_turns_on_at_the_seam():
    fake = FakeOrca()
    _report, writer = _run_loop(fake)
    text = writer.text()
    seam_marker = "; hybrid: non-planar wall begins here"
    after_seam = text.split(seam_marker, 1)[1]
    m_idx = after_seam.find("M106")
    fabric_idx = after_seam.find("; loop fabric (")
    check(m_idx != -1 and (fabric_idx == -1 or m_idx < fabric_idx),
          "mesh loop hybrid: fan (M106) turns on at the seam, before the "
          "fabric rows begin")


# ---------------------------------------------------------------------------
# The wall's shape function really blends the mesh ring in at the seam: row
# 0's own points (out_mm=0, so the emitted radius is EXACTLY _radius(theta,
# line_z) with no outward-lean term to subtract first -- see loop_fabric.py's
# _emit) must match mesh_r + w*(para_r - mesh_r) for a SINGLE weight w shared
# by the whole row -- _emit always calls _radius with the ROW's own nominal
# line height (loop_fabric.py's `line0`), not the per-sample dipped Z, so
# every stitch in row 0 blends at the SAME height and therefore the SAME w.
# w is computed independently here from the same _blend_weight/corner_extent
# formula loop_fabric.py itself uses, so this checks the actual geometry
# against the documented formula, not against itself.
# ---------------------------------------------------------------------------
def test_mesh_loop_blend_matches_weighted_interpolation():
    fake = FakeOrca()
    tris = load_stl(str(MESH_FIXTURE))
    scaled = [tuple((x * 0.1, y * 0.1, z * 0.1) for (x, y, z) in t) for t in tris]
    mesh_ring = top_contour_from_mesh(scaled, 60, z=0.45 - 0.45 * 0.5)

    clean_spec = LoopSpec(loops_per_turn=24, row_mm=0.5, up_mm=0.8,
                          stitch_mode="dip", out_mm=0.0)
    report, writer = _run_loop(fake, loop_spec=clean_spec)
    base_z = report["achieved_base_height_mm"]
    # loop_fabric.py's own dip-mode line0 formula, substituting base_z for
    # cuff_top (see build_loop_fabric's own "vertical layout" comments):
    # line0 = base_z + loop_h - cuff_hook, with wave_amp=0 (this spec's
    # default) and cuff_hook = min(_CUFF_HOOK_MM, 0.4 * loop_h).
    loop_h = clean_spec.up_mm
    cuff_hook = min(0.8, 0.4 * loop_h)
    line0 = base_z + loop_h - cuff_hook
    from trident_gcode.profile_stack import _blend_weight
    corner_extent = 1.0 * 1.0  # blend_height=1.0, seam_coverage=1.0 (defaults)
    t_blend = min(1.0, max(0.0, (line0 - base_z) / corner_extent))
    w = _blend_weight(t_blend, 1.0)  # seam_style="fillet" (default) -> intensity 1.0
    check(0.0 < w < 1.0,
          "mesh loop blend: row 0's own line height sits mid-blend (not "
          "clamped to 0 or 1), so this is a genuine test of the formula",
          str(w))

    text = writer.text()
    seam_marker = "; hybrid: non-planar wall begins here"
    fabric_marker = "; loop fabric ("
    after_fabric = text.split(seam_marker, 1)[1].split(fabric_marker, 1)[1]
    row_text = after_fabric.split("; row 1/", 1)[0]
    cx = cy = 102.5

    worst = 0.0
    n_checked = 0
    for line in row_text.splitlines():
        if not line.startswith("G1 X"):
            continue
        parts = line.split()
        x = float(parts[1][1:])
        y = float(parts[2][1:])
        theta = math.atan2(y - cy, x - cx)
        r_actual = math.hypot(x - cx, y - cy)
        r_mesh = mesh_ring_radius_at(mesh_ring, theta)
        r_expected = r_mesh + w * (2.5 - r_mesh)
        worst = max(worst, abs(r_actual - r_expected))
        n_checked += 1
    check(n_checked > 0,
          "mesh loop blend: found row-0 stitch points to check", str(n_checked))
    # Tolerance, not exactness: G-code coordinates are text-formatted to 4
    # decimal places (GcodeWriter's own precision), so reconstructing r from
    # parsed X/Y loses up to ~1e-4 mm to that rounding alone -- unrelated to
    # the blend math itself, which is otherwise checked exactly.
    check(worst < 2e-4,
          "mesh loop blend: row 0's actual radius matches "
          "mesh_r + w*(parametric_r - mesh_r) for every stitch sample, "
          "using the SAME w loop_fabric.py's own formula computes "
          "(tolerance accounts for G-code text rounding only)",
          f"worst deviation {worst}")

    # And genuinely a HEXAGON-shaped blend, not a circle-shaped one: mesh_r
    # varies enough across theta that the blended row is not itself a
    # perfect circle (a bug that would make the check above vacuous, since a
    # circle blended with a circle looks like a circle regardless of w).
    radii = [mesh_ring_radius_at(mesh_ring, 2.0 * math.pi * j / 360)
             for j in range(360)]
    check(max(radii) - min(radii) > 0.05,
          "mesh loop blend: the mesh ring genuinely is not a circle "
          "(otherwise this test could not distinguish a real blend from a "
          "bug that ignored blend_ring entirely)",
          str(max(radii) - min(radii)))


# ---------------------------------------------------------------------------
# EXPLICIT Z-CLAMP (loop_fabric.py's own blend_ring docstring): for any
# z <= base_z the blend weight clamps to EXACTLY 0 (pure blend_ring), never
# extrapolated toward the parametric shape. Row/stitch geometry itself never
# actually samples z below base_z for the RADIUS blend (_emit always blends
# at the row's own nominal line height, which sits above base_z by
# construction for every shipped LoopSpec) -- the one place build_loop_fabric
# really does query z <= base_z is the "move to fabric start" point
# (`sx = cx + _radius(seam_theta, 0.0)`), which this test exercises directly
# via a raw build_loop_fabric(resume=True, ...) call (bypassing the whole
# mesh-hybrid orchestration, since only the blend math is under test here).
# ---------------------------------------------------------------------------
def test_loop_fabric_blend_z_clamp_below_seam():
    import trident_gcode.hybrid as hybrid  # noqa: F401 -- ensures package import order
    from trident_gcode.generators.loop_fabric import build_loop_fabric
    from trident_gcode.paths import circle as circle_shape

    tris = load_stl(str(MESH_FIXTURE))
    scaled = [tuple((x * 0.1, y * 0.1, z * 0.1) for (x, y, z) in t) for t in tris]
    mesh_ring = top_contour_from_mesh(scaled, 60, z=0.45 - 0.45 * 0.5)

    profile = PrinterProfile()
    writer = _new_writer(profile, layer_height=0.2)
    base_z = 0.45
    build_loop_fabric(
        writer, shape=circle_shape(2.5), height=3.0,
        spec=LoopSpec(loops_per_turn=24, row_mm=0.5, up_mm=0.8,
                     stitch_mode="dip", out_mm=0.0),
        points_per_turn=60, center=(102.5, 102.5),
        resume=True, base_z=base_z, seam_theta=0.0, fan_already_on=True,
        blend_ring=mesh_ring, blend_height=1.0,
        seam_style="fillet", seam_coverage=1.0,
    )
    text = writer.text()
    # "move to fabric start" travels to (sx, cy, ...) BEFORE any extrusion --
    # sx = cx + _radius(seam_theta=0.0, z=0.0), and z=0.0 < base_z=0.45
    # unconditionally, so this must be EXACTLY the mesh ring's own radius at
    # theta=0, never blended toward the parametric circle(2.5).
    move_line = next(ln for ln in text.splitlines() if "; travel" in ln and "F" in ln
                     and ln.startswith("G1 X"))
    sx = float(move_line.split()[1][1:])
    expected_r = mesh_ring_radius_at(mesh_ring, 0.0)
    check(abs((sx - 102.5) - expected_r) < 1e-9,
          "z-clamp: at z=0.0 (below base_z=0.45), _radius returns EXACTLY "
          "the mesh ring's own radius, not blended toward the parametric "
          "circle(2.5)",
          f"sx-cx={sx - 102.5}, expected={expected_r}, "
          f"parametric would be 2.5")


def main() -> int:
    if not MESH_FIXTURE.exists():
        print(f"FAIL  mesh fixture missing: {MESH_FIXTURE}")
        return 1
    test_happy_path()
    test_height_semantics()
    test_ring0_is_mesh_top_contour()
    test_blend_height_edges()
    test_seam_style_coverage_edges()
    test_multi_loop_top_is_refused()
    test_unsliceable_mesh_refused_before_slicing()
    test_orca_z_drift_raises()
    test_non_finite_inputs_are_rejected()
    test_zone_overrides_reach_the_wall_not_the_mesh_base()
    test_fan_off_layers_counts_from_the_base()
    test_mesh_loop_happy_path()
    test_mesh_loop_placement_mismatch_is_refused()
    test_mesh_loop_seam_drift_raises()
    test_mesh_loop_fan_turns_on_at_the_seam()
    test_mesh_loop_blend_matches_weighted_interpolation()
    test_loop_fabric_blend_z_clamp_below_seam()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
