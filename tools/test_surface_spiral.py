#!/usr/bin/env python3
"""Functional (non-byte-exact) tests for the surface/conformal generator.

Plain ``python tools/test_surface_spiral.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors tools/test_hybrid.py's style.

Covers the bed-adhesion package added to
``trident_gcode/generators/surface_spiral.py`` on 2026-09-12 -- before it, the
conformal shell was laid as a single thin spiral straight onto bare glass, the
only print mode in the app with no adhesion at all.

Byte-exactness is check_regression.py's job (``ref_surface_spiral.gcode`` for
the defaults-are-a-no-op claim, ``ref_surface_spiral_adhesion.gcode`` for the
new geometry). What is checked HERE is behaviour: that the defaults really are
inert, that a base/brim/squish actually appear when asked for, that the shell
lands exactly one layer above its own base (the geometric risk of the whole
change -- too low and the nozzle ploughs back through the base, too high and
the shell prints into thin air), and that bad numbers are refused at the
boundary rather than clamped into something that looks plausible.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.profile import PrinterProfile
from trident_gcode.gcode import GcodeWriter
from trident_gcode.surface import dome, ripple
from trident_gcode.generators.surface_spiral import (
    build_surface_spiral, surface_spiral_geometry)


_FAILURES: list[str] = []

LH = 0.3
LW = 0.45
RADIUS = 12.0
SEAM = "; move to surface spiral start (centre)"


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _new_writer(profile=None) -> GcodeWriter:
    return GcodeWriter(
        profile=profile or PrinterProfile(), line_width=LW, layer_height=LH,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def _build(**kw):
    """Build the standard test shell; returns (report, gcode_text)."""
    writer = _new_writer(kw.pop("profile", None))
    field = kw.pop("field", dome(RADIUS, 4.0))
    radius = kw.pop("radius", RADIUS)
    kw.setdefault("shells", 2)
    report = build_surface_spiral(writer, field, radius, **kw)
    return report, writer.text()


def _extrudes(text: str) -> list[tuple[float, float, float]]:
    """(x, y, z) of every EXTRUDING move. Retracts ("G1 E-0.6 F...") carry no
    XYZ and are skipped by the X/Y/Z requirement, as are pure travels by the
    E requirement."""
    out = []
    for ln in text.splitlines():
        if not ln.startswith("G1 "):
            continue
        parts = ln.split(";")[0].split()
        vals = {}
        for p in parts[1:]:
            if p and p[0] in "XYZE":
                try:
                    vals[p[0]] = float(p[1:])
                except ValueError:
                    pass
        if {"X", "Y", "Z", "E"} <= set(vals):
            out.append((vals["X"], vals["Y"], vals["Z"]))
    return out


def _split_at_seam(text: str) -> tuple[str, str]:
    """(everything before the shell's own start, the shell itself)."""
    head, _, tail = text.partition(SEAM)
    return head, tail


def _raises(fn, label: str, needle: str = "") -> None:
    try:
        fn()
    except ValueError as e:
        if needle and needle not in str(e):
            check(False, label, f"wrong message: {e}")
        else:
            check(True, label)
        return
    except Exception as e:  # noqa: BLE001 -- a TypeError here is still a bug
        check(False, label, f"raised {type(e).__name__}, expected ValueError: {e}")
        return
    check(False, label, "no exception raised")


# ---------------------------------------------------------------------------
# 1. Defaults are a no-op: the whole package has to be inert until asked for.
#    (check_regression.py proves this byte-for-byte against a reference
#    generated from PRE-change code; this is the in-memory companion.)
# ---------------------------------------------------------------------------
def test_defaults_are_a_no_op():
    _, bare = _build()
    _, spelled_out = _build(
        first_layer_squish=1.0,
        first_layer_spacing_factor=1.0,
        first_layer_flow=1.0,
        base_layers=0,
        brim_loops=0,
        base_style="spiral",
        base_points_per_turn=240,
        resolution=0.4,
    )
    check(bare == spelled_out,
          "defaults: passing every adhesion control at its documented default "
          "is byte-identical to passing none of them")
    check("solid base" not in bare,
          "defaults: no base/brim section is emitted")
    zs = [z for (_, _, z) in _extrudes(bare)]
    check(abs(min(zs) - LH) < 1e-12,
          "defaults: the shell still starts at the nominal layer height "
          "(no squish applied behind the caller's back)", str(min(zs)))

    # first_layer_flow ALONE must not arm the package -- same contract as
    # build_continuous_spiral, where flow only parameterises an already-active
    # adhesion package. Otherwise a UI that always sends a flow value would
    # silently move every default print's Z.
    _, flow_only = _build(first_layer_flow=1.4)
    check(bare == flow_only,
          "defaults: first_layer_flow on its own does NOT arm the adhesion "
          "package (it cannot perturb default output)")


# ---------------------------------------------------------------------------
# 2. A solid base disk is actually emitted when asked for.
# ---------------------------------------------------------------------------
def test_base_disk_is_emitted():
    rep0, text0 = _build()
    rep2, text2 = _build(base_layers=2)

    check("; solid base (2 layers) + brim (0 loops)" in text2,
          "base disk: the base section is announced in the G-code")
    check(rep2["base_layers"] == 2 and rep2["base_points"] > 0,
          "base disk: the report counts the base and its points",
          f"{rep2['base_layers']} layers, {rep2['base_points']} points")
    check(rep0["base_points"] == 0,
          "base disk: no base points at all without one", str(rep0["base_points"]))

    head, _ = _split_at_seam(text2)
    base_moves = _extrudes(head)
    check(len(base_moves) == rep2["base_points"],
          "base disk: every reported base point is a real extruding move",
          f"{len(base_moves)} moves vs {rep2['base_points']} reported")

    # Two stacked disks -> exactly two distinct base Z plateaus, one layer apart.
    flz = rep2["first_layer_z_mm"]
    zs = sorted({round(z, 4) for (_, _, z) in base_moves})
    check(abs(min(zs) - flz) < 1e-9 and abs(max(zs) - (flz + LH)) < 1e-9,
          "base disk: the two disks span first_layer_z .. first_layer_z + "
          "layer_height", f"{min(zs)} .. {max(zs)} (first_layer_z={flz})")

    # The disk actually PAVES the footprint rather than tracing its outline:
    # some base move must land near the centre.
    cx, cy = PrinterProfile().bed_center
    r_min = min(math.hypot(x - cx, y - cy) for (x, y, _) in base_moves)
    check(r_min < LW * 2.0,
          "base disk: the disk is paved to the centre, not just outlined",
          f"closest base move sits {r_min:.3f} mm from the centre")


# ---------------------------------------------------------------------------
# 3. Brim loops are emitted, and they reach beyond the shell's footprint.
# ---------------------------------------------------------------------------
def test_brim_loops_are_emitted():
    rep, text = _build(base_layers=1, brim_loops=3, first_layer_squish=0.75,
                       first_layer_spacing_factor=1.25)
    check("brim (3 loops)" in text, "brim: the brim is announced in the G-code")

    cx, cy = PrinterProfile().bed_center
    head, tail = _split_at_seam(text)
    r_base = max(math.hypot(x - cx, y - cy) for (x, y, _) in _extrudes(head))
    r_shell = max(math.hypot(x - cx, y - cy) for (x, y, _) in _extrudes(tail))
    check(r_base > r_shell + 1e-6,
          "brim: the brim reaches beyond the shell's own footprint",
          f"base/brim reach {r_base:.3f} mm vs shell {r_shell:.3f} mm")

    # s0 = line_width / squish * spacing_factor = 0.45/0.75*1.25 = 0.75 mm, so
    # 3 loops reach 12 + 3*0.75 = 14.25 mm -- and the report's footprint (which
    # feeds the bed-fit check) must be that grown figure, not the bare radius.
    check(abs(rep["footprint_radius_mm"] - 14.25) < 1e-6,
          "brim: the reported footprint radius includes the brim's reach",
          str(rep["footprint_radius_mm"]))
    check(r_base <= rep["footprint_radius_mm"] + 1e-6,
          "brim: no emitted move escapes the reported footprint",
          f"{r_base:.4f} > {rep['footprint_radius_mm']}")

    rep0, _ = _build()
    check(abs(rep0["footprint_radius_mm"] - RADIUS) < 1e-9,
          "brim: with no brim the footprint is still the bare radius",
          str(rep0["footprint_radius_mm"]))


# ---------------------------------------------------------------------------
# 4. The first layer really is squished into the plate.
# ---------------------------------------------------------------------------
def test_first_layer_is_squished():
    rep, text = _build(first_layer_squish=0.75)
    zs = [z for (_, _, z) in _extrudes(text)]
    check(abs(min(zs) - 0.75 * LH) < 1e-9,
          "squish: the lowest extruding move sits at squish * layer_height",
          f"{min(zs)} vs {0.75 * LH}")
    check(abs(rep["first_layer_z_mm"] - 0.75 * LH) < 1e-9,
          "squish: the report agrees", str(rep["first_layer_z_mm"]))

    # With a base under it, it is the BASE that gets squished into the plate --
    # the shell above is an ordinary layer on solid plastic.
    rep_b, text_b = _build(first_layer_squish=0.75, base_layers=2)
    head, tail = _split_at_seam(text_b)
    check(abs(min(z for (_, _, z) in _extrudes(head)) - 0.225) < 1e-9,
          "squish: with a base, it is the base's first disk that is squished")
    check(min(z for (_, _, z) in _extrudes(tail)) > 0.225 + 1e-6,
          "squish: the shell above a base is not squished into anything")

    # An explicit first_layer_z still wins (documented override), so a caller
    # that forgets to drop it cannot be told the squish took effect.
    rep_x, _ = _build(first_layer_squish=0.75, first_layer_z=LH)
    check(abs(rep_x["first_layer_z_mm"] - LH) < 1e-12,
          "squish: an explicit first_layer_z overrides the squish-derived "
          "height, and the report reports the truth",
          str(rep_x["first_layer_z_mm"]))


# ---------------------------------------------------------------------------
# 5. THE geometric risk of the whole change: the shell must land exactly one
#    layer above its own base -- not into it, not floating above it.
# ---------------------------------------------------------------------------
def test_shell_sits_exactly_one_layer_above_its_base():
    for base_layers in (0, 1, 2, 4):
        rep, text = _build(first_layer_squish=0.75, base_layers=base_layers,
                           brim_loops=2 if base_layers else 0)
        flz = rep["first_layer_z_mm"]
        expected_floor = flz + base_layers * LH
        check(abs(rep["shell_floor_z_mm"] - expected_floor) < 1e-9,
              f"seam (base_layers={base_layers}): reported shell floor is "
              f"first_layer_z + base_layers*lh",
              f"{rep['shell_floor_z_mm']} vs {expected_floor}")

        head, tail = _split_at_seam(text)
        shell = _extrudes(tail)
        check(shell, f"seam (base_layers={base_layers}): the shell emitted moves")
        shell_floor = min(z for (_, _, z) in shell)
        check(abs(shell_floor - expected_floor) < 1e-9,
              f"seam (base_layers={base_layers}): the shell's LOWEST emitted "
              f"move is at that floor",
              f"{shell_floor} vs {expected_floor}")

        if base_layers == 0:
            check(abs(shell_floor - flz) < 1e-12,
                  "seam (base_layers=0): with nothing under it the shell IS "
                  "the first layer -- no phantom lift",
                  f"{shell_floor} vs {flz}")
            continue

        base_top = max(z for (_, _, z) in _extrudes(head) if z <= flz + base_layers * LH)
        check(abs(shell_floor - base_top - LH) < 1e-9,
              f"seam (base_layers={base_layers}): the gap between the top base "
              f"disk and the shell is exactly one layer height (no collision, "
              f"no float)", f"gap {shell_floor - base_top:.6f} mm")


# ---------------------------------------------------------------------------
# 6. The travel back to the shell's start clears the finished base. The shell
#    begins at the CENTRE while the base/brim ends on the OUTLINE, so this is
#    the one unavoidable travel in the mode -- it must lift, not drag.
# ---------------------------------------------------------------------------
def test_seam_travel_clears_the_base():
    rep, text = _build(first_layer_squish=0.75, base_layers=2, brim_loops=2)
    head, tail = _split_at_seam(text)
    base_top = max(z for (_, _, z) in _extrudes(head))

    travels = []
    for ln in tail.splitlines():
        if "; travel" not in ln:
            continue
        z = next((float(p[1:]) for p in ln.split() if p.startswith("Z")), None)
        if z is not None:
            travels.append(z)
        if ln.startswith("G1") and " E" in ln:
            break
    check(travels, "seam travel: a travel sequence follows the seam comment")
    check(max(travels) >= base_top + 1e-6,
          "seam travel: the nozzle climbs clear of the finished base before "
          "crossing back to the centre",
          f"peak travel Z {max(travels) if travels else None} vs base top {base_top}")
    check("G1 E-" in tail.split("; travel")[0],
          "seam travel: the bead is retracted before that travel, so no "
          "strand is dragged across the base")
    check(tail.count("initial safe lift") == 0,
          "seam travel: safe_lift (the decouple-from-PRINT_START move) is not "
          "emitted a second time mid-print")


# ---------------------------------------------------------------------------
# 7. Cooling: a solid base has already buried the first layer, so the shell
#    above it is not a bed-adhesion layer -- mirrors build_continuous_spiral's
#    `fan_immediate`.
# ---------------------------------------------------------------------------
def test_fan_turns_on_at_the_seam_when_a_base_exists():
    _, text = _build(base_layers=2)
    head, tail = _split_at_seam(text)
    check("M106" in head,
          "fan: with a solid base the fan is already on when the shell starts")

    _, plain = _build()
    p_head, _ = _split_at_seam(plain)
    check("M106" not in p_head,
          "fan: without a base the pre-existing timing is untouched (fan waits "
          "for the second shell)")


# ---------------------------------------------------------------------------
# 8. resolution is a real parameter, not a fixed constant.
# ---------------------------------------------------------------------------
def test_resolution_is_a_real_parameter():
    coarse, _ = _build(resolution=0.8)
    fine, _ = _build(resolution=0.2)
    check(fine["points"] > coarse["points"],
          "resolution: a finer resolution produces more spiral samples",
          f"{fine['points']} vs {coarse['points']}")
    check(abs(coarse["resolution_mm"] - 0.8) < 1e-9,
          "resolution: the resolution actually used is reported back",
          str(coarse["resolution_mm"]))
    default, _ = _build()
    check(abs(default["resolution_mm"] - 0.4) < 1e-9,
          "resolution: the default is still 0.4 mm", str(default["resolution_mm"]))


# ---------------------------------------------------------------------------
# 9. The bed-fit check has to see the BRIM, not just the shell. A brim that
#    escapes the print area is a nozzle dragged across the bed's edge.
# ---------------------------------------------------------------------------
def test_bed_fit_check_accounts_for_the_brim():
    profile = PrinterProfile()
    # 67.5 mm is the largest radius that fits from the bed centre on a stock
    # Trident (Y is the tight axis: 185.0 - 117.5).
    big = 66.0
    ok_rep, _ = _build(radius=big, field=dome(big, 2.0), shells=1)
    check(abs(ok_rep["footprint_radius_mm"] - big) < 1e-9,
          "bed fit: a 66 mm shell with no brim is accepted", str(ok_rep))

    _raises(lambda: _build(radius=big, field=dome(big, 2.0), shells=1,
                           first_layer_squish=0.75,
                           first_layer_spacing_factor=1.25,
                           base_layers=1, brim_loops=20),
            "bed fit: the same shell plus a 20-loop brim is REFUSED (the brim "
            "escapes the safe print area)",
            "outside the safe print area")


# ---------------------------------------------------------------------------
# 10. Invalid and non-finite inputs are REJECTED at the boundary, never
#     clamped. Every comparison against NaN is False, so a clamped NaN stays a
#     NaN and reaches the machine looking like a number.
# ---------------------------------------------------------------------------
def test_invalid_and_non_finite_inputs_are_rejected():
    nan, inf = float("nan"), float("inf")

    for name, kw in (
        ("radius=NaN", dict(radius=nan)),
        ("radius=Infinity", dict(radius=inf)),
        ("resolution=NaN", dict(resolution=nan)),
        ("resolution=Infinity", dict(resolution=inf)),
        ("first_layer_squish=NaN", dict(first_layer_squish=nan)),
        ("first_layer_z=NaN", dict(first_layer_z=nan)),
        ("first_layer_flow=NaN", dict(first_layer_flow=nan)),
        ("first_layer_spacing_factor=NaN", dict(first_layer_spacing_factor=nan)),
        ("travel_z_clearance=NaN", dict(travel_z_clearance=nan)),
        ("shells=NaN", dict(shells=nan)),
        ("base_layers=Infinity", dict(base_layers=inf)),
    ):
        _raises(lambda kw=kw: _build(**kw),
                f"non-finite: {name} is rejected, not clamped")

    for name, kw, needle in (
        ("radius=0", dict(radius=0.0), "radius must be positive"),
        ("radius=-5", dict(radius=-5.0), "radius must be positive"),
        ("resolution=0", dict(resolution=0.0), "resolution must be positive"),
        ("resolution=-0.4", dict(resolution=-0.4), "resolution must be positive"),
        ("first_layer_squish=0", dict(first_layer_squish=0.0), "first_layer_squish"),
        ("first_layer_squish=1.5", dict(first_layer_squish=1.5), "first_layer_squish"),
        ("shells=0", dict(shells=0), "shells must be >= 1"),
        ("shells=-2", dict(shells=-2), "shells must be >= 1"),
        ("base_layers=-1", dict(base_layers=-1), "base_layers must be >= 0"),
        ("brim_loops=-1", dict(brim_loops=-1), "brim_loops must be >= 0"),
        ("base_layers=1.5", dict(base_layers=1.5), "whole number"),
        ("first_layer_flow=0", dict(first_layer_flow=0.0), "first_layer_flow"),
        ("first_layer_spacing_factor=0", dict(first_layer_spacing_factor=0.0),
         "first_layer_spacing_factor"),
        ("travel_z_clearance=-1", dict(travel_z_clearance=-1.0), "travel_z_clearance"),
        ("base_style=bogus", dict(base_style="bogus", base_layers=1), "unknown base_style"),
    ):
        _raises(lambda kw=kw: _build(**kw), f"invalid: {name} is rejected", needle)

    # A height field that returns NaN is the sneakiest of all: it poisons
    # z_offset, survives min()/max(), and only _check_bounds' isfinite() guard
    # stands between it and the machine. Refuse it where it is first read.
    _raises(lambda: _build(field=lambda x, y: float("nan")),
            "non-finite: a height field returning NaN is refused",
            "non-finite value")
    _raises(lambda: _build(field=lambda x, y: (float("inf") if x > 5.0 else 0.0)),
            "non-finite: a height field going non-finite only in part of the "
            "footprint is still refused", "non-finite value")

    # ...and the clamp is genuinely absent: a NaN never reaches the writer.
    w = _new_writer()
    try:
        build_surface_spiral(w, dome(RADIUS, 4.0), float("nan"))
    except ValueError:
        pass
    check("nan" not in w.text().lower(),
          "non-finite: nothing resembling 'nan' was ever emitted to G-code")


# ---------------------------------------------------------------------------
# 11. surface_spiral_geometry() is the single source of the Z arithmetic, so a
#     caller pre-flighting the toolpath (serve.py's probe-slope check) cannot
#     drift from what the generator actually emits.
# ---------------------------------------------------------------------------
def test_geometry_helper_matches_the_real_build():
    kw = dict(shells=2, resolution=0.4, first_layer_squish=0.75,
              base_layers=2, brim_loops=3)
    geom = surface_spiral_geometry(_new_writer(), dome(RADIUS, 4.0), RADIUS, **kw)
    rep, text = _build(first_layer_spacing_factor=1.25, **kw)

    check(abs(geom["first_layer_z"] - rep["first_layer_z_mm"]) < 1e-12,
          "geometry helper: first_layer_z matches the real build")
    check(abs(geom["shell_floor_z"] - rep["shell_floor_z_mm"]) < 1e-12,
          "geometry helper: shell_floor_z matches the real build")
    check(abs(round(geom["top_z"], 2) - rep["top_z_mm"]) < 1e-12,
          "geometry helper: top_z matches the real build",
          f"{geom['top_z']} vs {rep['top_z_mm']}")
    check(len(geom["base_pts"]) == rep["points"] // kw["shells"],
          "geometry helper: the sample points are the ones actually printed",
          f"{len(geom['base_pts'])} vs {rep['points']}/{kw['shells']}")

    zs = [z for (_, _, z) in _extrudes(text)]
    check(abs(max(zs) - geom["top_z"]) < 1e-9,
          "geometry helper: top_z is the highest EXTRUDING Z in the output",
          f"{max(zs)} vs {geom['top_z']}")

    # It must reject the same bad numbers the builder does -- a pre-flight that
    # accepts what the builder refuses is a pre-flight that proves nothing.
    _raises(lambda: surface_spiral_geometry(_new_writer(), dome(RADIUS, 4.0),
                                            float("nan")),
            "geometry helper: rejects a non-finite radius too")


# ---------------------------------------------------------------------------
# 12. base_style reaches base_fill, and a Z ceiling is still enforced.
# ---------------------------------------------------------------------------
def test_base_style_and_z_ceiling():
    _, spiral = _build(base_layers=2, base_style="spiral")
    _, conc = _build(base_layers=2, base_style="concentric")
    check(spiral != conc,
          "base_style: 'concentric' produces a different base than 'spiral'")
    head_s, _ = _split_at_seam(spiral)
    head_c, _ = _split_at_seam(conc)
    check(len(_extrudes(head_s)) > 0 and len(_extrudes(head_c)) > 0,
          "base_style: both styles actually pave something")

    profile = PrinterProfile()
    _raises(lambda: _build(field=ripple(profile.z_max, 20.0), shells=2),
            "z ceiling: a field taller than the machine's Z travel is refused",
            "exceeds Z max")

    # A base raises the whole shell, so the ceiling has to be re-checked with
    # the base's own height included, not against the bare field.
    tall = profile.z_max - 0.5
    _raises(lambda: _build(field=dome(RADIUS, tall), shells=1,
                           first_layer_squish=0.75, base_layers=4),
            "z ceiling: the base's own height counts toward the Z ceiling",
            "exceeds Z max")


def main() -> int:
    test_defaults_are_a_no_op()
    test_base_disk_is_emitted()
    test_brim_loops_are_emitted()
    test_first_layer_is_squished()
    test_shell_sits_exactly_one_layer_above_its_base()
    test_seam_travel_clears_the_base()
    test_fan_turns_on_at_the_seam_when_a_base_exists()
    test_resolution_is_a_real_parameter()
    test_bed_fit_check_accounts_for_the_brim()
    test_invalid_and_non_finite_inputs_are_rejected()
    test_geometry_helper_matches_the_real_build()
    test_base_style_and_z_ceiling()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
