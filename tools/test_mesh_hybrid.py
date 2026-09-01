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
from trident_gcode.gcode import GcodeWriter
from trident_gcode.mesh import load_stl
from trident_gcode.paths import circle
from trident_gcode.profile import PrinterProfile
from trident_gcode.profile_stack import stack_from_shape, top_contour_from_mesh


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
    """Wraps build_profile_spiral to record the contour stack it was handed."""

    def __init__(self, real):
        self.real = real
        self.contours = None
        self.heights = None
        self.kwargs = None

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
    wall = CapturedWall(real_wall)
    hybrid.slice_stl_to_gcode = fake
    if capture_wall:
        hybrid.build_profile_spiral = wall
    try:
        report = hybrid.build_mesh_hybrid_print(writer, **args)
    finally:
        hybrid.slice_stl_to_gcode = real_slice
        hybrid.build_profile_spiral = real_wall
    return report, writer, wall


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
    parametric = stack_from_shape(circle(2.5), 2.5, 3.0, 0.2, 60)

    # blend_height == 0: hard seam. Ring 0 is still the mesh ring; ring 1
    # onward is fully parametric.
    report, _w, wall = _run(FakeOrca(), blend_height=0.0, capture_wall=True)
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


def main() -> int:
    if not MESH_FIXTURE.exists():
        print(f"FAIL  mesh fixture missing: {MESH_FIXTURE}")
        return 1
    test_happy_path()
    test_height_semantics()
    test_ring0_is_mesh_top_contour()
    test_blend_height_edges()
    test_multi_loop_top_is_refused()
    test_unsliceable_mesh_refused_before_slicing()
    test_orca_z_drift_raises()
    test_non_finite_inputs_are_rejected()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
