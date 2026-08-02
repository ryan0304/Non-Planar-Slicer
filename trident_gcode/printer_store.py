"""Persistence for custom (user-imported) printer profiles.

There are TWO independent stores here, and they deliberately do not share
state:

  * **Session store (in-memory)** -- what the browser uses. Profiles live in
    a per-session dict that dies with the server process. The browser owns
    the durable copy in its own localStorage and replays it on page load, so
    a user who clears their browser data re-imports the config file. See
    ``session_*`` below.
  * **Disk store (per-user directory)** -- what the ``generate.py`` CLI uses,
    written only by an explicit CLI save. See ``store_dir()``.

Both merge with the built-in PRINTER_PROFILES registry only at READ time --
the built-in dict itself is never mutated (principle 5 of the import spec:
custom profiles must never shadow or corrupt the built-ins).

Why the browser store is session-scoped rather than one shared dict: the
server is reachable by more than one browser, and a single shared store lets
one visitor see, select and delete another visitor's imported printers. For
a value that ends up feeding motion limits to a physical machine, silently
inheriting someone else's numbers is exactly the failure this whole module
is written to prevent.

Why the disk store is per-user rather than next to the code: a shipped app
is typically installed into a read-only location (Program Files,
/Applications, ...), and even where it isn't, a single shared directory
would let one user on a multi-user machine see (and silently inherit)
another user's imported printers.

Every read re-validates the stored profile with printer_validate, in both
stores. A hand-edited file or a replayed localStorage entry could otherwise
reach the generator with an unsafe value (e.g. someone pasting a huge
max_z_velocity straight into the JSON) -- skipping it is the conservative
choice, matching the "clamp or hard-error, never trust" rule everywhere else
in this feature.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import OrderedDict
from dataclasses import asdict

from .profile import PrinterProfile, PRINTER_PROFILES
from .printer_validate import validate_profile_dict

# Path-traversal guard: this is the ONLY shape of key ever allowed to reach a
# filesystem path in this module. Never interpolate an unvalidated key into a
# path -- every function below that touches disk re-checks this.
_KEY_RE = re.compile(r'^custom_[a-z0-9_]{1,48}$')


def _is_valid_key(key: str) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def _default_data_dir() -> str:
    """The per-user data directory for this platform. Pure function of
    sys.platform/the environment so tests can exercise every branch by
    monkeypatching ``sys.platform`` (this module's attribute) and the
    relevant env vars, without touching the real user profile."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            # Conservative fallback if APPDATA is somehow unset.
            base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "TridentGcode", "printers")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                             "Application Support", "TridentGcode", "printers")
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "trident-gcode", "printers")
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                         "trident-gcode", "printers")


def _env_override_dir() -> str | None:
    """TRIDENT_PRINTER_DIR, honoured only when it is an absolute path -- a
    relative value is ambiguous (relative to what? the cwd a server was
    launched from is not something a user should have to reason about for a
    setting that controls where their printer data lives), so it is treated
    as unset and the default is used instead."""
    override = os.environ.get("TRIDENT_PRINTER_DIR")
    if not override:
        return None
    if not os.path.isabs(override):
        print(f"WARNING: TRIDENT_PRINTER_DIR={override!r} is not an absolute "
              f"path; ignoring it and using the default printer directory.",
              file=sys.stderr)
        return None
    return override


# NOTE: there is deliberately no migration from the old pre-per-user location
# (``<repo_root>/custom_printers``). A silent copy-in on every process start
# meant a printer deleted through the UI came back the next time the server
# was restarted, because the delete only removed the copy in the data
# directory and the legacy directory re-seeded it. A delete must stay
# deleted; anyone still holding an old directory can re-import the config.


def store_dir() -> str:
    d = _env_override_dir() or _default_data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _make_key(name: str, existing: set) -> str:
    """Slugify ``name`` into a valid key not present in ``existing``."""
    slug = re.sub(r'[^a-z0-9]+', '_', (name or "").strip().lower()).strip('_') or "printer"
    n = 1
    while True:
        suffix = "" if n == 1 else f"_{n}"
        trimmed = slug[:max(1, 48 - len(suffix))].strip("_") or "printer"
        candidate = f"custom_{trimmed}{suffix}"
        if _is_valid_key(candidate) and candidate not in existing:
            return candidate
        n += 1


def make_key(name: str) -> str:
    """Slugify ``name`` into a valid, currently-unused DISK-store key."""
    return _make_key(name, set(list_custom().keys()))


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


# ---------------------------------------------------------------------------
# Session store: the browser-facing half. Nothing below this line touches
# disk.
#
# Lifetime is deliberately shorter than the disk store's: entries die with the
# server process, and the browser is the only holder of the durable copy (its
# own localStorage). That is the whole point -- "clear your browser data and
# the printer is gone, re-import the .cfg" is a lifetime a user can reason
# about, unlike a file in %APPDATA% they never knew was written.
#
# Both caps below exist because this store is filled from an untrusted client:
# a page that replayed an unbounded list would otherwise grow server memory
# without limit. Sessions evict least-recently-used; printers within a session
# are refused once full, so an attacker cannot push a user's real printer out
# of their own session by flooding it.
# ---------------------------------------------------------------------------
_SESSION_RE = re.compile(r'^[a-z0-9]{8,64}$')

MAX_SESSIONS = 32
MAX_PRINTERS_PER_SESSION = 32

_sessions: "OrderedDict[str, dict[str, dict]]" = OrderedDict()


def is_valid_session(sid) -> bool:
    """A session id is opaque to the server -- it is only ever a dict key, so
    the shape check exists to bound memory and keep logs ASCII, not to
    authenticate anything. It is NOT a secret and grants no authority beyond
    'these are the printers this browser replayed'."""
    return isinstance(sid, str) and bool(_SESSION_RE.match(sid))


def _session_bucket(sid: str, create: bool = False) -> dict[str, dict] | None:
    if not is_valid_session(sid):
        return None
    bucket = _sessions.get(sid)
    if bucket is None:
        if not create:
            return None
        while len(_sessions) >= MAX_SESSIONS:
            _sessions.popitem(last=False)
        bucket = {}
        _sessions[sid] = bucket
    _sessions.move_to_end(sid)
    return bucket


def session_list(sid) -> dict[str, dict]:
    """key -> {"profile": PrinterProfile, "meta": dict} for this session.
    An unknown or malformed session id is simply an empty set of custom
    printers -- never an error, and never another session's contents."""
    return dict(_session_bucket(sid) or {})


def session_make_key(sid, name: str) -> str:
    return _make_key(name, set((_session_bucket(sid) or {}).keys()))


def session_save(sid, key: str, profile: PrinterProfile, meta: dict) -> None:
    """Store ``profile``/``meta`` under ``key`` for this session. As with
    ``save_custom``, the caller must already have run the profile through
    printer_validate -- this enforces only key shape, built-in protection and
    the per-session cap."""
    if not is_valid_session(sid):
        raise ValueError("a valid session id is required to save a printer")
    if not _is_valid_key(key):
        raise ValueError(f"invalid custom printer key: {key!r}")
    if key in PRINTER_PROFILES:
        raise ValueError(f"'{key}' is a built-in printer key and cannot be overwritten")
    bucket = _session_bucket(sid, create=True)
    if key not in bucket and len(bucket) >= MAX_PRINTERS_PER_SESSION:
        raise ValueError(
            f"this session already holds the maximum of {MAX_PRINTERS_PER_SESSION} "
            f"custom printers; delete one before adding another")
    bucket[key] = {"profile": profile, "meta": dict(meta or {})}


def session_delete(sid, key: str) -> bool:
    bucket = _session_bucket(sid)
    if bucket is None or key not in bucket:
        return False
    del bucket[key]
    return True


def session_is_custom(sid, key: str) -> bool:
    return _is_valid_key(key) and key in (_session_bucket(sid) or {})


def session_all_profiles(sid) -> dict[str, PrinterProfile]:
    """{**PRINTER_PROFILES, **this session's custom} -- never mutates
    PRINTER_PROFILES, and never reads another session or the disk store."""
    merged: dict[str, PrinterProfile] = dict(PRINTER_PROFILES)
    for key, entry in session_list(sid).items():
        merged[key] = entry["profile"]
    return merged


def _reset_sessions() -> None:
    """Test hook: drop every session. Not used by the server."""
    _sessions.clear()
