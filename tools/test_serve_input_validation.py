#!/usr/bin/env python3
"""Tests for serve.py's server-side numeric input validation (SPEC A items 1-5).

Plain ``python tools/test_serve_input_validation.py``: prints PASS/FAIL per
case, exits non-zero on any failure. Mirrors tools/test_serve_limits.py's
style.

Background: none of these fields were validated server-side before -- only
the browser's own <input min/max> kept them sane, and CLAUDE.md is explicit
that a server clamp must be AT LEAST as strict as the UI's, because a raw
HTTP client bypasses the browser entirely. scratchpad/probe1.py (not part of
this repo) demonstrated the holes this file locks down: layer_height=0 threw
ZeroDivisionError -> HTTP 500, layer_height=0.01 hung the process for 20+
seconds, print_speed<=0 generated tens of thousands of F0/negative-F moves
with issues == [], and so on.

Each case below has a matching "teeth" assertion: a normal, in-range request
must still generate cleanly, so a clamp that is too aggressive fails just as
loudly as one that is missing.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _base_body(**extra):
    body = {"shape": "circle", "radius": 30, "height": 30, "layer_height": 0.3}
    body.update(extra)
    return body


def _rejects(fn, body, needle=None):
    try:
        fn(body)
        return False, "no exception raised"
    except ValueError as e:
        if needle and needle not in str(e):
            return False, f"wrong message: {e}"
        return True, str(e)
    except Exception as e:
        return False, f"wrong exception type {type(e).__name__}: {e}"


def _generates(fn, body):
    try:
        r = fn(body)
        return True, r
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 1. Numeric field validation, generate_design (parametric).
# ---------------------------------------------------------------------------
def test_layer_height():
    for bad in (0, -0.3):
        ok, detail = _rejects(serve.generate_design, _base_body(layer_height=bad),
                              "layer_height")
        check(ok, f"layer_height={bad} rejected", detail)

    # Out-of-range but positive -> clamped, not rejected, and fast (the
    # original hole: 0.01 ran 500k+ lines and 20+ seconds).
    import time
    t0 = time.time()
    ok, r = _generates(serve.generate_design, _base_body(layer_height=0.01, height=20))
    dt = time.time() - t0
    check(ok, "layer_height=0.01 clamped up, not rejected", r if not ok else "")
    check(dt < 5.0, "layer_height=0.01 clamped FAST (no 500k-line build)",
          f"{dt:.1f}s")

    ok, r = _generates(serve.generate_design, _base_body(layer_height=3))
    check(ok, "layer_height=3 clamped down, not rejected", r if not ok else "")

    # Teeth: an ordinary in-range value still generates.
    ok, r = _generates(serve.generate_design, _base_body(layer_height=0.3))
    check(ok, "layer_height=0.3 (in range) generates cleanly", r if not ok else "")


def test_print_speed():
    for bad in (-40, 0):
        ok, detail = _rejects(serve.generate_design, _base_body(print_speed=bad),
                              "print_speed")
        check(ok, f"print_speed={bad} rejected", detail)
    ok, r = _generates(serve.generate_design, _base_body(print_speed=40))
    check(ok, "print_speed=40 (in range) generates cleanly", r if not ok else "")
    # Clamped, not rejected, for an absurdly high in-range-sign value.
    ok, r = _generates(serve.generate_design, _base_body(print_speed=99999))
    check(ok, "print_speed=99999 clamped down, not rejected", r if not ok else "")
    if ok:
        check("F0" not in r["gcode"] and " F-" not in r["gcode"],
              "clamped print_speed never emits a bad F token")


def test_line_width():
    for bad in (-0.5, 0):
        ok, detail = _rejects(serve.generate_design, _base_body(line_width=bad),
                              "line_width")
        check(ok, f"line_width={bad} rejected", detail)
    ok, r = _generates(serve.generate_design, _base_body(line_width=5))
    check(ok, "line_width=5 clamped down, not rejected", r if not ok else "")
    ok, r = _generates(serve.generate_design, _base_body())  # line_width omitted -> auto
    check(ok, "line_width omitted (auto) still generates cleanly", r if not ok else "")


def test_radius_and_height():
    for bad in (-20, 0):
        ok, detail = _rejects(serve.generate_design, _base_body(radius=bad), "radius")
        check(ok, f"radius={bad} rejected", detail)
    for bad in (-10, 0):
        ok, detail = _rejects(serve.generate_design, _base_body(height=bad), "height")
        check(ok, f"height={bad} rejected", detail)
    # No UI ceiling on radius/height -- the safe-area / z_max checks are the
    # real limit, and they must still fire for something genuinely too big
    # (this is why radius/height have NO upper clamp in _num()).
    ok, detail = _rejects(serve.generate_design, _base_body(radius=300),
                          "safe print area")
    check(ok, "radius=300 still refused by the FOOTPRINT check (no upper "
              "clamp swallowed it)", detail)


def test_base_brim_skirt_clamped():
    ok, r = _generates(serve.generate_design,
                       _base_body(base_layers=500, height=20))
    check(ok, "base_layers=500 clamped, not rejected", r if not ok else "")
    ok, r = _generates(serve.generate_design, _base_body(brim=500))
    check(ok, "brim=500 clamped, not rejected", r if not ok else "")
    ok, r = _generates(serve.generate_design, _base_body(skirt=500))
    check(ok, "skirt=500 clamped, not rejected", r if not ok else "")


def test_z_and_pattern_fields_clamped():
    ok, r = _generates(serve.generate_design,
                       _base_body(z_waves=-4, amp_profile=[[0, 0.5], [1, 0.5]]))
    check(ok, "z_waves=-4 clamped up, not rejected", r if not ok else "")
    ok, r = _generates(serve.generate_design,
                       _base_body(pattern="vwave", pattern_waves=100000, height=15))
    check(ok, "pattern_waves=100000 clamped, not rejected", r if not ok else "")
    ok, r = _generates(serve.generate_design, _base_body(pattern_bands=-9))
    check(ok, "pattern_bands=-9 clamped, not rejected", r if not ok else "")
    ok, r = _generates(serve.generate_design, _base_body(xy_twist=-99, z_twist=99))
    check(ok, "xy_twist/z_twist extreme values clamped, not rejected",
          r if not ok else "")


def test_first_layer_height_negative_is_absent_not_error():
    ok, r = _generates(serve.generate_design, _base_body(first_layer_height=-1))
    check(ok, "first_layer_height=-1 is treated as absent, not rejected",
          r if not ok else "")


# ---------------------------------------------------------------------------
# 2. Defense in depth: GcodeWriter._move rejects F <= 0 directly.
# ---------------------------------------------------------------------------
def test_gcodewriter_rejects_nonpositive_feedrate():
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.profile import TRIDENT

    w = GcodeWriter(profile=TRIDENT, line_width=0.45, layer_height=0.3,
                    print_speed=40.0)
    w._x, w._y, w._z, w._has_position = 100.0, 100.0, 0.3, True
    try:
        w._move(105.0, 100.0, 0.3, e=1.0, speed=0.0, comment=None)
        check(False, "GcodeWriter._move rejects speed=0 (F0)",
              "no exception raised")
    except ValueError as e:
        check("greater than 0" in str(e), "GcodeWriter._move rejects speed=0 (F0)",
              str(e))
    try:
        w._move(110.0, 100.0, 0.3, e=1.0, speed=-40.0, comment=None)
        check(False, "GcodeWriter._move rejects a negative speed",
              "no exception raised")
    except ValueError as e:
        check("greater than 0" in str(e), "GcodeWriter._move rejects a negative speed",
              str(e))
    # Teeth: a normal positive speed still emits.
    w._move(115.0, 100.0, 0.3, e=1.0, speed=40.0, comment=None)
    check(any(" F2400" in ln for ln in w._lines),
          "a normal positive speed still emits an ordinary F token",
          w._lines[-3:])


def test_analyzer_flags_nonpositive_feedrate():
    """analyze_gcode must report F<=0 moves as an issue, classified DANGER."""
    import tempfile, os
    from trident_gcode.analyze import analyze_gcode
    from trident_gcode.profile import TRIDENT

    gcode = (
        "G90\n"
        "G1 X10 Y10 Z0.3 F1200\n"
        "G1 X20 Y10 Z0.3 E1.0 F0\n"       # bad: F0
        "G1 X30 Y10 Z0.3 E2.0 F-600\n"    # bad: negative F
        "G1 X40 Y10 Z0.3 E3.0 F1200\n"
    )
    fd, path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(gcode)
        a = analyze_gcode(path, TRIDENT)
    finally:
        os.remove(path)

    check(a.nonpositive_feedrate_moves == 2,
          "analyze_gcode counts exactly the 2 bad-F moves",
          a.nonpositive_feedrate_moves)
    check(any("F <= 0" in i for i in a.issues),
          "analyze_gcode's issues include the F<=0 finding", a.issues)
    for msg in a.issues:
        if "F <= 0" in msg:
            sev, ctrl = serve._classify_issue(msg, True)
            check(sev == serve._SEVERITY_DANGER,
                  "the F<=0 finding classifies as DANGER, not warn/note", sev)


def test_analyzer_ignores_moves_before_the_first_f_word():
    """curF starts at 0.0 before any F word has been parsed -- that is "no F
    word yet" (normal in external G-code, which may rely on the firmware's
    own default feedrate for early travel moves), NOT a commanded F0. Only an
    F word the file itself wrote may count as a nonpositive-feedrate finding.

    Repro that motivated this (QC finding against the first version of this
    check, which had no f_seen guard and flagged every pre-F move as DANGER):
    M83 / G1 Z5 / G1 X100 Y100 / G1 X110 Y100 E1 F1200
    """
    import tempfile, os
    from trident_gcode.analyze import analyze_gcode
    from trident_gcode.profile import TRIDENT

    gcode = (
        "M83\n"
        "G1 Z5\n"
        "G1 X100 Y100\n"
        "G1 X110 Y100 E1 F1200\n"
    )
    fd, path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(gcode)
        a = analyze_gcode(path, TRIDENT)
    finally:
        os.remove(path)

    check(a.nonpositive_feedrate_moves == 0,
          "moves before the first F word are NOT counted as nonpositive "
          "feedrate", a.nonpositive_feedrate_moves)
    check(not any("F <= 0" in i for i in a.issues),
          "...and no F<=0 issue is raised for them", a.issues)

    # And once F has been set (even to something bad), it is sticky: a LATER
    # move that omits F inherits the last commanded value, including if that
    # value was <= 0.
    gcode2 = (
        "M83\n"
        "G1 X10 Y10 F0\n"          # bad: explicit F0
        "G1 X20 Y10 E1\n"          # no F token, but F is STILL 0 (sticky/modal)
    )
    fd, path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(gcode2)
        a2 = analyze_gcode(path, TRIDENT)
    finally:
        os.remove(path)
    # Only 1, not 2: the very FIRST G1 in a file establishes the toolhead's
    # initial position rather than moving FROM one (analyze_gcode's own
    # `have` flag gates a.moves the same way), so only the SECOND line here
    # is counted as a move at all -- and it inherits F0 from the first line,
    # sticky/modal, with no F token of its own.
    check(a2.nonpositive_feedrate_moves == 1,
          "once F has been explicitly set, it stays sticky across a later "
          "move that omits an F token, including a bad one",
          a2.nonpositive_feedrate_moves)


# ---------------------------------------------------------------------------
# 3. /api/mesh_profile query-string layer_height boundary.
# ---------------------------------------------------------------------------
class _FakeMeshProfileHandler:
    """Minimal stand-in for the bits _handle_mesh_profile touches."""
    def __init__(self, query: str):
        self.path = "/api/mesh_profile?" + query
        self.headers = {}
        self.sent = None
        self.status = None

    def _send_json(self, obj, status=200):
        self.sent = obj
        self.status = status


def test_mesh_profile_layer_height_boundary():
    for bad in ("nan", "inf", "-inf", "0", "-0.3"):
        h = _FakeMeshProfileHandler("mesh_id=nope&layer_height=%s" % bad)
        serve.Handler._handle_mesh_profile(h)
        check(h.status == 400,
              f"mesh_profile layer_height={bad!r} rejected with 400",
              f"status={h.status} sent={h.sent}")

    # A tiny-but-positive value must be CLAMPED (not hang, not crash) --
    # reaches the mesh lookup (which 404s on a fake mesh id, proving the
    # layer_height boundary itself did not reject or hang).
    h = _FakeMeshProfileHandler("mesh_id=nope&layer_height=1e-9")
    serve.Handler._handle_mesh_profile(h)
    check(h.status == 404,
          "mesh_profile layer_height=1e-9 clamped, not rejected/hung -- "
          "request proceeds to the (fake) mesh lookup",
          f"status={h.status} sent={h.sent}")


# ---------------------------------------------------------------------------
# 4. _read_json_body: a non-dict JSON body must be a 400, not an AttributeError.
# ---------------------------------------------------------------------------
class _FakeBodyHandler:
    def __init__(self, body: bytes):
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.sent = None
        self.status = None
        self.close_connection = False

    def _send_json(self, obj, status=200):
        self.sent = obj
        self.status = status


def test_non_dict_json_body_is_400_not_attributeerror():
    for raw, label in (
        (b"[1, 2, 3]", "a JSON array"),
        (b'"just a string"', "a JSON string"),
        (b"42", "a bare number"),
        (b"true", "a bare bool"),
        (b"null", "a bare null"),
    ):
        h = _FakeBodyHandler(raw)
        got = serve._read_json_body(h)
        check(got is None, f"{label} body returns the None sentinel",
              repr(got))
        check(h.status == 400, f"{label} body is rejected with 400",
              f"status={h.status}")
        check(isinstance(h.sent, dict) and "JSON object" in h.sent.get("error", ""),
              f"{label} body's error names the real problem", h.sent)

    # Teeth: an ordinary object body is untouched by this change.
    h = _FakeBodyHandler(b'{"shape": "circle"}')
    got = serve._read_json_body(h)
    check(isinstance(got, dict) and got.get("shape") == "circle",
          "an ordinary JSON object body still parses normally",
          f"status={h.status} got={got}")


# ---------------------------------------------------------------------------
# 5. _handle_printer_session: a malformed-SHAPE stored key must be re-minted,
#    not rejected outright.
# ---------------------------------------------------------------------------
def test_malformed_key_shape_is_reminted_not_rejected():
    from trident_gcode import printer_store

    check(printer_store.is_valid_key("custom_my_printer") is True,
          "is_valid_key: a well-shaped key is valid")
    check(printer_store.is_valid_key("My Printer") is False,
          "is_valid_key: a hand-edited, wrongly-shaped key is invalid")
    check(printer_store.is_valid_key("") is False,
          "is_valid_key: an empty key is invalid")

    # The actual condition serve.py's _handle_printer_session now uses --
    # exercised directly against the fixed expression rather than a full
    # HTTP round trip (the surrounding handler is otherwise untouched).
    def _would_reuse(key):
        return bool(key and key not in serve.PRINTER_PROFILES
                    and printer_store.is_valid_key(key))

    check(_would_reuse("custom_my_printer") is True,
          "a valid, non-built-in key is reused as-is")
    check(_would_reuse("My Printer") is False,
          "a wrongly-shaped key is NOT reused (gets re-minted instead), "
          "closing the hole where it reached session_save() and the whole "
          "printer was rejected")
    check(_would_reuse("") is False, "an empty key is not reused")
    check(_would_reuse("trident") is False,
          "a built-in key is never reused for a custom printer")

    # End-to-end: a printer whose ONLY problem is a bad key shape must be
    # RESTORED (re-keyed), not show up in "rejected".
    import serve as _serve
    sid = "abcdefgh12345678"
    profile_dict = {
        "name": "Bench Printer", "firmware": "klipper",
        "bed_size_x": 235, "bed_size_y": 235, "z_max": 160, "z_min": -8,
        "max_velocity": 400, "max_z_velocity": 25,
        "max_accel": 8000, "max_z_accel": 500,
        "nozzle_diameter": 0.4, "filament_diameter": 1.75,
    }
    body = {"printers": [{"key": "My Printer", "profile": profile_dict}]}

    class _Sess:
        headers = {_serve._SESSION_HEADER: sid}
        def __init__(self):
            self.sent = None
            self.status = None
        def _send_json(self, obj, status=200):
            self.sent, self.status = obj, status

    h = _Sess()
    orig_read = _serve._read_json_body
    _serve._read_json_body = lambda handler: body
    try:
        _serve.Handler._handle_printer_session(h)
    finally:
        _serve._read_json_body = orig_read
        printer_store._reset_sessions()

    check(h.status is None or h.sent.get("ok") is True,
          "printer_session call succeeded", h.sent)
    check(len(h.sent.get("restored", [])) == 1,
          "the malformed-key printer was RESTORED (re-keyed), not rejected",
          h.sent)
    check(len(h.sent.get("rejected", [])) == 0,
          "nothing was rejected for a key-shape problem alone", h.sent)
    if h.sent.get("restored"):
        new_key = h.sent["restored"][0]["key"]
        check(printer_store.is_valid_key(new_key),
              "the re-minted key has the valid custom_ shape", new_key)


def main() -> int:
    test_layer_height()
    test_print_speed()
    test_line_width()
    test_radius_and_height()
    test_base_brim_skirt_clamped()
    test_z_and_pattern_fields_clamped()
    test_first_layer_height_negative_is_absent_not_error()
    test_gcodewriter_rejects_nonpositive_feedrate()
    test_analyzer_flags_nonpositive_feedrate()
    test_analyzer_ignores_moves_before_the_first_f_word()
    test_mesh_profile_layer_height_boundary()
    test_non_dict_json_body_is_400_not_attributeerror()
    test_malformed_key_shape_is_reminted_not_rejected()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
