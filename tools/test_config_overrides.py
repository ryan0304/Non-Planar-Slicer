#!/usr/bin/env python3
"""Tests for trident_gcode.config.apply_machine_overrides.

Plain ``python tools/test_config_overrides.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors test_printer_import.py's style.

Background (SPEC A item 7): apply_machine_overrides used to do
``setattr(profile, k, type(getattr(profile, k))(v))`` with no validation at
all. json.load accepts the bare tokens NaN/Infinity, so a config's "machine"
block could set a numeric field (e.g. max_z_velocity) to a non-finite value
that then survives every downstream min()/max() clamp silently (CLAUDE.md:
every comparison against NaN is False). bool("false") is also True for ANY
non-empty string, so a boolean field could end up the OPPOSITE of what the
config text said. And nothing re-checked the WHOLE resulting profile for an
in-range-per-field-but-unsafe combination (e.g. z_amp_max above this
module's absolute ceiling).

These tests exercise: the two per-field guards (non-finite reject, bool
type check), the whole-profile printer_validate.validate_profile_dict gate,
that a clean override still applies normally (the "teeth" case -- a test
suite that only ever calls SystemExit never proves the happy path still
works), and that generate.py never hands a SHARED profile instance
(trident_gcode.profile.TRIDENT or a PRINTER_PROFILES[...] entry) to this
function, since it mutates in place.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.config import apply_machine_overrides
from trident_gcode.profile import PrinterProfile, TRIDENT, PRINTER_PROFILES

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _fresh_profile() -> PrinterProfile:
    """A profile this function is allowed to mutate -- never TRIDENT itself."""
    from dataclasses import replace
    return replace(TRIDENT)


def test_unknown_key_exits_1():
    p = _fresh_profile()
    try:
        apply_machine_overrides(p, {"not_a_real_field": 1})
        check(False, "unknown machine key exits(1)", "no SystemExit raised")
    except SystemExit as e:
        check(e.code == 1, "unknown machine key exits(1)", f"code={e.code}")


def test_nan_rejected():
    """json.load turns the bare token NaN into a real float NaN -- must be
    rejected outright, never clamped (every comparison against NaN is False,
    so it would sail through every downstream min()/max())."""
    p = _fresh_profile()
    before = p.max_z_velocity
    overrides = json.loads('{"max_z_velocity": NaN}')
    try:
        apply_machine_overrides(p, overrides)
        check(False, "NaN machine value exits(1)", "no SystemExit raised")
    except SystemExit as e:
        check(e.code == 1, "NaN machine value exits(1)", f"code={e.code}")
    check(p.max_z_velocity == before,
          "NaN rejection happens BEFORE the field is written -- profile "
          "unchanged", p.max_z_velocity)


def test_infinity_rejected():
    p = _fresh_profile()
    overrides = json.loads('{"max_accel": Infinity}')
    try:
        apply_machine_overrides(p, overrides)
        check(False, "Infinity machine value exits(1)", "no SystemExit raised")
    except SystemExit as e:
        check(e.code == 1, "Infinity machine value exits(1)", f"code={e.code}")


def test_non_numeric_string_rejected():
    p = _fresh_profile()
    try:
        apply_machine_overrides(p, {"z_max": "not-a-number"})
        check(False, "non-numeric machine value exits(1)", "no SystemExit raised")
    except SystemExit as e:
        check(e.code == 1, "non-numeric machine value exits(1)", f"code={e.code}")


def test_string_bool_rejected_not_coerced_true():
    """bool("false") is True in Python -- the exact opposite of the config's
    own text. Must be a hard reject, not silently-wrong-way-round."""
    p = _fresh_profile()
    before = p.has_probe
    try:
        apply_machine_overrides(p, {"has_probe": "false"})
        check(False, "'false' string for a bool field exits(1)",
              "no SystemExit raised -- has_probe=%r" % p.has_probe)
    except SystemExit as e:
        check(e.code == 1, "'false' string for a bool field exits(1)",
              f"code={e.code}")
    check(p.has_probe == before,
          "rejected bool value never reached the profile", p.has_probe)


def test_real_json_bool_accepted():
    p = _fresh_profile()
    apply_machine_overrides(p, json.loads('{"has_probe": false}'))
    check(p.has_probe is False,
          "a real JSON bool is accepted and applied", p.has_probe)


def test_unsafe_combination_rejected_by_whole_profile_validation():
    """max_z_velocity > max_velocity is legal per-field (both are just
    numbers) but an unsafe COMBINATION -- printer_validate normally just
    clamps this with a warning (vr.ok stays True), so this asserts the
    milder case: the override still applies and the caller can see the
    clamp happened via the round-tripped value, never a value above
    max_velocity."""
    p = _fresh_profile()
    apply_machine_overrides(p, {"max_velocity": 100.0, "max_z_velocity": 500.0})
    check(p.max_z_velocity <= p.max_velocity,
          "max_z_velocity above max_velocity is clamped down by the "
          "whole-profile validate_profile_dict pass, not left as an unsafe "
          "combination", f"{p.max_z_velocity} vs {p.max_velocity}")


def test_out_of_range_z_amp_max_is_clamped_not_left_unsafe():
    """z_amp_max has an absolute [0, 10] ceiling in printer_validate,
    independent of any one printer's probe geometry. A config asking for 50
    must not leave 50 sitting on the profile just because clamping is only a
    WARNING, not an error (CLAUDE.md: an out-of-range value must be clamped,
    never silently accepted)."""
    p = _fresh_profile()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        apply_machine_overrides(p, {"z_amp_max": 50.0})
    check(p.z_amp_max <= 10.0,
          "an absurd z_amp_max is clamped to the module's absolute ceiling, "
          "not left at the requested value", p.z_amp_max)
    out = err.getvalue()
    check("WARNING: machine override z_amp_max" in out,
          "the clamp is printed to stderr -- someone who typed 50 must be "
          "told they actually got 10, not left to discover it by measuring "
          "a print", out)
    check("50" in out and "10" in out,
          "the warning names both the requested and clamped values", out)


def test_clean_override_applies_normally():
    """The teeth case: a suite that only ever asserts SystemExit never
    proves the happy path still works."""
    p = _fresh_profile()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        apply_machine_overrides(p, {"max_z_velocity": 18.0, "name": "Bench Test"})
    check(p.max_z_velocity == 18.0, "a normal numeric override applies",
          p.max_z_velocity)
    check(p.name == "Bench Test", "a normal string override applies", p.name)
    check(err.getvalue() == "",
          "a clean, in-range override prints no WARNING at all",
          err.getvalue())


def test_clean_profile_round_trips_byte_identical():
    """A profile with nothing out of range must come back byte-identical
    through the whole-profile validation pass -- including start_gcode/
    end_gcode, which the pass re-sanitizes every time. If this ever drifted,
    every config-file user's G-code would silently change on their next run
    even when they asked for a single, unrelated numeric override."""
    p = _fresh_profile()
    before = {f.name: getattr(p, f.name) for f in dc_fields(PrinterProfile)}
    apply_machine_overrides(p, {"max_z_velocity": TRIDENT.max_z_velocity})
    for f in dc_fields(PrinterProfile):
        check(getattr(p, f.name) == before[f.name],
              f"clean profile field '{f.name}' round-trips unchanged",
              f"{getattr(p, f.name)!r} vs {before[f.name]!r}")


def test_generate_py_never_passes_a_shared_profile_instance():
    """apply_machine_overrides mutates its argument in place -- generate.py
    must always hand it a profile this call owns, never TRIDENT or a
    PRINTER_PROFILES[...] entry directly (CLAUDE.md: no machine limit may be
    a module constant, and PRINTER_PROFILES must never be corrupted for
    every future caller by one config file)."""
    import inspect
    import generate
    src = inspect.getsource(generate)
    i = src.find("apply_machine_overrides(")
    check(i != -1, "generate.py calls apply_machine_overrides")
    # The two profile-construction sites immediately above that call, in
    # generate.py's own source: dataclasses.replace(...) for --printer (a
    # NEW instance) and a bare PrinterProfile(...) otherwise (also new).
    # Neither is `profiles[args.printer]` or `TRIDENT` passed directly.
    window = src[max(0, i - 700):i]
    check("replace(profiles[args.printer]" in window or "PrinterProfile(nozzle_diameter=" in window,
          "the profile reaching apply_machine_overrides is a fresh copy "
          "(dataclasses.replace(...) or PrinterProfile(...)), not a shared "
          "instance", window[-200:])
    check("apply_machine_overrides(TRIDENT" not in src
          and "apply_machine_overrides(PRINTER_PROFILES" not in src,
          "apply_machine_overrides is never called directly on TRIDENT or "
          "a PRINTER_PROFILES entry")


def main() -> int:
    test_unknown_key_exits_1()
    test_nan_rejected()
    test_infinity_rejected()
    test_non_numeric_string_rejected()
    test_string_bool_rejected_not_coerced_true()
    test_real_json_bool_accepted()
    test_unsafe_combination_rejected_by_whole_profile_validation()
    test_out_of_range_z_amp_max_is_clamped_not_left_unsafe()
    test_clean_override_applies_normally()
    test_clean_profile_round_trips_byte_identical()
    test_generate_py_never_passes_a_shared_profile_instance()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
