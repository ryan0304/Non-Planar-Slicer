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
def test_klipper_inline_comments():
    """Vendor configs annotate every line; the parser must survive that.

    Two real failures this guards, both from one user's FLY-D5 config:
    a section header with a trailing comment ("[printer]   # note") does not
    end with ']', so the whole section was silently dropped and its keys read
    back as "not found in config"; and a value with a trailing comment
    ("180   # note") reached the validator with the comment attached and
    failed as "not a number". Klipper reads both fine -- it parses with
    configparser(inline_comment_prefixes=(';', '#')).
    """
    text = _read_fixture("klipper_inline_comments.cfg")
    raw = parse_printer_config(text, "klipper_inline_comments.cfg")
    check(raw.fmt == "klipper_cfg", "inline_comments: detected format", raw.fmt)

    vr = validate_raw(raw)
    p = vr.profile
    check(vr.ok, "inline_comments: validates with no errors",
          "; ".join(i.message for i in vr.issues if i.severity == "error"))

    # The display name is the whole-line comment ABOVE [printer]. Finding it
    # means the name lookup recognises a header that carries a trailing
    # comment; when it compared for an exact "[printer]" it found nothing here
    # and every FLY/BTT config imported under its filename instead. The
    # header's own "# machine settings" must NOT be mistaken for the name.
    check(p.name == "FLY-D5 Mini",
          "inline_comments: display name read from the comment above [printer]",
          repr(p.name))

    # Sections whose HEADER carried a comment must still be found.
    check((p.max_velocity, p.max_z_velocity, p.max_accel, p.max_z_accel)
          == (400.0, 15.0, 7500.0, 100.0),
          "inline_comments: [printer] found despite a commented header",
          f"{p.max_velocity},{p.max_z_velocity},{p.max_accel},{p.max_z_accel}")
    check((p.nozzle_diameter, p.filament_diameter) == (0.4, 1.75),
          "inline_comments: [extruder] found despite a commented header",
          f"{p.nozzle_diameter},{p.filament_diameter}")
    check((p.max_nozzle_temp, p.max_bed_temp) == (280.0, 120.0),
          "inline_comments: heater max_temp values parsed",
          f"{p.max_nozzle_temp},{p.max_bed_temp}")

    # Values whose line carried a comment must parse as numbers.
    check((p.bed_size_x, p.bed_size_y, p.z_max, p.z_min) == (180.0, 180.0, 180.0, -5.0),
          "inline_comments: commented values parsed as numbers",
          f"{p.bed_size_x},{p.bed_size_y},{p.z_max},{p.z_min}")

    # A start macro with no temperature params can't be called with
    # temperatures, so it is inlined -- and that inlined body sets no
    # temperature and no M83, which the user must be told about.
    msgs = [i.message for i in vr.issues if i.severity == "warn"]
    check(any("never sets a temperature" in m for m in msgs),
          "inline_comments: warns that the start macro never heats", str(msgs))
    check(any("no M83" in m for m in msgs),
          "inline_comments: warns that the start macro sets no M83", str(msgs))
    check(any("[include" in n for n in raw.notes),
          "inline_comments: notes that [include] files were not uploaded", str(raw.notes))

    # The macro body gets INLINED (no temperature params to call it with), and
    # its printer.cfg '#' comments must become ';' on the way out. A G-code
    # file has no '#' comment rule -- Klipper strips only ';' -- so a line like
    # "#M117 Printing" would be sent as a command and answered with "Unknown
    # command", aborting the print on the first layer.
    sg = p.start_gcode
    offenders = [ln for ln in sg.splitlines()
                 if ln.strip().startswith("#") or " #" in ln.split(";", 1)[0]]
    check(not offenders,
          "inline_comments: no '#' survives into the emitted start G-code", str(offenders[:3]))
    check(any("'#' comment" in m for m in msgs),
          "inline_comments: warns that '#' comments were rewritten to ';'", str(msgs))


def test_analyze_footprint_is_always_finite():
    """No axis may report an infinite bound, however degenerate the G-code.

    analyze_gcode seeds every axis at +/-inf and only folds an axis into the
    footprint once it has actually been commanded, so that a start-G-code Z
    lift can't record the assumed origin as real toolpath. An axis that is
    never commanded then keeps the sentinel -- and GcodeAnalysis.footprint
    returns -inf, which serve.py rounds straight into json.dumps. The default
    encoder writes the bare token `-Infinity`, which is not valid JSON, so the
    browser's JSON.parse throws away the entire stats response rather than one
    field. Non-finite values must be resolved before they leave the analyzer.
    """
    import math
    with tempfile.TemporaryDirectory() as td:
        # Z-only motion: X and Y are never commanded at all.
        path = os.path.join(td, "z_only.gcode")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("G28\nG90\nM83\nG1 Z5 F600\nG1 Z10 F600\nG1 Z15 F600\n")
        a = analyze_gcode(path)

        finite = all(math.isfinite(v) for v in list(a.min) + list(a.max))
        check(finite, "analyze_footprint: no infinite bound on an uncommanded axis",
              f"min={a.min} max={a.max}")
        check(all(math.isfinite(v) for v in a.footprint) and math.isfinite(a.height),
              "analyze_footprint: footprint and height are finite",
              f"footprint={a.footprint} height={a.height}")

        # The actual failure mode: serve.py's stats dict must round-trip.
        stats = {"height_mm": round(a.height, 1),
                 "footprint_mm": [round(a.footprint[0], 1), round(a.footprint[1], 1)]}
        blob = json.dumps(stats)
        check("Infinity" not in blob and "NaN" not in blob,
              "analyze_footprint: stats serialize to valid JSON", blob)
        try:
            json.loads(blob, parse_constant=_reject_constant)
            parse_ok = True
        except ValueError:
            parse_ok = False
        check(parse_ok, "analyze_footprint: a strict JSON parser accepts the stats", blob)

        # An axis that IS commanded still reports its true span, not 0 -- the
        # sentinel fix must not undo the thing the sentinel was added for.
        path2 = os.path.join(td, "xy.gcode")
        with open(path2, "w", encoding="utf-8") as fh:
            fh.write("G28\nG90\nM83\nG1 Z0.2 F600\nG1 X60 Y60 F3000\n"
                     "G1 X120 Y60 E1 F1200\nG1 X120 Y120 E1\n")
        b = analyze_gcode(path2)
        check((b.min[0], b.max[0], b.min[1], b.max[1]) == (60.0, 120.0, 60.0, 120.0),
              "analyze_footprint: a commanded axis still reports its true span",
              f"min={b.min} max={b.max}")


def _reject_constant(name: str):
    """json.loads accepts the bare tokens NaN/Infinity by default; this makes
    them an error so the test cannot pass on a value a strict parser rejects."""
    raise ValueError(f"non-finite constant {name} in JSON")


def test_hash_comment_is_not_a_command():
    """A '#' comment must never read as a command.

    Every presence and blocked-token check runs on _command_text, which strips
    ';' and '(...)' but knows nothing about '#'. printer.cfg comments with '#',
    so before those comments were rewritten a line was scanned with a command
    head of literally '#PRINT_START' or '#M502'. Two consequences, both real:

      * _has_start_macro_call searches the head for PRINT_START, and
        '#PRINT_START' contains it -- so a comment merely MENTIONING the macro
        counted as a call, which suppresses both the missing-G28 error and the
        missing-temperature warning. A config that never homes was cleared.
      * '#M502' matches no blocked token, so the stripper left it in place.

    ('#G28' was never a hole: the homing check anchors on ^\\s*G28 and '#' is
    not whitespace. The macro-call check is the one that anchors loosely.)
    """
    # 1. A commented mention of the start macro must not suppress the checks.
    clean, issues, _ = sanitize_gcode(
        "#PRINT_START is called by the slicer\nG1 Z5 F600\n", "start")
    errs = [i.message for i in issues if i.severity == "error"]
    check(any("G28" in m for m in errs),
          "hash_comment: '#PRINT_START' does not count as a start-macro call",
          str(errs))
    check("#" not in clean,
          "hash_comment: no '#' survives sanitation", repr(clean))
    check("PRINT_START" in clean,
          "hash_comment: the author's text is rewritten, not deleted", repr(clean))

    # 2. A real macro call still counts -- the rule must not break working
    #    configs, or every Klipper user gets an error they have to ignore.
    _, issues_ok, _ = sanitize_gcode(
        "PRINT_START EXTRUDER=200 BED=60    # start the print\n", "start")
    check(not any(i.severity == "error" for i in issues_ok),
          "hash_comment: a real PRINT_START with a trailing '#' comment still counts",
          str([i.message for i in issues_ok]))

    # 3. Blocked tokens must not hide behind a leading '#'.
    clean3, _, _ = sanitize_gcode("G28\nM104 S200\n#M502\nM83\n", "start")
    commanded3 = [ln.strip() for ln in clean3.splitlines()
                  if ln.strip() and not ln.strip().startswith(";")]
    check(not any("M502" in ln.upper() for ln in commanded3),
          "hash_comment: '#M502' is not left as an executable line", str(commanded3))

    # 4. A '#' that is NOT comment syntax (no preceding whitespace) is left
    #    alone, matching configparser's inline-comment rule.
    clean4, _, _ = sanitize_gcode("G28\nM117 lot#42\nM83\n", "start")
    check("lot#42" in clean4,
          "hash_comment: a '#' inside a token is not treated as a comment",
          repr(clean4))


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

    # The error above is the load-bearing one. The fixture's macro body says
    # "#PRINT_START is called by the slicer, honest" -- a printer.cfg comment.
    # Read as a command it is a start-macro CALL, and a start-macro call
    # suppresses both the missing-G28 error and the missing-temperature
    # warning, clearing a config that never homes at all. So has_g28_error
    # above only stays true while '#' is rewritten to ';'.
    commanded = [ln.strip() for ln in p.start_gcode.splitlines()
                 if ln.strip() and not ln.strip().startswith(";")]
    check(not any("PRINT_START" in ln.split()[0].upper() for ln in commanded),
          "hostile: no line CALLS PRINT_START -- the '#PRINT_START' is a comment",
          str(commanded))

    removed = {
        i.message.split("'")[1] for i in vr.issues
        if i.message.startswith("removed '")
    }
    check({"M502", "M851", "M303"} <= removed, "hostile: M502/M851/M303 stripped", str(removed))
    # Commanded lines only, not a substring scan of the whole block: the
    # fixture's "#M502" is preserved as the comment ";M502" on purpose, and a
    # comment naming a dangerous code is exactly what must be allowed through
    # while the command itself is not.
    check(not any(ln.split()[0].upper() in {"M502", "M851", "M303"} for ln in commanded),
          "hostile: dangerous commands absent from the sanitized profile text",
          str(commanded))

    try:
        p.start_gcode.format_map({"nozzle_temp": 200.0, "bed_temp": 60.0, "material": "PLA"})
        fmt_ok = True
    except (KeyError, IndexError):
        fmt_ok = False
    check(fmt_ok, "hostile: format_map on sanitized template does not raise")

    check(vr.ok is False, "hostile: ok is False")

    # --- repair=True must coexist safely with every adversarial check above ---
    # This is hostile.cfg's counter-example for auto-repair-at-import
    # (printer_validate._repair_missing_heating): the macro body hides a fake
    # "#M109 S200" behind a '#' comment. Read as a command that would satisfy
    # the "already has heating" check and suppress repair entirely, leaving
    # the real abort-on-first-extrusion bug unfixed. Read correctly as a
    # comment (after the '#'->';' rewrite), repair still fires.
    vr2 = validate_raw(raw, repair=True)
    p2 = vr2.profile
    commanded2 = [ln.strip() for ln in p2.start_gcode.splitlines()
                  if ln.strip() and not ln.strip().startswith(";")]
    check(any(ln.split()[0].upper() == "M109" for ln in commanded2),
          "hostile+repair: a REAL M109 is inserted despite the fake commented one",
          str(commanded2))
    check(any(ln.split()[0].upper() == "M140" for ln in commanded2),
          "hostile+repair: a REAL M140 (bed heat) is inserted", str(commanded2))

    # The fixture's macro body already has a real (uncommented) M83 -- repair
    # must not insert a second one next to it.
    m83_count = sum(1 for ln in commanded2 if ln.split()[0].upper() == "M83")
    check(m83_count == 1, "hostile+repair: M83 is not duplicated", str(commanded2))

    # Dangerous tokens must still be stripped when repair=True -- repair is
    # not a bypass of the rest of sanitize_gcode.
    check(not any(ln.split()[0].upper() in {"M502", "M851", "M303"} for ln in commanded2),
          "hostile+repair: dangerous commands are still stripped", str(commanded2))

    # No G28 anywhere in the fixture and no real start-macro call -- repair
    # only adds heating/M83, it does not and must not fabricate homing.
    has_g28_error2 = any(
        i.severity == "error" and "G28" in i.message for i in vr2.issues)
    check(has_g28_error2, "hostile+repair: missing-G28 error still present after repair")
    check(vr2.ok is False, "hostile+repair: ok is still False (G28 still missing)")

    try:
        p2.start_gcode.format_map({"nozzle_temp": 200.0, "bed_temp": 60.0, "material": "PLA"})
        fmt_ok2 = True
    except (KeyError, IndexError):
        fmt_ok2 = False
    check(fmt_ok2, "hostile+repair: format_map on the repaired template does not raise")


# ---------------------------------------------------------------------------
# 4b. klipper_delegating_macro.cfg -- the real user report, end to end.
#
# A PRINT_START macro with no EXTRUDER/BED params gets inlined verbatim by
# the importer. Its body resets the extruder, homes, calls the user's own
# sub-macro (PRINT_HUAXIAN), and lifts Z -- and never heats anything and
# never sets M83. Klipper refuses to extrude below min_extrude_temp (default
# 170C), so this is a guaranteed abort on the first extrusion move, not a
# risk -- and WITHOUT repair, it still validates ok:True (only a warning),
# which is exactly the historical bug this feature fixes.
# ---------------------------------------------------------------------------
def test_klipper_delegating_macro():
    text = _read_fixture("klipper_delegating_macro.cfg")
    raw = parse_printer_config(text, "klipper_delegating_macro.cfg")
    check(raw.fmt == "klipper_cfg", "delegating_macro: detected format", raw.fmt)

    # Without repair (the default -- what re-validation/editing uses), this
    # reproduces the historical bug: it validates ok:True with only a
    # warning, even though the block can never actually print.
    vr_norepair = validate_raw(raw)
    check(vr_norepair.ok,
          "delegating_macro: without repair, this reproduces the historical bug "
          "(ok:True despite being unprintable)",
          "; ".join(i.message for i in vr_norepair.issues if i.severity == "error"))
    warn_msgs0 = [i.message for i in vr_norepair.issues if i.severity == "warn"]
    check(any("never sets a temperature" in m for m in warn_msgs0),
          "delegating_macro: without repair, only the ordinary warning fires",
          str(warn_msgs0))
    check("M140" not in vr_norepair.profile.start_gcode,
          "delegating_macro: without repair, the block is left broken",
          vr_norepair.profile.start_gcode)

    # With repair=True (the actual import path -- serve.py's
    # /api/printer/parse and generate.py's --import-printer), the block is
    # fixed automatically.
    vr = validate_raw(raw, repair=True)
    check(vr.ok, "delegating_macro: validates clean (ok) once repaired",
          "; ".join(i.message for i in vr.issues if i.severity == "error"))
    p = vr.profile
    heads = [ln.split()[0] for ln in p.start_gcode.splitlines()
              if ln.strip() and not ln.strip().startswith(";")]
    check(heads == ["M140", "G92", "G28", "M190", "M104", "M109", "PRINT_HUAXIAN", "G1", "M83"],
          "delegating_macro: repaired order matches the mandated placement", str(heads))

    warn_msgs = [i.message for i in vr.issues if i.severity == "warn"]
    check(any("Automatically added" in m for m in warn_msgs),
          "delegating_macro: repair issue explains what was added", str(warn_msgs))
    check(not any("never sets a temperature" in m for m in warn_msgs),
          "delegating_macro: the now-inaccurate warning is gone, replaced by the repair issue",
          str(warn_msgs))

    try:
        p.start_gcode.format_map({"nozzle_temp": 210.0, "bed_temp": 60.0, "material": "PLA"})
        renders = True
    except (KeyError, IndexError, ValueError):
        renders = False
    check(renders, "delegating_macro: repaired start_gcode renders without raising")


# ---------------------------------------------------------------------------
# 4c. Auto-repair unit coverage: order/placement, gating conditions, and the
# opt-in-only-at-import contract, all exercised directly through
# sanitize_gcode(..., repair=True) for precise control over each case.
# ---------------------------------------------------------------------------
_BACKGROUND_BLOCK = (
    "G92 E0                         # reset extruder\n"
    "G28                            # home all\n"
    "PRINT_HUAXIAN                  # their own sub-macro\n"
    "G1 Z20 F3000                   # raise gantry"
)


def test_auto_repair_start_gcode():
    # --- A. the real user report: exact order/placement --------------------
    clean, issues, _stripped = sanitize_gcode(_BACKGROUND_BLOCK, "start", repair=True)
    lines = clean.splitlines()
    heads = [ln.split()[0] for ln in lines]
    check(heads == ["M140", "G92", "G28", "M190", "M104", "M109", "PRINT_HUAXIAN", "G1", "M83"],
          "repair: real-world block gets the exact mandated order/placement", str(heads))
    check(lines[0].startswith("M140 S{bed_temp:.0f}"), "repair: M140 is the very first line", lines[0])
    check(lines[3].startswith("M190 S{bed_temp:.0f}") and lines[4].startswith("M104 S{nozzle_temp:.0f}")
          and lines[5].startswith("M109 S{nozzle_temp:.0f}"),
          "repair: M190/M104/M109 sit immediately after the last G28, in that order", str(lines[3:6]))
    check(lines[-1].startswith("M83"), "repair: M83 is appended at the very end", lines[-1])
    warn_msgs = [i.message for i in issues if i.severity == "warn"]
    check(any("Automatically added" in m and "M140" in m for m in warn_msgs),
          "repair: issue explains the added heating lines", str(warn_msgs))
    check(any("appended M83" in m for m in warn_msgs),
          "repair: issue explains the added M83 line", str(warn_msgs))
    check(not any(i.severity == "error" for i in issues),
          "repair: the real-world block has zero errors after repair", str(issues))
    try:
        clean.format_map({"nozzle_temp": 210.0, "bed_temp": 60.0, "material": "PLA"})
        renders = True
    except (KeyError, IndexError, ValueError):
        renders = False
    check(renders, "repair: the repaired real-world block still survives format_map")

    # --- B. no G28 at all: the three post-home commands land right after M140 ---
    clean_b, _i, _s = sanitize_gcode("PRINT_HUAXIAN\nG1 Z5 F600", "start", repair=True)
    heads_b = [ln.split()[0] for ln in clean_b.splitlines()]
    check(heads_b == ["M140", "M190", "M104", "M109", "PRINT_HUAXIAN", "G1", "M83"],
          "repair: with no G28 at all, heating lands directly after M140", str(heads_b))

    # --- C. heating already present (any of the 4 forms) -> no repair at all ---
    for existing in ("G28\nM104 S200\nM83", "G28\nM109 S200\nM83",
                     "G28\nM140 S60\nM83", "G28\nM190 S60\nM83"):
        clean_c, issues_c, _s = sanitize_gcode(existing, "start", repair=True)
        check(clean_c == existing,
              f"repair: no-op when heating already present ({existing.splitlines()[1]})",
              repr(clean_c))
        check(not any("automatically added" in i.message.lower() for i in issues_c),
              "repair: no repair issue is raised when heating is already present",
              str([i.message for i in issues_c]))

    # --- D. a parameterized start-macro call counts as heating -> no repair ---
    clean_d, _issues_d, _s = sanitize_gcode(
        "PRINT_START EXTRUDER=210 BED=60\nG28\nM83", "start", repair=True)
    check("M140" not in clean_d and "auto-added" not in clean_d,
          "repair: a parameterized macro call (literal temps) suppresses repair entirely", clean_d)
    clean_d2, _issues_d2, _s = sanitize_gcode(
        "PRINT_START EXTRUDER={nozzle_temp:.0f} BED={bed_temp:.0f}\nG28\nM83",
        "start", repair=True)
    check("M140" not in clean_d2 and "auto-added" not in clean_d2,
          "repair: a synthesized (placeholder) parameterized macro call also suppresses repair",
          clean_d2)

    # --- E. a COMMENT mentioning a temperature command must not count ------
    clean_e, _issues_e, _s = sanitize_gcode(
        "; M109 S200 is set elsewhere\nG28\nM83", "start", repair=True)
    code_e = [ln.strip() for ln in clean_e.splitlines()
              if ln.strip() and not ln.strip().startswith(";")]
    check(any(ln.split()[0] == "M109" for ln in code_e),
          "repair: a commented '; M109 ...' does not count as heating -- repair still fires",
          str(code_e))

    # E2. Same idea, but for the macro-call-with-params check, and shaped
    # exactly like hostile.cfg's own "#PRINT_START is called by the slicer,
    # honest" counter-example: a '#' with NO space before the macro name.
    # This one is genuinely load-bearing (unlike a plain "; M109 ..." above,
    # which a leading '^\s*' anchor already rejects regardless of whether
    # comments are stripped first): split(None,1)[0] on the raw, un-stripped
    # line glues the marker onto the macro name as ONE token (";PRINT_START"),
    # and a naive substring search on that raw token finds "PRINT_START"
    # inside it and "=" later in the line -- so an implementation that skips
    # _command_text() here would wrongly read this comment as a parameterized
    # start-macro call and suppress repair entirely, leaving the printer
    # unable to heat.
    clean_e2, _issues_e2, _s = sanitize_gcode(
        "#PRINT_START EXTRUDER=210 BED=60 was tried once\nG28\nM83", "start", repair=True)
    code_e2 = [ln.strip() for ln in clean_e2.splitlines()
               if ln.strip() and not ln.strip().startswith(";")]
    check(any(ln.split()[0] == "M109" for ln in code_e2),
          "repair: a '#PRINT_START EXTRUDER=...' comment (no space, hostile.cfg-style) "
          "does not count as a parameterized macro call -- repair still fires",
          str(code_e2))

    # --- F. M82 stays a hard error and is NEVER masked by the M83 repair ---
    clean_f, issues_f, _s = sanitize_gcode("G28\nM82\nG92 E0", "start", repair=True)
    errs_f = [i.message for i in issues_f if i.severity == "error"]
    check(any("M82" in m and "over-extrude" in m for m in errs_f),
          "repair: M82 is still a hard error after repair runs", str(errs_f))
    code_f = [ln.strip() for ln in clean_f.splitlines()
              if ln.strip() and not ln.strip().startswith(";")]
    check(not any(ln.split()[0] == "M83" for ln in code_f),
          "repair: M83 is NOT appended when M82 is present (would mask the error)", str(code_f))
    check(any(ln.split()[0] == "M140" for ln in code_f),
          "repair: heating is still added even though M82 is present (independent problems)",
          str(code_f))

    # --- G. repair=False (the default / re-validation path) never fires ----
    clean_g, issues_g, _s = sanitize_gcode(_BACKGROUND_BLOCK, "start")  # repair defaults False
    check("M140" not in clean_g, "repair: repair=False (default) leaves the block unrepaired", clean_g)
    warn_g = [i.message for i in issues_g if i.severity == "warn"]
    check(any("never sets a temperature" in m for m in warn_g),
          "repair: repair=False still emits the ordinary missing-temperature warning", str(warn_g))
    check(any("no M83" in m for m in warn_g),
          "repair: repair=False still emits the ordinary missing-M83 warning", str(warn_g))

    # --- also via validate_profile_dict (the re-validate/save path) --------
    prof_dict = {"name": "x", "bed_size_x": 235, "bed_size_y": 235, "z_max": 250,
                 "start_gcode": _BACKGROUND_BLOCK, "end_gcode": "M104 S0\nM140 S0"}
    vr_reval = validate_profile_dict(prof_dict)  # repair defaults False
    check("M140" not in vr_reval.profile.start_gcode,
          "repair: validate_profile_dict (re-validate/save path) never auto-repairs",
          vr_reval.profile.start_gcode)


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
# 6a. Store directory resolution: per-user path, env override, migration.
#
# store_dir() used to resolve to <repo_root>/custom_printers -- fine for a
# dev checkout, wrong for a shipped app: the install directory is typically
# read-only, and a single shared directory would let one user on a
# multi-user machine see (and silently inherit) another user's imported
# printers. These tests exercise the replacement without ever touching the
# real per-user data directory: TRIDENT_PRINTER_DIR always points at a
# tempfile.TemporaryDirectory(), and the per-platform check calls the pure
# _default_data_dir() helper directly rather than store_dir() itself.
# ---------------------------------------------------------------------------
def test_store_dir_env_override(tmp_path: Path):
    import trident_gcode.printer_store as store_mod

    orig_env = os.environ.get("TRIDENT_PRINTER_DIR")
    orig_migrated = store_mod._migration_attempted
    target = tmp_path / "override_dir"
    try:
        os.environ["TRIDENT_PRINTER_DIR"] = str(target)
        store_mod._migration_attempted = True  # migration is covered separately below
        d = store_mod.store_dir()
        check(os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.abspath(str(target))),
              "store_dir: TRIDENT_PRINTER_DIR override is honoured", f"{d} vs {target}")
        check(os.path.isdir(d), "store_dir: override directory is created if missing")
    finally:
        if orig_env is None:
            os.environ.pop("TRIDENT_PRINTER_DIR", None)
        else:
            os.environ["TRIDENT_PRINTER_DIR"] = orig_env
        store_mod._migration_attempted = orig_migrated

    # A relative override must be REJECTED, not resolved relative to the cwd
    # (ambiguous -- relative to what? a server's launch directory is not
    # something a user should have to reason about for where their printer
    # data lives). Test the pure resolver directly so a bad env var can never
    # cause this test to create/touch the real default data directory.
    orig_env2 = os.environ.get("TRIDENT_PRINTER_DIR")
    try:
        os.environ["TRIDENT_PRINTER_DIR"] = "relative/not_absolute"
        resolved = store_mod._env_override_dir()
        check(resolved is None, "store_dir: a relative TRIDENT_PRINTER_DIR is ignored",
              repr(resolved))
    finally:
        if orig_env2 is None:
            os.environ.pop("TRIDENT_PRINTER_DIR", None)
        else:
            os.environ["TRIDENT_PRINTER_DIR"] = orig_env2


def test_default_data_dir_per_platform():
    import trident_gcode.printer_store as store_mod

    orig_platform = store_mod.sys.platform
    orig_appdata = os.environ.get("APPDATA")
    orig_xdg = os.environ.get("XDG_DATA_HOME")
    try:
        store_mod.sys.platform = "win32"
        os.environ["APPDATA"] = r"C:\Users\tester\AppData\Roaming"
        d = store_mod._default_data_dir()
        check(d == os.path.join(r"C:\Users\tester\AppData\Roaming", "TridentGcode", "printers"),
              "default_data_dir: windows resolves to %APPDATA%\\TridentGcode\\printers", d)

        store_mod.sys.platform = "darwin"
        d = store_mod._default_data_dir()
        expected_mac = os.path.join(os.path.expanduser("~"), "Library",
                                     "Application Support", "TridentGcode", "printers")
        check(d == expected_mac,
              "default_data_dir: macOS resolves to ~/Library/Application Support/TridentGcode/printers", d)

        # os.path.isabs() is bound to the HOST os.path module (ntpath on this
        # test runner even while we masquerade as "linux" for sys.platform),
        # so the absolute path used here must be one the host itself
        # recognises as absolute -- os.path.abspath() guarantees that on any
        # platform, which a hardcoded "/tmp/..." does not on Windows.
        store_mod.sys.platform = "linux"
        xdg_abs = os.path.abspath(os.path.join("xdgdata_test_root"))
        os.environ["XDG_DATA_HOME"] = xdg_abs
        d = store_mod._default_data_dir()
        check(d == os.path.join(xdg_abs, "trident-gcode", "printers"),
              "default_data_dir: linux honours an absolute XDG_DATA_HOME", d)

        os.environ.pop("XDG_DATA_HOME", None)
        d = store_mod._default_data_dir()
        expected_fallback = os.path.join(os.path.expanduser("~"), ".local", "share",
                                          "trident-gcode", "printers")
        check(d == expected_fallback,
              "default_data_dir: linux falls back to ~/.local/share/trident-gcode/printers "
              "when XDG_DATA_HOME is unset", d)

        os.environ["XDG_DATA_HOME"] = "relative/xdg"
        d = store_mod._default_data_dir()
        check(d == expected_fallback,
              "default_data_dir: a relative XDG_DATA_HOME is not honoured, falls back", d)
    finally:
        store_mod.sys.platform = orig_platform
        for name, val in (("APPDATA", orig_appdata), ("XDG_DATA_HOME", orig_xdg)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_migration_copies_without_overwriting(tmp_path: Path):
    import trident_gcode.printer_store as store_mod

    old_dir = tmp_path / "legacy" / "custom_printers"
    new_dir = tmp_path / "newloc"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)

    (old_dir / "custom_a.json").write_text('{"marker": "old-a"}', encoding="utf-8")
    (old_dir / "custom_b.json").write_text('{"marker": "old-b"}', encoding="utf-8")
    (old_dir / "not_custom.json").write_text('{"marker": "ignored"}', encoding="utf-8")
    (new_dir / "custom_a.json").write_text('{"marker": "existing-a"}', encoding="utf-8")

    copied = store_mod._copy_legacy_files(str(old_dir), str(new_dir))
    check(copied == 1, "migration: only the missing file is copied", str(copied))
    check((new_dir / "custom_b.json").exists(), "migration: custom_b.json was copied")
    check(json.loads((new_dir / "custom_a.json").read_text(encoding="utf-8"))["marker"] == "existing-a",
          "migration: pre-existing custom_a.json in the new dir was NOT overwritten")
    check(not (new_dir / "not_custom.json").exists(),
          "migration: files not matching custom_*.json are not migrated")
    check((old_dir / "custom_a.json").exists() and (old_dir / "custom_b.json").exists(),
          "migration: the legacy directory is left untouched (not deleted)")

    copied_again = store_mod._copy_legacy_files(str(old_dir), str(new_dir))
    check(copied_again == 0, "migration: re-running the copy is idempotent", str(copied_again))


def test_store_dir_triggers_migration_once(tmp_path: Path):
    import trident_gcode.printer_store as store_mod

    old_dir = tmp_path / "legacy2"
    new_dir = tmp_path / "target2"
    old_dir.mkdir(parents=True)
    (old_dir / "custom_z.json").write_text('{"marker": "z"}', encoding="utf-8")

    orig_legacy_fn = store_mod._legacy_store_dir
    orig_env = os.environ.get("TRIDENT_PRINTER_DIR")
    orig_migrated = store_mod._migration_attempted
    store_mod._legacy_store_dir = lambda: str(old_dir)
    try:
        os.environ["TRIDENT_PRINTER_DIR"] = str(new_dir)
        store_mod._migration_attempted = False
        d = store_mod.store_dir()
        check(os.path.isfile(os.path.join(d, "custom_z.json")),
              "store_dir: an end-to-end call migrates a legacy file into the new store dir")
        check(store_mod._migration_attempted is True,
              "store_dir: migration is marked attempted after the first call")

        # One-shot per process: deleting the migrated file and calling
        # store_dir() again must NOT bring it back, or a user deleting an
        # imported printer would find it silently restored on next launch.
        os.remove(os.path.join(d, "custom_z.json"))
        store_mod.store_dir()
        check(not os.path.isfile(os.path.join(d, "custom_z.json")),
              "store_dir: migration does not re-run within the same process")
    finally:
        store_mod._legacy_store_dir = orig_legacy_fn
        if orig_env is None:
            os.environ.pop("TRIDENT_PRINTER_DIR", None)
        else:
            os.environ["TRIDENT_PRINTER_DIR"] = orig_env
        store_mod._migration_attempted = orig_migrated


def test_key_traversal_guard():
    """The _KEY_RE guard is the only thing standing between a stored key and
    a filesystem path; re-check it explicitly (independent of
    test_store_roundtrip) since store_dir()'s relocation is exactly the kind
    of change that could tempt someone to "simplify" key handling nearby."""
    import trident_gcode.printer_store as store_mod

    bad_keys = ("../evil", "custom_../x", "trident", "", "custom_" + "x" * 49)
    for bad_key in bad_keys:
        check(not store_mod._is_valid_key(bad_key),
              f"key guard: {bad_key!r} is rejected")
    check(store_mod._is_valid_key("custom_my_printer"),
          "key guard: a well-formed key is accepted")


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


def test_nozzle_temp_ceiling_comes_from_the_profile():
    """The nozzle-temp override must be bounded by the SELECTED profile.

    serve.py used to clamp with a module constant, `max(150, min(temp, 320))`.
    Every built-in profile declares 260 or 300, so the constant was looser than
    every real machine it was meant to protect -- a request for 320 C went
    through on a printer declaring 260. CLAUDE.md names this exact shape: a
    limit typed into serve.py belongs on the profile.

    float() also accepts the strings "NaN"/"Infinity", and every comparison
    against NaN is False, so it survives min()/max() rather than being clamped
    by it. Non-finite input must be rejected at the boundary, not clamped.
    """
    import serve
    from dataclasses import replace

    p260 = replace(TRIDENT, max_nozzle_temp=260.0)
    check(serve._parse_nozzle_temp({"nozzle_temp": "320"}, p260) == 260.0,
          "nozzle_temp: 320 requested on a 260 C profile is capped at 260",
          str(serve._parse_nozzle_temp({"nozzle_temp": "320"}, p260)))
    check(serve._parse_nozzle_temp({"nozzle_temp": "250"}, p260) == 250.0,
          "nozzle_temp: an in-range value passes through untouched")
    check(serve._parse_nozzle_temp({"nozzle_temp": "100"}, p260) == 150.0,
          "nozzle_temp: the 150 C cold-extrude floor still applies")

    # No profile may be raised ABOVE its own declared ceiling by this path.
    for key, prof in PRINTER_PROFILES.items():
        got = serve._parse_nozzle_temp({"nozzle_temp": "999"}, prof)
        check(got is not None and got <= prof.max_nozzle_temp + 1e-9,
              f"nozzle_temp: '{key}' cannot be driven above its own max_nozzle_temp",
              f"{got} vs {prof.max_nozzle_temp}")

    for bad in ("NaN", "Infinity", "-Infinity"):
        check(serve._parse_nozzle_temp({"nozzle_temp": bad}, p260) is None,
              f"nozzle_temp: {bad!r} is rejected, not clamped",
              str(serve._parse_nozzle_temp({"nozzle_temp": bad}, p260)))


def test_bed_temp_ceiling_comes_from_the_profile():
    """The bed-temp override must be bounded by the SELECTED profile, mirroring
    _parse_nozzle_temp -- with one deliberate difference: 0 is a real bed
    setting ("bed off"), not "no override", so it must NOT be swallowed the
    way a 0 C nozzle_temp is. See serve.py's _parse_bed_temp docstring.

    Same non-finite hazard as the nozzle path: float() accepts "NaN"/
    "Infinity"/"-Infinity", and every comparison against NaN is False, so it
    would survive min()/max() clamping (and the writer's own min() at
    gcode.py:91) to be emitted as "M140 Snan" if not rejected at the boundary.
    """
    import serve
    from dataclasses import replace

    p100 = replace(TRIDENT, max_bed_temp=100.0)

    # Over-ceiling request is capped to the profile's own max_bed_temp.
    check(serve._parse_bed_temp({"bed_temp": "150"}, p100) == 100.0,
          "bed_temp: 150 requested on a 100 C profile is capped at 100",
          str(serve._parse_bed_temp({"bed_temp": "150"}, p100)))

    # In-range value passes through untouched.
    check(serve._parse_bed_temp({"bed_temp": "60"}, p100) == 60.0,
          "bed_temp: an in-range value passes through untouched",
          str(serve._parse_bed_temp({"bed_temp": "60"}, p100)))

    # 0 is preserved as a REAL override -- the one deliberate asymmetry with
    # nozzle_temp, where 0 means "no override" instead.
    got_zero = serve._parse_bed_temp({"bed_temp": "0"}, p100)
    check(got_zero == 0.0 and got_zero is not None,
          "bed_temp: 0 is preserved as a genuine override, not swallowed",
          str(got_zero))

    # Blank / absent gives None (no override -- filament/profile default flows
    # through), same contract as nozzle_temp.
    check(serve._parse_bed_temp({"bed_temp": ""}, p100) is None,
          "bed_temp: blank string gives None", str(serve._parse_bed_temp({"bed_temp": ""}, p100)))
    check(serve._parse_bed_temp({}, p100) is None,
          "bed_temp: absent key gives None", str(serve._parse_bed_temp({}, p100)))
    check(serve._parse_bed_temp({"bed_temp": "not-a-number"}, p100) is None,
          "bed_temp: unparseable string gives None",
          str(serve._parse_bed_temp({"bed_temp": "not-a-number"}, p100)))

    # NaN/Infinity/-Infinity are rejected outright, never clamped.
    for bad in ("NaN", "Infinity", "-Infinity"):
        check(serve._parse_bed_temp({"bed_temp": bad}, p100) is None,
              f"bed_temp: {bad!r} is rejected, not clamped",
              str(serve._parse_bed_temp({"bed_temp": bad}, p100)))

    # No built-in profile may be driven above its own declared max_bed_temp.
    for key, prof in PRINTER_PROFILES.items():
        got = serve._parse_bed_temp({"bed_temp": "999"}, prof)
        check(got is not None and got <= prof.max_bed_temp + 1e-9,
              f"bed_temp: '{key}' cannot be driven above its own max_bed_temp",
              f"{got} vs {prof.max_bed_temp}")


def test_z_amp_max_default_is_not_another_machines_number():
    """An unknown non-planar clearance must not inherit the Trident's value.

    The old derivation was min(0.95, max(0.2, probe_clearance * 0.5)) with a
    no-probe branch of 1.0. probe_clearance is never parsed by ANY of the five
    importers, so it is always its own 2.0 default and the formula collapsed to
    0.95 -- the Trident's empirically measured constant -- for essentially
    every imported machine. CLAUDE.md: missing values become conservative
    defaults, never another machine's.

    The no-probe branch was also LOOSER (1.0 > 0.95). Probe detection is a
    fixed list of section-name heads, so an unrecognised probe reads as "no
    probe"; undetected hardware must get more margin, not less.
    """
    from trident_gcode.profile import TRIDENT as _T

    seen = {}
    for fx in ("klipper_trident.cfg", "klipper_inline_comments.cfg"):
        vr = validate_raw(parse_printer_config(_read_fixture(fx), fx))
        seen[fx] = (vr.profile.z_amp_max, vr.profile.has_probe)

    for fx, (val, _probe) in seen.items():
        check(val != _T.z_amp_max,
              f"z_amp_max: {fx} does not inherit the Trident's measured {_T.z_amp_max}",
              str(val))
        check(0.0 < val <= 0.5,
              f"z_amp_max: {fx} defaults into the conservative band", str(val))

    probe_val = seen["klipper_trident.cfg"][0]        # has_probe True
    noprobe_val = seen["klipper_inline_comments.cfg"][0]  # has_probe False
    check(noprobe_val <= probe_val,
          "z_amp_max: a printer with NO detected probe is never given more dip "
          "than one with a probe", f"no-probe {noprobe_val} vs probe {probe_val}")

    # The built-in Trident keeps its real measured constant -- this fix must
    # only change what is GUESSED for an import, never a measured value.
    check(_T.z_amp_max == 0.95,
          "z_amp_max: the built-in TRIDENT profile still declares its measured 0.95",
          str(_T.z_amp_max))


def main() -> int:
    test_klipper_trident()
    test_orca_machine()
    test_prusa_ender3()
    test_klipper_inline_comments()
    test_analyze_footprint_is_always_finite()
    test_hash_comment_is_not_a_command()
    test_cura_ender3()
    test_cura_center_zero()
    test_creality_print_machine()
    test_hostile()
    test_klipper_delegating_macro()
    test_auto_repair_start_gcode()
    test_comment_evasion()
    test_sanitize_is_idempotent()
    test_non_finite_rejected()
    test_builtin_profiles_self_validate()
    test_builtin_start_gcode_z_feedrates()
    test_creality_limits()
    test_machine_derived_ceilings()
    test_nozzle_temp_ceiling_comes_from_the_profile()
    test_bed_temp_ceiling_comes_from_the_profile()
    test_z_amp_max_default_is_not_another_machines_number()

    with tempfile.TemporaryDirectory() as tmp:
        test_smoke(Path(tmp))

    with tempfile.TemporaryDirectory() as tmp:
        test_store_roundtrip(Path(tmp) / "custom_printers")

    with tempfile.TemporaryDirectory() as tmp:
        test_store_dir_env_override(Path(tmp))
    test_default_data_dir_per_platform()
    with tempfile.TemporaryDirectory() as tmp:
        test_migration_copies_without_overwriting(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_store_dir_triggers_migration_once(Path(tmp))
    test_key_traversal_guard()

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
