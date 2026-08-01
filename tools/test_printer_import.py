#!/usr/bin/env python3
"""Tests for the custom-printer import pipeline (parser, validator, store).

Plain ``python tools/test_printer_import.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors tools/check_regression.py's style.

Fixtures live in tools/fixtures/printers/.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tools" / "fixtures" / "printers"
sys.path.insert(0, str(ROOT))

from trident_gcode.printer_import import parse as parse_printer_config, PrinterImportError
from trident_gcode.printer_validate import validate_raw, validate_profile_dict, sanitize_gcode
from trident_gcode.profile import TRIDENT, PRINTER_PROFILES
from trident_gcode.gcode import GcodeWriter
from trident_gcode.paths import SpiralSpec, circle
from trident_gcode.generators import build_continuous_spiral
from trident_gcode.analyze import analyze_gcode


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
# 1. klipper_trident.cfg -- must reproduce the built-in TRIDENT profile.
# ---------------------------------------------------------------------------
def test_klipper_trident():
    text = _read_fixture("klipper_trident.cfg")
    raw = parse_printer_config(text, "klipper_trident.cfg")
    check(raw.fmt == "klipper_cfg", "klipper_trident: detected format")

    vr = validate_raw(raw)
    check(vr.ok, "klipper_trident: validates clean (ok)",
          "; ".join(i.message for i in vr.issues if i.severity == "error"))

    p = vr.profile
    check(p.bed_size_x == TRIDENT.bed_size_x and p.bed_size_y == TRIDENT.bed_size_y,
          "klipper_trident: bed size matches TRIDENT",
          f"{p.bed_size_x},{p.bed_size_y} vs {TRIDENT.bed_size_x},{TRIDENT.bed_size_y}")
    check(p.z_max == TRIDENT.z_max, "klipper_trident: z_max matches TRIDENT",
          f"{p.z_max} vs {TRIDENT.z_max}")
    check(
        (p.print_min_x, p.print_min_y, p.print_max_x, p.print_max_y)
        == (TRIDENT.print_min_x, TRIDENT.print_min_y, TRIDENT.print_max_x, TRIDENT.print_max_y),
        "klipper_trident: print area matches TRIDENT")
    check(
        (p.max_velocity, p.max_z_velocity, p.max_accel, p.max_z_accel)
        == (TRIDENT.max_velocity, TRIDENT.max_z_velocity, TRIDENT.max_accel, TRIDENT.max_z_accel),
        "klipper_trident: motion limits match TRIDENT")
    check(p.nozzle_diameter == TRIDENT.nozzle_diameter, "klipper_trident: nozzle matches TRIDENT")
    check(p.has_probe and (p.probe_dx, p.probe_dy) == (TRIDENT.probe_dx, TRIDENT.probe_dy),
          "klipper_trident: probe offsets match TRIDENT")

    rendered_imported = p.start_gcode.format_map(
        {"nozzle_temp": 210.0, "bed_temp": 60.0, "material": "PLA"})
    rendered_trident = TRIDENT.start_gcode.format_map(
        {"nozzle_temp": 210.0, "bed_temp": 60.0, "material": "PLA"})
    check(
        rendered_imported.splitlines()[0].startswith("PRINT_START EXTRUDER=210 BED=60")
        and rendered_trident.splitlines()[0].startswith("PRINT_START EXTRUDER=210 BED=60"),
        "klipper_trident: start_gcode renders the same PRINT_START EXTRUDER=.. BED=.. call",
        rendered_imported.splitlines()[0])


# ---------------------------------------------------------------------------
# 2. orca_machine.json -- placeholder normalization + template rendering.
# ---------------------------------------------------------------------------
def test_orca_machine():
    text = _read_fixture("orca_machine.json")
    raw = parse_printer_config(text, "orca_machine.json")
    check(raw.fmt == "orca_json", "orca_machine: detected format")

    vr = validate_raw(raw)
    check(vr.ok, "orca_machine: validates clean (ok)",
          "; ".join(i.message for i in vr.issues if i.severity == "error"))

    sg = vr.profile.start_gcode
    check("{nozzle_temp:.0f}" in sg and "{bed_temp:.0f}" in sg,
          "orca_machine: placeholders normalized to {nozzle_temp:.0f}/{bed_temp:.0f}", sg)
    try:
        rendered = sg.format_map({"nozzle_temp": 210.0, "bed_temp": 60.0, "material": "PLA"})
        ok = "210" in rendered and "60" in rendered
    except (KeyError, IndexError):
        ok = False
    check(ok, "orca_machine: template renders without raising")
    check(vr.profile.max_z_velocity == 12.0, "orca_machine: max_z_velocity took first list element")


# ---------------------------------------------------------------------------
# 3. prusa_ender3.ini -- literal \n unescape + bed size + G28 detection.
# ---------------------------------------------------------------------------
def test_prusa_ender3():
    text = _read_fixture("prusa_ender3.ini")
    raw = parse_printer_config(text, "prusa_ender3.ini")
    check(raw.fmt == "prusa_ini", "prusa_ender3: detected format")

    check("\\n" not in raw.fields["start_gcode"].value and "\n" in raw.fields["start_gcode"].value,
          "prusa_ender3: literal backslash-n unescaped to real newlines")

    vr = validate_raw(raw)
    check(vr.ok, "prusa_ender3: validates clean (ok)",
          "; ".join(i.message for i in vr.issues if i.severity == "error"))
    check(vr.profile.bed_size_x == 220.0 and vr.profile.bed_size_y == 220.0,
          "prusa_ender3: bed size 220x220", f"{vr.profile.bed_size_x},{vr.profile.bed_size_y}")

    import re
    check(bool(re.search(r'^\s*G28\b', vr.profile.start_gcode, re.MULTILINE)),
          "prusa_ender3: G28 detected in sanitized start_gcode")


# ---------------------------------------------------------------------------
# 3b. cura_ender3.def.json -- Cura printer definition parser.
#
# Covers: format detection, value-only 'value' expression settings are
# skipped (never evaluated), the huge machine_max_feedrate_z fdmprinter
# placeholder is clamped and noted, and the fixture's M82 start G-code is
# caught as a hard error by the existing validator (this is the regression
# guard for the relative-extrusion trap -- the generator emits E deltas, so
# M82 would massively over-extrude on a real machine).
# ---------------------------------------------------------------------------
def test_cura_ender3():
    text = _read_fixture("cura_ender3.def.json")
    raw = parse_printer_config(text, "cura_ender3.def.json")
    check(raw.fmt == "cura_def_json", "cura_ender3: detected format", raw.fmt)

    bx = raw.fields.get("bed_size_x")
    by = raw.fields.get("bed_size_y")
    zm = raw.fields.get("z_max")
    check(bx is not None and bx.value == 220.0, "cura_ender3: raw bed_size_x 220",
          str(bx.value if bx else None))
    check(by is not None and by.value == 220.0, "cura_ender3: raw bed_size_y 220",
          str(by.value if by else None))
    check(zm is not None and zm.value == 250.0, "cura_ender3: raw z_max 250",
          str(zm.value if zm else None))

    vr = validate_raw(raw)
    check(vr.profile.bed_size_x == 220.0 and vr.profile.bed_size_y == 220.0
          and vr.profile.z_max == 250.0,
          "cura_ender3: validated profile is bed 220x220x250",
          f"{vr.profile.bed_size_x},{vr.profile.bed_size_y},{vr.profile.z_max}")

    # Case 1: the value-only setting ('machine_nozzle_size': {"value": "=...999999"})
    # must be skipped, never evaluated, and noted by name.
    expr_marker = "999999"
    check("nozzle_diameter" not in raw.fields,
          "cura_ender3: value-only setting produced no field")
    check(any("machine_nozzle_size" in n for n in raw.notes),
          "cura_ender3: value-only setting produced a note naming it", str(raw.notes))
    all_values = json.dumps([pf.value for pf in raw.fields.values()], default=str)
    check(expr_marker not in all_values,
          "cura_ender3: the value expression's contents appear nowhere in a parsed field",
          all_values)

    # Case 2: machine_max_feedrate_z's placeholder is DROPPED, not clamped.
    # Clamping it would land on the LIMITS ceiling of 100 mm/s -- and on the
    # Ender 3 this definition describes, the real figure is 5. Passing a
    # sentinel through the clamp silently converts "unset" into "this app's
    # maximum", which is the exact opposite of the conservative-default rule.
    # Dropping it lets the validator apply its own conservative default.
    zvel = raw.fields.get("max_z_velocity")
    check(zvel is None,
          "cura_ender3: the fdmprinter sentinel is dropped, not carried through",
          str(zvel.value if zvel else None))
    check(any("machine_max_feedrate_z" in n for n in raw.notes),
          "cura_ender3: sentinel feedrate_z produced an explanatory note", str(raw.notes))
    check(vr.profile.max_z_velocity == 10.0,
          "cura_ender3: sentinel feedrate_z falls back to the conservative default (10)",
          str(vr.profile.max_z_velocity))
    zsrc = [f.source for f in vr.fields if f.name == "max_z_velocity"]
    check(zsrc == ["default"],
          "cura_ender3: sentinel feedrate_z is tagged 'default', not 'clamped'", str(zsrc))

    # Case 4: M82 in the fixture's start G-code is a hard error.
    errs = [i.message for i in vr.issues if i.severity == "error"]
    check(any("M82" in m and "over-extrude" in m for m in errs),
          "cura_ender3: M82 start G-code caught as an error", str(errs))
    check(vr.ok is False, "cura_ender3: overall validation is not ok (M82 present)")


# ---------------------------------------------------------------------------
# 3c. machine_center_is_zero -- centre-origin machines must not silently
# produce a negative print_min (this app assumes a front-left origin).
# ---------------------------------------------------------------------------
def test_cura_center_zero():
    text = json.dumps({
        "version": 2,
        "inherits": "fdmprinter",
        "overrides": {
            "machine_name": {"default_value": "Custom Centre-Origin Machine"},
            "machine_width": {"default_value": 200},
            "machine_depth": {"default_value": 200},
            "machine_height": {"default_value": 300},
            "machine_center_is_zero": {"default_value": True},
            "machine_gcode_flavor": {"default_value": "Marlin"},
            "machine_start_gcode": {"default_value": "G28\nM83\nG92 E0\n"},
            "machine_end_gcode": {"default_value": "M104 S0\nM140 S0\n"},
        },
    })
    raw = parse_printer_config(text, "cura_center_zero.def.json")
    check(raw.fmt == "cura_def_json", "cura_center_zero: detected format")
    check("print_min_x" not in raw.fields and "print_max_x" not in raw.fields
          and "print_min_y" not in raw.fields and "print_max_y" not in raw.fields,
          "cura_center_zero: print range left unset by the parser rather than mangled")
    check(any("machine_center_is_zero" in n for n in raw.notes),
          "cura_center_zero: note added about the centred origin", str(raw.notes))

    vr = validate_raw(raw)
    check(vr.profile.print_min_x >= 0.0 and vr.profile.print_min_y >= 0.0,
          "cura_center_zero: validator's default inset keeps print_min non-negative",
          f"{vr.profile.print_min_x},{vr.profile.print_min_y}")


# ---------------------------------------------------------------------------
# 3d. creality_print_machine.json -- Creality Print (Orca fork) machine
# profile must parse through the existing orca_json path, unmodified.
# ---------------------------------------------------------------------------
def test_creality_print_machine():
    text = _read_fixture("creality_print_machine.json")
    raw = parse_printer_config(text, "creality_print_machine.json")
    check(raw.fmt == "orca_json", "creality_print_machine: detected as orca_json (Orca-fork keys)",
          raw.fmt)

    vr = validate_raw(raw)
    check(vr.ok, "creality_print_machine: validates clean (ok)",
          "; ".join(i.message for i in vr.issues if i.severity == "error"))
    check(vr.profile.firmware == "klipper", "creality_print_machine: firmware klipper")
    check(vr.profile.bed_size_x == 220.0 and vr.profile.bed_size_y == 220.0,
          "creality_print_machine: bed 220x220")


# ---------------------------------------------------------------------------
# 4. hostile.cfg -- the adversarial case.
# ---------------------------------------------------------------------------
def test_hostile():
    text = _read_fixture("hostile.cfg")
    raw = parse_printer_config(text, "hostile.cfg")
    vr = validate_raw(raw)

    p = vr.profile
    check(20.0 <= p.bed_size_x <= 2000.0, "hostile: bed_size_x clamped into LIMITS", str(p.bed_size_x))
    check(0.5 <= p.max_z_velocity <= 100.0, "hostile: max_z_velocity clamped into LIMITS",
          str(p.max_z_velocity))
    check(0.1 <= p.nozzle_diameter <= 2.0, "hostile: nozzle_diameter clamped into LIMITS",
          str(p.nozzle_diameter))
    check(p.max_z_velocity <= p.max_velocity + 1e-9,
          "hostile: max_z_velocity <= max_velocity after validation",
          f"{p.max_z_velocity} vs {p.max_velocity}")

    has_g28_error = any(
        i.severity == "error" and "G28" in i.message for i in vr.issues)
    check(has_g28_error, "hostile: missing-G28 error present")

    removed = {
        i.message.split("'")[1] for i in vr.issues
        if i.message.startswith("removed '")
    }
    check({"M502", "M851", "M303"} <= removed, "hostile: M502/M851/M303 stripped", str(removed))
    check("M502" not in p.start_gcode and "M851" not in p.start_gcode and "M303" not in p.start_gcode,
          "hostile: dangerous commands absent from the sanitized profile text")

    try:
        p.start_gcode.format_map({"nozzle_temp": 200.0, "bed_temp": 60.0, "material": "PLA"})
        fmt_ok = True
    except (KeyError, IndexError):
        fmt_ok = False
    check(fmt_ok, "hostile: format_map on sanitized template does not raise")

    check(vr.ok is False, "hostile: ok is False")


# ---------------------------------------------------------------------------
# 5. End-to-end smoke: build a small vase on each imported profile and check
#    analyze_gcode comes back clean.
# ---------------------------------------------------------------------------
def _smoke_build(profile, tmpdir: Path, label: str):
    cx = (profile.print_min_x + profile.print_max_x) / 2.0
    cy = (profile.print_min_y + profile.print_max_y) / 2.0
    usable = min(profile.print_max_x - profile.print_min_x,
                 profile.print_max_y - profile.print_min_y)
    radius = max(5.0, min(15.0, usable / 2.0 - 5.0))
    height = min(15.0, profile.z_max * 0.5)

    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.2,
        bed_temp=60.0, nozzle_temp=200.0, material="PLA",
        print_speed=30.0, first_layer_speed=15.0,
    )
    # Flat (z_amp=0) circular vase: this smoke test exercises the imported
    # profile's plumbing (bounds, feedrate clamping, probe keep-out geometry),
    # not non-planar geometry -- keeping it flat avoids incidental probe/
    # unsupported-extrusion issues that would be about the WAVE shape, not
    # about whether the import pipeline wired the profile correctly.
    spec = SpiralSpec(base_radius=radius, height=height, layer_height=0.2,
                       points_per_turn=120, z_amp=0.0, z_waves=0)
    shape = circle(radius)
    build_continuous_spiral(writer, spec, shape=shape, center=(cx, cy), base_layers=2)

    out_path = tmpdir / f"{label}.gcode"
    writer.save(str(out_path))
    return out_path


def test_smoke(tmpdir: Path):
    cases = [
        ("klipper_trident.cfg", "smoke_klipper_trident"),
        ("orca_machine.json", "smoke_orca_machine"),
        ("prusa_ender3.ini", "smoke_prusa_ender3"),
        ("creality_print_machine.json", "smoke_creality_print_machine"),
    ]
    for fixture_name, label in cases:
        text = _read_fixture(fixture_name)
        raw = parse_printer_config(text, fixture_name)
        vr = validate_raw(raw)
        if not vr.ok:
            check(False, f"{label}: profile validates (prerequisite for smoke test)",
                  "; ".join(i.message for i in vr.issues if i.severity == "error"))
            continue
        out_path = _smoke_build(vr.profile, tmpdir, label)
        a = analyze_gcode(str(out_path), vr.profile)
        check(a.issues == [], f"{label}: analyze_gcode reports no issues", str(a.issues))
        check(a.max_z_rate <= vr.profile.max_z_velocity + 0.1,
              f"{label}: max_z_rate within max_z_velocity",
              f"{a.max_z_rate} vs {vr.profile.max_z_velocity}")


# ---------------------------------------------------------------------------
# 6. Store round-trip + key validation.
# ---------------------------------------------------------------------------
def test_store_roundtrip(tmp_store: Path):
    # Point printer_store at a scratch directory by monkeypatching store_dir,
    # so this test never touches the real repo's custom_printers/.
    import trident_gcode.printer_store as store_mod

    def fake_store_dir():
        tmp_store.mkdir(parents=True, exist_ok=True)
        return str(tmp_store)

    orig_store_dir = store_mod.store_dir
    store_mod.store_dir = fake_store_dir
    store_mod._invalidate_cache()
    try:
        text = _read_fixture("klipper_trident.cfg")
        raw = parse_printer_config(text, "klipper_trident.cfg")
        vr = validate_raw(raw)
        key = store_mod.make_key(vr.profile.name)
        check(key.startswith("custom_"), "store: make_key produces a custom_ prefixed key", key)

        store_mod.save_custom(key, vr.profile, {"source_format": "klipper_cfg", "warnings": []})
        listed = store_mod.list_custom()
        check(key in listed, "store: saved key appears in list_custom()")

        loaded = store_mod.load_custom(key)
        check(loaded is not None, "store: load_custom returns the saved entry")
        if loaded is not None:
            check(asdict(loaded[0]) == asdict(vr.profile), "store: loaded profile equals saved profile")

        merged = store_mod.all_profiles()
        check(key in merged and "trident" in merged,
              "store: all_profiles() merges built-ins with custom")

        check(store_mod.delete_custom(key) is True, "store: delete_custom removes the entry")
        check(key not in store_mod.list_custom(), "store: deleted key no longer listed")

        for bad_key in ("../evil", "custom_../x", "trident", "", "custom_" + "x" * 49):
            check(not store_mod._is_valid_key(bad_key),
                  f"store: key validation rejects {bad_key!r}")
            ok_save = True
            try:
                store_mod.save_custom(bad_key, vr.profile, {})
                ok_save = False
            except ValueError:
                pass
            check(ok_save, f"store: save_custom rejects {bad_key!r}")
    finally:
        store_mod.store_dir = orig_store_dir
        store_mod._invalidate_cache()


# ---------------------------------------------------------------------------
# 6b. Presence checks must read COMMANDS, not comments.
#
# These three all shipped as real holes once: the homing/cool-down checks
# searched the raw text, so a comment merely MENTIONING a start macro silenced
# the "never homes the printer" error entirely -- the single check most likely
# to save a bed. M82 was also accepted as satisfying the extrusion-mode check,
# when in fact it is the dangerous case: the generator emits relative E deltas,
# which an absolute-mode printer reads as absolute targets.
# ---------------------------------------------------------------------------
def _severities(text: str, kind: str = "start"):
    _clean, issues, _stripped = sanitize_gcode(text, kind)
    return ([i.message for i in issues if i.severity == "error"],
            [i.message for i in issues if i.severity == "warn"])


def test_comment_evasion():
    errs, _ = _severities("; see PRINT_START in printer.cfg\nM104 S200\nG1 Z5 F600")
    check(any("never homes" in m for m in errs),
          "evasion: a comment naming PRINT_START does not satisfy the homing check")

    errs, _ = _severities("SET_GCODE_VARIABLE MACRO=PRINT_START VARIABLE=x VALUE=1\nM104 S200")
    check(any("never homes" in m for m in errs),
          "evasion: a macro named in ARGUMENTS is not a start-macro call")

    errs, _ = _severities("G28\nM109 S200\nM82\nG92 E0")
    check(any("M82" in m and "over-extrude" in m for m in errs),
          "evasion: explicit M82 is an error (generator emits relative E)")

    _errs, warns = _severities("; remember M104 S0 later\nG91\nG1 Z5", "end")
    check(any("heaters may be left on" in m for m in warns),
          "evasion: a comment naming M104 S0 does not satisfy the cool-down check")

    # Legitimate start blocks must stay clean -- a check that flags everything
    # is as useless as one that flags nothing.
    errs, _ = _severities(
        "PRINT_START EXTRUDER={nozzle_temp:.0f} BED={bed_temp:.0f}\nM83\nG92 E0\nM107")
    check(not errs, "evasion: a real Klipper start macro call still validates clean", str(errs))
    errs, _ = _severities("G28\nM190 S60\nM109 S210\nM83\nG92 E0")
    check(not errs, "evasion: a real Marlin start block still validates clean", str(errs))


# ---------------------------------------------------------------------------
# 6c. Non-finite numbers must never reach a PrinterProfile.
#
# NaN defeats every guard it passes through rather than tripping them: all its
# comparisons are False, so it survives min()/max() clamping and would sail
# straight through GcodeWriter._check_bounds too. Python's json.loads accepts
# the bare tokens NaN/Infinity, so this is reachable from the HTTP API.
# ---------------------------------------------------------------------------
def test_sanitize_is_idempotent():
    """The review dialog re-validates the current textarea contents on every
    keystroke, so sanitize_gcode runs over its own output constantly. A
    non-idempotent escape grew '{x}' into '{{{{x}}}}' and kept doubling."""
    samples = [
        "G28\nM83\nM109 S200\n; layer {unknown_thing} here",
        "G28\nM83\nM109 S{nozzle_temp:.0f}\nM190 S{bed_temp:.0f}\n; mat {material}",
        "G28\nM83\nM109 S200\nSET_X V={printer.toolhead.x}\n{% if foo %}",
        "G28\nM83\nM109 S200\n; oops {",
    ]
    for text in samples:
        first, _issues, _stripped = sanitize_gcode(text, "start")
        again, issues, _ = sanitize_gcode(first, "start")
        third, _issues3, _ = sanitize_gcode(again, "start")
        label = text.splitlines()[-1][:28]
        check(first == again == third,
              f"idempotent: repeated sanitation is stable ({label!r})",
              f"{first!r} -> {again!r} -> {third!r}")
        try:
            third.format_map({"nozzle_temp": 200.0, "bed_temp": 60.0, "material": "PLA"})
            renders = True
        except (KeyError, IndexError, ValueError):
            renders = False
        check(renders, f"idempotent: still renders after 3 passes ({label!r})")

    # The warning must survive the round-trip, or the user loses the prompt to
    # fix a placeholder that is now silently escaped.
    once, _i, _s = sanitize_gcode("G28\nM83\nM109 S200\n; {stray}", "start")
    _twice, issues, _s = sanitize_gcode(once, "start")
    check(any("placeholder" in i.message for i in issues),
          "idempotent: unrecognised-placeholder warning persists on re-validation")


def test_non_finite_rejected():
    import math

    base = {"name": "x", "bed_size_x": 235, "bed_size_y": 235, "z_max": 160,
            "start_gcode": "G28\nM83\nM109 S200", "end_gcode": "M104 S0"}
    numeric = ["bed_size_x", "bed_size_y", "z_max", "max_velocity", "max_z_velocity",
               "max_accel", "max_z_accel", "nozzle_diameter", "filament_diameter",
               "probe_dx", "probe_dy", "z_amp_max", "max_nozzle_temp", "max_bed_temp"]
    leaked = []
    for name in numeric:
        for bad in (float("nan"), float("inf"), float("-inf")):
            prof = validate_profile_dict(dict(base, **{name: bad})).profile
            v = getattr(prof, name)
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                leaked.append(f"{name}={bad}")
    check(not leaked, "non-finite: no NaN/Inf reaches the profile", str(leaked[:6]))

    r = validate_profile_dict(dict(base, max_z_velocity=float("nan")))
    check(not r.ok, "non-finite: NaN is reported as an error, not silently defaulted")

    # The HTTP boundary refuses the bare JSON tokens outright.
    import json as _json
    from serve import _reject_nonfinite
    raised = False
    try:
        _json.loads('{"z_amp_max": NaN}', parse_constant=_reject_nonfinite)
    except ValueError:
        raised = True
    check(raised, "non-finite: json body parser rejects the bare NaN token")


# ---------------------------------------------------------------------------
# 6d. Every built-in profile must pass its OWN validator.
#
# This is the check that catches a built-in profile shipping M82 (the
# generator emits relative E deltas -- an absolute-mode start block would
# massively over-extrude) or a start/end template that can't render. Must
# cover every entry in PRINTER_PROFILES, Trident and Bambu included, not just
# the new Creality ones.
# ---------------------------------------------------------------------------
def test_builtin_profiles_self_validate():
    fmt_kwargs = {"nozzle_temp": 210.0, "bed_temp": 60.0, "material": "PLA"}
    for key, p in PRINTER_PROFILES.items():
        _clean_start, start_issues, _s = sanitize_gcode(p.start_gcode, "start")
        _clean_end, end_issues, _s2 = sanitize_gcode(p.end_gcode, "end")
        start_errors = [i.message for i in start_issues if i.severity == "error"]
        end_errors = [i.message for i in end_issues if i.severity == "error"]
        check(not start_errors, f"builtin {key}: start_gcode has zero validator errors",
              str(start_errors))
        check(not end_errors, f"builtin {key}: end_gcode has zero validator errors",
              str(end_errors))

        try:
            p.start_gcode.format_map(fmt_kwargs)
            start_renders = True
        except (KeyError, IndexError, ValueError) as e:
            start_renders = False
            start_err = str(e)
        check(start_renders, f"builtin {key}: start_gcode template renders",
              "" if start_renders else start_err)

        try:
            p.end_gcode.format_map(fmt_kwargs)
            end_renders = True
        except (KeyError, IndexError, ValueError) as e:
            end_renders = False
            end_err = str(e)
        check(end_renders, f"builtin {key}: end_gcode template renders",
              "" if end_renders else end_err)


def test_builtin_start_gcode_z_feedrates():
    """No built-in template may command a pure-Z move faster than its own
    max_z_velocity.

    Slicer start G-code hardcodes F3000 lifts everywhere and the firmware
    clamps them harmlessly, so this looks cosmetic -- but analyze_gcode reads
    the emitted file, and a 50 mm/s lift on a 30 mm/s machine made EVERY Bambu
    print report a peak-Z-rate warning. A warning that fires unconditionally
    is one users learn to scroll past, which costs more than it saves.
    """
    import re as _re
    for key, p in PRINTER_PROFILES.items():
        worst = 0.0
        worst_line = ""
        for block in (p.start_gcode, p.end_gcode):
            for line in block.splitlines():
                s = line.split(";", 1)[0].strip().upper()
                if not (s.startswith("G1") or s.startswith("G0")):
                    continue
                if " Z" not in " " + s:
                    continue
                # Only pure-Z moves: with no X/Y component the whole commanded
                # feedrate lands on the Z axis.
                if _re.search(r'\b[XY]-?[\d{]', s):
                    continue
                m = _re.search(r'\bF(\d+(?:\.\d+)?)', s)
                if m and float(m.group(1)) / 60.0 > worst:
                    worst = float(m.group(1)) / 60.0
                    worst_line = line.strip()
        check(worst <= p.max_z_velocity + 1e-6,
              f"builtin {key}: no pure-Z move exceeds max_z_velocity",
              f"{worst:g} mm/s > {p.max_z_velocity:g} in: {worst_line}")


# ---------------------------------------------------------------------------
# 6e. Creality Z limits guard -- stock Marlin Creality machines cap Z at
# ~5 mm/s (DEFAULT_MAX_FEEDRATE), and the Klipper-flashed ones in this table
# are still conservative entries. This guards against someone later
# "optimising" these upward without measuring a real machine.
# ---------------------------------------------------------------------------
def test_creality_limits():
    creality_keys = [k for k in PRINTER_PROFILES if k.startswith("creality_")]
    check(len(creality_keys) == 9, "creality: 9 built-in Creality profiles registered",
          str(sorted(creality_keys)))
    for key in creality_keys:
        p = PRINTER_PROFILES[key]
        check(p.max_z_velocity <= 15.0, f"creality {key}: max_z_velocity <= 15 mm/s",
              str(p.max_z_velocity))
        check(p.z_amp_max <= 1.0, f"creality {key}: z_amp_max <= 1.0 mm", str(p.z_amp_max))
        check(p.firmware in ("marlin", "klipper"),
              f"creality {key}: firmware is marlin or klipper", p.firmware)
        check(p.pa_gcode_style == ("klipper" if p.firmware == "klipper" else "marlin"),
              f"creality {key}: pa_gcode_style matches firmware", p.pa_gcode_style)


# ---------------------------------------------------------------------------
# 6f. Every Z-excursion ceiling (wave amplitude, loop row/height) must come
# from the selected PrinterProfile's own z_amp_max, not a hardcoded constant.
#
# This is the regression guard for the core defect: z_amp_max was reported by
# /api/printers but never enforced, so a custom printer declaring 0.40 mm of
# clearance was still allowed the Trident's hardcoded 0.95 -- 2.4x over its
# own stated limit -- while a Bambu's 4.0 mm was needlessly capped at 0.95.
# ---------------------------------------------------------------------------
def test_machine_derived_ceilings():
    from dataclasses import replace
    from serve import amp_ceiling, _parse_loop_spec, _make_interp

    custom_tight = replace(TRIDENT, name="Custom Tight", z_amp_max=0.40)
    cases = [
        ("trident", PRINTER_PROFILES["trident"]),
        ("bambu_a1", PRINTER_PROFILES["bambu_a1"]),
        ("custom_0.40", custom_tight),
    ]

    # 1. amp_ceiling(p) reads straight through to p.z_amp_max.
    for label, p in cases:
        check(amp_ceiling(p) == p.z_amp_max,
              f"amp_ceiling: {label} ceiling equals its own z_amp_max",
              f"{amp_ceiling(p)} vs {p.z_amp_max}")

    # 2. A curve requesting 3.0 mm resolves to min(3.0, the machine's own
    #    ceiling) -- NOT a constant 0.95 for every profile, which is what the
    #    old AMP_MIN/AMP_MAX module constant did. Before the fix every one of
    #    these resolved to 0.95: the 0.40mm custom printer got 2.4x over its
    #    own stated limit, and the 4.0mm Bambu was needlessly capped.
    request_curve = [[0.0, 3.0], [1.0, 3.0]]
    for label, p in cases:
        amp_fn = _make_interp(request_curve, 0.0, amp_ceiling(p))
        resolved = amp_fn(0.5)
        expected = min(3.0, p.z_amp_max)
        check(resolved == expected,
              f"amp curve: {label} requesting 3.0mm resolves to min(3.0, z_amp_max)={expected}",
              f"got {resolved}")
        check(resolved != 0.95 or p.z_amp_max == 0.95,
              f"amp curve: {label} is not silently pinned to the old 0.95 constant",
              f"got {resolved}")
    # Explicitly spell out the safety-relevant case named in the spec: the
    # tight custom printer must yield 0.40, not the Trident's old 0.95.
    tight_fn = _make_interp(request_curve, 0.0, amp_ceiling(custom_tight))
    check(tight_fn(0.5) == 0.40 and tight_fn(0.5) != 0.95,
          "amp curve: 0.40mm custom printer is NOT silently allowed the Trident's 0.95",
          str(tight_fn(0.5)))
    # And the safety-relevant flip side: the 4.0mm Bambu must be able to use
    # its own headroom (3.0mm is well within it) rather than being capped at
    # the Trident's 0.95.
    bambu_fn = _make_interp(request_curve, 0.0, amp_ceiling(PRINTER_PROFILES["bambu_a1"]))
    check(bambu_fn(0.5) == 3.0,
          "amp curve: bambu_a1 keeps its requested 3.0mm instead of being capped at 0.95",
          str(bambu_fn(0.5)))

    # 3. _parse_loop_spec clamps loop_up to the machine's z_amp_max, and the
    #    floor (min(1.0, cap)) never exceeds the ceiling for any built-in
    #    profile -- the bug the spec calls out: a floor of 1.0 was larger than
    #    the Trident's 0.95 ceiling, making the permitted range empty.
    for key, p in PRINTER_PROFILES.items():
        body = {"loop_per_turn": 6, "loop_up": 8.0, "loop_row": 8.0}
        spec = _parse_loop_spec(body, p, radius=30.0)
        check(spec is not None, f"loop spec: {key} loops enabled with per_turn>0")
        if spec is None:
            continue
        cap = amp_ceiling(p)
        cap_row = max(0.4, cap - 0.3)
        check(spec.up_mm <= cap + 1e-9,
              f"loop spec: {key} loop_up=8.0 clamps to z_amp_max ({cap})",
              f"up_mm={spec.up_mm}")
        check(spec.row_mm <= cap_row + 1e-9,
              f"loop spec: {key} loop_row clamps to cap-0.3 floored at 0.4 ({cap_row})",
              f"row_mm={spec.row_mm}")
        check(min(1.0, cap) <= cap + 1e-9,
              f"loop spec: {key} up_mm floor never exceeds its own ceiling")
        check(min(1.0, cap_row) <= cap_row + 1e-9,
              f"loop spec: {key} row_mm floor never exceeds its own ceiling")


# ---------------------------------------------------------------------------
# 7. Regression: built-in profiles must still generate byte-identical output.
# ---------------------------------------------------------------------------
def test_regression():
    import subprocess
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_regression.py")],
        cwd=str(ROOT), capture_output=True, text=True)
    check(result.returncode == 0, "regression: tools/check_regression.py exits 0",
          result.stdout[-800:] + result.stderr[-800:])


def main() -> int:
    test_klipper_trident()
    test_orca_machine()
    test_prusa_ender3()
    test_cura_ender3()
    test_cura_center_zero()
    test_creality_print_machine()
    test_hostile()
    test_comment_evasion()
    test_sanitize_is_idempotent()
    test_non_finite_rejected()
    test_builtin_profiles_self_validate()
    test_builtin_start_gcode_z_feedrates()
    test_creality_limits()
    test_machine_derived_ceilings()

    with tempfile.TemporaryDirectory() as tmp:
        test_smoke(Path(tmp))

    with tempfile.TemporaryDirectory() as tmp:
        test_store_roundtrip(Path(tmp) / "custom_printers")

    test_regression()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
