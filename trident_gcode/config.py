"""Config file support for generate.py.

A JSON config file can supply defaults for any generate.py CLI flag (using the
same long-option name without leading dashes, with hyphens replaced by
underscores).  CLI flags explicitly given on the command line always win.

An optional "machine" object in the same JSON can override fields of
PrinterProfile (bed_size_x, max_z_velocity, etc.).

Example::

    {
        "shape": "star",
        "z_amp": 0.8,
        "height": 80,
        "machine": {
            "max_z_velocity": 20
        }
    }
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import fields as dc_fields


# Keys that are NOT CLI arg destinations — they live inside the JSON at top
# level but are handled separately (machine sub-object) or are meta-keys.
_NON_FLAG_KEYS = {"machine"}


def load_config(path: str) -> tuple[dict, dict]:
    """Load a config JSON file.

    Returns ``(flag_defaults, machine_overrides)`` where ``flag_defaults`` maps
    argparse dest names to values and ``machine_overrides`` maps PrinterProfile
    field names to values.  Unknown keys cause an error message + exit(1).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read config '{path}': {e}", file=sys.stderr)
        raise SystemExit(1)

    if not isinstance(data, dict):
        print(f"ERROR: config '{path}' must be a JSON object.", file=sys.stderr)
        raise SystemExit(1)

    # Separate flag keys from machine block and unknown keys.
    machine_raw = data.get("machine", {})
    flag_raw = {k: v for k, v in data.items() if k not in _NON_FLAG_KEYS}

    return flag_raw, machine_raw


def apply_machine_overrides(profile, overrides: dict):
    """Apply ``overrides`` dict to a PrinterProfile IN-PLACE.

    Unknown keys trigger a clear error message and exit(1).

    ``profile`` must already be the caller's OWN copy (generate.py builds one
    fresh with ``PrinterProfile(...)`` or ``dataclasses.replace(...)`` before
    ever calling this) -- this function has no way to tell a shared instance
    from a private one, so it is the caller's job to never pass
    trident_gcode.profile.TRIDENT or a PRINTER_PROFILES[...] entry directly.

    Two rounds of validation, for two different reasons:

    1. Per-field, here, BEFORE anything is written to ``profile``:
       ``json.load`` accepts the bare tokens NaN/Infinity, and
       ``type(getattr(profile, k))(v)`` used to hand those straight to
       ``float()`` (which happily returns a NaN/inf float) and hand
       ``bool("false")`` straight to ``bool()`` (which is True for ANY
       non-empty string) -- so a config could set a numeric field to a
       non-finite value or a bool field to the OPPOSITE of what its own text
       said. Every comparison against NaN is False, so a NaN limit would
       survive every downstream clamp silently (CLAUDE.md).
    2. Whole-profile, after every override is applied:
       printer_validate.validate_profile_dict runs the exact pipeline a
       browser-imported profile goes through, catching what no single field
       check can -- an in-range-but-unsafe COMBINATION, e.g. max_z_velocity
       left above max_velocity, or a z_amp_max above this profile's own
       probe clearance.
    """
    from trident_gcode.profile import PrinterProfile
    valid = {f.name for f in dc_fields(PrinterProfile)}
    unknown = [k for k in overrides if k not in valid]
    if unknown:
        print(
            f"ERROR: unknown machine key(s) in config: {', '.join(sorted(unknown))}\n"
            f"  Valid keys: {', '.join(sorted(valid))}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    for k, v in overrides.items():
        current = getattr(profile, k)
        target_type = type(current)
        if target_type is bool:
            if not isinstance(v, bool):
                print(
                    f"ERROR: machine key '{k}' must be a JSON boolean (true or "
                    f"false), got {v!r}. (bool('false') in Python is True for "
                    f"any non-empty string -- this is rejected outright rather "
                    f"than silently doing the opposite of what the config says.)",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            setattr(profile, k, v)
            continue
        if target_type in (int, float):
            try:
                num = float(v)
            except (TypeError, ValueError):
                print(f"ERROR: machine key '{k}' must be a number, got {v!r}.",
                      file=sys.stderr)
                raise SystemExit(1)
            if not math.isfinite(num):
                print(
                    f"ERROR: machine key '{k}' must be a finite number, got "
                    f"{v!r} -- NaN/Infinity would survive every downstream "
                    f"min()/max() clamp instead of tripping it.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            setattr(profile, k, target_type(num))
            continue
        # str (name, firmware, start_gcode, end_gcode, pa_gcode_style, ...):
        # unchanged coercion, nothing non-finite or boolean-truthy to guard.
        setattr(profile, k, target_type(v))

    from trident_gcode.printer_validate import validate_profile_dict
    vr = validate_profile_dict(asdict_profile(profile))
    if not vr.ok:
        errs = "; ".join(i.message for i in vr.issues if i.severity == "error")
        print(
            f"ERROR: machine overrides in config produced an unsafe profile: {errs}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # ``ok`` only means no ERROR-severity issue -- validate_profile_dict also
    # CLAMPS an in-range-per-field-but-still-unsafe value with a WARNING
    # instead of erroring (e.g. z_amp_max=50 clamps to 10, the module's own
    # absolute ceiling, rather than failing outright). The browser/CLI
    # printer-IMPORT path already treats a clamped-with-warning result as
    # "safe to use vr.profile" (see generate.py's --import handling,
    # printer_store.save_custom(key, vr.profile, ...)) -- mirrored here so a
    # config's "machine" block cannot leave an out-of-range number sitting on
    # `profile` just because it was a warning, not an error. Every field
    # round-trips byte-identically when nothing was actually out of range
    # (verified: TRIDENT through this same pipeline comes back unchanged,
    # including start_gcode/end_gcode), so this is a no-op for a clean config.
    #
    # But a silent clamp is exactly the "convenient over safe" failure
    # CLAUDE.md warns about: someone who typed 50 for z_amp_max must be told
    # they actually got 10, not left to discover it by measuring a print.
    # Printed BEFORE the clamp is applied to `profile` below, one line per
    # warning, so a config with several out-of-range fields shows every one.
    for i in vr.issues:
        if i.severity == "warn":
            print(f"WARNING: machine override {i.field}: {i.message}", file=sys.stderr)
    for f in dc_fields(PrinterProfile):
        setattr(profile, f.name, getattr(vr.profile, f.name))


def asdict_profile(profile) -> dict:
    """dataclasses.asdict(profile), pulled out to one place so both this
    module's re-validation and any future caller share the same conversion."""
    from dataclasses import asdict
    return asdict(profile)


def validate_flag_keys(flag_raw: dict, valid_dests: set[str], config_path: str) -> None:
    """Error and exit(1) if any flag_raw key is not a known argparse dest."""
    unknown = [k for k in flag_raw if k not in valid_dests]
    if unknown:
        print(
            f"ERROR: unknown key(s) in config '{config_path}': {', '.join(sorted(unknown))}\n"
            f"  Valid keys: {', '.join(sorted(valid_dests))}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def save_config(path: str, args, profile) -> None:
    """Write a pretty JSON config capturing all generation-relevant settings.

    ``args`` is the fully-resolved argparse Namespace.  ``profile`` is the
    PrinterProfile (so machine overrides round-trip correctly).
    """
    from trident_gcode.profile import PrinterProfile, TRIDENT
    default_profile = TRIDENT

    # Generation-relevant arg destinations (everything except --out, --format,
    # --config, --save-config which are purely I/O / meta).
    skip = {"out", "format", "config", "save_config"}

    data: dict = {}
    for k, v in vars(args).items():
        if k in skip:
            continue
        data[k] = v

    # Machine overrides: only fields that differ from the TRIDENT default.
    machine: dict = {}
    for f in dc_fields(PrinterProfile):
        val = getattr(profile, f.name)
        default_val = getattr(default_profile, f.name)
        if val != default_val:
            machine[f.name] = val
    if machine:
        data["machine"] = machine

    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except OSError as e:
        print(f"ERROR: cannot write config '{path}': {e}", file=sys.stderr)
        raise SystemExit(1)
