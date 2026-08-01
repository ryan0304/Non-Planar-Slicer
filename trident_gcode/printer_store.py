"""On-disk persistence for custom (user-imported) printer profiles.

Custom printers live in ``<repo_root>/custom_printers/<key>.json`` and are
merged with the built-in PRINTER_PROFILES registry only at READ time, via
``all_profiles()`` -- the built-in dict itself is never mutated (principle 5
of the import spec: custom profiles must never shadow or corrupt the
built-ins).

Every read re-validates the stored profile with printer_validate. A hand
-edited or corrupted file could otherwise reach the generator with an unsafe
value (e.g. someone pasting a huge max_z_velocity straight into the JSON) --
skipping it with a stderr warning is the conservative choice, matching the
"clamp or hard-error, never trust" rule everywhere else in this feature.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict

from .profile import PrinterProfile, PRINTER_PROFILES
from .printer_validate import validate_profile_dict

# Path-traversal guard: this is the ONLY shape of key ever allowed to reach a
# filesystem path in this module. Never interpolate an unvalidated key into a
# path -- every function below that touches disk re-checks this.
_KEY_RE = re.compile(r'^custom_[a-z0-9_]{1,48}$')


def _is_valid_key(key: str) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def _repo_root() -> str:
    # trident_gcode/printer_store.py -> repo root is one directory up.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def store_dir() -> str:
    d = os.path.join(_repo_root(), "custom_printers")
    os.makedirs(d, exist_ok=True)
    return d


def make_key(name: str) -> str:
    """Slugify ``name`` into a valid, currently-unused custom printer key."""
    slug = re.sub(r'[^a-z0-9]+', '_', (name or "").strip().lower()).strip('_') or "printer"
    existing = set(list_custom().keys())
    n = 1
    while True:
        suffix = "" if n == 1 else f"_{n}"
        trimmed = slug[:max(1, 48 - len(suffix))].strip("_") or "printer"
        candidate = f"custom_{trimmed}{suffix}"
        if _is_valid_key(candidate) and candidate not in existing:
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Cache: keyed on the store directory's mtime. Directory mtime changes when a
# file is created or removed (every save/delete does one of those via
# os.replace/os.remove), so this is enough to detect external changes made
# via the CLI or by hand -- explicit invalidation on save/delete on top of it
# just removes any dependency on filesystem mtime-resolution timing.
# ---------------------------------------------------------------------------
_cache: dict[str, dict] | None = None
_cache_mtime: float | None = None


def _invalidate_cache() -> None:
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = None


def list_custom() -> dict[str, dict]:
    """key -> {"profile": PrinterProfile, "meta": dict} for every valid,
    currently-passing custom printer file. Corrupt/invalid files are skipped
    with a stderr warning, never raised."""
    global _cache, _cache_mtime
    d = store_dir()
    try:
        mtime = os.stat(d).st_mtime
    except OSError:
        mtime = 0.0

    if _cache is not None and _cache_mtime == mtime:
        return _cache

    result: dict[str, dict] = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []

    for fn in names:
        if not fn.endswith(".json"):
            continue
        key = fn[:-5]
        if not _is_valid_key(key):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"WARNING: custom printer file '{fn}' could not be read ({e}); skipped.",
                  file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"WARNING: custom printer file '{fn}' is not a JSON object; skipped.",
                  file=sys.stderr)
            continue
        profile_raw = data.get("profile")
        if not isinstance(profile_raw, dict):
            print(f"WARNING: custom printer file '{fn}' has no 'profile' object; skipped.",
                  file=sys.stderr)
            continue

        vr = validate_profile_dict(profile_raw)
        if not vr.ok:
            errs = "; ".join(i.message for i in vr.issues if i.severity == "error")
            print(f"WARNING: custom printer '{key}' failed validation and was skipped: {errs}",
                  file=sys.stderr)
            continue

        meta = data.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        result[key] = {"profile": vr.profile, "meta": meta}

    _cache = result
    _cache_mtime = mtime
    return result


def load_custom(key: str) -> tuple[PrinterProfile, dict] | None:
    if not _is_valid_key(key):
        return None
    entry = list_custom().get(key)
    if entry is None:
        return None
    return entry["profile"], entry["meta"]


def save_custom(key: str, profile: PrinterProfile, meta: dict) -> None:
    """Persist ``profile``/``meta`` under ``key``. Caller must have already
    run this profile through printer_validate -- this function does not
    re-validate (the API layer is where that happens, right before calling
    this), it only enforces the key shape and writes atomically."""
    if not _is_valid_key(key):
        raise ValueError(f"invalid custom printer key: {key!r}")
    if key in PRINTER_PROFILES:
        raise ValueError(f"'{key}' is a built-in printer key and cannot be overwritten")

    path = os.path.join(store_dir(), f"{key}.json")
    data = {
        "schema": "trident-printer/1",
        "key": key,
        "profile": asdict(profile),
        "meta": dict(meta or {}),
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)
    _invalidate_cache()


def delete_custom(key: str) -> bool:
    if not _is_valid_key(key):
        return False
    path = os.path.join(store_dir(), f"{key}.json")
    if not os.path.isfile(path):
        return False
    os.remove(path)
    _invalidate_cache()
    return True


def is_custom(key: str) -> bool:
    return _is_valid_key(key) and key in list_custom()


def all_profiles() -> dict[str, PrinterProfile]:
    """{**PRINTER_PROFILES, **custom} -- never mutates PRINTER_PROFILES itself."""
    merged: dict[str, PrinterProfile] = dict(PRINTER_PROFILES)
    for key, entry in list_custom().items():
        merged[key] = entry["profile"]
    return merged
