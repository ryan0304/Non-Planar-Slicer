"""Import filament settings from OrcaSlicer profiles.

OrcaSlicer stores each filament profile as JSON where every value is a one-element
list of strings, and profiles inherit from parents via an ``"inherits"`` key
(often several levels deep, ending in built-in bases like ``fdm_filament_pet``).
This module locates the Orca config, resolves the full inheritance chain, and
flattens the result into a typed :class:`FilamentSettings` that maps cleanly onto
the generator's :class:`~trident_gcode.gcode.GcodeWriter`.

The single most useful field for thick non-planar prints is
``filament_max_volumetric_speed`` (mm^3/s): the generator uses it to cap print
speed so we never ask the hotend to melt more plastic than it can.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from functools import lru_cache


# ---------------------------------------------------------------- locating ---
def orca_config_root() -> str | None:
    """Best-effort path to the OrcaSlicer config directory for this OS."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "OrcaSlicer"))
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".config", "OrcaSlicer"),                 # Linux
        os.path.join(home, "Library", "Application Support", "OrcaSlicer"),  # macOS
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def orca_binary_path(explicit: str | None = None) -> str | None:
    """Best-effort path to the OrcaSlicer CLI binary for this OS.

    Resolution order: *explicit* (if given and a real file) -> the
    TRIDENT_ORCA_PATH environment variable (if set and a real file) ->
    PATH lookup for either binary name Orca has shipped as -> OS-
    conventional install locations. Never raises -- returns None on total
    failure so callers can give one clear, actionable error message rather
    than a stack trace.

    Deliberately never reads this from a browser request: serve.py can be
    bound to 0.0.0.0 for a hosted deployment, and letting a client pick
    which local binary the server subprocess-execs would be a path-
    traversal/RCE-adjacent vector. This function is for server-side/CLI
    resolution only.
    """
    if explicit and os.path.isfile(explicit):
        return explicit

    env_path = os.environ.get("TRIDENT_ORCA_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    for name in ("orca-slicer", "orcaslicer"):
        found = shutil.which(name)
        if found:
            return found

    candidates = []
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        candidates.append(os.path.join(program_files, "OrcaSlicer", "orca-slicer.exe"))
    candidates.append("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".local", "bin", "orca-slicer"),
        "/usr/bin/orca-slicer",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _filament_files(root: str) -> list[str]:
    out = []
    for base in (os.path.join(root, "user"), os.path.join(root, "system")):
        for dirpath, _dirs, files in os.walk(base):
            if os.sep + "filament" not in os.sep + dirpath:
                continue
            for fn in files:
                if fn.endswith(".json"):
                    out.append(os.path.join(dirpath, fn))
    return out


@lru_cache(maxsize=1)
def _profile_index() -> dict[str, str]:
    """Map every profile's ``name`` (and filename stem) -> file path."""
    root = orca_config_root()
    index: dict[str, str] = {}
    if not root:
        return index
    for path in _filament_files(root):
        stem = os.path.splitext(os.path.basename(path))[0]
        index.setdefault(stem, path)
        try:
            with open(path, encoding="utf-8") as fh:
                name = json.load(fh).get("name")
            if isinstance(name, str):
                index.setdefault(name, path)
        except (json.JSONDecodeError, OSError):
            continue
    return index


def list_filaments() -> list[str]:
    """User-facing filament names, with built-in `fdm_*` bases hidden."""
    names = [n for n in _profile_index() if not n.startswith("fdm_")]
    return sorted(set(names))


# ------------------------------------------------------------- value parsing -
def _scalar(v):
    """Orca values are usually ``["123"]``; pull out a single clean value."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, str):
        s = v.strip()
        if s in ("", "nil"):
            return None
        if s.endswith("%"):
            try:
                return float(s[:-1]) / 100.0
            except ValueError:
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


# ------------------------------------------------------- inheritance resolve -
def _resolve_raw(name: str, _seen: frozenset[str] = frozenset()) -> dict:
    """Return the fully-merged raw JSON dict for ``name`` (child overrides parent)."""
    index = _profile_index()
    path = index.get(name)
    if not path or name in _seen:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    parent_name = data.get("inherits")
    if isinstance(parent_name, list):
        parent_name = parent_name[0] if parent_name else None
    merged: dict = {}
    if parent_name:
        merged.update(_resolve_raw(parent_name, _seen | {name}))
    merged.update(data)
    return merged


# ------------------------------------------------------------- public model -
@dataclass
class FilamentSettings:
    name: str = "Unknown"
    filament_type: str = "PLA"
    vendor: str | None = None

    nozzle_temp: float | None = None
    nozzle_temp_first_layer: float | None = None
    bed_temp: float | None = None
    bed_temp_first_layer: float | None = None

    flow_ratio: float | None = None
    max_volumetric_speed: float | None = None   # mm^3/s
    retraction_length: float | None = None
    z_hop: float | None = None

    fan_min_speed: float | None = None           # 0..1
    fan_max_speed: float | None = None           # 0..1
    pressure_advance: float | None = None
    exhaust_during_print: float | None = None     # 0..1
    exhaust_after_print: float | None = None      # 0..1

    raw: dict = field(default_factory=dict, repr=False)

    # --- construction -----------------------------------------------------
    @classmethod
    def from_orca(cls, name: str) -> "FilamentSettings":
        raw = _resolve_raw(name)
        if not raw:
            raise KeyError(
                f"Filament profile '{name}' not found in OrcaSlicer config. "
                f"Run `python -m trident_gcode.orca --list` to see options."
            )
        def first(*keys):
            for k in keys:
                v = _num(raw.get(k))
                if v is not None:
                    return v
            return None

        def pct(key):
            """Orca fan/exhaust speeds are 0..100 ints; return as 0..1."""
            v = _num(raw.get(key))
            return v / 100.0 if v is not None else None

        return cls(
            name=_scalar(raw.get("name")) or name,
            filament_type=_scalar(raw.get("filament_type")) or "PLA",
            vendor=_scalar(raw.get("filament_vendor")),
            nozzle_temp=first("nozzle_temperature"),
            nozzle_temp_first_layer=first("nozzle_temperature_initial_layer"),
            bed_temp=first("hot_plate_temp", "bed_temperature"),
            bed_temp_first_layer=first("hot_plate_temp_initial_layer", "bed_temperature_initial_layer"),
            flow_ratio=first("filament_flow_ratio"),
            max_volumetric_speed=first("filament_max_volumetric_speed"),
            retraction_length=first("filament_retraction_length"),
            z_hop=first("filament_z_hop"),
            fan_min_speed=pct("fan_min_speed"),
            fan_max_speed=pct("fan_max_speed"),
            pressure_advance=first("pressure_advance"),
            exhaust_during_print=pct("during_print_exhaust_fan_speed"),
            exhaust_after_print=pct("complete_print_exhaust_fan_speed"),
            raw=raw,
        )

    # --- mapping onto the generator --------------------------------------
    def writer_kwargs(self) -> dict:
        """Subset suitable for ``GcodeWriter(**profile.writer_kwargs())``."""
        kw: dict = {"material": (self.filament_type or "PLA").upper()}
        if self.nozzle_temp is not None:
            kw["nozzle_temp"] = self.nozzle_temp
        if self.bed_temp is not None:
            kw["bed_temp"] = self.bed_temp
        if self.flow_ratio is not None:
            kw["flow_multiplier"] = self.flow_ratio
        if self.max_volumetric_speed:
            kw["max_volumetric_speed"] = self.max_volumetric_speed
        if self.retraction_length is not None:
            kw["retraction_length"] = self.retraction_length
        if self.pressure_advance is not None:
            kw["pressure_advance"] = self.pressure_advance
        if self.fan_max_speed is not None:
            kw["fan_speed"] = self.fan_max_speed
        # Note: z_hop is intentionally NOT passed — Orca's z-hop is for travels;
        # we have no mid-print travels, so we leave the writer default (0.0).
        return kw

    def summary(self) -> str:
        def f(v, unit=""):
            return f"{v:g}{unit}" if v is not None else "-"
        return (
            f"{self.name}  ({self.filament_type}"
            f"{', ' + self.vendor if self.vendor else ''})\n"
            f"  nozzle      : {f(self.nozzle_temp,'C')} "
            f"(first {f(self.nozzle_temp_first_layer,'C')})\n"
            f"  bed         : {f(self.bed_temp,'C')} "
            f"(first {f(self.bed_temp_first_layer,'C')})\n"
            f"  flow ratio  : {f(self.flow_ratio)}\n"
            f"  max vol.    : {f(self.max_volumetric_speed,' mm^3/s')}\n"
            f"  retraction  : {f(self.retraction_length,' mm')}  z-hop {f(self.z_hop,' mm')}\n"
            f"  fan         : {f(self.fan_min_speed)}..{f(self.fan_max_speed)}  "
            f"PA {f(self.pressure_advance)}"
        )


# ------------------------------------------------------------------- CLI ----
def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Inspect OrcaSlicer filament profiles")
    ap.add_argument("--list", action="store_true", help="list available filament profiles")
    ap.add_argument("name", nargs="?", help="profile name to show resolved settings for")
    args = ap.parse_args(argv)

    root = orca_config_root()
    if not root:
        print("OrcaSlicer config directory not found.")
        return 1
    print(f"Orca config: {root}\n")

    if args.list or not args.name:
        for n in list_filaments():
            print(f"  {n}")
        if not args.name:
            print("\nShow one with:  python -m trident_gcode.orca \"<name>\"")
        return 0

    try:
        fs = FilamentSettings.from_orca(args.name)
    except KeyError as e:
        print(e)
        return 1
    print(fs.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
