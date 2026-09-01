#!/usr/bin/env python3
"""Tests for trident_gcode/orca_gcode_parser.py.

Plain ``python tools/test_orca_gcode_parser.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors tools/check_regression.py's and
tools/test_printer_import.py's style. This exercises an unrelated subsystem
from either of those (parsing untrusted external-binary G-code output), so
it is its own script rather than folded into test_printer_import.py.

Fixtures live in tools/fixtures/orca_gcode/ (see README.md there).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tools" / "fixtures" / "orca_gcode"
sys.path.insert(0, str(ROOT))

from trident_gcode.orca_gcode_parser import parse_orca_gcode, OrcaGcodeParseError


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _read_fixture(name: str) -> str:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. sample_base.gcode -- a clean, realistic two-layer body must parse.
# ---------------------------------------------------------------------------
def test_clean_base_parses():
    text = _read_fixture("sample_base.gcode")
    moves = parse_orca_gcode(text)
    check(len(moves) > 0, "sample_base: produced at least one move", f"got {len(moves)}")

    extrude_moves = [m for m in moves if m.e_delta is not None and m.e_delta > 0]
    check(len(extrude_moves) >= 6, "sample_base: has multiple extruding moves",
          f"got {len(extrude_moves)}")

    # The fixture has three explicit retracts, each -0.6mm relative.
    neg_deltas = [m.e_delta for m in moves if m.e_delta is not None and m.e_delta < 0]
    check(len(neg_deltas) == 3, "sample_base: exactly three retract moves",
          f"got {len(neg_deltas)}: {neg_deltas}")
    check(all(abs(d - (-0.6)) < 1e-9 for d in neg_deltas),
          "sample_base: retract deltas are -0.6mm", str(neg_deltas))

    # A pure travel (no E) must round-trip as e_delta is None, not 0.0.
    travel_moves = [m for m in moves if m.e_delta is None]
    check(len(travel_moves) >= 1, "sample_base: has at least one pure travel move",
          f"got {len(travel_moves)}")

    # First layer's Z is 0.20, second layer's is 0.40 -- both must appear.
    zs = sorted({round(m.z, 4) for m in moves})
    check(zs == [0.20, 0.40], "sample_base: both layer Z heights present", str(zs))

    total_extruded = sum(m.e_delta for m in moves if m.e_delta is not None and m.e_delta > 0)
    check(total_extruded > 0, "sample_base: net positive filament extruded",
          f"got {total_extruded}")


# ---------------------------------------------------------------------------
# 2. sample_base_m82.gcode -- absolute extrusion must normalize to the same
#    shape of relative deltas as the M83 fixture (retracts, unretracts,
#    positive extrudes), not be replayed as raw absolute E.
# ---------------------------------------------------------------------------
def test_m82_normalizes_to_relative_deltas():
    text = _read_fixture("sample_base_m82.gcode")
    moves = parse_orca_gcode(text)

    deltas = [m.e_delta for m in moves if m.e_delta is not None]
    # No delta should ever be a large absolute-looking value (e.g. >1.0mm in
    # a single move) -- if normalization failed and raw absolute E leaked
    # through as a "delta", later moves would show deltas near 2.13 (the
    # fixture's final absolute E), not the small per-move increments.
    check(all(abs(d) <= 1.0 for d in deltas),
          "sample_base_m82: every delta is a plausible per-move increment, "
          "not a leaked absolute E value",
          str(deltas))

    neg_deltas = [d for d in deltas if d < 0]
    check(len(neg_deltas) == 3, "sample_base_m82: exactly three retract moves recovered",
          f"got {len(neg_deltas)}: {neg_deltas}")
    check(all(abs(d - (-0.6)) < 1e-6 for d in neg_deltas),
          "sample_base_m82: retract deltas recovered as -0.6mm", str(neg_deltas))

    pos_deltas = [d for d in deltas if d > 0]
    check(all(0.0 < d <= 0.85 for d in pos_deltas),
          "sample_base_m82: positive deltas are per-segment, not cumulative",
          str(pos_deltas))


# ---------------------------------------------------------------------------
# 3. Adversarial fixtures: each must raise OrcaGcodeParseError, never
#    silently succeed or crash with an unrelated exception.
# ---------------------------------------------------------------------------
def test_g91_is_rejected():
    text = _read_fixture("sample_base_g91.gcode")
    try:
        parse_orca_gcode(text)
        check(False, "sample_base_g91: G91 raises OrcaGcodeParseError", "no exception raised")
    except OrcaGcodeParseError as e:
        check("G91" in str(e), "sample_base_g91: G91 raises OrcaGcodeParseError", str(e))


def test_arc_is_rejected():
    text = _read_fixture("sample_base_arc.gcode")
    try:
        parse_orca_gcode(text)
        check(False, "sample_base_arc: G2 raises OrcaGcodeParseError", "no exception raised")
    except OrcaGcodeParseError as e:
        check("arc" in str(e).lower(), "sample_base_arc: G2 raises OrcaGcodeParseError", str(e))


def test_nan_is_rejected():
    text = _read_fixture("sample_base_nan.gcode")
    try:
        parse_orca_gcode(text)
        check(False, "sample_base_nan: Enan raises OrcaGcodeParseError", "no exception raised")
    except OrcaGcodeParseError as e:
        check("finite" in str(e).lower(), "sample_base_nan: Enan raises OrcaGcodeParseError",
              str(e))


def test_unknown_command_is_rejected():
    text = _read_fixture("sample_base_unknown.gcode")
    try:
        parse_orca_gcode(text)
        check(False, "sample_base_unknown: M600 raises OrcaGcodeParseError",
              "no exception raised")
    except OrcaGcodeParseError as e:
        check("M600" in str(e), "sample_base_unknown: M600 raises OrcaGcodeParseError", str(e))


# ---------------------------------------------------------------------------
# 4. Comment-only lines (including a command mentioned only in a comment)
#    must not be mistaken for the command itself -- CLAUDE.md's "presence
#    checks must read commands, not comments" lesson, in this new surface.
# ---------------------------------------------------------------------------
def test_comment_mentioning_a_command_is_not_executed():
    text = "; G91 is not actually here, just mentioned in a comment\nG1 X1 Y1 Z0.2 E0.01 F1200\n"
    moves = parse_orca_gcode(text)
    check(len(moves) == 1, "comment-only G91 mention: does not trigger rejection",
          f"got {len(moves)} moves")


# ---------------------------------------------------------------------------
# 5. Direct non-finite check, independent of file fixtures: inf must also
#    be rejected, not just nan.
# ---------------------------------------------------------------------------
def test_infinity_is_rejected():
    text = "M83\nG92 E0\nG1 X1 Y1 Z0.2 Einf F1200\n"
    try:
        parse_orca_gcode(text)
        check(False, "inline Einf: raises OrcaGcodeParseError", "no exception raised")
    except OrcaGcodeParseError as e:
        check("finite" in str(e).lower(), "inline Einf: raises OrcaGcodeParseError", str(e))


def main() -> int:
    test_clean_base_parses()
    test_m82_normalizes_to_relative_deltas()
    test_g91_is_rejected()
    test_arc_is_rejected()
    test_nan_is_rejected()
    test_unknown_command_is_rejected()
    test_comment_mentioning_a_command_is_not_executed()
    test_infinity_is_rejected()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
