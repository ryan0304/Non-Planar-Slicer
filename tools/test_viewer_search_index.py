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
someone adds a control whose label collides or whose section is missing, this
fails instead of the search quietly showing two identical rows.

The mirrored logic is deliberately tiny (a document-order sweep carrying the
last heading forward) and is documented on both sides.
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
    """Collect every .drow / label.row and every h3.section-heading, in
    document order, with the id/class stack each sits under.

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
        # h2 as well as h3: Print stats and Display use <h2>, and a scan that
        # only saw h3 was exactly how the Viewer panel's rows ended up
        # attributed to the wrong section.
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

    Section attribution mirrors sectionOf(): the registered section whose BODY
    contains the row -- not the nearest heading above it. Containment is the
    rule because the markup is not a flat heading/body alternation (see
    sectionOf()'s comment in designer.js for the three places they differ).
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
        # Innermost containing registered section, matching sectionOf()'s
        # first-match-wins over a non-nested registry.
        sec = None
        for anc in reversed(ids):
            if anc in body_to_heading:
                sec = body_to_heading[anc]
                break
        rows.append({
            "label": node["label"],
            "base": base,
            "section": sec,
            "crumb": f"{base} > {sec}" if sec else base,
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


def test_every_row_has_a_section(rows):
    """A row with no section shows a bare crumb like "Design", which is what
    made the planar bar's results useless. Planar rows especially must resolve
    to their group."""
    orphan_planar = [r for r in rows if r["planar"] and not r["section"]]
    check(not orphan_planar,
          "crumbs: every planar-bar row resolves to its own group heading",
          f"{[r['label'] for r in orphan_planar][:6]}")

    # A handful of rows genuinely sit in no collapsible section -- they are
    # loose in the Print step. Their crumb is just "Print", which is honest
    # and unambiguous, so this is an allow-list rather than a failure. It is
    # pinned so a NEW sectionless row shows up here instead of quietly
    # rendering a bare step crumb next to fully-qualified neighbours.
    expected_sectionless = {
        ("Nozzle (mm)", "Print"),
        ("Print speed (mm/s)", "Print"),
        ("Line width override", "Print"),
    }
    orphans = {(r["label"], r["crumb"]) for r in rows if not r["section"]}
    check(orphans == expected_sectionless,
          "crumbs: the only sectionless rows are the known loose Print ones",
          f"unexpected {sorted(orphans - expected_sectionless)}, "
          f"missing {sorted(expected_sectionless - orphans)}")


def test_planar_rows_are_not_labelled_design(rows):
    """The reported bug, pinned directly: the planar bar is not 'Design'."""
    mislabelled = [r for r in rows if r["planar"] and r["base"] != "Planar"]
    check(not mislabelled,
          "crumbs: no planar-bar row is attributed to Design",
          f"{[(r['label'], r['crumb']) for r in mislabelled][:6]}")

    wall_gen = [r for r in rows if r["label"] == "Wall generator"]
    check(len(wall_gen) == 1 and wall_gen[0]["crumb"] == "Planar > Quality",
          "crumbs: 'Wall generator' reports as Planar > Quality",
          f"{[(r['label'], r['crumb']) for r in wall_gen]}")


def test_no_two_results_look_identical(rows):
    """The property a user actually cares about: two rows must never render
    as the same label AND the same crumb, because the dropdown then offers a
    choice with no way to tell which is which. Quality's "Outer wall" (a line
    width) and Speed's "Outer wall" (a speed) were exactly this."""
    counts = collections.Counter((r["label"], r["crumb"]) for r in rows)
    dupes = sorted(k for k, v in counts.items() if v > 1)
    check(not dupes,
          "results: no two indexed rows render as the same label + crumb",
          f"{dupes}")


def test_designer_js_still_uses_this_shape(rows):
    """Guard the mirrored logic above against silently going stale.

    If designer.js stops taking direct text nodes, stops sweeping headings, or
    goes back to deriving the crumb from the step alone, the assertions here
    would still pass while the real UI regressed. These are cheap source
    checks on the three pieces this file assumes.
    """
    js = DESIGNER_JS.read_text(encoding="utf-8")
    check("function ownText(" in js and "nodeType === 3" in js,
          "source: designer.js still takes labels from direct text nodes")
    check("function sectionOf(" in js and "bodyEl.contains(row)" in js,
          "source: designer.js still attributes rows by section CONTAINMENT, "
          "not by the nearest heading above them")
    check("function crumbFor(" in js and "m.section" in js,
          "source: designer.js still builds the crumb from panel + section")
    check("indexScroll(planarScroll, 'Planar')" in js,
          "source: the planar bar is indexed with its own panel label")


def test_every_registered_section_exists(rows):
    """A registerSection() call whose elements are missing does nothing --
    silently. That is how this bug hid: designer.js had always registered
    'importstl' against a '#sec-head-importstl' that was never in the markup,
    so the section was not collapsible AND its rows had no section to be
    attributed to. registerSection() returns early on a null, so nothing ever
    complained.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    dead = []
    for key, head_id, body_id in registered_sections():
        missing = [i for i in (head_id, body_id) if f'id="{i}"' not in html]
        if missing:
            dead.append((key, missing))
    check(not dead,
          "sections: every registerSection() call resolves to real elements",
          f"{dead}")


def test_known_crumbs(rows):
    """Spot-check the specific attributions that were wrong, so a regression
    names the case rather than just a count."""
    want = {
        "Wall generator": "Planar > Quality",
        "Order of walls": "Planar > Quality",
        "Skirt loops": None,          # appears twice, checked below
        "Show travels": "Viewer > Display",
        "Use this STL as": "Model > Import STL",
    }
    by_label = collections.defaultdict(list)
    for r in rows:
        by_label[r["label"]].append(r["crumb"])
    for label, expected in want.items():
        if expected is None:
            continue
        got = by_label.get(label, [])
        check(got == [expected], f"crumb: {label!r} -> {expected}", f"got {got}")

    # "Skirt loops" legitimately exists in two places; they must be
    # distinguishable rather than deduplicated away.
    got = sorted(by_label.get("Skirt loops", []))
    check(len(got) == 2 and len(set(got)) == 2,
          "crumb: the two 'Skirt loops' rows carry different crumbs", f"{got}")


def main() -> int:
    if not INDEX_HTML.exists():
        print(f"FAIL  missing {INDEX_HTML}")
        return 1
    rows = build_index()
    test_index_is_populated(rows)
    test_labels_carry_no_child_decoration(rows)
    test_every_row_has_a_section(rows)
    test_planar_rows_are_not_labelled_design(rows)
    test_no_two_results_look_identical(rows)
    test_every_registered_section_exists(rows)
    test_known_crumbs(rows)
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
