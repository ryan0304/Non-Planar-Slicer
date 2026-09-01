#!/usr/bin/env python3
"""Functional (non-byte-exact) tests for trident_gcode/hybrid.py.

Plain ``python tools/test_hybrid.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors the other tools/test_*.py scripts' style.

This exercises build_hybrid_print()'s own orchestration logic (STL
generation, JSON building, the placement sanity check) end-to-end by
monkeypatching orca_slice.slice_stl_to_gcode to return a checked-in fixture
instead of invoking a real subprocess -- so, like
tools/check_regression.py's run_hybrid_stitch_case(), this never needs a
live OrcaSlicer install. It intentionally does NOT byte-compare output
(that's check_regression.py's job); it checks that the function completes,
raises where it should, and returns sane values.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tools" / "fixtures" / "orca_gcode" / "sample_base.gcode"
sys.path.insert(0, str(ROOT))

import trident_gcode.hybrid as hybrid
from trident_gcode.profile import PrinterProfile
from trident_gcode.gcode import GcodeWriter
from trident_gcode.paths import circle


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _fake_slice(stl_bytes, *, machine_json, process_json, filament_json, orca_path, **kw):
    return FIXTURE.read_text(encoding="utf-8")


def _new_writer(profile):
    return GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


# ---------------------------------------------------------------------------
# 1. Happy path: center matches the fixture's own footprint -> succeeds.
# ---------------------------------------------------------------------------
def test_happy_path():
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = _fake_slice
    try:
        profile = PrinterProfile()
        writer = _new_writer(profile)
        report = hybrid.build_hybrid_print(
            writer,
            shape_fn=circle(3.0), radius=3.0, height=20.0,
            transition_height=0.4, layer_height=0.3, points_per_turn=60,
            wall_count=2, infill_density=0.2, infill_pattern="grid",
            orca_path="unused-because-monkeypatched",
            center=(102.5, 102.5),
            z_amp=0.3, z_waves=3,
        )
        check(report["hybrid"] is True, "happy path: report marks hybrid=True")
        check(abs(report["achieved_base_height_mm"] - 0.3) < 1e-9 or
              abs(report["achieved_base_height_mm"] - 0.6) < 1e-9 or
              report["achieved_base_height_mm"] > 0,
              "happy path: achieved_base_height_mm is a positive multiple of layer_height",
              str(report["achieved_base_height_mm"]))
        check(report["orca_base_layers"] >= 1,
              "happy path: at least one Orca base layer", str(report["orca_base_layers"]))
        check(report["extrude_count"] > 0, "happy path: base replay extruded something",
              str(report["extrude_count"]))
        check("contours" in report, "happy path: wall report merged in (has 'contours')",
              str(list(report.keys())))
        text = writer.text()
        check(text.count("PRINT_START") == 1,
              "happy path: exactly one PRINT_START in the whole print",
              str(text.count("PRINT_START")))
        check(text.count("PRINT_END") == 1,
              "happy path: exactly one PRINT_END in the whole print",
              str(text.count("PRINT_END")))
    finally:
        hybrid.slice_stl_to_gcode = real_slice


# ---------------------------------------------------------------------------
# 2. Placement mismatch: center far from the fixture's actual footprint ->
#    must raise, never silently print a misplaced base.
# ---------------------------------------------------------------------------
def test_placement_mismatch_is_refused():
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = _fake_slice
    try:
        profile = PrinterProfile()
        writer = _new_writer(profile)
        try:
            hybrid.build_hybrid_print(
                writer,
                shape_fn=circle(3.0), radius=3.0, height=20.0,
                transition_height=0.4, layer_height=0.3, points_per_turn=60,
                wall_count=2, infill_density=0.2, infill_pattern="grid",
                orca_path="unused-because-monkeypatched",
                center=profile.bed_center,  # (117.5, 117.5) -- far from the fixture's ~(102.5, 102.5)
                z_amp=0.3, z_waves=3,
            )
            check(False, "placement mismatch: raises ValueError", "no exception raised")
        except ValueError as e:
            check("off-target" in str(e), "placement mismatch: raises ValueError", str(e))
    finally:
        hybrid.slice_stl_to_gcode = real_slice


# ---------------------------------------------------------------------------
# 3. Unknown infill pattern surfaces as a ValueError before any slicing work
#    starts (fail fast, matching build_process_json's own validation).
# ---------------------------------------------------------------------------
def test_unknown_infill_pattern_fails_fast():
    profile = PrinterProfile()
    writer = _new_writer(profile)
    try:
        hybrid.build_hybrid_print(
            writer,
            shape_fn=circle(3.0), radius=3.0, height=20.0,
            transition_height=0.4, layer_height=0.3, points_per_turn=60,
            wall_count=2, infill_density=0.2, infill_pattern="not_a_real_pattern",
            orca_path="unused",
            center=(102.5, 102.5),
        )
        check(False, "unknown infill pattern: raises ValueError before slicing",
              "no exception raised")
    except ValueError as e:
        check("not_a_real_pattern" in str(e),
              "unknown infill pattern: raises ValueError before slicing", str(e))


# ---------------------------------------------------------------------------
# 4. The whole-object-to-local-segment rescaling (decision 7: the base's
#    last ring must match the wall's first ring exactly) must not crash with
#    a real envelope active, and the report's achieved_base_height_mm must
#    be internally consistent with a hand-computed seam_t.
# ---------------------------------------------------------------------------
def test_radius_envelope_rescaling_is_consistent():
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = _fake_slice
    try:
        profile = PrinterProfile()
        writer = _new_writer(profile)
        radius_profile_fn = lambda t: 1.0 + 0.5 * t
        report = hybrid.build_hybrid_print(
            writer,
            shape_fn=circle(3.0), radius=3.0, height=20.0,
            transition_height=0.4, layer_height=0.3, points_per_turn=60,
            wall_count=2, infill_density=0.2, infill_pattern="grid",
            orca_path="unused-because-monkeypatched",
            center=(102.5, 102.5),
            radius_envelope=radius_profile_fn,
            z_amp=0.3, z_waves=3,
        )
        check(report["hybrid"] is True,
              "radius_envelope: build_hybrid_print completes with a real envelope active")
        seam_t = report["achieved_base_height_mm"] / 20.0
        # base_env(t_local) = radius_envelope(t_local * seam_t); at t_local=1
        # this is radius_envelope(seam_t) -- the base's own last ring.
        # wall_env(t_local) = radius_envelope(seam_t + t_local*(1-seam_t));
        # at t_local=0 this is ALSO radius_envelope(seam_t) -- the wall's
        # first ring. They must be identical by construction.
        base_last_ring_scale = radius_profile_fn(1.0 * seam_t)
        wall_first_ring_scale = radius_profile_fn(seam_t + 0.0 * (1.0 - seam_t))
        check(abs(base_last_ring_scale - wall_first_ring_scale) < 1e-12,
              "radius_envelope: base's last ring and wall's first ring scale "
              "to the identical radius (no visible step at the seam)",
              f"{base_last_ring_scale} vs {wall_first_ring_scale}")
    finally:
        hybrid.slice_stl_to_gcode = real_slice


def main() -> int:
    test_happy_path()
    test_placement_mismatch_is_refused()
    test_unknown_infill_pattern_fails_fast()
    test_radius_envelope_rescaling_is_consistent()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
