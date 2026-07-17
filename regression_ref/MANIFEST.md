# regression_ref/ manifest

Reference G-code files checked by `tools/check_regression.py`. If a reference
is ever regenerated on purpose (an intentional output change, not a bug),
update both the file and this manifest in the same commit, and explain why.

Regenerated 2026-07-18: the previous references predated the multi-printer
profile system (their header comment read `; Trident continuous non-planar
G-code`, i.e. `PrinterProfile.name == "Trident"`; the current default is
`"Voron Trident"`) and used a `SET_PRESSURE_ADVANCE` value sourced from an
OrcaSlicer install that's no longer the current default -- they were stale
snapshots from an earlier, undocumented version of the tool, not reproducible
invocations. None used `--filament`, deliberately: OrcaSlicer profile import
reads from `%APPDATA%`, which the regression suite must not depend on.

| File | Invocation |
|---|---|
| `ref_circle.gcode` | `python generate.py --out ref_circle.gcode` (all defaults) |
| `ref_star.gcode` | `python generate.py --shape star --out ref_star.gcode` |
| `ref_base_brim.gcode` | `python generate.py --base-layers 2 --brim 2 --out ref_base_brim.gcode` |
| `ref_textured.gcode` | `python generate.py --pattern ripple --pattern-amp 1.0 --pattern-waves 8 --out ref_textured.gcode` |
| `ref_profile_spiral.gcode` | Direct Python call (no CLI flag routes to `build_profile_spiral` -- it's only reachable via `serve.py`'s mesh-texture mode today). See `run_profile_spiral_case()` in `tools/check_regression.py` for the exact construction: `PrinterProfile()` defaults, `stack_from_shape(circle(30.0), 30.0, 60.0, 0.3, 240)`, `build_profile_spiral(..., z_amp=0.8, z_waves=4, base_layers=2)`. |

`test_circle.gcode` (byte-identical duplicate of `ref_circle.gcode`) and
`test_profile_spiral.gcode` (superseded by `ref_profile_spiral.gcode`) were
removed as part of this regeneration.
