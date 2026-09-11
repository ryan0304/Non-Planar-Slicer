#!/usr/bin/env python3
"""Tests for serve.py's issue severity classification (_ISSUE_RULES,
_classify_issue, _issues_detail).

Background: the generate response's "issues" array is a flat list of plain
strings, and the viewer used to render nothing but a COUNT of them plus an
undifferentiated text blob -- so a probe-strike risk and a cosmetic "this
setting was ignored" note looked identical. TASTES.md requires the opposite:
"Give risk/caution copy its own visually distinct treatment, not the muted
style of ordinary help text."

Severity now travels in a parallel "issues_detail" array. The flat "issues"
array is deliberately UNCHANGED (still strings) because
tools/test_report_extra_issues.py, tools/test_printer_import.py's said()
helper and any external caller all compare it as strings.

THE POINT OF THIS FILE
----------------------
_classify_issue matches on substrings of messages this codebase itself emits.
That is safe only for as long as a reword cannot silently reclassify a
message -- which is precisely the silent-downgrade class CLAUDE.md warns
about. So test_every_analyze_message_is_explicitly_classified below drives
every message trident_gcode/analyze.py's _evaluate() can produce through the
rule table and asserts an EXPLICIT rule matched, never the fallback. Reword a
message in analyze.py and this test fails loudly.

Plain ``python tools/test_issue_severity.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors tools/test_report_extra_issues.py's
style.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve
from trident_gcode.analyze import GcodeAnalysis, _evaluate
from trident_gcode.profile import PrinterProfile

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _matched_explicitly(text: str) -> bool:
    """True when some rule in the table actually names this message."""
    return any(needle in text for needle, _sev, _ctrl in serve._ISSUE_RULES)


# ---------------------------------------------------------------------------
# 1. Every machine-safety message analyze.py can emit is explicitly classified.
# ---------------------------------------------------------------------------
def test_every_analyze_message_is_explicitly_classified():
    """Drive _evaluate() into every one of its six branches at once.

    A single analysis deliberately violating everything is enough: _evaluate
    appends independently per condition, so one pass over a maximally-bad
    analysis yields the full set of strings it is capable of producing.
    """
    p = PrinterProfile()
    a = GcodeAnalysis()
    # Outside the safe area in X and Y, and above Z max.
    a.min = (-50.0, -50.0, 0.0)
    a.max = (p.print_max_x + 50.0, p.print_max_y + 50.0, p.z_max + 10.0)
    # Over both Z ceilings.
    a.max_z_rate = p.max_z_velocity + 25.0
    a.peak_z_accel = p.max_z_accel * 5.0
    # Printing in mid-air. unsupported_pct is derived from these two, not set.
    a.extrude_moves = 1000
    a.unsupported_moves = 500
    # Into the probe keep-out (only reported when the profile HAS a probe).
    a.probe_hits = 7
    a.probe_worst_mm = 3.2

    _evaluate(a, p)

    check(p.has_probe,
          "fixture sanity: the default profile has a probe, so the probe "
          "branch is reachable", str(p.has_probe))
    check(len(a.issues) == 6,
          "all six _evaluate() branches fired (if this count changed, a "
          "branch was added/removed and the rule table needs revisiting)",
          f"got {len(a.issues)}: {a.issues}")

    for msg in a.issues:
        check(_matched_explicitly(msg),
              "analyze.py message is named by an explicit rule: "
              f"{msg[:60]!r}",
              "NO RULE MATCHED -- it would fall back to a generic severity")

    # And none of them may be classified as a mere note.
    for msg in a.issues:
        sev, _ctrl = serve._classify_issue(msg, True)
        check(sev in (serve._SEVERITY_DANGER, serve._SEVERITY_WARN),
              f"machine-safety message is danger/warn, not a note: {msg[:50]!r}",
              sev)


# ---------------------------------------------------------------------------
# 2. The severities that actually matter are the ones that carry risk.
# ---------------------------------------------------------------------------
def test_probe_collision_is_danger():
    msg = ("PROBE COLLISION RISK: 7 moves where printed material rises up to "
           "3.2 mm into the probe keep-out (probe at nozzle +(0,25), "
           "clearance 0.5 mm). Reduce z-amp / surface amplitude, or verify "
           "the real probe clearance.")
    sev, ctrl = serve._classify_issue(msg, True)
    check(sev == serve._SEVERITY_DANGER,
          "probe collision risk is DANGER (this is the exact class of finding "
          "that had the Trident's z_amp_max revised down after a real strike)",
          sev)
    check(ctrl == "amp-curve",
          "probe collision points at the amplitude curve, the control that "
          "actually reduces it", str(ctrl))


def test_out_of_bounds_is_danger():
    for msg, expect_ctrl in (
        ("Footprint X[-50,300] Y[-50,300] falls outside the safe area "
         "X[0,235] Y[0,235] - recenter or scale down.", "d-radius"),
        ("Top Z 300.0 exceeds Z max 160.", "d-height"),
    ):
        sev, ctrl = serve._classify_issue(msg, True)
        check(sev == serve._SEVERITY_DANGER,
              f"out-of-bounds finding is DANGER: {msg[:40]!r}", sev)
        check(ctrl == expect_ctrl,
              f"out-of-bounds finding points at {expect_ctrl}", str(ctrl))


def test_throttling_findings_are_warnings_not_dangers():
    """Firmware will compensate -- the print degrades, nothing is destroyed."""
    for msg in (
        "Peak Z-rate 40.0 mm/s exceeds max_z_velocity 25 - firmware will slow "
        "these moves; slope/feedrate is too steep.",
        "Peak Z-accel demand 900 mm/s^2 exceeds max_z_accel 500 - firmware "
        "will throttle wave crests; expect slower prints and possible blobbing.",
    ):
        sev, _ctrl = serve._classify_issue(msg, True)
        check(sev == serve._SEVERITY_WARN,
              f"throttling finding is WARN, not DANGER: {msg[:40]!r}", sev)


def test_shared_phrase_messages_point_at_their_own_control():
    """Two unrelated warnings both contain "exceeds this printer's printable".

    They are fixed by different controls, so each must match on its own
    distinctive opening rather than on the shared tail. This was a real bug
    found by live testing: a wave-slope warning offered to jump to the
    mesh-blend field, which cannot affect it.
    """
    wave = ("Peak wave slope 0.70 exceeds this printer's printable ~0.25 "
            "(amp*waves/radius) - upper waves may collapse. At its narrowest "
            "wall radius 32.0 mm with 25 waves that caps amplitude at 0.32 mm.")
    mesh = ("the mesh-to-wall blend needs an overhang up to 32.1 deg from "
            "vertical (slope 0.63, 0.188 mm horizontal step over a 0.30 mm "
            "layer) - exceeds this printer's printable ~0.25 slope; increase "
            "mesh_base_blend_height to ease the transition.")

    sev, ctrl = serve._classify_issue(wave, False)
    check(sev == serve._SEVERITY_WARN, "wave-slope warning is WARN", sev)
    check(ctrl == "amp-curve",
          "wave-slope warning points at the amplitude curve, NOT the mesh "
          "blend field", str(ctrl))

    sev, ctrl = serve._classify_issue(mesh, False)
    check(sev == serve._SEVERITY_WARN, "mesh-blend overhang warning is WARN", sev)
    check(ctrl == "d-meshbase-blend",
          "mesh-blend overhang warning points at the blend height field",
          str(ctrl))


def test_advisories_are_notes():
    for msg in (
        "GUESS, NOT PRINT-TESTED: this loop fabric wall resumes onto a real "
        "OrcaSlicer-sliced planar base - the seam geometry has not been "
        "verified on a physical print.",
        "zone overrides only apply to the parametric wall (not loop fabric) "
        "- ignored for this design.",
        "loop fabric anchors itself with its own solid cuff, so the requested "
        "base layers were not printed.",
    ):
        sev, _ctrl = serve._classify_issue(msg, False)
        check(sev == serve._SEVERITY_NOTE,
              f"advisory is a NOTE: {msg[:45]!r}", sev)


# ---------------------------------------------------------------------------
# 3. The fallbacks: an unrecognised finding must never render as cosmetic.
# ---------------------------------------------------------------------------
def test_unknown_analysis_finding_falls_back_to_warn_not_note():
    sev, ctrl = serve._classify_issue(
        "Some future machine-safety finding nobody has written a rule for", True)
    check(sev == serve._SEVERITY_WARN,
          "an UNRECOGNISED machine-safety finding falls back to WARN -- it "
          "must never be able to render with the weight of a cosmetic note",
          sev)
    check(ctrl is None, "unknown finding has no control to jump to", str(ctrl))


def test_unknown_advisory_falls_back_to_note():
    sev, _ctrl = serve._classify_issue("Some future advisory", False)
    check(sev == serve._SEVERITY_NOTE,
          "an unrecognised advisory falls back to NOTE", sev)


# ---------------------------------------------------------------------------
# 4. _issues_detail: order and contents line up with the flat array.
# ---------------------------------------------------------------------------
def test_issues_detail_mirrors_the_flat_array():
    analysis_issues = ["Top Z 300.0 exceeds Z max 160."]
    extra = ["zone overrides only apply to the parametric wall (not loop "
             "fabric) - ignored for this design."]
    detail = serve._issues_detail(analysis_issues, extra)
    flat = list(analysis_issues) + extra

    check(len(detail) == len(flat),
          "issues_detail has one entry per flat issue", f"{len(detail)} vs {len(flat)}")
    check([d["text"] for d in detail] == flat,
          "issues_detail preserves the flat array's exact order and text",
          str([d["text"] for d in detail]))
    check(detail[0]["severity"] == serve._SEVERITY_DANGER
          and detail[1]["severity"] == serve._SEVERITY_NOTE,
          "the two entries carry their own distinct severities",
          str([d["severity"] for d in detail]))
    check(all(set(d) == {"text", "severity", "control"} for d in detail),
          "every entry has exactly text/severity/control",
          str([sorted(d) for d in detail]))


def test_issues_detail_handles_empty():
    check(serve._issues_detail([], []) == [], "no issues -> empty detail array")
    check(serve._issues_detail([], None) == [],
          "a None extras list is treated as empty, not a crash")


# ---------------------------------------------------------------------------
# 5. Every control id a rule points at must be a real control.
# ---------------------------------------------------------------------------
def test_every_control_id_in_the_rules_exists_in_the_ui():
    html = (ROOT / "viewer" / "index.html").read_text(encoding="utf-8")
    for needle, _sev, control in serve._ISSUE_RULES:
        if control is None:
            continue
        check(('id="%s"' % control) in html,
              f"rule {needle[:35]!r} points at a control that exists: #{control}",
              "no such id in viewer/index.html -- the jump would do nothing")


def main() -> int:
    test_every_analyze_message_is_explicitly_classified()
    test_probe_collision_is_danger()
    test_out_of_bounds_is_danger()
    test_throttling_findings_are_warnings_not_dangers()
    test_shared_phrase_messages_point_at_their_own_control()
    test_advisories_are_notes()
    test_unknown_analysis_finding_falls_back_to_warn_not_note()
    test_unknown_advisory_falls_back_to_note()
    test_issues_detail_mirrors_the_flat_array()
    test_issues_detail_handles_empty()
    test_every_control_id_in_the_rules_exists_in_the_ui()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
