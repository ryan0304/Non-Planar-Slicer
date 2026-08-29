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
| `ref_loop_fabric.gcode` | Direct Python call (no CLI flag routes to `build_loop_fabric`). See `run_loop_fabric_case()` in `tools/check_regression.py`: `PrinterProfile()` defaults, `LoopSpec(loops_per_turn=24, row_mm=0.5, up_mm=0.8, stitch_mode="dip")`, `build_loop_fabric(..., shape=star(30.0, 5, 0.3), height=30.0, xy_twist_turns=0.0)`. Generated from PRE-change code (before the 2026-08-25 `xy_twist_turns` addition to `build_loop_fabric`), specifically to prove the twist formula (`theta - xy_twist_turns * 2.0 * math.pi * t`) is a bit-exact no-op at `xy_twist_turns=0.0` -- the post-change code was verified to reproduce this file byte-for-byte before `ref_loop_fabric_twist.gcode` below was generated. |
| `ref_loop_fabric_twist.gcode` | Same construction as `ref_loop_fabric.gcode` but `xy_twist_turns=1.5`. Generated from POST-change code; locks the twist formula and its sign (lobes advance counter-clockwise with height for positive twist, matching `spiral_path()` in `trident_gcode/paths.py`). |
| `ref_zone_overrides.gcode` | Added 2026-08-27 for the Zone Overrides feature (`SpiralSpec.zones`, `trident_gcode/paths.py`). No CLI flag routes to it. See `run_zone_override_case()` in `tools/check_regression.py`: `PrinterProfile()` defaults, `SpiralSpec(base_radius=30.0, height=30.0, layer_height=0.3, points_per_turn=240, xy_twist_turns=0.0, r_pattern="vwave", r_amp=1.0, zones=[ZoneOverride(t_lo=0.35, t_hi=0.70, blend=0.02, r_pattern="diamond", r_amp=2.0, xy_twist_turns=1.0)])`. The same function also asserts, in memory, that `zones=None` and `zones=[]` produce byte-identical `.text()` output -- proof the feature is a bit-exact no-op when unused, without a second reference file. |
| `ref_zone_overlap.gcode` | Added 2026-08-27 for Zone Overrides v2 (overlapping zones + per-zone `pattern_twist`/`r_twist_turns`, `spiral_path()`'s weighted-normalization crossfade). No CLI flag routes to it. See `run_zone_overlap_case()` in `tools/check_regression.py`: same `PrinterProfile()`/writer defaults as `ref_zone_overrides.gcode`, but with TWO overlapping zones sharing the band `0.45-0.60`: `ZoneOverride(t_lo=0.25, t_hi=0.60, blend=0.05, r_pattern="diamond", r_amp=3.0)` and `ZoneOverride(t_lo=0.45, t_hi=0.80, blend=0.05, r_pattern="pleats", r_amp=3.0, r_twist_turns=1.5)`. Locks the `max(1, sum-of-weights)` normalization formula and the per-zone texture-twist offset in the overlap region. `ref_zone_overrides.gcode` above (non-overlapping) is the proof this normalization is a byte-exact no-op when zones don't overlap -- it was NOT regenerated when overlap support was added. |

`test_circle.gcode` (byte-identical duplicate of `ref_circle.gcode`) and
`test_profile_spiral.gcode` (superseded by `ref_profile_spiral.gcode`) were
removed as part of this regeneration.
