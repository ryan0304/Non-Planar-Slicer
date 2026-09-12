#!/usr/bin/env python3
"""Tests for serve.py's _append_extra_issues -- the fix for a silent-warning
bug in generate_design / generate_surface_design / generate_mesh_texture_design.

Background: each of those three functions computes ``report_text =
format_report(analysis, profile)`` FIRST, then builds a local ``issues_extra``
list of scope/gate warnings (e.g. "a hybrid planar base only applies to the
parametric wall (not loop fabric) - ignored for this design."). Before this
fix, ``issues_extra`` was folded ONLY into the JSON response's top-level
``"issues"`` array -- never into ``report_text``. The viewer
(viewer/designer.js's applyGenerateResult) shows ``report_text`` verbatim and
separately shows only a bare COUNT of ``issues`` ("N safety warning(s) - see
report"), so the explanation the count points to was never actually in the
report. A user combining, say, Loop Fabric with a hybrid planar base saw the
base silently vanish with a warning count that led nowhere -- exactly the
silent-failure class CLAUDE.md's "Presence checks must read commands, not
comments" precedent warns about.

This file checks the fix two ways:
  1. Directly, against _append_extra_issues itself (the merge helper).
  2. End-to-end, by calling the real serve.py request handlers with bodies
     that are known (from serve.py's own scope-issue logic) to populate
     issues_extra, and asserting the exact warning text is a SUBSTRING of
     ``result["report"]`` -- not merely present in ``result["issues"]``,
     which is the check the bug hid behind (see test_printer_import.py's
     ``said()`` helper, which only ever looked at ``result["issues"]``).

Plain ``python tools/test_report_extra_issues.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors tools/test_serve_mesh_params.py's style.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve
from trident_gcode.mesh import load_stl

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1. Direct tests of the merge helper.
# ---------------------------------------------------------------------------
def test_append_extra_issues_unit():
    base = "line one\nline two"

    check(serve._append_extra_issues(base, []) == base,
          "unit: an empty issues_extra list is a true no-op (byte-identical text)")
    check(serve._append_extra_issues(base, None) == base,
          "unit: None issues_extra is also a no-op")

    msgs = [
        "a hybrid planar base only applies to the parametric wall (not loop "
        "fabric) - ignored for this design.",
        "STL mode cannot vary the base fill or lay a skirt - concentric base "
        "style (the spiral disk was used) was not printed.",
    ]
    out = serve._append_extra_issues(base, msgs)
    check(out.startswith(base),
          "unit: the original report text is preserved as a prefix", out)
    check("ADDITIONAL WARNINGS" in out,
          "unit: a visible section header is added", out)
    for m in msgs:
        check(m in out,
              f"unit: every issues_extra message appears verbatim in the merged text: {m!r}",
              out)


# ---------------------------------------------------------------------------
# 2. End-to-end: generate_design's own scope warnings must reach report_text.
#
# NOTE: this file originally used loop-fabric + a hybrid planar base
# (hybrid_base_height / mesh_base_id) as its two loop-fabric examples, because
# at the time that combination was unconditionally ignored (hybrid_scope_issue
# / mesh_hybrid_scope_issue). Both combinations are now REAL, supported
# features (build_loop_hybrid_print / build_mesh_loop_hybrid_print -- see
# trident_gcode/hybrid.py and the plan at
# .claude/plans/optimized-churning-hare.md), so hybrid_scope_issue and
# mesh_hybrid_scope_issue are now permanently None (serve.py never assigns
# them anymore -- dead variables kept only as the issues_extra slots a future
# restriction could reuse). Asserting for that now-unreachable message here
# would either silently pass on a rewritten no-op or -- as it did the moment
# this file was actually run -- crash the whole suite by falling all the way
# through into a REAL OrcaSlicer subprocess call, which the old
# `serve.orca_binary_path` monkeypatch alone no longer prevents once the
# combined builder is what actually gets called. Switched to two OTHER
# loop-fabric scope restrictions that are still genuinely unsupported today
# (zone_scope_issue, radius_speed_scope_issue) and need no Orca install at
# all, while still proving the same general fix -- two differently-worded
# issues_extra messages, both reaching report_text.
# ---------------------------------------------------------------------------
def test_loop_fabric_zone_scope_reaches_report_text():
    """Loop fabric + zone overrides: zone overrides only apply to the
    parametric wall (build_profile_spiral's per-point texture zones), which
    loop fabric replaces entirely -- see serve.py's comment above
    ``if loop_spec is not None:``. No hybrid base, no mesh, no Orca
    involved at all."""
    MSG = ("zone overrides only apply to the parametric wall (not loop "
           "fabric) - ignored for this design.")

    body = {
        "loop_per_turn": 12, "radius": 20.0, "height": 20.0,
        "printer": "trident",
        # "pattern" makes this a REAL override (an empty entry with no
        # pattern/depth/twist is dropped as a no-op zone -- see
        # _parse_zone_overrides -- which would trip zone_empty_issue instead
        # of the zone_scope_issue this test wants).
        "zone_overrides": [{"t_lo": 0.2, "t_hi": 0.6, "pattern": "diamond"}],
    }
    r = serve.generate_design(dict(body))

    check(any(m == MSG for m in r["issues"]),
          "e2e/generate_design: the zone scope warning is in the issues "
          "array (sanity check)", str(r["issues"]))
    check(MSG in r["report"],
          "e2e/generate_design: the zone scope warning text is ALSO in "
          "report_text, not just the issues count the viewer shows",
          r["report"])
    check("ADDITIONAL WARNINGS" in r["report"],
          "e2e/generate_design: report_text carries a visible warnings section",
          r["report"])


def test_loop_fabric_radius_speed_scope_reaches_report_text():
    """Same scope restriction, for radius-based speed compensation instead
    of zone overrides -- a second, differently-worded issues_extra message
    that must also reach report_text."""
    MSG = ("variable speed by radius only applies to the parametric wall "
           "(not loop fabric) - ignored for this design.")

    body = {
        "loop_per_turn": 12, "radius": 20.0, "height": 20.0,
        "printer": "trident", "radius_speed_comp": True,
    }
    r = serve.generate_design(dict(body))

    check(any(m == MSG for m in r["issues"]),
          "e2e/generate_design: the radius-speed scope warning is in the "
          "issues array (sanity check)", str(r["issues"]))
    check(MSG in r["report"],
          "e2e/generate_design: the radius-speed scope warning text is ALSO "
          "in report_text", r["report"])


# ---------------------------------------------------------------------------
# 3. End-to-end: generate_mesh_texture_design's own scope warnings.
# ---------------------------------------------------------------------------
def test_mesh_texture_base_style_scope_reaches_report_text():
    serve._mesh_cache_put("t_report_extra_issues_mt",
                           load_stl(str(ROOT / "examples" / "cylinder.stl")))

    body = {
        "mode": "mesh_texture", "mesh_id": "t_report_extra_issues_mt",
        "layer_height": 0.4, "points_per_turn": 120, "printer": "trident",
        "base_layers": 3, "base_style": "concentric",
    }
    r = serve.generate_mesh_texture_design(dict(body))

    NEEDLE = "cannot vary the base fill"
    check(any(NEEDLE in m for m in r["issues"]),
          "e2e/generate_mesh_texture_design: the base-fill scope warning is "
          "in the issues array (sanity check)", str(r["issues"]))
    check(NEEDLE in r["report"],
          "e2e/generate_mesh_texture_design: the base-fill scope warning "
          "text is ALSO in report_text", r["report"])


# ---------------------------------------------------------------------------
# 3b. End-to-end: Height CONTROLS the print in Texture the whole model.
#
# This mode used to slice the mesh over its own maxz-minz and ignore the
# Height field outright, so a 5mm mount could only ever print 5mm tall. The
# field was inert, and for a while the viewer greyed it out and said so --
# which read as the app overriding a number the user had deliberately set
# ("the height is dimmed still and i wouldnt able to adjust the height ...
# thats the thing that ive been mentioned to be fixed 3 times"). Height is
# now honoured as a Z stretch of the mesh's sliced stack.
#
# cylinder.stl is 40mm tall; asking for 25mm must print ~25mm, and asking
# for 60mm must print ~60mm -- the FIELD decides, not the file.
# ---------------------------------------------------------------------------
def test_mesh_texture_height_controls_the_print():
    serve._mesh_cache_put("t_report_extra_issues_mt_height",
                           load_stl(str(ROOT / "examples" / "cylinder.stl")))

    for want in (25.0, 60.0):
        body = {
            "mode": "mesh_texture", "mesh_id": "t_report_extra_issues_mt_height",
            "layer_height": 0.4, "points_per_turn": 120, "printer": "trident",
            "height": want,
        }
        r = serve.generate_mesh_texture_design(dict(body))
        got = r["stats"]["height_mm"]
        check(abs(got - want) < 1.5,
              "e2e/generate_mesh_texture_design: Height %.0f prints ~%.0fmm "
              "(mesh's own height is 40mm)" % (want, want),
              "got %s" % got)


# Teeth: the footprint must NOT move. A Z stretch re-spaces cross-sections;
# widening the part because someone raised its height would be its own bug
# (Scale owns X/Y). cylinder.stl is 50mm across whatever the height is.
def test_mesh_texture_height_does_not_change_footprint():
    serve._mesh_cache_put("t_report_extra_issues_mt_fp",
                           load_stl(str(ROOT / "examples" / "cylinder.stl")))
    foots = []
    for want in (20.0, 80.0):
        body = {
            "mode": "mesh_texture", "mesh_id": "t_report_extra_issues_mt_fp",
            "layer_height": 0.4, "points_per_turn": 120, "printer": "trident",
            "height": want,
        }
        foots.append(serve.generate_mesh_texture_design(dict(body))["stats"]["footprint_mm"])
    check(foots[0] == foots[1],
          "e2e/generate_mesh_texture_design: a Z stretch leaves the footprint "
          "alone (X/Y belong to Scale)", str(foots))


# The machine's own Z ceiling still wins: Height is honoured, not obeyed
# blindly. A height past the printer's Z max must raise, exactly as it does
# for the parametric wall -- this mode must not become a way around a
# machine limit.
def test_mesh_texture_height_still_respects_z_max():
    serve._mesh_cache_put("t_report_extra_issues_mt_zmax",
                           load_stl(str(ROOT / "examples" / "cylinder.stl")))
    body = {
        "mode": "mesh_texture", "mesh_id": "t_report_extra_issues_mt_zmax",
        "layer_height": 0.4, "points_per_turn": 120, "printer": "trident",
        "height": 999.0,
    }
    raised = False
    try:
        serve.generate_mesh_texture_design(dict(body))
    except ValueError as e:
        raised = "Z max" in str(e)
    check(raised,
          "e2e/generate_mesh_texture_design: a Height past the printer's Z "
          "max is still refused, not stretched into a crash", "no ValueError")


# ---------------------------------------------------------------------------
# 3c. End-to-end: generate_mesh_texture_design's hybrid-base-height-is-ignored
# note.
#
# Regression for a SECOND live bug reported in the same session: a hybrid
# planar base (a solid, Orca-sliced silhouette extrusion from the bed up to
# hybrid_base_height, then the parametric wall resumes) has nothing to
# resume onto in Texture-the-whole-model mode -- the wall IS the mesh's own
# contour stack start to finish. The viewer used to still show the Hybrid
# base height field as a normal, live control whenever the mesh was used as
# Texture rather than Planar base (its hide condition checked only the
# planar-base case), and the field's value was silently dropped server-side
# with zero explanation -- reported live as "the planar base is missing."
# ---------------------------------------------------------------------------
def test_mesh_texture_hybrid_base_height_ignored_scope_reaches_report_text():
    serve._mesh_cache_put("t_report_extra_issues_mt_hybrid",
                           load_stl(str(ROOT / "examples" / "cylinder.stl")))

    body = {
        "mode": "mesh_texture", "mesh_id": "t_report_extra_issues_mt_hybrid",
        "layer_height": 0.4, "points_per_turn": 120, "printer": "trident",
        "hybrid_base_height": 20.0,
    }
    r = serve.generate_mesh_texture_design(dict(body))

    NEEDLE = "a hybrid planar base only applies"
    check(any(NEEDLE in m for m in r["issues"]),
          "e2e/generate_mesh_texture_design: the hybrid-base-height-ignored "
          "note is in the issues array (sanity check)", str(r["issues"]))
    check(NEEDLE in r["report"],
          "e2e/generate_mesh_texture_design: the hybrid-base-height-ignored "
          "note text is ALSO in report_text", r["report"])
    # Confirm it did not silently print a hybrid base either -- the request's
    # ONLY effect must be the note, not a partially-honoured base.
    check(abs(r["stats"]["height_mm"] - 40.0) < 1.0,
          "e2e/generate_mesh_texture_design: the ignored hybrid base did not "
          "change the printed height (still the mesh's own 40mm)",
          str(r["stats"]))


# Teeth: hybrid_base_height=0 (the default / off state) must NOT get this
# note -- every ordinary Texture-mode design leaves this field at 0.
def test_mesh_texture_hybrid_base_height_off_gets_no_note():
    serve._mesh_cache_put("t_report_extra_issues_mt_hybrid_off",
                           load_stl(str(ROOT / "examples" / "cylinder.stl")))

    body = {
        "mode": "mesh_texture", "mesh_id": "t_report_extra_issues_mt_hybrid_off",
        "layer_height": 0.4, "points_per_turn": 120, "printer": "trident",
        "hybrid_base_height": 0,
    }
    r = serve.generate_mesh_texture_design(dict(body))
    NEEDLE = "a hybrid planar base only applies"
    check(not any(NEEDLE in m for m in r["issues"]),
          "e2e/generate_mesh_texture_design: no hybrid-base note when "
          "hybrid_base_height is 0 (off)", str(r["issues"]))


# ---------------------------------------------------------------------------
# 4. End-to-end: generate_surface_design's own issues_extra (the probe-slope
# pass note) must reach report_text too -- a differently-shaped issues_extra
# than the other two functions (advisory note, not a dropped-feature scope
# warning), proving the fix is not special-cased to one message shape.
# ---------------------------------------------------------------------------
def test_surface_probe_note_reaches_report_text():
    body = {"surface": "dome", "radius": 20.0, "surface_amp": 2.0,
            "printer": "trident"}
    r = serve.generate_surface_design(dict(body))

    NEEDLE = "Probe-slope check passed"
    check(any(NEEDLE in m for m in r["issues"]),
          "e2e/generate_surface_design: the probe-slope note is in the "
          "issues array (sanity check)", str(r["issues"]))
    check(NEEDLE in r["report"],
          "e2e/generate_surface_design: the probe-slope note text is ALSO "
          "in report_text", r["report"])


def main() -> int:
    test_append_extra_issues_unit()
    test_loop_fabric_zone_scope_reaches_report_text()
    test_loop_fabric_radius_speed_scope_reaches_report_text()
    test_mesh_texture_base_style_scope_reaches_report_text()
    test_mesh_texture_height_controls_the_print()
    test_mesh_texture_height_does_not_change_footprint()
    test_mesh_texture_height_still_respects_z_max()
    test_mesh_texture_hybrid_base_height_ignored_scope_reaches_report_text()
    test_mesh_texture_hybrid_base_height_off_gets_no_note()
    test_surface_probe_note_reaches_report_text()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
