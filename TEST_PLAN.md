# Test plan

This is not an index of test files -- README's "Tests" section already lists
every `tools/test_*.py` script and what it covers. This document exists
because that index kept passing (1,500+ checks, all green) while real bugs
shipped to the public site anyway. It answers a different question: **what
does passing the suite NOT tell you, and what do you have to check by hand
because nothing automated will?**

Update this file whenever a bug reaches production (or a live-tested build)
that the existing suite did not catch. The "Incident log" at the bottom is
the point of the whole document -- if it stops growing, either bugs have
stopped slipping through, or nobody is writing them down. Assume the latter
and check.

## What each layer actually proves

| Layer | Proves | Does NOT prove |
|---|---|---|
| `tools/test_*.py` (unit) | One function, one module, with the Orca subprocess mocked away | That two features behave correctly *combined*, or that the server and viewer agree on anything |
| `tools/check_regression.py` | Output is byte-identical to a known-good reference | That the reference was ever correct, or that a *new* code path (nothing to regress against yet) works at all |
| `tools/test_phase_integration.py` | A real server, driven over real HTTP, with several features combined in one request | Anything about the browser UI -- it never touches `viewer/*.js` |
| `viewer/dev_smoke.html?selftest=1` | Viewer logic in a real browser: panel visibility rules, draft-preview math, severity/jump wiring | Nothing about the deployed server's behavior, and it is **not hermetic** (see CLAUDE.md) -- a failure here can be your own leftover `localStorage`, not a real bug |
| Manual pass on `http://localhost:8777` | The local server and the local viewer agree, right now, on your machine | Nothing about what is actually deployed |
| Manual pass on the live site (trident.mmucybertron.com) | What a real visitor actually gets, on the actual deployed build, with a cold browser profile | Nothing you didn't think to click -- this layer only catches what you go looking for |

The gap between "1,500 checks pass" and "the live site works" lives entirely
in the last two rows. Both bugs in the incident log's most recent entries
were invisible to every layer above the live-site pass.

## Failure patterns this project keeps having

Naming a recurring shape makes it recognizable the next time, rather than
each instance looking like a one-off. Five keep coming back:

1. **A control looks global but is silently scoped.** A checkbox, a mode
   radio, a field on the main panel -- it reads as "this affects the whole
   design," but one mode/combination quietly narrows what it does, with no
   signal anywhere that it happened. (Zone overrides ignored under loop
   fabric; radius-speed comp ignored for a mesh; the Height field doing
   nothing in Texture mode.) The fix is never just "make it work everywhere"
   -- often it structurally can't (a mesh-derived contour has no nominal
   radius to scale speed against). The fix is **telling the user**, in the
   report AND in the panel before they ever click Generate.

2. **State lives in N places; teardown clears fewer than N.** A mesh upload
   is a client `meshState` object, an IndexedDB record, a 3D object on the
   bed, and a server-side per-session cache entry. A "clear" or "reset"
   action that only touches the place it was first written against looks
   complete in review -- nothing forces a diff to check the others. When you
   add a second place a piece of state lives, immediately go find every
   place that already "clears" the first place and check whether it still
   clears all of them.

3. **A warning is computed but never reaches the user.** `issues_extra` used
   to be folded into the JSON `issues` array (so a check counting entries in
   `result["issues"]` would go green) but never into the human-readable
   `report_text` the viewer actually displays. Any test that asserts against
   the structured array without ALSO asserting the same string is a substring
   of the rendered report can pass while the feature is invisible to a real
   user. `test_report_extra_issues.py` exists specifically to keep both
   checked together.

4. **A test outlives the assumption it was written against.** Six
   `test_mesh_hybrid.py` cases pinned exact geometry from before a seam-angle
   rotation was added; the tests were never wrong about what the *old* code
   did, they just never got re-derived against what the *new* code does, and
   nothing forced that re-check. When behavior underneath a passing test
   changes on purpose, re-derive the test's expected values from the new
   code path explicitly -- don't just leave the old assertion green by
   coincidence or update it without checking why it needed updating.

5. **A control is gated on the wrong condition.** Two mutually exclusive
   modes both make some OTHER control dead (mesh-as-planar-base and
   mesh-as-texture both make the parametric hybrid-base fields meaningless,
   for different reasons), but the gate that hides/disables the dead control
   only checks ONE of the two conditions -- usually because it was written
   for the first mode that needed it, and the second mode's version of the
   same bug was never re-derived, just assumed covered. Grep for every place
   a sibling condition is checked (`meshBaseActive` in one place,
   `meshLoaded` in another) whenever you add a new "this makes that field
   dead" rule, rather than trusting the first one you find.

## Before shipping: scenario checklist

Sweeping `tools/test_*.py` is necessary and is already documented in
CLAUDE.md. It is not sufficient for anything that touches session/UI state
or a mode with special-cased behavior. For those, walk the actual scenario
by hand -- locally at minimum, on the live site before telling the user a
fix is confirmed.

**Any change touching mesh upload, mesh state, or session restore:**
- [ ] Upload a mesh, reload the page (no other action) -- it must restore.
- [ ] Upload a mesh, immediately (sub-50ms) trigger whatever reset/clear
      action exists, before any async restore/upload could have finished --
      the mesh must NOT reappear. This is the race that hid the
      "Start new design" bug: waiting a comfortable few seconds before
      testing made it look fixed when it wasn't.
- [ ] After a reset/clear, check EVERY place the mesh could still be
      referenced: in-memory state, IndexedDB, the visible 3D bed, the panel
      text (filename/height/triangle count), and the next Generate request
      body (it must not send a stale `mesh_id`).
- [ ] Two browsers/sessions uploading different meshes must not evict or
      cross-contaminate each other.

**Any change to a mode with scoped/special-cased behavior (mesh texture,
loop fabric, hybrid planar base, zone overrides, point edits):**
- [ ] For every field that mode ignores or treats differently, confirm there
      is a signal BOTH before Generate (a dimmed row, a hint, a disabled
      control) AND after Generate (a report line, not just a JSON field
      nobody reads).
- [ ] Switch INTO and OUT OF the mode with a value already sitting in the
      now-irrelevant field, and confirm the signal appears/disappears
      correctly rather than needing a page reload to catch up.
- [ ] Generate once in the mode and check the actual numeric result (height,
      footprint, whatever the field claimed to control) against what the
      field's value would predict -- don't just check that *a* file came
      back.

**Any change to warnings/issues:**
- [ ] The new message must appear as a substring of `report_text`, not just
      the structured `issues`/`issues_detail` arrays -- write the assertion
      against the rendered report the way `test_report_extra_issues.py` does.
- [ ] Check `_ISSUE_RULES` classifies it (severity + jump target) rather than
      falling back silently -- `test_issue_severity.py`'s
      `test_every_control_id_in_the_rules_exists_in_the_ui` will catch a
      dangling control id, but only if the rule was added at all.
- [ ] Add the "teeth" case: a request where the message must NOT fire (e.g.
      the field already matches, or the mode wasn't active), so the check
      can't pass by matching everything.

**Any change spanning the server AND the viewer (most features do):**
- [ ] Extend `test_phase_integration.py` rather than trusting two green
      unit-test files to imply the combination works -- this repo has
      shipped "both units pass, the combination is broken" at least three
      times (see incident log).
- [ ] Run `dev_smoke.html?selftest=1` against a clean profile (private
      window, or clear site data first) before trusting a failure count --
      it is not hermetic and inherited `localStorage` changes results.

**Before telling the user a live-site fix is confirmed:**
- [ ] Fetch the deployed `viewer/designer.js` (or relevant file) and grep for
      a string unique to your fix, to prove the deploy actually shipped
      before testing against it -- "Render - Application loading" tabs and
      CDN/edge caches make it easy to test stale code and believe it passed.
- [ ] Reproduce the ORIGINAL failure on the live site first if you can (not
      just the fix) -- confirming a fix without ever having reproduced the
      break leaves open the possibility you tested the wrong thing.
- [ ] Race-test timing-sensitive fixes at the actual speed a fast user
      would trigger them (see the mesh-state checklist above), not a
      comfortable multi-second gap.

## Incident log

Each row is a real bug this project shipped (or nearly shipped) that the
suite at the time did not catch, what pattern above it matches, and what now
guards against it. Newest first.

| Symptom | Pattern | Now guarded by |
|---|---|---|
| A hybrid planar base (`hybrid_base_height`) typed in while the mesh was used to Texture the whole model produced no base and no explanation ("the planar base is missing") -- the field looked live because its hide condition only checked the OTHER mesh usage (planar-base mode) | #5 gated on the wrong condition, found immediately after "fixing" the sibling #1 bug above -- same root cause, different field | `refreshShapeRows()`'s hide condition widened from `meshBaseActive` to `meshLoaded`; matching `buildGenerateBody()` send-guard; server NOTE + `_ISSUE_RULES` entry as defense in depth; `test_mesh_texture_hybrid_base_height_ignored_scope_reaches_report_text` + its teeth twin |
| "Texture the whole model" printed only the mesh's own height with the Height field showing a different number and no explanation -- looked like "just a small base" | #1 silently scoped control | Server NOTE comparing requested vs. actual height (`generate_mesh_texture_design`); `#row-height` dims + hint the instant Texture mode is picked; `test_mesh_texture_height_ignored_scope_reaches_report_text` + its teeth twin |
| "Start new design" reset every design field but left the previous mesh in memory, in IndexedDB, and on the 3D bed; a fast click could even race the page-load mesh restore and put it back after the reset | #2 incomplete teardown | Single `clearMeshEverywhere()` used by both "Clear mesh" and "Start new design"; `meshEpoch` counter guards the restore-vs-clear race; dev_smoke assertions on `__clearMeshEverywhere` |
| A DANGER-severity result (e.g. probe collision risk) still auto-switched the user to the G-code viewer, navigating away from the one warning row that mattered | New pattern: navigation logic decided from a source the render path didn't use | `__shouldSwitchToViewer` extracted and exposed so the actual branch is pinned, not just `__worstSeverity`'s ranking |
| "Show setting" on curve-editor warnings (e.g. amplitude) did nothing -- `#amp-curve` was never in the UI's own control-search index | New pattern: two features (search index, jump-to-control) silently assumed the same coverage without a shared source of truth | `window.__revealControl` given a fallback path for non-indexed controls |
| A wave-slope warning's "Show setting" jumped to the mesh-blend field -- two different messages shared the substring "exceeds this printer's printable" | New pattern: substring-matching classification is only as good as its ordering | More specific discriminators moved ahead of the shared phrase in `_ISSUE_RULES`; `test_shared_phrase_messages_point_at_their_own_control` |
| Mesh restored into `dev_smoke.html`'s own test iframe on every load, silently changing which rows the suite's baseline expected to be visible | Test-infra pollution: a real feature interacting badly with the harness's own assumptions | `if(window.top === window.self)` guard before the restore call |
| Seam blend applied after the envelope/cage instead of before, and a `blendHeight<=0` gate skipped the mesh ring entirely at a hard seam | Not caught by any test -- found by manual review, not automated | Coverage added to `test_mesh_hybrid.py` after the fact |
| Draft preview showed yellow dots instead of the real texture whenever a mesh was loaded, even when the mesh was unrelated to the loop-fabric feature that broke | #1 silently scoped control, inverted -- a check disqualified too broadly, not too narrowly | `loopFabricActive()` narrowed to check `mesh_base_mode` specifically |
| Six `test_mesh_hybrid.py` cases failed at HEAD -- pinned geometry from before a seam-angle rotation (commit `d2300c8`) was added | #4 test outlived its assumption | Reference geometry re-derived by tracing the real `seam_k` through `_nearest_ring_index` |
| Scope/advisory warnings (`issues_extra`) reached the JSON `issues` array but never the rendered `report_text` the viewer actually shows -- "the planar base doesn't make it into the slicing" was really this | #3 warning computed, never shown | `_append_extra_issues`; `test_report_extra_issues.py` asserts against `report_text`, not just `issues` |
