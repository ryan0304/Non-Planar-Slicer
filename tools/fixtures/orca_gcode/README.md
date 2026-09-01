# orca_gcode_parser.py fixtures

`sample_base.gcode` is a small, realistic two-layer square base (M83
relative extrusion, a perimeter, a short zig-zag infill, a retract/travel
between islands, a `G92 E0` reset) -- body only, no Orca machine
start/end G-code, matching what `orca_slice.py` will configure Orca to
emit. It also stands in for "whatever Orca would have produced" in the
`check_regression.py` hybrid-stitch case, so that suite never needs a live
OrcaSlicer install to stay byte-stable.

The rest are adversarial, one deliberate counter-example each, mirroring
`tools/fixtures/printers/hostile.cfg`'s philosophy -- every new parser rule
needs a fixture that proves it actually fires:

- `sample_base_m82.gcode` -- same shape, `M82` (absolute extrusion) instead
  of `M83`. Proves the parser normalizes absolute-mode E into per-move
  relative deltas rather than replaying Orca's raw absolute values.
- `sample_base_g91.gcode` -- a stray `G91` right after the opening `G92
  E0`. Proves relative positioning is hard-rejected; this app's convention
  is absolute XYZ throughout and nothing downstream has been reviewed
  against relative coordinates.
- `sample_base_arc.gcode` -- a `G2` arc move planted in the middle of an
  otherwise-clean perimeter. Proves arc moves are rejected even though
  `orca_slice.py::build_machine_json` already asks Orca to disable arc
  fitting -- this is the defense-in-depth check for when that setting
  doesn't take effect.
- `sample_base_nan.gcode` -- one line reading `... Enan F1200`. Proves
  `float("nan")` does not sail through: it parses successfully in Python
  but must never reach a comparison, since every comparison against NaN is
  False. This fixture recreates, in G-code form, the exact
  `json.loads`-accepts-`NaN`/`Infinity` trap CLAUDE.md documents for the
  JSON request boundary.
- `sample_base_unknown.gcode` -- an `M600` (filament change) planted
  mid-body. Proves the explicit command allow-list rejects anything not
  specifically vetted, rather than silently skipping an unrecognized line.
