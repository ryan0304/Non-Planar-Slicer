"""Parse a foreign printer config file into a raw, UNTRUSTED field dict.

This module only extracts values -- it does no validation, no clamping, and no
safety judgement of any kind. That is deliberate: keeping "what does this file
say" strictly separate from "is that safe" (trident_gcode/printer_validate.py)
means the validator can be exercised against hand-crafted dicts in tests
without ever going through a parser, and a parsing bug can never accidentally
skip a safety check.

Five source formats are recognised:
  klipper_cfg  -- Klipper's printer.cfg (Voron Trident and friends)
  orca_json    -- an OrcaSlicer / Bambu Studio machine profile .json (also
                  covers Creality Print 5.x machine profiles, which are an
                  OrcaSlicer fork using the same keys)
  cura_def_json -- a Cura / Creality Slicer printer definition .def.json
  prusa_ini    -- a PrusaSlicer / SuperSlicer exported printer .ini
  trident_json -- our own export (printer_store.py's on-disk format)

Every extracted value is wrapped in a ParsedField so the validator can show
the user exactly what text in the source file produced each field.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields as dc_fields


class PrinterImportError(Exception):
    """The file could not be parsed at all (undetectable format, or the
    detected format's structure is too broken to extract anything useful).
    Distinct from a validation failure: a structurally sound file with unsafe
    *values* still parses fine and is instead flagged by printer_validate."""


@dataclass
class ParsedField:
    value: object            # parsed python value (str/float/bool/None)
    raw: str                 # the literal source text it came from
    key: str                 # source key, e.g. "stepper_x.position_max"


@dataclass
class RawConfig:
    fmt: str                              # "klipper_cfg"|"orca_json"|"prusa_ini"|"trident_json"
    fields: dict[str, ParsedField] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source_name: str = ""


_MAX_INPUT_BYTES = 2 * 1024 * 1024  # 2 MB; caller (serve.py) also enforces this on upload


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------
def detect_format(text: str, filename: str) -> str | None:
    """Sniff which of the 5 supported formats ``text`` is.

    Order matters: JSON is tried first (cheap and unambiguous once it parses),
    then the two text-based formats are told apart by a section-header probe
    (klipper) vs a flat "key = value" probe (prusa ini).

    Within JSON, cura_def_json MUST be checked before the generic orca_json
    fallback (which otherwise accepts any valid JSON object): a Cura printer
    definition is JSON with an "overrides" object, and/or "inherits":
    "fdmprinter", and/or "version": 2 with a "metadata" object. Creality
    Print's newer machine profiles are an OrcaSlicer fork using orca_json's
    own keys (printable_area, machine_max_speed_z, ...) and carry none of
    these Cura signals, so they still fall through to orca_json correctly.
    """
    stripped = text.strip()
    if stripped:
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            data = None
        else:
            if isinstance(data, dict) and data.get("schema") == "trident-printer/1":
                return "trident_json"
            if isinstance(data, dict) and _looks_like_cura_def(data):
                return "cura_def_json"
            return "orca_json"

    if re.search(r'^\s*\[\s*(stepper_x|printer|extruder)\s*\]', text,
                 re.MULTILINE | re.IGNORECASE):
        return "klipper_cfg"

    low = text.lower()
    if "bed_shape" in low or "printer_settings_id" in low or "max_print_height" in low:
        return "prusa_ini"

    return None


def _looks_like_cura_def(data: dict) -> bool:
    """The reliable Cura signal is "overrides" being a dict of settings.
    "inherits": "fdmprinter" and version-2-with-metadata are secondary
    signals for definitions that (unusually) carry no overrides of their
    own -- e.g. a bare machine variant that inherits everything."""
    if isinstance(data.get("overrides"), dict):
        return True
    if data.get("inherits") == "fdmprinter":
        return True
    if data.get("version") == 2 and isinstance(data.get("metadata"), dict):
        return True
    return False


def parse(text: str, filename: str = "") -> RawConfig:
    """Parse ``text`` into a RawConfig. Raises PrinterImportError on failure.

    ``filename`` (just the name, not a filesystem path) is used for the
    filename-stem name fallback and to label the result. Caller (serve.py /
    generate.py) is responsible for enforcing the 2 MB size cap on the raw
    upload before it ever reaches here; we re-check defensively.
    """
    if len(text.encode("utf-8", "replace")) > _MAX_INPUT_BYTES:
        raise PrinterImportError("config file exceeds the 2 MB import limit")

    fmt = detect_format(text, filename)
    if fmt is None:
        raise PrinterImportError(
            "could not detect a supported printer config format. Expected one "
            "of: Klipper printer.cfg, OrcaSlicer/Bambu Studio/Creality Print "
            "machine .json, Cura/Creality Slicer printer .def.json, "
            "PrusaSlicer/SuperSlicer printer .ini, or an exported Trident "
            "printer .json.")

    try:
        if fmt == "trident_json":
            return _parse_trident_json(text, filename)
        if fmt == "orca_json":
            return _parse_orca_json(text, filename)
        if fmt == "cura_def_json":
            return _parse_cura_def_json(text, filename)
        if fmt == "klipper_cfg":
            return _parse_klipper_cfg(text, filename)
        if fmt == "prusa_ini":
            return _parse_prusa_ini(text, filename)
    except PrinterImportError:
        raise
    except Exception as e:  # pragma: no cover - defensive: never leak a traceback
        raise PrinterImportError(f"could not parse {fmt} file: {e}")

    raise PrinterImportError(f"unsupported format: {fmt}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Shared scalar helpers (Orca-style "value or [value]" fields)
# ---------------------------------------------------------------------------
def _scalar(v):
    """Orca/Bambu JSON values are often one-element lists of strings; unwrap."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        return s
    return v


def _num(v):
    s = _scalar(v)
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_polygon(raw: str) -> list[tuple[float, float]]:
    """Parse a "0x0,256x0,256x256,0x256" style polygon into (x, y) points."""
    pts: list[tuple[float, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip().lower()
        if "x" not in chunk:
            continue
        xs, _, ys = chunk.partition("x")
        try:
            pts.append((float(xs), float(ys)))
        except ValueError:
            continue
    return pts


# ---------------------------------------------------------------------------
# trident_json -- our own export, read straight through
# ---------------------------------------------------------------------------
def _parse_trident_json(text: str, filename: str) -> RawConfig:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PrinterImportError(f"invalid JSON: {e}")
    if not isinstance(data, dict):
        raise PrinterImportError("trident printer file must be a JSON object")
    profile_raw = data.get("profile")
    if not isinstance(profile_raw, dict):
        raise PrinterImportError("trident printer file is missing its 'profile' object")

    from .profile import PrinterProfile
    valid = {f.name for f in dc_fields(PrinterProfile)}

    fields: dict[str, ParsedField] = {}
    for k, v in profile_raw.items():
        if k not in valid:
            raise PrinterImportError(f"unknown printer field '{k}' in trident_json profile")
        fields[k] = ParsedField(value=v, raw=json.dumps(v), key=f"profile.{k}")

    return RawConfig(fmt="trident_json", fields=fields, notes=[], source_name=filename)


# ---------------------------------------------------------------------------
# orca_json -- OrcaSlicer / Bambu Studio machine profile
# ---------------------------------------------------------------------------
def _parse_orca_json(text: str, filename: str) -> RawConfig:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PrinterImportError(f"invalid JSON: {e}")
    if not isinstance(data, dict):
        raise PrinterImportError("orca/bambu machine profile must be a JSON object")

    fields: dict[str, ParsedField] = {}
    notes: list[str] = []

    def put_num(field_name, raw_key):
        if raw_key not in data:
            return
        v = _num(data[raw_key])
        if v is not None:
            fields[field_name] = ParsedField(value=v, raw=json.dumps(data[raw_key]), key=raw_key)

    if "inherits" in data:
        notes.append(
            "profile 'inherits' from a parent machine profile that was not "
            "resolved (the parent may not be installed on this machine) - "
            "review every value, some limits may actually live in the parent.")

    name = _scalar(data.get("name")) or _scalar(data.get("printer_model"))
    if name:
        fields["name"] = ParsedField(value=name, raw=str(name), key="name/printer_model")

    area = _scalar(data.get("printable_area"))
    if isinstance(area, str) and area:
        pts = _parse_polygon(area)
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            fields["bed_size_x"] = ParsedField(value=max(xs), raw=area, key="printable_area")
            fields["bed_size_y"] = ParsedField(value=max(ys), raw=area, key="printable_area")
            fields["print_min_x"] = ParsedField(value=min(xs), raw=area, key="printable_area")
            fields["print_min_y"] = ParsedField(value=min(ys), raw=area, key="printable_area")
            fields["print_max_x"] = ParsedField(value=max(xs), raw=area, key="printable_area")
            fields["print_max_y"] = ParsedField(value=max(ys), raw=area, key="printable_area")

    put_num("z_max", "printable_height")
    put_num("max_velocity", "machine_max_speed_x")
    put_num("max_z_velocity", "machine_max_speed_z")
    put_num("max_accel", "machine_max_acceleration_x")
    put_num("max_z_accel", "machine_max_acceleration_z")
    put_num("nozzle_diameter", "nozzle_diameter")

    sg = _scalar(data.get("machine_start_gcode"))
    if sg:
        fields["start_gcode"] = ParsedField(value=sg, raw=sg, key="machine_start_gcode")
    eg = _scalar(data.get("machine_end_gcode"))
    if eg:
        fields["end_gcode"] = ParsedField(value=eg, raw=eg, key="machine_end_gcode")

    flavor = (_scalar(data.get("gcode_flavor")) or "")
    flavor_l = flavor.lower()
    if flavor_l:
        if flavor_l == "klipper":
            firmware = "klipper"
        elif flavor_l.startswith("marlin") or flavor_l.startswith("reprap"):
            firmware = "marlin"
        else:
            firmware = flavor_l
        fields["firmware"] = ParsedField(value=firmware, raw=flavor, key="gcode_flavor")
        fields["pa_gcode_style"] = ParsedField(
            value=("klipper" if firmware == "klipper" else "marlin"),
            raw=flavor, key="gcode_flavor")

    return RawConfig(fmt="orca_json", fields=fields, notes=notes, source_name=filename)


# ---------------------------------------------------------------------------
# cura_def_json -- Cura / Creality Slicer printer definition (.def.json)
#
# Cura settings live under "overrides" as either
#   {"default_value": 220}                 -- the literal, safe to read
#   {"value": "=machine_width / 2"}        -- a PYTHON EXPRESSION, never eval'd
#   220                                     -- a bare value (accepted too)
# We only ever read default_value (or a bare scalar). A setting that has only
# a "value" expression is skipped and noted by name -- evaluating an
# arbitrary string pulled from an uploaded file would be executing untrusted
# code disguised as data.
# ---------------------------------------------------------------------------
_CURA_SKIP = object()  # sentinel: setting has only a 'value' expression


def _cura_setting(overrides: dict, key: str):
    """Return (value, raw_text) for a Cura setting, _CURA_SKIP-tagged if the
    setting only carries an unevaluated 'value' expression, or None if the
    key is absent entirely."""
    if key not in overrides:
        return None
    v = overrides[key]
    if isinstance(v, dict):
        if "default_value" in v:
            dv = v["default_value"]
            return dv, json.dumps(dv)
        if "value" in v:
            return _CURA_SKIP, None
        return None
    # Bare "machine_width": 220 form.
    return v, json.dumps(v)


# Cura's fdmprinter.def.json uses absurd magnitudes as "no limit set"
# sentinels on motion settings -- the canonical one is 299792458000, the speed
# of light in mm/s. No real machine setting comes within orders of magnitude of
# this, so anything at or above it is a sentinel, not a value.
_CURA_SENTINEL = 1e6


def _parse_cura_def_json(text: str, filename: str) -> RawConfig:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PrinterImportError(f"invalid JSON: {e}")
    if not isinstance(data, dict):
        raise PrinterImportError("cura printer definition must be a JSON object")

    overrides = data.get("overrides")
    if not isinstance(overrides, dict):
        overrides = {}

    fields: dict[str, ParsedField] = {}
    notes: list[str] = []

    inherits = data.get("inherits")
    if inherits:
        notes.append(
            f"definition 'inherits' from '{inherits}' (usually 'fdmprinter') "
            "which was not resolved - the parent definition was not uploaded "
            "here, so review every value; some settings may actually live in "
            "the parent.")

    def get(key: str):
        """Fetch a Cura setting, appending the standard skip-note for
        value-only settings and returning None for both 'absent' and
        'skipped' so callers can treat them the same way."""
        got = _cura_setting(overrides, key)
        if got is None:
            return None
        val, raw = got
        if val is _CURA_SKIP:
            notes.append(
                f"setting '{key}' has only a 'value' expression (no "
                f"default_value) and was skipped - Cura's 'value' is a "
                f"Python expression evaluated at slice time, not a literal, "
                f"so it is never read here.")
            return None
        return val, raw

    def put_num(field_name: str, *keys: str):
        for key in keys:
            got = get(key)
            if got is None:
                continue
            val, raw = got
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            if val >= _CURA_SENTINEL:
                # Cura's fdmprinter ships "unlimited" sentinels on the motion
                # settings a definition does not override. Passing one through
                # to be clamped would turn "unset" into "this app's maximum",
                # so it is dropped and the validator's conservative default
                # applies instead.
                notes.append(
                    f"{key} is {val:g}, Cura's 'unset' placeholder rather than "
                    f"a real machine limit - treated as missing so the "
                    f"conservative default applies.")
                continue
            fields[field_name] = ParsedField(value=val, raw=raw, key=f"overrides.{key}")
            return

    def put_str(field_name: str, key: str):
        got = get(key)
        if got is None:
            return
        val, raw = got
        if not isinstance(val, str) or not val.strip():
            return
        fields[field_name] = ParsedField(value=val, raw=raw, key=f"overrides.{key}")

    # --- name -----------------------------------------------------------
    name_got = get("machine_name")
    name = name_got[0] if (name_got and isinstance(name_got[0], str) and name_got[0].strip()) else None
    if name is None:
        top_name = data.get("name")
        if isinstance(top_name, str) and top_name.strip():
            name = top_name
    if name:
        fields["name"] = ParsedField(value=name, raw=str(name), key="overrides.machine_name / name")

    # --- geometry / motion ------------------------------------------------
    put_num("bed_size_x", "machine_width")
    put_num("bed_size_y", "machine_depth")
    put_num("z_max", "machine_height")
    put_num("max_velocity", "machine_max_feedrate_x")
    put_num("max_accel", "machine_max_acceleration_x", "machine_acceleration")
    put_num("nozzle_diameter", "machine_nozzle_size")
    put_num("filament_diameter", "material_diameter")

    # machine_max_feedrate_z gets its own handling: Cura's stock fdmprinter
    # definition ships an absurd placeholder here (e.g. 299792458000, the
    # speed of light in mm/s -- literally "unset, don't clamp me"). The
    # validator's LIMITS clamp already catches this, but a plain "clamped"
    # tag doesn't explain WHY the source number was nonsense, so flag it.
    z_got = get("machine_max_feedrate_z")
    if z_got is not None:
        zval, zraw = z_got
        try:
            zval_f = float(zval)
        except (TypeError, ValueError):
            zval_f = None
        if zval_f is not None:
            if zval_f >= _CURA_SENTINEL:
                # Do NOT pass a sentinel through to be clamped. Clamping lands
                # on the LIMITS ceiling (100 mm/s), and on the Ender 3 this
                # definition describes the real figure is 5 -- a 20x
                # over-estimate, arrived at by treating "unset" as "as fast as
                # possible". Dropping the field instead lets the validator
                # apply its conservative missing-value default, which is the
                # whole point of principle 1.
                notes.append(
                    f"machine_max_feedrate_z is {zval_f:g} mm/s -- Cura's "
                    "fdmprinter 'unset' placeholder, not a real machine "
                    "limit. Treated as missing so the conservative default "
                    "applies; set your machine's real Z feedrate.")
            else:
                fields["max_z_velocity"] = ParsedField(
                    value=zval_f, raw=zraw, key="overrides.machine_max_feedrate_z")
                if zval_f > 1000:
                    notes.append(
                        f"machine_max_feedrate_z is {zval_f:g} mm/s, an absurdly "
                        "high value - this looks like a placeholder rather than "
                        "a real machine limit. It will be clamped to this app's "
                        "safety ceiling; set the real Z feedrate instead.")

    put_num("max_z_accel", "machine_max_acceleration_z")

    # --- center-origin machines -------------------------------------------
    # machine_center_is_zero means the origin sits at the bed centre (deltas
    # and some custom kinematics), so the printable range is -w/2..+w/2. This
    # app assumes a front-left origin and the validator requires
    # print_min >= 0, so we do NOT attempt to translate the coordinates here
    # -- silently mangling a centred bed into a corner-origin one would be
    # worse than saying nothing. Leave print_min/print_max unset (the
    # validator's 5mm-inset default takes over) and flag it loudly.
    center_got = get("machine_center_is_zero")
    is_center_zero = False
    if center_got is not None:
        cv = center_got[0]
        is_center_zero = (cv.strip().lower() in ("true", "1", "yes")
                           if isinstance(cv, str) else bool(cv))
    if is_center_zero:
        notes.append(
            "machine_center_is_zero is true (origin at bed centre); this "
            "app assumes a front-left origin and does not support a centred "
            "safe area automatically - the safe print area was left at the "
            "default inset, set it by hand to match your machine's real "
            "usable area.")

    # --- G-code -----------------------------------------------------------
    put_str("start_gcode", "machine_start_gcode")
    put_str("end_gcode", "machine_end_gcode")

    # --- firmware -----------------------------------------------------------
    flavor_got = get("machine_gcode_flavor")
    if flavor_got is not None and isinstance(flavor_got[0], str) and flavor_got[0].strip():
        flavor = flavor_got[0].strip()
        flavor_l = flavor.lower()
        if "klipper" in flavor_l:
            firmware = "klipper"
        elif flavor_l == "griffin" or flavor_l.startswith("marlin") or flavor_l.startswith("reprap"):
            firmware = "marlin"
        else:
            firmware = flavor_l
        fields["firmware"] = ParsedField(value=firmware, raw=flavor, key="overrides.machine_gcode_flavor")
        fields["pa_gcode_style"] = ParsedField(
            value=("klipper" if firmware == "klipper" else "marlin"),
            raw=flavor, key="overrides.machine_gcode_flavor")

    return RawConfig(fmt="cura_def_json", fields=fields, notes=notes, source_name=filename)


# ---------------------------------------------------------------------------
# prusa_ini -- PrusaSlicer / SuperSlicer exported printer profile
# ---------------------------------------------------------------------------
def _parse_prusa_ini(text: str, filename: str) -> RawConfig:
    kv: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        kv[key.strip()] = val.strip()

    if not kv:
        raise PrinterImportError("prusa/superslicer ini has no 'key = value' lines")

    fields: dict[str, ParsedField] = {}
    notes: list[str] = []

    name = kv.get("printer_model") or (
        os.path.splitext(os.path.basename(filename))[0] if filename else None)
    if name:
        fields["name"] = ParsedField(value=name, raw=name, key="printer_model")

    bed_shape = kv.get("bed_shape")
    if bed_shape:
        pts = _parse_polygon(bed_shape)
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            fields["bed_size_x"] = ParsedField(value=max(xs), raw=bed_shape, key="bed_shape")
            fields["bed_size_y"] = ParsedField(value=max(ys), raw=bed_shape, key="bed_shape")

    def numfield(ini_key, field_name):
        if ini_key not in kv:
            return
        raw = kv[ini_key]
        # Prusa often stores "normal,silent" pairs (or per-extruder lists);
        # the first value is the one that matters for our single-limit model.
        first = raw.split(",")[0].strip()
        fields[field_name] = ParsedField(value=first, raw=raw, key=ini_key)

    numfield("max_print_height", "z_max")
    numfield("machine_max_feedrate_z", "max_z_velocity")
    numfield("machine_max_acceleration_z", "max_z_accel")
    numfield("machine_max_feedrate_x", "max_velocity")
    numfield("machine_max_acceleration_x", "max_accel")
    numfield("nozzle_diameter", "nozzle_diameter")

    if "start_gcode" in kv:
        raw = kv["start_gcode"]
        fields["start_gcode"] = ParsedField(value=raw.replace("\\n", "\n"), raw=raw, key="start_gcode")
    if "end_gcode" in kv:
        raw = kv["end_gcode"]
        fields["end_gcode"] = ParsedField(value=raw.replace("\\n", "\n"), raw=raw, key="end_gcode")

    flavor = kv.get("gcode_flavor", "")
    flavor_l = flavor.lower()
    if flavor_l:
        if flavor_l.startswith("marlin") or flavor_l.startswith("reprap"):
            firmware = "marlin"
        elif flavor_l == "klipper":
            firmware = "klipper"
        else:
            firmware = flavor_l
        fields["firmware"] = ParsedField(value=firmware, raw=flavor, key="gcode_flavor")
        fields["pa_gcode_style"] = ParsedField(
            value=("klipper" if firmware == "klipper" else "marlin"),
            raw=flavor, key="gcode_flavor")

    return RawConfig(fmt="prusa_ini", fields=fields, notes=notes, source_name=filename)


# ---------------------------------------------------------------------------
# klipper_cfg -- Klipper's printer.cfg (section/key INI-ish, with multi-line
# gcode_macro bodies)
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r'\[([^\]]+)\]\s*(.*)$')

# Klipper reads printer.cfg with
# configparser.RawConfigParser(inline_comment_prefixes=(';', '#')), so a
# comment may follow a value on the same line. Requiring whitespace before
# the marker matches configparser and leaves a '#' that is genuinely part of
# a value alone.
_INLINE_COMMENT_RE = re.compile(r'(?:^|\s)[#;]')


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing '# ...' / '; ...' comment from a config value."""
    m = _INLINE_COMMENT_RE.search(value)
    return (value[:m.start()] if m else value).strip()


def _section_header(line: str) -> str | None:
    """Return the section name if ``line`` is a section header, else None.

    Every place that needs to recognise a header goes through this, so the
    parser and the things that search the raw text cannot drift apart about
    what one looks like. They did: once headers were allowed to carry a
    trailing comment, the parser accepted "[printer]  # machine settings" but
    _klipper_name_from_comment still compared for an exact "[printer]", so the
    display name silently stopped being found on precisely the vendor configs
    that motivated the change.

    A commented-out header ("#[printer]") is not a header -- the regex is
    anchored at the start of the stripped line, so '#' fails to match.
    """
    m = _SECTION_RE.match(line.strip())
    if not m:
        return None
    trailing = m.group(2).strip()
    if trailing and trailing[0] not in ("#", ";"):
        return None
    return m.group(1).strip()


def _parse_klipper_sections(text: str) -> dict[str, dict[str, str]]:
    """Split printer.cfg into {section_name: {key: value}}.

    Continuation lines (indented under a "key:") are joined with "\\n" into
    the same value -- this is how a [gcode_macro FOO] body under "gcode:" is
    captured. Whole-line comments (# or ; as the FIRST non-space character of
    an unindented line) are dropped; comments that are part of an indented
    macro body are kept, since they are literal G-code text there, not config
    syntax.
    """
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    current_key: str | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indented = raw_line[:1] in (" ", "\t")
        if indented and current is not None and current_key is not None:
            sections[current][current_key] += "\n" + raw_line.strip()
            continue

        stripped = raw_line.strip()
        if stripped[0] in ("#", ";"):
            continue
        if stripped.startswith("["):
            # A section header may carry a trailing comment: vendor configs
            # (FLY, BTT, Voron kits) annotate almost every line, so
            # "[printer]   # printer settings" is the common shape, not the
            # exception. Requiring the line to END with ']' silently dropped
            # those headers, and every key under them was then attributed to
            # the previous section or discarded.
            name = _section_header(stripped)
            if name is not None:
                current = name
                sections.setdefault(current, {})
                current_key = None
                continue
        if current is None:
            continue

        idx_colon = stripped.find(":")
        idx_eq = stripped.find("=")
        candidates = [i for i in (idx_colon, idx_eq) if i != -1]
        if not candidates:
            continue
        idx = min(candidates)
        key = stripped[:idx].strip()
        val = _strip_inline_comment(stripped[idx + 1:])
        sections[current][key] = val
        current_key = key

    return sections


def _parse_pair(raw: str) -> tuple[float, float] | None:
    parts = [p for p in raw.replace(",", " ").split() if p]
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _klipper_name_from_comment(text: str) -> str | None:
    """A comment on the line immediately above [printer] is treated as the
    printer's display name (a common Klipper config convention).

    Only a WHOLE-line comment above the header counts. A comment on the header
    itself ("[printer]  # machine settings") is an annotation, not a name --
    harvesting it would label every vendor config "machine settings", which is
    worse than falling back to the filename.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        name = _section_header(line)
        if name is None or name.lower() != "printer":
            continue
        for j in range(i - 1, -1, -1):
            prev = lines[j].strip()
            if not prev:
                continue
            if prev[0] in ("#", ";"):
                return prev.lstrip("#;").strip()
            break
        break
    return None


def _find_macro(sections: dict[str, dict[str, str]], names: tuple[str, ...]):
    """Find a [gcode_macro NAME] section whose NAME is one of ``names``
    (case-insensitive). Returns (canonical_name, section_dict) or (None, None).
    """
    names_upper = {n.upper() for n in names}
    for key, body in sections.items():
        parts = key.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "gcode_macro" \
                and parts[1].strip().upper() in names_upper:
            return parts[1].strip().upper(), body
    return None, None


def _first_param(body: str, names: tuple[str, ...]) -> str | None:
    """First of ``names`` that appears as ``params.NAME`` in the macro body."""
    for n in names:
        if re.search(r'\bparams\.' + re.escape(n) + r'\b', body, re.IGNORECASE):
            return n
    return None


_PROBE_SECTION_HEADS = ("probe", "bltouch", "beacon", "cartographer", "smart_effector")


def _parse_klipper_cfg(text: str, filename: str) -> RawConfig:
    sections = _parse_klipper_sections(text)
    if not sections:
        raise PrinterImportError("klipper config has no recognisable [section] blocks")

    fields: dict[str, ParsedField] = {}
    notes: list[str] = []

    def put(field_name, value, raw, key):
        fields[field_name] = ParsedField(value=value, raw=raw, key=key)

    name = _klipper_name_from_comment(text)
    if not name and filename:
        name = os.path.splitext(os.path.basename(filename))[0]
    if name:
        put("name", name.strip(), name, "<comment above [printer], or filename>")

    put("firmware", "klipper", "klipper", "<constant>")
    put("pa_gcode_style", "klipper", "klipper", "<constant>")

    def sect(name_):
        return sections.get(name_, {})

    sx, sy, sz = sect("stepper_x"), sect("stepper_y"), sect("stepper_z")
    if "position_max" in sx:
        put("bed_size_x", sx["position_max"], sx["position_max"], "stepper_x.position_max")
    if "position_max" in sy:
        put("bed_size_y", sy["position_max"], sy["position_max"], "stepper_y.position_max")
    if "position_max" in sz:
        put("z_max", sz["position_max"], sz["position_max"], "stepper_z.position_max")
    if "position_min" in sz:
        put("z_min", sz["position_min"], sz["position_min"], "stepper_z.position_min")

    bm = sect("bed_mesh")
    if "mesh_min" in bm and "mesh_max" in bm:
        lo = _parse_pair(bm["mesh_min"])
        hi = _parse_pair(bm["mesh_max"])
        if lo and hi:
            put("print_min_x", lo[0], bm["mesh_min"], "bed_mesh.mesh_min")
            put("print_min_y", lo[1], bm["mesh_min"], "bed_mesh.mesh_min")
            put("print_max_x", hi[0], bm["mesh_max"], "bed_mesh.mesh_max")
            put("print_max_y", hi[1], bm["mesh_max"], "bed_mesh.mesh_max")

    pr = sect("printer")
    for cfg_key, field_name in (
        ("max_velocity", "max_velocity"),
        ("max_z_velocity", "max_z_velocity"),
        ("max_accel", "max_accel"),
        ("max_z_accel", "max_z_accel"),
    ):
        if cfg_key in pr:
            put(field_name, pr[cfg_key], pr[cfg_key], f"printer.{cfg_key}")

    ex = sect("extruder")
    for cfg_key, field_name in (
        ("nozzle_diameter", "nozzle_diameter"),
        ("filament_diameter", "filament_diameter"),
        ("max_temp", "max_nozzle_temp"),
        ("max_extrude_cross_section", "max_extrude_cross_section"),
    ):
        if cfg_key in ex:
            put(field_name, ex[cfg_key], ex[cfg_key], f"extruder.{cfg_key}")

    hb = sect("heater_bed")
    if "max_temp" in hb:
        put("max_bed_temp", hb["max_temp"], hb["max_temp"], "heater_bed.max_temp")

    probe_section_name = None
    for key in sections:
        head = key.split()[0].lower() if key.split() else key.lower()
        if head in _PROBE_SECTION_HEADS or "eddy_current" in head:
            probe_section_name = key
            break
    has_probe = probe_section_name is not None
    put("has_probe", has_probe, str(has_probe), "<derived from [probe]-like sections>")
    if probe_section_name:
        ps = sections[probe_section_name]
        if "x_offset" in ps:
            put("probe_dx", ps["x_offset"], ps["x_offset"], f"{probe_section_name}.x_offset")
        if "y_offset" in ps:
            put("probe_dy", ps["y_offset"], ps["y_offset"], f"{probe_section_name}.y_offset")

    start_key, start_sect = _find_macro(sections, ("PRINT_START", "START_PRINT"))
    if start_sect is not None:
        body = start_sect.get("gcode", "")
        nozzle_param = _first_param(
            body, ("EXTRUDER", "EXTRUDER_TEMP", "HOTEND", "HOTEND_TEMP", "TOOL_TEMP", "NOZZLE_TEMP"))
        bed_param = _first_param(body, ("BED", "BED_TEMP", "BEDTEMP"))
        material_param = _first_param(body, ("MATERIAL", "FILAMENT_TYPE", "FILAMENT"))

        if nozzle_param or bed_param:
            # The macro looks like a standard templated call -- synthesize
            # one rather than inlining the body (the body may reference
            # other macros/variables that only exist on the real printer).
            call_parts = [start_key]
            if nozzle_param:
                call_parts.append(f"{nozzle_param}={{nozzle_temp:.0f}}")
            if bed_param:
                call_parts.append(f"{bed_param}={{bed_temp:.0f}}")
            if material_param:
                call_parts.append(f"{material_param}={{material}}")
            call = " ".join(call_parts)
            start_gcode = (
                f"{call}\n"
                "M83            ; relative extrusion\n"
                "G92 E0\n"
                "M107           ; fan off for first layer(s)"
            )
            put("start_gcode", start_gcode, body, f"gcode_macro {start_key}.gcode")
            detected = [p for p in (nozzle_param, bed_param, material_param) if p]
            notes.append(
                f"start G-code was synthesized as a call to macro {start_key} "
                f"(params detected: {', '.join(detected) if detected else 'none'})")
        else:
            # No params.EXTRUDER/params.BED-style hook found -- there is no
            # safe way to inject our print temperatures into a parameterized
            # call, so the macro's own body is inlined verbatim instead (it
            # then goes through the same dangerous-command sanitation as any
            # other start G-code, rather than silently disappearing behind a
            # no-op "PRINT_START" call).
            put("start_gcode", body, body, f"gcode_macro {start_key}.gcode (inlined)")
            notes.append(
                f"start G-code inlined verbatim from macro {start_key} - no "
                f"params.EXTRUDER/params.BED-style hooks were found, so it "
                f"could not be turned into a parameterized call.")

    end_key, end_sect = _find_macro(sections, ("PRINT_END",))
    if end_sect is not None:
        put("end_gcode", end_key, end_key, f"gcode_macro {end_key}")

    if re.search(r'^\s*\[include\s', text, re.MULTILINE):
        notes.append(
            "config contains [include ...] lines; some limits may live in a "
            "file that was not uploaded here - review every value.")

    return RawConfig(fmt="klipper_cfg", fields=fields, notes=notes, source_name=filename)
