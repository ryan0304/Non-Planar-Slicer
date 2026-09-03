#!/usr/bin/env python3
"""Tests for build_continuous_spiral's fan_off_layers handling.

Plain ``python tools/test_fan_off_layers.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors the other tools/test_*.py scripts'
style.

Found live: with no solid base (base_layers=0, the default -- "Bottom" set
to open, or simply no base requested), build_continuous_spiral prints a
"flat priming loop" revolution to anchor the squished first layer BEFORE the
wall spiral's own first turn starts. That revolution is a real, fully
off-fan pass over the bed, but _fan_on_threshold's layer count never knew
about it -- so an explicit fan_off_layers=N request left the part-cooling
fan off for N+1 revolutions (the priming loop, plus N wall turns) instead of
N. Confirmed against real generated G-code before the fix: fan_off_layers=1
kept the fan off through two full revolutions, turning on only at the START
of the wall's SECOND turn.

These tests inspect the emitted line list directly (writer._lines) rather
than byte-comparing a whole file -- this is functional coverage for one
setting, not a fixture check_regression.py should own.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.generators.continuous_spiral import build_continuous_spiral
from trident_gcode.paths import SpiralSpec, circle
from trident_gcode.profile import PrinterProfile
from trident_gcode.gcode import GcodeWriter

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _new_writer(profile):
    return GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def test_fan_off_1_layer_no_base():
    """The exact bug report: fan_off_layers=1, no solid base. The fan must
    turn on right after ONE off-fan revolution (the flat priming loop),
    not two (priming loop + the wall's own first turn)."""
    profile = PrinterProfile()
    writer = _new_writer(profile)
    spec = SpiralSpec(base_radius=20.0, height=6.0, layer_height=0.3,
                       points_per_turn=60, z_amp=0.0)
    build_continuous_spiral(writer, spec, shape=circle(spec.base_radius),
                             first_layer_squish=0.75, fan_off_layers=1)
    lines = writer._lines
    m106_idx = next((i for i, l in enumerate(lines) if l.startswith("M106")), -1)
    check(m106_idx >= 0, "fan_off_layers=1: an M106 is emitted at all")
    priming_idx = next(i for i, l in enumerate(lines) if "flat priming loop" in l)
    wall_idx = next(i for i, l in enumerate(lines) if "wall spiral" in l)
    check(priming_idx < wall_idx < m106_idx,
          "fan_off_layers=1: M106 comes after both the priming loop and the "
          "start of the wall spiral")
    # The real assertion: M106 must be one of the FIRST few lines of the wall
    # spiral (right at its start), not after a full extra turn (~60 lines of
    # G1 moves) into it.
    lines_into_wall = m106_idx - wall_idx
    check(lines_into_wall <= 2,
          "fan_off_layers=1: fan turns on within the wall spiral's own first "
          "move, not after an extra whole turn",
          f"M106 landed {lines_into_wall} lines into the wall spiral "
          f"(points_per_turn=60)")


def test_fan_off_2_layers_no_base():
    """fan_off_layers=2 with no base: off through the priming loop AND the
    wall's own first turn, on at the start of the wall's SECOND turn."""
    profile = PrinterProfile()
    writer = _new_writer(profile)
    spec = SpiralSpec(base_radius=20.0, height=6.0, layer_height=0.3,
                       points_per_turn=60, z_amp=0.0)
    build_continuous_spiral(writer, spec, shape=circle(spec.base_radius),
                             first_layer_squish=0.75, fan_off_layers=2)
    lines = writer._lines
    wall_idx = next(i for i, l in enumerate(lines) if "wall spiral" in l)
    m106_idx = next(i for i, l in enumerate(lines) if l.startswith("M106"))
    lines_into_wall = m106_idx - wall_idx
    # One full wall turn (60 points) must have already happened, but not two.
    check(55 <= lines_into_wall <= 65,
          "fan_off_layers=2: fan turns on after exactly one full wall turn "
          "(the priming loop already covered layer 1)",
          f"M106 landed {lines_into_wall} lines into the wall spiral "
          f"(expected ~60, points_per_turn=60)")


def test_fan_off_layers_zero_is_unchanged():
    """fan_off_layers<=0 (unset) is the pre-existing default path and must
    stay byte-identical to before ``primed`` existed -- check_regression.py
    is the real guard for this, but pin it here too since it is the one case
    this file's fix must never touch."""
    profile = PrinterProfile()
    writer_a = _new_writer(profile)
    writer_b = _new_writer(profile)
    spec = SpiralSpec(base_radius=20.0, height=6.0, layer_height=0.3,
                       points_per_turn=60, z_amp=0.0)
    build_continuous_spiral(writer_a, spec, shape=circle(spec.base_radius),
                             first_layer_squish=0.75, fan_off_layers=0)
    build_continuous_spiral(writer_b, spec, shape=circle(spec.base_radius),
                             first_layer_squish=0.75)
    check(writer_a._lines == writer_b._lines,
          "fan_off_layers=0: identical to the fan_off_layers-unset default")


def test_fan_off_with_real_base_unaffected():
    """base_layers > 0 has no separate priming loop -- the base disks are
    the anchor -- so its threshold must be unaffected by this fix. Request
    fan_off_layers below the base layer count: fan comes on immediately
    after the base finishes, same as before."""
    profile = PrinterProfile()
    writer = _new_writer(profile)
    spec = SpiralSpec(base_radius=20.0, height=6.0, layer_height=0.3,
                       points_per_turn=60, z_amp=0.0)
    build_continuous_spiral(writer, spec, shape=circle(spec.base_radius),
                             base_layers=2, fan_off_layers=1)
    lines = writer._lines
    base_idx = next(i for i, l in enumerate(lines) if "solid base" in l)
    m106_idx = next(i for i, l in enumerate(lines) if l.startswith("M106"))
    priming_present = any("flat priming loop" in l for l in lines)
    check(not priming_present,
          "base_layers=2: no separate flat priming loop is emitted")
    check(base_idx < m106_idx,
          "base_layers=2, fan_off_layers=1 (<= base_layers): fan comes on "
          "immediately once the base finishes, unaffected by this fix")


def main() -> int:
    test_fan_off_1_layer_no_base()
    test_fan_off_2_layers_no_base()
    test_fan_off_layers_zero_is_unchanged()
    test_fan_off_with_real_base_unaffected()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
