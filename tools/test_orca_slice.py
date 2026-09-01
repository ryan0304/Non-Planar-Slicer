#!/usr/bin/env python3
"""Tests for trident_gcode/orca_slice.py's JSON-building functions.

Plain ``python tools/test_orca_slice.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors check_regression.py's/test_printer_import.py's
style.

Deliberately does NOT invoke the OrcaSlicer subprocess (slice_stl_to_gcode) --
that requires a real installed binary and is exercised only by the optional
tools/test_orca_live_integration.py, which skips when Orca isn't found. This
script only proves the pure JSON-shape/derivation functions behave correctly
for every PrinterProfile field they map, independent of whether Orca is
installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.profile import PrinterProfile, PRINTER_PROFILES
from trident_gcode.orca import FilamentSettings
from trident_gcode.orca_slice import (
    build_machine_json, build_process_json, build_filament_json, _machine_name,
)


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# build_machine_json: every value must derive from the given profile, never
# a hardcoded constant (CLAUDE.md's "no machine limit may be a module
# constant" rule).
# ---------------------------------------------------------------------------
def test_machine_json_derives_from_profile():
    trident = PrinterProfile()
    m = build_machine_json(trident)
    check(m["printable_area"] == ["0x0", "235x0", "235x235", "0x235"],
          "machine_json: bed shape matches TRIDENT bed size", m["printable_area"])
    # "from": "system" (not "User") is deliberate -- confirmed empirically
    # against a real OrcaSlicer install that "User" makes the CLI try to
    # resolve the profile against Orca's own user database, which silently
    # fails for a one-off generated file with no such database entry. See
    # orca_slice.py's _PROFILE_FROM docstring.
    check(m["type"] == "machine" and m["from"] == "system" and m["instantiation"] == "true",
          "machine_json: has the type/from/instantiation fields Orca requires",
          str({k: m.get(k) for k in ("type", "from", "instantiation")}))
    check(m["inherits"] == "fdm_klipper_common",
          "machine_json: klipper firmware inherits fdm_klipper_common", m["inherits"])
    check(float(m["printable_height"]) == trident.z_max,
          "machine_json: printable_height matches z_max", m["printable_height"])
    check(m["machine_max_speed_z"] == [f"{trident.max_z_velocity:g}"] * 2,
          "machine_json: max Z speed matches profile.max_z_velocity",
          m["machine_max_speed_z"])
    check(m["nozzle_diameter"] == [f"{trident.nozzle_diameter:g}"],
          "machine_json: nozzle diameter matches profile")
    check(m["gcode_flavor"] == "klipper",
          "machine_json: klipper firmware maps to klipper flavor")
    check(m["machine_start_gcode"] == "" and m["machine_end_gcode"] == "",
          "machine_json: start/end gcode left empty (this app supplies its own)")
    check(m["enable_arc_fitting"] == "0",
          "machine_json: arc fitting disabled (parser has no G2/G3 primitive)")

    bambu = PRINTER_PROFILES["bambu_a1"]
    m2 = build_machine_json(bambu)
    check(m2["printable_area"] != m["printable_area"],
          "machine_json: a different profile produces a different bed shape "
          "(never falls back to another machine's numbers)")
    check(m2["gcode_flavor"] == "marlin",
          "machine_json: non-klipper firmware maps to marlin flavor")
    check(m2["inherits"] == "fdm_machine_common",
          "machine_json: non-klipper firmware inherits fdm_machine_common", m2["inherits"])


# ---------------------------------------------------------------------------
# build_process_json: clamping and the infill-pattern allow-list.
# ---------------------------------------------------------------------------
def test_process_json_clamps_and_validates():
    profile = PrinterProfile()
    p = build_process_json(
        profile, layer_height=0.2, first_layer_height=0.24,
        wall_count=3, infill_density=0.15, infill_pattern="grid", line_width=0.42,
    )
    check(p["wall_loops"] == "3", "process_json: wall_loops passes through in range")
    check(p["sparse_infill_density"] == "15%", "process_json: infill density formatted as percent",
          p["sparse_infill_density"])
    # Confirmed empirically against a real OrcaSlicer install: without this,
    # slicing fails SILENTLY (no error message, no output, just a nonzero
    # exit) -- Orca never explains that it rejected the pairing because the
    # process profile's compatible_printers didn't list the machine's name.
    m = build_machine_json(profile)
    check(p["compatible_printers"] == [_machine_name(profile)] == [m["name"]],
          "process_json: compatible_printers matches the paired machine's "
          "exact name (silent-slice-failure prevention)",
          f"process={p['compatible_printers']} machine={m['name']}")

    p_over = build_process_json(
        profile, layer_height=0.2, first_layer_height=0.24,
        wall_count=99, infill_density=5.0, infill_pattern="gyroid", line_width=0.42,
    )
    check(p_over["wall_loops"] == "8", "process_json: wall_loops clamped to <= 8",
          p_over["wall_loops"])
    check(p_over["sparse_infill_density"] == "100%",
          "process_json: infill density clamped to <= 100%", p_over["sparse_infill_density"])

    try:
        build_process_json(
            profile, layer_height=0.2, first_layer_height=0.24,
            wall_count=3, infill_density=0.15, infill_pattern="not_a_real_pattern",
            line_width=0.42,
        )
        check(False, "process_json: unknown infill pattern is rejected", "no exception raised")
    except ValueError as e:
        check("not_a_real_pattern" in str(e),
              "process_json: unknown infill pattern is rejected", str(e))

    # Every speed/accel key must never exceed this printer's own ceiling,
    # even for a printer profile with unusually low limits.
    slow_profile = PrinterProfile(max_velocity=30.0, max_accel=500.0)
    p_slow = build_process_json(
        slow_profile, layer_height=0.2, first_layer_height=0.24,
        wall_count=3, infill_density=0.15, infill_pattern="grid", line_width=0.42,
    )
    check(float(p_slow["inner_wall_speed"]) <= slow_profile.max_velocity,
          "process_json: inner_wall_speed never exceeds profile.max_velocity",
          p_slow["inner_wall_speed"])
    check(float(p_slow["travel_speed"]) <= slow_profile.max_velocity,
          "process_json: travel_speed never exceeds profile.max_velocity",
          p_slow["travel_speed"])
    check(float(p_slow["default_acceleration"]) <= slow_profile.max_accel,
          "process_json: default_acceleration never exceeds profile.max_accel",
          p_slow["default_acceleration"])


# ---------------------------------------------------------------------------
# build_filament_json: defaults when no FilamentSettings given, and
# temperature clamping to the profile's own ceilings.
# ---------------------------------------------------------------------------
def test_filament_json_defaults_and_clamps():
    profile = PrinterProfile()
    f = build_filament_json(None, profile)
    check(f["nozzle_temperature"] == ["210"], "filament_json: default nozzle temp is 210",
          f["nozzle_temperature"])
    check(f["hot_plate_temp"] == ["60"], "filament_json: default bed temp is 60",
          f["hot_plate_temp"])

    hot_fs = FilamentSettings(name="TooHot", nozzle_temp=999.0, bed_temp=999.0)
    f_hot = build_filament_json(hot_fs, profile)
    check(float(f_hot["nozzle_temperature"][0]) <= profile.max_nozzle_temp,
          "filament_json: nozzle temp clamped to profile.max_nozzle_temp",
          f_hot["nozzle_temperature"])
    check(float(f_hot["hot_plate_temp"][0]) <= profile.max_bed_temp,
          "filament_json: bed temp clamped to profile.max_bed_temp",
          f_hot["hot_plate_temp"])

    cold_profile = PrinterProfile(max_nozzle_temp=50.0, max_bed_temp=10.0)
    f_cold = build_filament_json(None, cold_profile)
    check(float(f_cold["nozzle_temperature"][0]) <= cold_profile.max_nozzle_temp,
          "filament_json: even the DEFAULT 210 nozzle temp is clamped to a "
          "restrictive profile's ceiling, never a hardcoded 210",
          f_cold["nozzle_temperature"])


def main() -> int:
    test_machine_json_derives_from_profile()
    test_process_json_clamps_and_validates()
    test_filament_json_defaults_and_clamps()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
