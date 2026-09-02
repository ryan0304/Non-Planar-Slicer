#!/usr/bin/env python3
"""Validate what the viewer's control search (jump-to-control) will show.

Plain ``python tools/test_viewer_search_index.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors test_orca_slice.py's style.

WHY THIS IS A PYTHON TEST OVER THE HTML, not a JS one
-----------------------------------------------------
``viewer/`` is vanilla JS with no build step and no test runner (CLAUDE.md), so
there is nothing to execute designer.js against a DOM without adding a
dependency this repo does not want. What actually broke, though, was never the
search algorithm -- it was the STRUCTURE the algorithm reads:

  * a row's label was taken with ``textContent``, which swallowed the nested
    info-button glyph, so a result rendered as "Wall generator (i)";
  * the crumb came from ``.step-panel``, which the right-hand planar bar sits
    outside, so all 41 of its rows claimed to be in "Design" -- the report
    that started this ("wall generator is at the planar > quality");
  * with those two wrong, the Quality and Speed groups each contributed an
    "Outer wall" and an "Inner wall" row that were indistinguishable in the
    dropdown.

Every one of those is decidable from index.html alone. So this file re-derives
each row's label and crumb the same way indexScroll()/crumbFor() do and
asserts the properties a user actually sees. It is a contract test on the
markup, in the spirit of test_serve_mesh_params.py's cross-file tests: if
someone adds a control whose label collides or whose crumb goes missing, this
fails instead of the search quietly showing two identical rows.

SCOPE, after a follow-up correction
------------------------------------
The first pass of this fix also section-qualified the LEFT sidebar (every
crumb became "Model > Shape" etc.) and added a compound "Planar > Quality"
prefix to the planar bar. Neither was asked for and both were reverted: the
non-planar side keeps the exact crumb it has always shown (its wizard step, or
"Viewer"), and the planar bar shows its group name ALONE -- "Quality", not
"Planar > Quality" -- since the bar is one visible panel with those headings
already on screen, so the prefix was noise. This file's assertions follow that
corrected shape; do not "fix" it back toward compound crumbs without checking
with the user first.
"""
from __future__ import annotations

import collections
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "viewer" / "index.html"
DESIGNER_JS = ROOT / "viewer" / "designer.js"

# Tags that never nest, so they must not be pushed onto the element stack.
_VOID = {"input", "br", "hr", "img", "meta", "link", "source", "area", "base",
         "col", "embed", "param", "track", "wbr"}

# The step panels the left sidebar is divided into, and the crumb each shows.
# Must match STEP_LABEL in designer.js's param-search IIFE.
_STEP_LABEL = {"step-model": "Model", "step-texture": "Texture",
               "step-print": "Print", "step-generate": "Generate"}

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


class _RowScanner(HTMLParser):
    """Collect every .drow / label.row and every section-heading (h2 or h3
    with an id starting 'sec-head-'), in document order, with the id/class
    stack each sits under.

    Labels and heading text are taken from DIRECT TEXT NODES ONLY -- the same
    rule as designer.js's ownText(). That is what drops a row's info button
    (.fm-info-btn) and a heading's collapse chevron (.sec-chevron) instead of
    concatenating their glyphs into the label.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None, set]] = []
        self.nodes: list[dict] = []       # rows and headings, document order
        self._row: dict | None = None
        self._span_depth: int | None = None
        self._span_buf: list[str] = []
        self._h3_depth: int | None = None
        self._h3_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set((a.get("class") or "").split())
        if tag in _VOID:
            return
        self.stack.append((tag, a.get("id"), cls))
        depth = len(self.stack)
        # h2 as well as h3: Print stats and Display use <h2>.
        if tag in ("h2", "h3") and a.get("id", "").startswith("sec-head-"):
            self._h3_depth = depth
            self._h3_buf = []
        if self._row is None and ("drow" in cls or (tag == "label" and "row" in cls)):
            self._row = {
                "kind": "row",
                "depth": depth,
                "ids": [i for (_t, i, _c) in self.stack if i],
                "classes": [c for (_t, _i, c) in self.stack],
                "label": None,
            }
        elif (self._row is not None and tag == "span"
              and self._row["label"] is None and self._span_depth is None):
            # Only the row's own leading <span>, matching ':scope > span'.
            if depth == self._row["depth"] + 1:
                self._span_depth = depth
                self._span_buf = []

    def handle_data(self, data):
        # Direct text nodes only: a nested element pushes the stack deeper, so
        # comparing against the recorded depth is what excludes button/chevron
        # text without needing to know their class names.
        if self._span_depth is not None and len(self.stack) == self._span_depth:
            self._span_buf.append(data)
        if self._h3_depth is not None and len(self.stack) == self._h3_depth:
            self._h3_buf.append(data)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        depth = len(self.stack)
        if self._span_depth is not None and depth == self._span_depth:
            self._row["label"] = " ".join("".join(self._span_buf).split())
            self._span_depth = None
        if self._h3_depth is not None and depth == self._h3_depth:
            self.nodes.append({
                "kind": "heading",
                "text": " ".join("".join(self._h3_buf).split()),
                "ids": [i for (_t, i, _c) in self.stack if i],
                "classes": [c for (_t, _i, c) in self.stack],
            })
            self._h3_depth = None
        if self._row is not None and depth == self._row["depth"]:
            self.nodes.append(self._row)
            self._row = None
        if self.stack:
            self.stack.pop()


def _in_scroll(node) -> str | None:
    """Which of the two containers indexScroll() is called with holds this
    node -- or None if the search never sees it at all (the Point Edit modal,
    for instance, lives outside both)."""
    ids = node["ids"]
    if "planar-panel" in ids and any("panel-scroll" in c for c in node["classes"]):
        return "planar"
    if "panel-scroll" in ids:
        return "left"
    return None


_REGISTER_RE = re.compile(
    r"registerSection\(\s*'([^']+)'\s*,\s*document\.getElementById\('([^']+)'\)"
    r"\s*,\s*document\.getElementById\('([^']+)'\)")


def registered_sections() -> list[tuple[str, str, str]]:
    """(key, headingId, bodyId) for every registerSection() call in
    designer.js -- the app's own section registry, read from source."""
    return _REGISTER_RE.findall(DESIGNER_JS.read_text(encoding="utf-8"))


def build_index() -> list[dict]:
    """Re-derive designer.js's search index from the markup.

    Mirrors indexScroll()/sectionOf()/crumbFor() exactly:
      * section is resolved (by CONTAINMENT in the registry, not "nearest
        heading above the row") for PLANAR rows only;
      * a planar row's crumb is its group name alone ("Quality"), never a
        compound "Planar > Quality";
      * a left-sidebar row's crumb is exactly what it has always been -- its
        wizard step, or "Viewer" -- unqualified by section.
    """
    p = _RowScanner()
    p.feed(INDEX_HTML.read_text(encoding="utf-8"))

    heading_text = {n["ids"][-1]: n["text"] for n in p.nodes
                    if n["kind"] == "heading" and n["ids"]}
    # body id -> heading text, for the registrations that actually resolve.
    body_to_heading = {}
    for _key, head_id, body_id in registered_sections():
        if head_id in heading_text:
            body_to_heading[body_id] = heading_text[head_id]

    rows: list[dict] = []
    for node in p.nodes:
        where = _in_scroll(node)
        if where is None or node["kind"] != "row" or not node["label"]:
            continue
        ids = node["ids"]
        if where == "planar":
            base = "Planar"
        elif "mode-viewer" in ids:
            base = "Viewer"
        else:
            step = next((i for i in ids if i in _STEP_LABEL), None)
            base = _STEP_LABEL.get(step, "Design")
        # Section is resolved for the planar bar ONLY. Innermost containing
        # registered section, matching sectionOf()'s first-match-wins over a
        # non-nested registry.
        sec = None
        if where == "planar":
            for anc in reversed(ids):
                if anc in body_to_heading:
                    sec = body_to_heading[anc]
                    break
        rows.append({
            "label": node["label"],
            "base": base,
            "section": sec,
            # Planar: the group name alone. Everything else: the step/Viewer
            # label it has always shown -- never a compound crumb.
            "crumb": (sec or base) if where == "planar" else base,
            "planar": where == "planar",
        })
    return rows


# ---------------------------------------------------------------------------
def test_index_is_populated(rows):
    check(len(rows) > 80, f"index: search sees {len(rows)} rows", "suspiciously few")
    planar = [r for r in rows if r["planar"]]
    check(len(planar) >= 40,
          f"index: the planar bar contributes {len(planar)} rows", str(len(planar)))


# Glyphs that belong to a row's or heading's DECORATIVE child elements and so
# must never appear in a label: the info button's circled-i (.fm-info-btn) and
# the collapse chevron (.sec-chevron). Their presence is proof that the label
# was built from textContent rather than the element's own text nodes.
#
# Deliberately a named list, not "reject all non-ASCII": a label may legally
# contain typography of its own. "Strand thickness x" uses a real multiplication
# sign to mean "a multiplier", written &times; in the markup, and rejecting it
# would be testing the wrong thing -- the bug class is leaked child content,
# not the codepoint range.
_DECORATION_GLYPHS = {"ⓘ": "info button (U+24D8)",
                      "▾": "collapse chevron (U+25BE)"}


def test_labels_carry_no_child_decoration(rows):
    for glyph, name in _DECORATION_GLYPHS.items():
        dirty = [r["label"] for r in rows if glyph in r["label"]]
        check(not dirty,
              f"labels: no search label has swallowed the {name}",
              f"{dirty[:6]}")

    ctrl = [r["label"] for r in rows
            if any(ord(c) < 32 or ord(c) == 127 for c in r["label"])]
    check(not ctrl, "labels: no label contains control characters", f"{ctrl[:6]}")

    blank = [r for r in rows if not r["label"].strip()]
    check(not blank, "labels: no indexed row has an empty label")

    # A label that swallowed a nested element tends to be far longer than a
    # real control name; this catches the bug class even for a decoration
    # whose glyph is not yet in the list above.
    long = [r["label"] for r in rows if len(r["label"]) > 40]
    check(not long, "labels: no label is long enough to suggest leaked markup",
          f"{long[:4]}")


# The planar bar's groups, exactly as their headings read on screen. A planar
# crumb must be one of these and nothing else -- in particular never a
# "Planar > ..." compound, and never a bare "Planar" fallback.
_PLANAR_GROUPS = {"Quality", "Strength", "Speed", "Support", "Others",
                  "Seam blend"}


def test_planar_rows_crumb_as_their_group(rows):
    """The reported bug, pinned directly: a planar row must show its OWN
    group ("Quality") -- not "Design" (the original bug) and not a compound
    "Planar > Quality" (an over-correction reverted at the user's request)."""
    planar = [r for r in rows if r["planar"]]
    bad = [(r["label"], r["crumb"]) for r in planar if r["crumb"] not in _PLANAR_GROUPS]
    check(not bad,
          "crumbs: every planar row shows its group name alone",
          f"{bad[:6]}")

    compound = [(r["label"], r["crumb"]) for r in rows if ">" in r["crumb"]]
    check(not compound,
          "crumbs: no result shows a compound 'Panel > Section' crumb",
          f"{compound[:6]}")

    wall_gen = [r for r in rows if r["label"] == "Wall generator"]
    check(len(wall_gen) == 1 and wall_gen[0]["crumb"] == "Quality",
          "crumbs: 'Wall generator' reports as Quality",
          f"{[(r['label'], r['crumb']) for r in wall_gen]}")


def test_left_sidebar_crumbs_are_untouched(rows):
    """The non-planar side keeps the unqualified crumb it has always had.

    Section-qualifying the left sidebar was a change to a part of the UI this
    fix has no business touching -- the reported bug (and the user's explicit
    instruction) was about the PLANAR side only. Pinned here so it cannot
    drift back toward "Model > Shape" style crumbs.
    """
    allowed = {"Model", "Texture", "Print", "Generate", "Viewer", "Design"}
    bad = [(r["label"], r["crumb"]) for r in rows
           if not r["planar"] and r["crumb"] not in allowed]
    check(not bad,
          "crumbs: left-sidebar rows still show only their step (or Viewer)",
          f"{bad[:6]}")


def test_no_two_planar_results_look_identical(rows):
    """The property a user actually cares about, scoped to the planar bar:
    two planar rows must never render as the same label AND the same crumb.
    Quality's "Outer wall" (a line width) and Speed's "Outer wall" (a speed)
    were exactly this -- the group name alone is what now keeps them apart."""
    planar_counts = collections.Counter(
        (r["label"], r["crumb"]) for r in rows if r["planar"])
    dupes = sorted(k for k, v in planar_counts.items() if v > 1)
    check(not dupes,
          "results: no two planar rows render as the same label + crumb",
          f"{dupes}")

    # The pairs that used to be indistinguishable, spot-checked by name so a
    # regression names the case rather than just a count.
    by_planar_label = collections.defaultdict(list)
    for r in rows:
        if r["planar"]:
            by_planar_label[r["label"]].append(r["crumb"])
    for label in ("Outer wall", "Inner wall", "Top surface",
                  "Sparse infill", "Internal solid infill"):
        got = sorted(by_planar_label.get(label, []))
        check(got == ["Quality", "Speed"],
              f"crumb: {label!r} is split across Quality and Speed", f"{got}")

    # The left sidebar has two long-standing ambiguous pairs -- the design's
    # own Height/Layer height against the imported mesh's -- that predate this
    # fix and are out of its scope (the left sidebar is deliberately left
    # unqualified; see test_left_sidebar_crumbs_are_untouched). Pinned as
    # known-and-accepted so the list cannot silently grow.
    known_left = {("Height (mm)", "Model"), ("Layer height", "Model")}
    left_counts = collections.Counter(
        (r["label"], r["crumb"]) for r in rows if not r["planar"])
    left_dupes = {k for k, v in left_counts.items() if v > 1}
    check(left_dupes == known_left,
          "results: the left sidebar's ambiguous pairs are only the known ones",
          f"unexpected {sorted(left_dupes - known_left)}, "
          f"resolved {sorted(known_left - left_dupes)}")


def test_designer_js_still_uses_this_shape(rows):
    """Guard the mirrored logic above against silently going stale.

    If designer.js stops taking direct text nodes, stops attributing planar
    rows by containment, or starts compound-prefixing crumbs again, the
    assertions here would still pass while the real UI regressed. These are
    cheap source checks on the pieces this file assumes.
    """
    js = DESIGNER_JS.read_text(encoding="utf-8")
    check("function ownText(" in js and "nodeType === 3" in js,
          "source: designer.js still takes labels from direct text nodes")
    check("function sectionOf(" in js and "bodyEl.contains(row)" in js,
          "source: designer.js still attributes planar rows by section "
          "CONTAINMENT, not by the nearest heading above them")
    check("function crumbFor(" in js and "m.panel" in js and "m.section" in js,
          "source: designer.js still builds the planar crumb from its group "
          "alone, and the left sidebar from step/Viewer alone")
    check("indexScroll(planarScroll, 'Planar')" in js,
          "source: the planar bar is indexed with its own panel label")


def test_dead_registrations_are_the_known_ones(rows):
    """A registerSection() call whose elements are missing does nothing --
    silently: registerSection() returns early on a null heading/body, so
    nothing ever complains.

    'importstl' and 'cooling' are exactly this, and BOTH PREDATE this fix and
    live on the non-planar side -- deliberately left alone rather than fixed,
    per the instruction to not touch the non-planar side. What this fix
    actually depends on is that every PLANAR section resolves, which is
    checked separately below.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    dead = []
    for key, head_id, body_id in registered_sections():
        missing = [i for i in (head_id, body_id) if f'id="{i}"' not in html]
        if missing:
            dead.append(key)
    known_dead = {"importstl", "cooling"}
    check(set(dead) == known_dead,
          "sections: the only dead registerSection() calls are the known "
          "non-planar ones (left untouched on purpose)",
          f"unexpected {sorted(set(dead) - known_dead)}, "
          f"revived {sorted(known_dead - set(dead))}")

    planar_dead = [k for k in dead if k.startswith("mb-")]
    check(not planar_dead,
          "sections: every planar-bar section registers against real elements",
          f"{planar_dead}")


def main() -> int:
    if not INDEX_HTML.exists():
        print(f"FAIL  missing {INDEX_HTML}")
        return 1
    rows = build_index()
    test_index_is_populated(rows)
    test_labels_carry_no_child_decoration(rows)
    test_planar_rows_crumb_as_their_group(rows)
    test_left_sidebar_crumbs_are_untouched(rows)
    test_no_two_planar_results_look_identical(rows)
    test_dead_registrations_are_the_known_ones(rows)
    test_designer_js_still_uses_this_shape(rows)

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
