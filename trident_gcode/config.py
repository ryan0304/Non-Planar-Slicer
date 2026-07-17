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
    """Apply ``overrides`` dict to a PrinterProfile in-place.

    Unknown keys trigger a clear error message and exit(1).
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
        setattr(profile, k, type(getattr(profile, k))(v))


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
