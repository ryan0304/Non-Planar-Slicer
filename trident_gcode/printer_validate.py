"""Turn an untrusted parsed config (or an untrusted browser-supplied profile
dict) into a safe PrinterProfile plus a human-readable safety report.

This is the safety core of custom-printer import. Every rule here follows the
non-negotiable principles documented in the project's import spec:

  1. A missing value becomes a CONSERVATIVE default, never the Trident's own
     value -- inheriting another machine's limits is exactly how a printer
     gets destroyed.
  2. An out-of-range value is clamped to the absolute ceiling (LIMITS below)
     with a loud warning, never silently accepted and never a hard crash for
     something recoverable.
  3. Every write is re-validated from scratch here, including ones the
     browser claims it already validated -- ``validate_profile_dict`` is
     exactly as strict as ``validate_raw``.
  4. Foreign start/end G-code is sanitized (sanitize_gcode), never passed
     through verbatim: unknown placeholders are escaped so str.format_map can
     never raise at generate time, and config-mutating / firmware-state
     commands are stripped unless the caller opts in with allow_raw_gcode.
  5. This module never touches trident_gcode.profile.PRINTER_PROFILES.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .printer_import import RawConfig, ParsedField
from .profile import PrinterProfile


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class Issue:
    field: str        # PrinterProfile field name, or "start_gcode"/"end_gcode"/"" for global
    message: str      # user-facing, ASCII only
    severity: str      # "error" | "warn"


@dataclass
class FieldInfo:
    name: str
    label: str
    unit: str
    group: str
    value: object
    source: str        # "parsed" | "default" | "clamped"
    raw: str | None
    note: str | None


@dataclass
class ValidationResult:
    profile: PrinterProfile
    fields: list[FieldInfo] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


# ---------------------------------------------------------------------------
# Field metadata: label / unit / UI group, in display order.
# ---------------------------------------------------------------------------
# (label, unit, group)
_FIELD_META: dict[str, tuple[str, str, str]] = {
    "name":                       ("Name", "", "identity"),
    "firmware":                   ("Firmware", "", "identity"),
    "bed_size_x":                 ("Bed size X", "mm", "volume"),
    "bed_size_y":                 ("Bed size Y", "mm", "volume"),
    "z_max":                      ("Z max", "mm", "volume"),
    "z_min":                      ("Z min", "mm", "volume"),
    "print_min_x":                ("Safe area min X", "mm", "area"),
    "print_min_y":                ("Safe area min Y", "mm", "area"),
    "print_max_x":                ("Safe area max X", "mm", "area"),
    "print_max_y":                ("Safe area max Y", "mm", "area"),
    "max_velocity":                ("Max velocity", "mm/s", "motion"),
    "max_z_velocity":              ("Max Z velocity", "mm/s", "motion"),
    "max_accel":                   ("Max acceleration", "mm/s^2", "motion"),
    "max_z_accel":                 ("Max Z acceleration", "mm/s^2", "motion"),
    "nozzle_diameter":             ("Nozzle diameter", "mm", "hardware"),
    "filament_diameter":           ("Filament diameter", "mm", "hardware"),
    "max_extrude_cross_section":   ("Max extrude cross-section", "mm^2", "hardware"),
    "has_probe":                   ("Has probe", "", "probe"),
    "probe_dx":                    ("Probe offset X", "mm", "probe"),
    "probe_dy":                    ("Probe offset Y", "mm", "probe"),
    "probe_clearance":             ("Probe clearance", "mm", "probe"),
    "probe_radius":                ("Probe keep-out radius", "mm", "probe"),
    "z_amp_max":                   ("Max wave amplitude", "mm", "probe"),
    "quality_slope_max":           ("Max wave slope", "", "hardware"),
    "max_nozzle_temp":             ("Max nozzle temp", "C", "thermal"),
    "max_bed_temp":                ("Max bed temp", "C", "thermal"),
    "pa_gcode_style":               ("Pressure advance style", "", "gcode"),
    "start_gcode":                  ("Start G-code", "", "gcode"),
    "end_gcode":                    ("End G-code", "", "gcode"),
}

GROUP_ORDER = ["identity", "volume", "area", "motion", "hardware", "probe", "thermal", "gcode"]

# field -> (hard_min, hard_max, default_when_missing, required)
LIMITS: dict[str, tuple[float, float, float | None, bool]] = {
    "bed_size_x":        (20, 2000, None, True),
    "bed_size_y":        (20, 2000, None, True),
    "z_max":             (10, 2000, None, True),
    "z_min":             (-50, 0, 0.0, False),
    "max_velocity":      (5, 1000, 100.0, False),
    "max_z_velocity":    (0.5, 100, 10.0, False),
    "max_accel":         (50, 50000, 1000.0, False),
    "max_z_accel":       (5, 20000, 100.0, False),
    "nozzle_diameter":   (0.1, 2.0, 0.4, False),
    "filament_diameter": (1.0, 4.0, 1.75, False),
    "probe_dx":          (-150, 150, 0.0, False),
    "probe_dy":          (-150, 150, 0.0, False),
    "max_nozzle_temp":   (150, 500, 260.0, False),
    "max_bed_temp":      (0, 200, 100.0, False),
    "max_extrude_cross_section": (0, 100, 0.0, False),
}

# Klipper aborts a move whose extrusion cross-section (line_width * layer_height)
# exceeds max_extrude_cross_section. The app's nominal bead is line_width 0.45mm
# x layer_height 0.30mm.
_NOMINAL_BEAD_AREA = 0.45 * 0.30

_REQUIRED_FALLBACK = {"bed_size_x": 20.0, "bed_size_y": 20.0, "z_max": 10.0}

_FIRMWARES = {"klipper", "marlin", "bambu_marlin"}

_DEFAULT_START_GCODE = (
    "G28                ; home all axes\n"
    "M104 S{nozzle_temp:.0f}\n"
    "M140 S{bed_temp:.0f}\n"
    "M190 S{bed_temp:.0f}\n"
    "M109 S{nozzle_temp:.0f}\n"
    "M83                ; relative extrusion\n"
    "G92 E0\n"
    "M107               ; fan off for first layer(s)"
)
_DEFAULT_END_GCODE = (
    "M104 S0            ; heater off\n"
    "M140 S0            ; bed off\n"
    "M107               ; fan off\n"
    "G91\n"
    "G1 Z5 F600         ; lift\n"
    "G90\n"
    "M84                ; steppers off"
)


# ---------------------------------------------------------------------------
# G-code sanitation
# ---------------------------------------------------------------------------
_NOZZLE_PLACEHOLDER_NAMES = (
    "first_layer_temperature", "nozzle_temperature_initial_layer",
    "nozzle_temperature", "temperature", "extruder_temperature",
)
_BED_PLACEHOLDER_NAMES = (
    "first_layer_bed_temperature", "bed_temperature_initial_layer",
    "bed_temperature", "hot_plate_temp_initial_layer", "hot_plate_temp",
)
_MATERIAL_PLACEHOLDER_NAMES = ("filament_type",)

_KNOWN_PLACEHOLDER_RE = re.compile(r'\{(nozzle_temp:\.0f|bed_temp:\.0f|material)\}')


def _alternation(names: tuple[str, ...]) -> str:
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


_SUBST_GROUPS = [
    (_alternation(_NOZZLE_PLACEHOLDER_NAMES), "{nozzle_temp:.0f}"),
    (_alternation(_BED_PLACEHOLDER_NAMES), "{bed_temp:.0f}"),
    (_alternation(_MATERIAL_PLACEHOLDER_NAMES), "{material}"),
]


def _normalize_placeholders(text: str) -> str:
    """Map known slicer placeholders ([foo], {foo}, {foo[0]}) onto ours."""
    for names_pat, repl in _SUBST_GROUPS:
        text = re.sub(r'\{(?:' + names_pat + r')(?:\[\d+\])?\}', repl, text, flags=re.IGNORECASE)
        text = re.sub(r'\[(?:' + names_pat + r')(?:\[\d+\])?\]', repl, text, flags=re.IGNORECASE)
    return text


def _escape_unknown_braces(text: str) -> tuple[str, list[str]]:
    """Double every '{'/'}' that is not part of one of our 3 known
    placeholders, so str.format_map can never raise KeyError/IndexError.
    Returns (escaped_text, names_of_escaped_placeholders).

    Idempotent: an already-doubled brace is passed through untouched rather
    than doubled again. The review dialog re-validates the current textarea
    contents on every keystroke, so a non-idempotent escape would grow
    '{x}' into '{{{{x}}}}' and beyond within seconds of editing. The
    placeholder is still REPORTED each pass, so the warning telling the user
    to fix it does not disappear after the first round-trip.
    """
    out: list[str] = []
    escaped_names: list[str] = []
    i, n = 0, len(text)
    while i < n:
        m = _KNOWN_PLACEHOLDER_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        pair = text[i:i + 2]
        if pair in ("{{", "}}"):
            if pair == "{{":
                j = text.find("}", i)
                name = text[i + 2:j].strip("{} ") if j != -1 else text[i + 2:i + 22].strip()
                escaped_names.append(name or "{}")
            out.append(pair)
            i += 2
            continue
        ch = text[i]
        if ch == "{":
            j = text.find("}", i)
            name = text[i + 1:j].strip() if j != -1 else text[i + 1:i + 21].strip()
            escaped_names.append(name or "{}")
            out.append("{{")
            i += 1
        elif ch == "}":
            out.append("}}")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), escaped_names


_BLOCKED_TOKENS = {
    "M500", "M502", "M501", "M851", "M206", "M290", "M112", "M999",
    "RESTART", "FIRMWARE_RESTART", "M303", "SET_KINEMATIC_POSITION",
}
_WARN_TOKENS = {
    "M92", "M201", "M203", "M204", "M205", "M301", "M304", "M900",
    "SET_VELOCITY_LIMIT", "SET_PRESSURE_ADVANCE",
}
_BLOCKED_REASONS = {
    "M500": "saves the EEPROM (permanently overwrites the printer's stored config)",
    "M502": "factory-resets the printer's stored config",
    "M501": "reloads stored EEPROM settings over the running config",
    "M851": "changes the stored probe Z offset",
    "M206": "changes the stored home offset",
    "M290": "applies a persistent babystep offset",
    "M112": "emergency stop - would abort every print that uses this printer",
    "M999": "clears a firmware fault/reset state unexpectedly",
    "RESTART": "restarts the firmware mid-macro",
    "FIRMWARE_RESTART": "restarts the firmware mid-macro",
    "M303": "PID autotune - heats unattended for minutes with no operator present",
    "SET_KINEMATIC_POSITION": "defeats limit checking outright",
}
_SET_OFFSET_Z_RE = re.compile(r'\bZ(?:_ADJUST)?\s*=\s*(-?\d+(?:\.\d+)?)', re.IGNORECASE)

_MAX_GCODE_BYTES = 20 * 1024
_MAX_GCODE_LINES = 500

_START_MACRO_RE = re.compile(r'(PRINT_START|START_PRINT|PRINT_BEGIN)', re.IGNORECASE)


def _hash_comments_to_semicolons(text: str) -> tuple[str, int]:
    """Rewrite config-style '#' comments as G-code ';' comments.

    printer.cfg is read by configparser, where '#' starts a comment. A G-code
    FILE has no such rule -- Klipper strips only ';'. So a macro body inlined
    into our start G-code carries '#' lines that the printer would try to
    EXECUTE: '#M117 Printing' comes back as 'Unknown command' and aborts the
    print. Rewriting rather than deleting keeps the author's text visible.

    Only '#' at the start of a line or preceded by whitespace counts, matching
    configparser's inline-comment rule and leaving a '#' inside a value alone.
    """
    out = []
    n = 0
    for line in text.splitlines():
        m = re.search(r'(?:^|\s)#', line)
        if m:
            idx = m.end() - 1
            line = line[:idx] + ";" + line[idx + 1:]
            n += 1
        out.append(line)
    return "\n".join(out), n


def _command_text(text: str) -> str:
    """Strip comments, leaving only lines the printer actually executes.

    Presence checks (homing, heat-up, extrusion mode, cool-down) must never be
    satisfied by a comment that merely mentions the command.
    """
    out = []
    for line in text.splitlines():
        s = line.split(";", 1)[0]
        s = re.sub(r'\([^)]*\)', " ", s)
        out.append(s)
    return "\n".join(out)


def _has_start_macro_call(code: str) -> bool:
    """True when a line actually CALLS a start macro (first token), rather than
    merely mentioning one somewhere in its arguments or text."""
    for line in code.splitlines():
        s = line.strip()
        if not s:
            continue
        head = s.split(None, 1)[0]
        if _START_MACRO_RE.search(head):
            return True
    return False


def _has_parameterized_start_macro_call(code: str) -> bool:
    """True when a line CALLS a start macro AND passes it arguments (a bare
    '=' anywhere on the line, Klipper's only call syntax for macro params).
    That is the signal that the macro is responsible for heating -- e.g. a
    synthesized 'PRINT_START EXTRUDER={nozzle_temp:.0f} BED={bed_temp:.0f}'
    call. A bare, param-less call ('PRINT_START' alone) gives no such
    guarantee, so it does NOT count here (unlike _has_start_macro_call, which
    is deliberately more permissive for the pre-existing "never sets a
    temperature" warning)."""
    for line in code.splitlines():
        s = line.strip()
        if not s:
            continue
        head = s.split(None, 1)[0]
        if _START_MACRO_RE.search(head) and "=" in s:
            return True
    return False


# M104/M140 SET a target and return immediately; M109/M190 SET **and wait**
# for it to be reached. A block with only the non-blocking form is not
# actually done heating -- see _repair_missing_heating.
_M104_RE = re.compile(r'^\s*M104\b', re.IGNORECASE)
_M109_RE = re.compile(r'^\s*M109\b', re.IGNORECASE)
_M140_RE = re.compile(r'^\s*M140\b', re.IGNORECASE)
_M190_RE = re.compile(r'^\s*M190\b', re.IGNORECASE)
_G28_LINE_RE = re.compile(r'^\s*G28\b', re.IGNORECASE)
_M82_LINE_RE = re.compile(r'^\s*M82\b', re.IGNORECASE)
_M83_LINE_RE = re.compile(r'^\s*M83\b', re.IGNORECASE)

_AUTO_TAG = "[auto-added]"


def _last_line_index(lines: list[str], cmd_re: re.Pattern) -> int | None:
    """Index of the LAST line in ``lines`` whose command text (','-free of
    inline ';' comments and '(...)' asides) matches ``cmd_re``, or None."""
    idx = None
    for i, ln in enumerate(lines):
        code = ln.split(";", 1)[0]
        code = re.sub(r'\([^)]*\)', " ", code)
        if cmd_re.match(code):
            idx = i
    return idx


def _repair_missing_heating(text: str) -> tuple[str, list[Issue]]:
    """Insert heating (and M83, if also missing) into a start G-code block
    that has none of it at all, or complete a block that SETS a temperature
    (M104/M140) but never WAITS for it (no M109/M190).

    Real report that motivated this: a Klipper PRINT_START macro with no
    EXTRUDER/BED params got inlined verbatim by the importer (see
    printer_import._parse_klipper_cfg). Its body reset the extruder, homed,
    called the user's own sub-macro, and lifted Z -- and never heated
    anything and never set M83. Klipper refuses to extrude below
    min_extrude_temp (default 170C), so that block is not a risky print, it
    is a guaranteed abort on the first extrusion move. Most users cannot
    diagnose or hand-fix that, so it is repaired here instead of merely
    warned about.

    Placement (matches the user's own working, hand-fixed config):
      - M140 (bed, no wait) goes at the very TOP, so the bed starts heating
        immediately, in parallel with homing.
      - M190/M104/M109 go immediately AFTER the LAST G28 (or right after the
        M140 if there is no G28 at all): the nozzle is heated LAST, after
        homing has already happened, so it oozes less onto the bed before
        the first move.
      - M83 (relative extrusion) is appended at the very END of the block,
        if missing and only if the block does not already set M82 -- an
        M82 hard error must never be silently resolved by adding M83 next
        to it; see the M82 check below in sanitize_gcode, which still fires.
        Appending rather than prepending means it cannot change the
        extrusion semantics of a purge line already in the user's own start
        G-code, and it lands after any macro call the block makes (which
        might itself set M82 or M83).

    Only called when repair=True (import time only -- see sanitize_gcode). A
    start-macro call that itself carries parameters (see
    _has_parameterized_start_macro_call) always suppresses repair entirely --
    the macro is assumed to handle its own heating. Otherwise there are three
    cases:

      1. No heating in any form at all: insert the full M140/M190/M104/M109
         block described below (and M83, if that's also missing).
      2. A SET command is present with no matching WAIT (M104 with no M109,
         or M140 with no M190): M104/M140 return immediately, so the block
         only LOOKS like it heats -- the print can start moving/extruding
         while the nozzle or bed is still cold. Insert just the missing wait
         command right after the existing set command.
      3. One axis is fully absent while the other is present or partial:
         flagged with a warning rather than auto-inserted, since there is no
         single obviously-correct place to add a brand-new M140/M190 pair
         without risking a worse interaction with the rest of the block.

    A comment that merely MENTIONS one of these commands does not count --
    ``text`` here is expected to already have had '#' rewritten to ';' and
    placeholders normalized/escaped (sanitize_gcode's ordering), so
    _command_text(text) reflects only what the printer would actually
    execute.

    Returns (possibly-modified text, extra Issues to report). Never touches
    ``text`` at all (returns it unchanged, with an empty issue list) when
    heating is already fully present (M109 AND M190, or a parameterized
    macro call).
    """
    code_lines = _command_text(text).splitlines()

    has_m104 = any(_M104_RE.match(ln) for ln in code_lines)
    has_m109 = any(_M109_RE.match(ln) for ln in code_lines)
    has_m140 = any(_M140_RE.match(ln) for ln in code_lines)
    has_m190 = any(_M190_RE.match(ln) for ln in code_lines)
    has_temp = has_m104 or has_m109 or has_m140 or has_m190
    has_macro_params = _has_parameterized_start_macro_call("\n".join(code_lines))
    if has_macro_params:
        return text, []

    if has_temp:
        return _repair_incomplete_heating(
            text, has_m104=has_m104, has_m109=has_m109, has_m140=has_m140, has_m190=has_m190)

    lines = text.splitlines()

    bed_line = ("M140 S{bed_temp:.0f}          ; " + _AUTO_TAG +
                " start heating the bed now, in parallel with homing")
    lines.insert(0, bed_line)

    # Recompute against the NEW lines (with M140 already prepended) to find
    # the LAST G28 -- the shift by one line is deliberate, not a bug.
    code_after_bed = _command_text("\n".join(lines)).splitlines()
    g28_idx = None
    for i, ln in enumerate(code_after_bed):
        if _G28_LINE_RE.match(ln):
            g28_idx = i
    insert_at = (g28_idx + 1) if g28_idx is not None else 1

    nozzle_block = [
        "M190 S{bed_temp:.0f}          ; " + _AUTO_TAG + " wait for the bed to finish heating",
        "M104 S{nozzle_temp:.0f}       ; " + _AUTO_TAG + " start heating the nozzle last, so it oozes less before the first move",
        "M109 S{nozzle_temp:.0f}       ; " + _AUTO_TAG + " wait for the nozzle to reach temp before extruding",
    ]
    lines[insert_at:insert_at] = nozzle_block

    issues = [Issue(
        "start_gcode",
        "start G-code never set a temperature -- Klipper refuses to extrude below "
        "min_extrude_temp, so this would have aborted on the first extrusion move. "
        "Automatically added M140/M190/M104/M109 (bed heats first, in parallel with "
        "homing; nozzle heats last, after homing, so it oozes less before the first "
        "move). Lines are marked " + _AUTO_TAG + " -- review them before printing.",
        "warn")]

    has_m82 = any(_M82_LINE_RE.match(ln) for ln in code_lines)
    has_m83 = any(_M83_LINE_RE.match(ln) for ln in code_lines)
    if not has_m82 and not has_m83:
        m83_line = "M83                            ; " + _AUTO_TAG + " relative extrusion (this generator emits E deltas)"
        lines.append(m83_line)
        issues.append(Issue(
            "start_gcode",
            "no M83 and no start macro seen; automatically appended M83 at the end of "
            "the start block, since this generator emits relative extrusion (E deltas "
            "per move). Line is marked " + _AUTO_TAG + " -- review it before printing.",
            "warn"))

    return "\n".join(lines), issues


def _repair_incomplete_heating(text: str, *, has_m104: bool, has_m109: bool,
                                has_m140: bool, has_m190: bool) -> tuple[str, list[Issue]]:
    """Handle case 2 and case 3 of _repair_missing_heating's docstring: some
    heating is present, but it is not both SET and WAITED-for on both axes.

    Only called when at least one of M104/M109/M140/M190 is present (a fully
    empty block goes through _repair_missing_heating's own full-block path
    instead, so its M140/M190/M104/M109 ordering is untouched by this
    function).
    """
    lines = text.splitlines()
    issues: list[Issue] = []

    if has_m140 and not has_m190:
        idx = _last_line_index(lines, _M140_RE)
        lines.insert(idx + 1, "M190 S{bed_temp:.0f}          ; " + _AUTO_TAG +
                     " wait for the bed to finish heating (was set but never waited on)")
        issues.append(Issue(
            "start_gcode",
            "start G-code sets the bed temperature (M140) but never waits for it (no "
            "M190) -- M140 does not block, so the print could start on a cold bed. "
            "Automatically added M190 right after the existing M140. Line is marked " +
            _AUTO_TAG + " -- review it before printing.",
            "warn"))
    elif not has_m140 and not has_m190:
        issues.append(Issue(
            "start_gcode",
            "start G-code never heats the bed (no M140/M190), though it does set up "
            "other heating -- the print may start on a cold bed. Add M140/M190 by hand.",
            "warn"))

    if has_m104 and not has_m109:
        idx = _last_line_index(lines, _M104_RE)
        lines.insert(idx + 1, "M109 S{nozzle_temp:.0f}       ; " + _AUTO_TAG +
                     " wait for the nozzle to reach temp (was set but never waited on)")
        issues.append(Issue(
            "start_gcode",
            "start G-code sets the nozzle temperature (M104) but never waits for it "
            "(no M109) -- M104 does not block, so the print could start extruding "
            "cold. Automatically added M109 right after the existing M104. Line is "
            "marked " + _AUTO_TAG + " -- review it before printing.",
            "warn"))
    elif not has_m104 and not has_m109:
        issues.append(Issue(
            "start_gcode",
            "start G-code never heats the nozzle (no M104/M109), though it does set "
            "up other heating -- the print may start extruding cold. Add M104/M109 by "
            "hand.",
            "warn"))

    if not issues:
        return text, []
    return "\n".join(lines), issues


def sanitize_gcode(text: str, kind: str, *, allow_raw_gcode: bool = False, repair: bool = False
                    ) -> tuple[str, list[Issue], list[tuple[str, str]]]:
    """Normalize placeholders, escape unknown ones, and strip dangerous
    commands from ``text`` (start or end G-code, selected by ``kind``).

    Returns (clean_text, issues, stripped_lines) where stripped_lines is a
    list of (original_line, reason) for every line removed by the dangerous
    -command scan. The format_map round-trip check (mandatory -- this is the
    guarantee that a bad template cannot blow up generation) always runs
    before returning.

    ``repair`` opts into auto-inserting heating (and M83) into a start block
    that has none at all -- see _repair_missing_heating. It defaults to
    False and must stay that way except on the actual import path: this
    function also runs every time the review dialog re-validates the
    textarea as the user edits it, and if repair fired there too, a user who
    deliberately deleted the auto-inserted heating would find it silently
    reappearing on the next keystroke -- not "editable" at all. Only the
    caller that turns an uploaded file into a profile for the first time
    (serve.py's /api/printer/parse handler, generate.py's --import-printer)
    passes repair=True.
    """
    issues: list[Issue] = []
    stripped: list[tuple[str, str]] = []
    field_name = "start_gcode" if kind == "start" else "end_gcode"

    if len(text.encode("utf-8", "replace")) > _MAX_GCODE_BYTES:
        issues.append(Issue(field_name, f"{kind} G-code exceeds the 20 KB size cap.", "error"))
        text = text.encode("utf-8", "replace")[:_MAX_GCODE_BYTES].decode("utf-8", "ignore")

    lines = text.splitlines()
    if len(lines) > _MAX_GCODE_LINES:
        issues.append(Issue(
            field_name,
            f"{kind} G-code has {len(lines)} lines, exceeding the {_MAX_GCODE_LINES} line cap.",
            "error"))
        lines = lines[:_MAX_GCODE_LINES]
    text = "\n".join(lines)

    text, hash_comments = _hash_comments_to_semicolons(text)
    if hash_comments:
        issues.append(Issue(
            field_name,
            f"rewrote {hash_comments} '#' comment(s) to ';'. In printer.cfg '#' starts a "
            f"comment, but in a G-code FILE it does not -- Klipper only strips ';', so a "
            f"line like '#M117 Printing' would have been sent as a command and answered "
            f"with 'Unknown command', aborting the print.",
            "warn"))

    text = _normalize_placeholders(text)
    text, escaped_names = _escape_unknown_braces(text)
    if escaped_names:
        shown = escaped_names[:5]
        issues.append(Issue(
            field_name,
            "unrecognised placeholder(s) " + ", ".join("{%s}" % n for n in shown) +
            " will be printed literally as comments/text - edit or remove them.",
            "warn"))

    if repair and kind == "start":
        text, repair_issues = _repair_missing_heating(text)
        issues.extend(repair_issues)

    out_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in (";", "("):
            out_lines.append(line)
            continue
        head = s.split(None, 1)[0].upper()
        reason = None
        if head in _BLOCKED_TOKENS:
            reason = _BLOCKED_REASONS.get(head, "mutates persistent printer state")
        elif head == "SET_GCODE_OFFSET":
            m = _SET_OFFSET_Z_RE.search(s)
            if m and abs(float(m.group(1))) > 1.0:
                reason = "sets a large Z offset, changing the effective bed position"
        if reason and not allow_raw_gcode:
            stripped.append((line, reason))
            issues.append(Issue(field_name, f"removed '{head}': {reason}.", "warn"))
            continue
        if head in _WARN_TOKENS:
            issues.append(Issue(
                field_name,
                f"'{head}' changes machine motion/PID settings for the rest of the session.",
                "warn"))
        out_lines.append(line)

    clean = "\n".join(out_lines)

    # Mandatory: sanitized text must never make str.format_map raise at
    # generate time. This is the whole point of steps 1-2 above; verify it.
    try:
        clean.format_map({"nozzle_temp": 200.0, "bed_temp": 60.0, "material": "PLA"})
    except (KeyError, IndexError, ValueError) as e:
        issues.append(Issue(
            field_name,
            f"sanitized {kind} G-code still fails to render ({e}) - internal "
            f"sanitation bug, please report.",
            "error"))

    # Every presence check below runs on COMMAND text only. Matching raw text
    # would let a mere mention defeat the check -- a start block whose only
    # reference to homing is "; see PRINT_START in printer.cfg" executes no
    # G28 at all, and that is the one failure that reliably drives a nozzle
    # through the bed.
    code = _command_text(clean)

    if kind == "start":
        has_home = bool(re.search(r'^\s*G28\b', code, re.IGNORECASE | re.MULTILINE))
        has_macro = _has_start_macro_call(code)
        if not has_home and not has_macro:
            issues.append(Issue(
                "start_gcode",
                "start G-code never homes the printer; the first move would "
                "crash into the bed. Add G28.",
                "error"))
        has_m104_p = bool(re.search(r'^\s*M104\b', code, re.IGNORECASE | re.MULTILINE))
        has_m109_p = bool(re.search(r'^\s*M109\b', code, re.IGNORECASE | re.MULTILINE))
        has_m140_p = bool(re.search(r'^\s*M140\b', code, re.IGNORECASE | re.MULTILINE))
        has_m190_p = bool(re.search(r'^\s*M190\b', code, re.IGNORECASE | re.MULTILINE))
        has_temp = has_m104_p or has_m109_p or has_m140_p or has_m190_p
        if not has_temp and not has_macro:
            issues.append(Issue(
                "start_gcode",
                "start G-code never sets a temperature; the printer may try "
                "to extrude cold.",
                "warn"))
        elif not has_macro:
            # M104/M140 SET a target and return immediately; only M109/M190
            # actually WAIT for it. A set-without-wait means the print can
            # start moving/extruding before the target temperature is
            # reached, even though "some" heating command is present.
            if has_m104_p and not has_m109_p:
                issues.append(Issue(
                    "start_gcode",
                    "start G-code sets the nozzle temperature (M104) but never waits "
                    "for it (no M109) - the printer may start extruding before the "
                    "nozzle reaches temperature.",
                    "warn"))
            if has_m140_p and not has_m190_p:
                issues.append(Issue(
                    "start_gcode",
                    "start G-code sets the bed temperature (M140) but never waits for "
                    "it (no M190) - the print may start on a bed that has not reached "
                    "temperature.",
                    "warn"))
        # The generator emits RELATIVE extrusion (an E delta per move), so an
        # explicit M82 is worse than saying nothing: absolute mode reads every
        # small delta as an absolute target and the extruder grinds/over-
        # extrudes from the first move. That is an error, not a warning.
        if re.search(r'^\s*M82\b', code, re.IGNORECASE | re.MULTILINE):
            issues.append(Issue(
                "start_gcode",
                "start G-code sets M82 (absolute extrusion), but the generator "
                "emits relative E deltas - the printer would read each delta as "
                "an absolute target and massively over-extrude. Use M83.",
                "error"))
        elif not re.search(r'^\s*M83\b', code, re.IGNORECASE | re.MULTILINE) and not has_macro:
            issues.append(Issue(
                "start_gcode",
                "no M83 and no start macro seen; the generator emits relative "
                "extrusion (E deltas per move) - without M83 an absolute-mode "
                "printer will massively over-extrude.",
                "warn"))
    else:
        has_cooldown = bool(re.search(
            r'(M104\s*S0|M140\s*S0|PRINT_END|TURN_OFF_HEATERS)', code, re.IGNORECASE))
        if not has_cooldown:
            issues.append(Issue(
                "end_gcode",
                "end G-code has no M104 S0 / M140 S0 / PRINT_END / "
                "TURN_OFF_HEATERS - heaters may be left on.",
                "warn"))

    return clean, issues, stripped


# ---------------------------------------------------------------------------
# Extraction: pull a raw value + raw text out of either source type
# ---------------------------------------------------------------------------
def _extract(source, field_name: str):
    """Return (value, raw_text) for ``field_name``, or None if absent.

    ``source`` is either a RawConfig (parser output) or a plain dict (browser
    JSON). Both are equally untrusted -- this is exactly why
    validate_profile_dict must run the same pipeline as validate_raw.
    """
    if isinstance(source, RawConfig):
        pf = source.fields.get(field_name)
        if pf is None or pf.value is None:
            return None
        return pf.value, pf.raw
    if not isinstance(source, dict):
        return None
    if field_name not in source or source[field_name] is None:
        return None
    return source[field_name], None


def _fmt_num(v: float) -> str:
    return f"{v:g}"


def _validate_numeric(source, field_name: str, issues: list[Issue],
                       fields_out: list[FieldInfo]) -> float:
    lo, hi, default, required = LIMITS[field_name]
    label, unit, group = _FIELD_META[field_name]
    extracted = _extract(source, field_name)

    if extracted is None:
        if required:
            fallback = _REQUIRED_FALLBACK[field_name]
            issues.append(Issue(
                field_name, f"{label} is required but was not found in the config.", "error"))
            fields_out.append(FieldInfo(
                field_name, label, unit, group, value=fallback, source="default",
                raw=None, note="required, missing - profile is unsafe until this is set"))
            return fallback
        issues.append(Issue(
            field_name,
            f"{label} not found in config; using the conservative default "
            f"{_fmt_num(default)} {unit}.".strip(),
            "warn"))
        fields_out.append(FieldInfo(
            field_name, label, unit, group, value=default, source="default",
            raw=None, note="missing from config; conservative default"))
        return default

    raw_value, raw_text = extracted
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        fallback = default if default is not None else lo
        issues.append(Issue(
            field_name, f"{label} value {raw_value!r} is not a number.", "error"))
        fields_out.append(FieldInfo(
            field_name, label, unit, group, value=fallback, source="default",
            raw=str(raw_value), note="non-numeric; using default"))
        return fallback

    # NaN/Infinity must be rejected outright, never clamped. Every comparison
    # against NaN is False, so it survives the range clamp below AND defeats
    # every downstream guard the same way -- including GcodeWriter._check_bounds,
    # whose out-of-bounds tests would all silently pass. Python's json.loads
    # accepts the bare tokens NaN/Infinity, so this is reachable from the API.
    if value != value or value in (float("inf"), float("-inf")):
        fallback = default if default is not None else lo
        issues.append(Issue(
            field_name,
            f"{label} value {raw_value!r} is not a finite number.", "error"))
        fields_out.append(FieldInfo(
            field_name, label, unit, group, value=fallback, source="default",
            raw=str(raw_value), note="not finite; using default"))
        return fallback

    if value < lo or value > hi:
        clamped = min(max(value, lo), hi)
        issues.append(Issue(
            field_name,
            f"{label} {_fmt_num(value)} is outside the safe range "
            f"[{_fmt_num(lo)},{_fmt_num(hi)}]; clamped to {_fmt_num(clamped)}.",
            "warn"))
        fields_out.append(FieldInfo(
            field_name, label, unit, group, value=clamped, source="clamped",
            raw=raw_text if raw_text is not None else str(raw_value),
            note=f"config said {_fmt_num(value)}, clamped to {_fmt_num(clamped)}"))
        return clamped

    fields_out.append(FieldInfo(
        field_name, label, unit, group, value=value, source="parsed",
        raw=raw_text if raw_text is not None else str(raw_value), note=None))
    return value


def _resolve_print_range(source, axis: str, bed_size: float,
                          issues: list[Issue], fields_out: list[FieldInfo]) -> tuple[float, float]:
    """Resolve print_min_<axis>/print_max_<axis> together (they come from the
    same bed-mesh/printable-polygon source, or default together)."""
    min_field, max_field = f"print_min_{axis}", f"print_max_{axis}"
    lo_label, lo_unit, lo_group = _FIELD_META[min_field]
    hi_label, hi_unit, hi_group = _FIELD_META[max_field]

    lo_ext = _extract(source, min_field)
    hi_ext = _extract(source, max_field)

    if lo_ext is not None and hi_ext is not None:
        try:
            lo_val = float(lo_ext[0])
            hi_val = float(hi_ext[0])
            ok = True
        except (TypeError, ValueError):
            ok = False
        if ok:
            # Hard range is 0..bed_size for both, dynamic on bed size.
            c_lo = min(max(lo_val, 0.0), bed_size)
            c_hi = min(max(hi_val, 0.0), bed_size)
            note_lo = note_hi = None
            source_lo = source_hi = "parsed"
            if c_lo != lo_val:
                issues.append(Issue(
                    min_field,
                    f"{lo_label} {_fmt_num(lo_val)} is outside the bed "
                    f"[0,{_fmt_num(bed_size)}]; clamped to {_fmt_num(c_lo)}.",
                    "warn"))
                note_lo = f"config said {_fmt_num(lo_val)}, clamped to {_fmt_num(c_lo)}"
                source_lo = "clamped"
            if c_hi != hi_val:
                issues.append(Issue(
                    max_field,
                    f"{hi_label} {_fmt_num(hi_val)} is outside the bed "
                    f"[0,{_fmt_num(bed_size)}]; clamped to {_fmt_num(c_hi)}.",
                    "warn"))
                note_hi = f"config said {_fmt_num(hi_val)}, clamped to {_fmt_num(c_hi)}"
                source_hi = "clamped"
            fields_out.append(FieldInfo(min_field, lo_label, lo_unit, lo_group,
                                         value=c_lo, source=source_lo, raw=lo_ext[1], note=note_lo))
            fields_out.append(FieldInfo(max_field, hi_label, hi_unit, hi_group,
                                         value=c_hi, source=source_hi, raw=hi_ext[1], note=note_hi))
            return c_lo, c_hi

    # No usable bed mesh / printable polygon: safe inset default.
    c_lo = 5.0
    c_hi = max(5.0, bed_size - 5.0)
    note = "no bed mesh / printable polygon given; defaulted to a 5 mm inset - widen if your bed's usable area is larger"
    issues.append(Issue(
        min_field,
        f"{lo_label}/{hi_label} not found; defaulted to a 5 mm inset of the bed "
        f"({_fmt_num(c_lo)} to {_fmt_num(c_hi)}).",
        "warn"))
    fields_out.append(FieldInfo(min_field, lo_label, lo_unit, lo_group,
                                 value=c_lo, source="default", raw=None, note=note))
    fields_out.append(FieldInfo(max_field, hi_label, hi_unit, hi_group,
                                 value=c_hi, source="default", raw=None, note=note))
    return c_lo, c_hi


def _validate_name(source, issues: list[Issue], fields_out: list[FieldInfo]) -> str:
    label, unit, group = _FIELD_META["name"]
    extracted = _extract(source, "name")
    raw_text = extracted[1] if extracted else None
    raw_name = str(extracted[0]) if extracted else ""
    cleaned = "".join(c for c in raw_name.strip() if c.isprintable() and ord(c) < 128)
    cleaned = cleaned[:60].strip()
    if not cleaned:
        cleaned = "Custom Printer"
    source_tag = "parsed" if (extracted and cleaned == raw_name.strip()) else (
        "clamped" if extracted else "default")
    fields_out.append(FieldInfo("name", label, unit, group, value=cleaned,
                                 source=source_tag, raw=raw_text, note=None))
    return cleaned


def _validate_firmware(source, issues: list[Issue], fields_out: list[FieldInfo]) -> str:
    label, unit, group = _FIELD_META["firmware"]
    extracted = _extract(source, "firmware")
    if extracted is None:
        fw = "marlin"
        fields_out.append(FieldInfo("firmware", label, unit, group, value=fw,
                                     source="default", raw=None,
                                     note="not found; defaulting to marlin (most conservative g-code dialect assumption)"))
        return fw
    raw_val, raw_text = extracted
    fw = str(raw_val).strip().lower()
    if fw not in _FIRMWARES:
        issues.append(Issue(
            "firmware", f"unrecognised firmware '{raw_val}'; treating as marlin.", "warn"))
        fields_out.append(FieldInfo("firmware", label, unit, group, value="marlin",
                                     source="clamped", raw=raw_text or str(raw_val),
                                     note=f"config said '{raw_val}', unknown - defaulted to marlin"))
        return "marlin"
    fields_out.append(FieldInfo("firmware", label, unit, group, value=fw,
                                 source="parsed", raw=raw_text or str(raw_val), note=None))
    return fw


def _validate_bool(source, field_name: str, default: bool,
                    fields_out: list[FieldInfo]) -> bool:
    label, unit, group = _FIELD_META[field_name]
    extracted = _extract(source, field_name)
    if extracted is None:
        fields_out.append(FieldInfo(field_name, label, unit, group, value=default,
                                     source="default", raw=None, note=None))
        return default
    raw_val, raw_text = extracted
    value = bool(raw_val) if not isinstance(raw_val, str) else raw_val.strip().lower() in (
        "1", "true", "yes", "on")
    fields_out.append(FieldInfo(field_name, label, unit, group, value=value,
                                 source="parsed", raw=raw_text or str(raw_val), note=None))
    return value


def _validate_probe_extra(source, field_name: str, has_probe: bool,
                           default_with_probe: float, warn_msg: str,
                           issues: list[Issue], fields_out: list[FieldInfo]) -> float:
    """probe_clearance / probe_radius: conservative-with-probe default, hard
    0.0 (irrelevant) default without one."""
    label, unit, group = _FIELD_META[field_name]
    lo, hi = 0.0, 50.0
    extracted = _extract(source, field_name)
    if extracted is None:
        if has_probe:
            issues.append(Issue(field_name,
                                 f"{label} not found; defaulting to {default_with_probe:g} {unit} - {warn_msg}.",
                                 "warn"))
            value = default_with_probe
            note = "missing; conservative default while has_probe is true - " + warn_msg
        else:
            value = 0.0
            note = "no probe; irrelevant"
        fields_out.append(FieldInfo(field_name, label, unit, group, value=value,
                                     source="default", raw=None, note=note))
        return value
    raw_val, raw_text = extracted
    try:
        value = float(raw_val)
    except (TypeError, ValueError):
        issues.append(Issue(field_name, f"{label} value {raw_val!r} is not a number.", "error"))
        fields_out.append(FieldInfo(field_name, label, unit, group,
                                     value=default_with_probe if has_probe else 0.0,
                                     source="default", raw=str(raw_val), note="non-numeric; using default"))
        return default_with_probe if has_probe else 0.0
    if value < lo or value > hi:
        clamped = min(max(value, lo), hi)
        issues.append(Issue(field_name,
                             f"{label} {_fmt_num(value)} is outside [{lo:g},{hi:g}]; clamped to {_fmt_num(clamped)}.",
                             "warn"))
        fields_out.append(FieldInfo(field_name, label, unit, group, value=clamped,
                                     source="clamped", raw=raw_text,
                                     note=f"config said {_fmt_num(value)}, clamped to {_fmt_num(clamped)}"))
        return clamped
    fields_out.append(FieldInfo(field_name, label, unit, group, value=value,
                                 source="parsed", raw=raw_text, note=None))
    return value


# The non-planar dip allowed on a printer whose real clearance we do not know.
# Deliberately small: it caps how far the nozzle drops below already-printed
# material, so under-guessing costs print quality while over-guessing costs
# hardware. Not tied to any specific machine -- see _validate_z_amp_max.
_Z_AMP_MAX_UNKNOWN_DEFAULT = 0.4


def _validate_z_amp_max(source, has_probe: bool, probe_clearance: float,
                         issues: list[Issue], fields_out: list[FieldInfo]) -> float:
    label, unit, group = _FIELD_META["z_amp_max"]
    extracted = _extract(source, "z_amp_max")
    if extracted is not None:
        raw_val, raw_text = extracted
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            value = None
        # Non-finite would survive the clamp below (every NaN comparison is
        # False) and then defeat every downstream min() that bounds the wave
        # amplitude. Fall through to the derived default instead.
        if value is not None and (value != value or value in (float("inf"), float("-inf"))):
            issues.append(Issue(
                "z_amp_max",
                f"{label} value {raw_val!r} is not a finite number; using the "
                f"derived conservative default instead.", "error"))
            value = None
        if value is not None:
            clamped = min(max(value, 0.0), 10.0)
            if clamped != value:
                issues.append(Issue("z_amp_max",
                                     f"{label} {_fmt_num(value)} is outside [0,10]; clamped to {_fmt_num(clamped)}.",
                                     "warn"))
                fields_out.append(FieldInfo("z_amp_max", label, unit, group, value=clamped,
                                             source="clamped", raw=raw_text,
                                             note=f"config said {_fmt_num(value)}, clamped to {_fmt_num(clamped)}"))
            else:
                fields_out.append(FieldInfo("z_amp_max", label, unit, group, value=clamped,
                                             source="parsed", raw=raw_text, note=None))
            return clamped

    # No config format on earth records this value, so there is nothing to
    # derive it FROM. It used to be computed as
    #     has_probe -> min(0.95, max(0.2, probe_clearance * 0.5))
    #     else      -> 1.0
    # which was wrong twice over:
    #
    #   * 0.95 is the TRIDENT's measured constant (CLAUDE.md: revised down
    #     empirically after a probe strike). probe_clearance is itself never
    #     parsed by any of the five importers -- it is always its own 2.0
    #     default -- so the formula collapsed to min(0.95, 1.0) = 0.95 for
    #     essentially every imported machine. That is "missing config values
    #     become another machine's numbers", the exact invariant CLAUDE.md
    #     names, dressed up as a derivation.
    #   * the no-probe branch (1.0) was LOOSER than the probe branch (0.95).
    #     Probe detection is a fixed list of section-name heads, so an
    #     unrecognised probe reads as "no probe" and was handed MORE dip.
    #     Hardware we failed to detect must get more margin, not less.
    #
    # One flat conservative floor instead, on the same footing as CLAUDE.md's
    # "an unknown max_z_velocity is 10 mm/s": low enough that it cannot drive
    # the nozzle into printed material on a machine we know nothing about.
    # Smaller is unambiguously safer here -- this caps how far the nozzle dips
    # below already-printed material. It is a GUESS, not a measurement; the
    # user raises it deliberately after a test print.
    value = _Z_AMP_MAX_UNKNOWN_DEFAULT
    issues.append(Issue("z_amp_max",
                         f"{label} is not recorded in any printer config, so it was set to a "
                         f"conservative {_fmt_num(value)} mm. This is a guess, not a measurement - "
                         f"raise it only after a test print confirms the toolhead clears the "
                         f"printed wall.",
                         "warn"))
    fields_out.append(FieldInfo("z_amp_max", label, unit, group, value=value, source="default",
                                 raw=None,
                                 note="conservative default; not derivable from any config - "
                                      "confirm with a test print before raising"))
    return value


# Peak printable wave-wall slope (amp*waves/(radius*silhouette)) for a machine
# whose real quality ceiling we do not know. 0.25 is the TRIDENT's own
# 2026-07-05 measurement (see PrinterProfile.quality_slope_max) -- reused
# here as the unknown-machine default because it is the most conservative
# figure this app has ever measured, NOT because it is claimed to hold for
# any other machine. Unlike _Z_AMP_MAX_UNKNOWN_DEFAULT above, getting this
# wrong costs print QUALITY, not hardware -- which is why (see
# _validate_quality_slope_max) the absent-value path below emits no Issue.
_QUALITY_SLOPE_UNKNOWN_DEFAULT = 0.25


def _validate_quality_slope_max(source, issues: list[Issue], fields_out: list[FieldInfo]) -> float:
    """Mirror _validate_z_amp_max's shape, with one deliberate divergence in
    the absent-value case.

    z_amp_max going missing can let the toolhead strike printed material, so
    it warns on every import. quality_slope_max going missing only risks a
    worse-looking upper wave on THIS ONE machine -- a cosmetic risk, not a
    hardware one -- and no format parser this app ships extracts this field
    from any real config today (it is not a real firmware/slicer setting,
    same as z_amp_max), so "absent" is the ordinary case for every import,
    not an exceptional one. Warning on every import for that would just
    churn issue counts across the whole existing test suite for no safety
    gain, so the absent case below emits a FieldInfo only, never an Issue.
    """
    label, unit, group = _FIELD_META["quality_slope_max"]
    extracted = _extract(source, "quality_slope_max")
    if extracted is not None:
        raw_val, raw_text = extracted
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            value = None
        # Same non-finite trap as z_amp_max: NaN/inf would survive the clamp
        # below (every NaN comparison is False) and then defeat every
        # downstream min() that bounds the wave slope. Fall through to the
        # conservative default instead of clamping it.
        if value is not None and (value != value or value in (float("inf"), float("-inf"))):
            issues.append(Issue(
                "quality_slope_max",
                f"{label} value {raw_val!r} is not a finite number; using the "
                f"conservative default instead.", "error"))
            value = None
        if value is not None:
            clamped = min(max(value, 0.0), 10.0)
            if clamped != value:
                issues.append(Issue(
                    "quality_slope_max",
                    f"{label} {_fmt_num(value)} is outside [0,10]; clamped to {_fmt_num(clamped)}.",
                    "warn"))
                fields_out.append(FieldInfo("quality_slope_max", label, unit, group, value=clamped,
                                             source="clamped", raw=raw_text,
                                             note=f"config said {_fmt_num(value)}, clamped to {_fmt_num(clamped)}"))
            else:
                fields_out.append(FieldInfo("quality_slope_max", label, unit, group, value=clamped,
                                             source="parsed", raw=raw_text, note=None))
            return clamped

    # Absent: the ordinary case (see docstring above) -- no Issue, just a
    # FieldInfo recording that this is a guess, not a measurement.
    value = _QUALITY_SLOPE_UNKNOWN_DEFAULT
    fields_out.append(FieldInfo(
        "quality_slope_max", label, unit, group, value=value, source="default", raw=None,
        note="not recorded in any printer config; using the Trident's measured "
             "0.25 as a conservative placeholder, not a measurement on this machine"))
    return value


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def _validate(source, *, allow_raw_gcode: bool = False, repair: bool = False) -> ValidationResult:
    issues: list[Issue] = []
    fields_out: list[FieldInfo] = []

    name = _validate_name(source, issues, fields_out)
    firmware = _validate_firmware(source, issues, fields_out)
    pa_gcode_style = "klipper" if firmware == "klipper" else "marlin"
    _label, _unit, _group = _FIELD_META["pa_gcode_style"]
    fields_out.append(FieldInfo("pa_gcode_style", _label, _unit, _group,
                                 value=pa_gcode_style, source="default" if firmware != "klipper"
                                 else "parsed", raw=None,
                                 note="forced consistent with firmware"))

    bed_size_x = _validate_numeric(source, "bed_size_x", issues, fields_out)
    bed_size_y = _validate_numeric(source, "bed_size_y", issues, fields_out)
    z_max = _validate_numeric(source, "z_max", issues, fields_out)
    z_min = _validate_numeric(source, "z_min", issues, fields_out)

    print_min_x, print_max_x = _resolve_print_range(source, "x", bed_size_x, issues, fields_out)
    print_min_y, print_max_y = _resolve_print_range(source, "y", bed_size_y, issues, fields_out)

    max_velocity = _validate_numeric(source, "max_velocity", issues, fields_out)
    max_z_velocity = _validate_numeric(source, "max_z_velocity", issues, fields_out)
    max_accel = _validate_numeric(source, "max_accel", issues, fields_out)
    max_z_accel = _validate_numeric(source, "max_z_accel", issues, fields_out)

    nozzle_diameter = _validate_numeric(source, "nozzle_diameter", issues, fields_out)
    filament_diameter = _validate_numeric(source, "filament_diameter", issues, fields_out)
    max_extrude_cross_section = _validate_numeric(
        source, "max_extrude_cross_section", issues, fields_out)

    has_probe = _validate_bool(source, "has_probe", False, fields_out)
    probe_dx = _validate_numeric(source, "probe_dx", issues, fields_out)
    probe_dy = _validate_numeric(source, "probe_dy", issues, fields_out)
    probe_clearance = _validate_probe_extra(
        source, "probe_clearance", has_probe, 2.0,
        "verify the real clearance before printing anything tall", issues, fields_out)
    probe_radius = _validate_probe_extra(
        source, "probe_radius", has_probe, 10.0,
        "conservative keep-out radius, verify against the real probe body",
        issues, fields_out)

    z_amp_max = _validate_z_amp_max(source, has_probe, probe_clearance, issues, fields_out)
    quality_slope_max = _validate_quality_slope_max(source, issues, fields_out)

    max_nozzle_temp = _validate_numeric(source, "max_nozzle_temp", issues, fields_out)
    max_bed_temp = _validate_numeric(source, "max_bed_temp", issues, fields_out)

    # --- G-code -------------------------------------------------------
    start_ext = _extract(source, "start_gcode")
    end_ext = _extract(source, "end_gcode")

    start_label, start_unit, start_group = _FIELD_META["start_gcode"]
    if start_ext is not None:
        raw_start = str(start_ext[0])
        clean_start, gcode_issues, stripped_start = sanitize_gcode(
            raw_start, "start", allow_raw_gcode=allow_raw_gcode, repair=repair)
        issues.extend(gcode_issues)
        note = f"{len(stripped_start)} dangerous line(s) removed" if stripped_start else None
        fields_out.append(FieldInfo("start_gcode", start_label, start_unit, start_group,
                                     value=clean_start, source="parsed",
                                     raw=start_ext[1] or raw_start, note=note))
    else:
        # The generic default already heats (see _DEFAULT_START_GCODE), so
        # repair is a guaranteed no-op here -- passed anyway for uniformity.
        clean_start, gcode_issues, stripped_start = sanitize_gcode(
            _DEFAULT_START_GCODE, "start", allow_raw_gcode=allow_raw_gcode, repair=repair)
        issues.extend(gcode_issues)
        issues.append(Issue("start_gcode",
                             "no start G-code found; installed a generic safe default - review it.",
                             "warn"))
        fields_out.append(FieldInfo("start_gcode", start_label, start_unit, start_group,
                                     value=clean_start, source="default", raw=None,
                                     note="generic safe default installed; please review"))

    end_label, end_unit, end_group = _FIELD_META["end_gcode"]
    if end_ext is not None:
        raw_end = str(end_ext[0])
        clean_end, gcode_issues, stripped_end = sanitize_gcode(
            raw_end, "end", allow_raw_gcode=allow_raw_gcode)
        issues.extend(gcode_issues)
        note = f"{len(stripped_end)} dangerous line(s) removed" if stripped_end else None
        fields_out.append(FieldInfo("end_gcode", end_label, end_unit, end_group,
                                     value=clean_end, source="parsed",
                                     raw=end_ext[1] or raw_end, note=note))
    else:
        clean_end, gcode_issues, stripped_end = sanitize_gcode(
            _DEFAULT_END_GCODE, "end", allow_raw_gcode=allow_raw_gcode)
        issues.extend(gcode_issues)
        fields_out.append(FieldInfo("end_gcode", end_label, end_unit, end_group,
                                     value=clean_end, source="default", raw=None,
                                     note="generic safe default installed"))

    # --- Cross-field invariants ----------------------------------------
    if print_min_x >= print_max_x:
        issues.append(Issue("print_min_x", "print area min X must be less than max X.", "error"))
    if print_min_y >= print_max_y:
        issues.append(Issue("print_min_y", "print area min Y must be less than max Y.", "error"))

    if z_max <= 0:
        issues.append(Issue("z_max", "z_max must be greater than 0.", "error"))

    if max_z_velocity > max_velocity:
        issues.append(Issue(
            "max_z_velocity",
            f"max_z_velocity {_fmt_num(max_z_velocity)} exceeds max_velocity "
            f"{_fmt_num(max_velocity)}; clamped down to match.",
            "warn"))
        max_z_velocity = max_velocity
        for fi in fields_out:
            if fi.name == "max_z_velocity":
                fi.value, fi.source = max_z_velocity, "clamped"

    if max_z_accel > max_accel:
        issues.append(Issue(
            "max_z_accel",
            f"max_z_accel {_fmt_num(max_z_accel)} exceeds max_accel "
            f"{_fmt_num(max_accel)}; clamped down to match.",
            "warn"))
        max_z_accel = max_accel
        for fi in fields_out:
            if fi.name == "max_z_accel":
                fi.value, fi.source = max_z_accel, "clamped"

    # An implausibly high heater ceiling is legal (some tool-changers really do
    # declare 500C) but it silently disables the protection that ceiling exists
    # to provide: GcodeWriter clamps the requested temperature DOWN to it, so a
    # 500C limit never clamps anything. Say so rather than showing a bare
    # "parsed" tag next to a number that is doing no work.
    if max_nozzle_temp > 320.0:
        issues.append(Issue(
            "max_nozzle_temp",
            f"max nozzle temp {_fmt_num(max_nozzle_temp)}C is far above a typical "
            f"hotend limit (~300C); temperatures will effectively never be clamped. "
            f"Set the real limit for your hotend.", "warn"))
    if max_bed_temp > 130.0:
        issues.append(Issue(
            "max_bed_temp",
            f"max bed temp {_fmt_num(max_bed_temp)}C is far above a typical bed limit "
            f"(~120C); bed temperatures will effectively never be clamped.", "warn"))

    usable_x = print_max_x - print_min_x
    usable_y = print_max_y - print_min_y
    if usable_x < 20 or usable_y < 20:
        issues.append(Issue(
            "", f"usable print area {_fmt_num(usable_x)} x {_fmt_num(usable_y)} mm is smaller "
            f"than the 20x20 mm minimum - no usable print area.", "error"))

    if abs(filament_diameter - 1.75) > 0.1 and abs(filament_diameter - 2.85) > 0.1:
        issues.append(Issue(
            "filament_diameter",
            f"filament diameter {_fmt_num(filament_diameter)} mm is unusual "
            f"(neither ~1.75 nor ~2.85 mm) - verify it.",
            "warn"))

    if max_extrude_cross_section > 0.0 and max_extrude_cross_section < _NOMINAL_BEAD_AREA:
        issues.append(Issue(
            "max_extrude_cross_section",
            f"max_extrude_cross_section {_fmt_num(max_extrude_cross_section)} mm^2 is smaller "
            f"than this app's nominal bead ({_NOMINAL_BEAD_AREA:.3f} mm^2) - Klipper will abort "
            f"mid-print; raise it or use a thinner line width/layer height.",
            "warn"))

    if has_probe and probe_dx == 0.0 and probe_dy == 0.0:
        issues.append(Issue(
            "probe_dx",
            "has_probe is true but probe offsets are both 0 - they look unset; "
            "probe keep-out checking will be wrong until real offsets are entered.",
            "warn"))

    profile = PrinterProfile(
        name=name,
        firmware=firmware,
        bed_size_x=bed_size_x, bed_size_y=bed_size_y, z_max=z_max, z_min=z_min,
        print_min_x=print_min_x, print_min_y=print_min_y,
        print_max_x=print_max_x, print_max_y=print_max_y,
        max_velocity=max_velocity, max_z_velocity=max_z_velocity,
        max_accel=max_accel, max_z_accel=max_z_accel,
        nozzle_diameter=nozzle_diameter, filament_diameter=filament_diameter,
        has_probe=has_probe, probe_dx=probe_dx, probe_dy=probe_dy,
        probe_clearance=probe_clearance, probe_radius=probe_radius,
        z_amp_max=z_amp_max,
        quality_slope_max=quality_slope_max,
        start_gcode=clean_start, end_gcode=clean_end,
        pa_gcode_style=pa_gcode_style,
        max_nozzle_temp=max_nozzle_temp, max_bed_temp=max_bed_temp,
        max_extrude_cross_section=max_extrude_cross_section,
    )

    # Stable field ordering for the UI: group order, then insertion order within a group.
    fields_out.sort(key=lambda fi: GROUP_ORDER.index(fi.group)
                     if fi.group in GROUP_ORDER else len(GROUP_ORDER))

    return ValidationResult(profile=profile, fields=fields_out, issues=issues)


def validate_raw(raw: RawConfig, *, allow_raw_gcode: bool = False, repair: bool = False) -> ValidationResult:
    """``repair`` defaults to False; pass True only from the actual import
    boundary (a freshly uploaded/loaded config becoming a profile for the
    first time), never from a re-validation of something already imported --
    see sanitize_gcode's docstring for why."""
    return _validate(raw, allow_raw_gcode=allow_raw_gcode, repair=repair)


def validate_profile_dict(d: dict, *, allow_raw_gcode: bool = False, repair: bool = False) -> ValidationResult:
    """Validate a browser-supplied profile dict. Exactly as strict as
    validate_raw -- the browser is never trusted, so every field goes through
    the same LIMITS/derived-rule/sanitize_gcode pipeline, just with
    source="parsed" instead of provenance tied to a parsed config file.

    ``repair`` defaults to False for the same reason as validate_raw: the
    review dialog's live re-validation and the final save both go through
    this function, and repairing on every call would make the auto-inserted
    heating reappear the instant a user deleted it."""
    if not isinstance(d, dict):
        d = {}
    return _validate(d, allow_raw_gcode=allow_raw_gcode, repair=repair)
