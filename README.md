# Trident Custom Non-Planar G-code Generator

A standalone G-code generator + 3D viewer for exploring **continuous, non-planar
"thick" prints** on a Voron Trident — the kind of seamless, wavy, single-bead
sculptures in the style of [Alex Scott Creations](https://www.instagram.com/alexscottcreations/).

It does **not** post-process a slicer's output — it generates toolpaths directly
from parametric geometry (and, later, from meshes), so Z can vary continuously
along an XY path instead of being locked to flat layers.

## Why "continuous single-Z" and not independent tri-Z tilt?

On a stock Trident, the three Z steppers (`stepper_z/z1/z2`) move in **lockstep** —
they only move independently during `Z_TILT_ADJUST`. So we get the non-planar look
the *correct, safe way*: the whole bed rises/falls as a level unit while the nozzle
height is modulated continuously along the path. (True dynamic bed-tilt would need a
custom Klipper kinematics module and is deliberately out of scope for now.)

Everything is built around your actual machine limits from `printer.cfg`, the most
important being **`max_z_velocity: 25 mm/s`** — every move's feedrate is automatically
clamped so the Z axis is never commanded faster than that.

## Requirements

- Python 3.10+ (standard library only — **no pip installs needed** for generation)
- A modern browser for the viewer (loads Three.js from a CDN)

## Quick start

```bash
# Default: a wavy non-planar vase, ~60 mm tall
python generate.py --out output.gcode

# A taller twisting star column with bigger waves
python generate.py --shape star --radius 30 --height 80 \
    --z-amp 3 --z-waves 6 --z-twist 1.5 --out star.gcode
```

Then open `viewer/index.html` in a browser and **drag the `.gcode` file onto it**.
The path is colored low→high so you can see the non-planar Z modulation, and the
viewer reports the max Z-rate so you can confirm it stays under 25 mm/s.

## Tuned presets (best starting point)

Don't hand-tune flags for your first print — run the curated presets:

```bash
python presets.py        # writes 5 tuned, machine-safe files into examples/
```

| Preset | What it is | Z-rate |
|--------|-----------|--------|
| `01_gentle_wave_vase` | **Start here.** Round vase, soft 5-wave rim, no twist | 14.6 mm/s |
| `02_twisted_star` | 5-point star lobes spiral 1.5 turns up | 20.6 mm/s |
| `03_twisted_squircle` | Square column twisting 1 turn, bold 4-wave top | 19.1 mm/s |
| `04_thick_wave_pot` | Genuinely thick 1.2 mm bead — the chunky look | 16.4 mm/s |
| `05_aggressive_twist_star` | Show-off: 6-pt star, 3 twists, 8-wave rim, fast | 25.0 mm/s |

Each is tuned to fit the bed, stay under 160 mm, keep Z-rate ≤ 25 mm/s, and stay
inside the **probe keep-out** (z-amp ≤ 1.9 mm — collision re-measured at 2.0 mm
wave amplitude against the toolhead body/Omron probe behind the nozzle).
**For a first real print, use `01_gentle_wave_vase` in PLA** and watch the first
few layers. Then open the matching settings in `presets.py` and tweak from there.

## Printing from an STL mesh

Wrap a continuous non-planar spiral around any STL silhouette (vase-style — a
single outer wall per height). No numpy/trimesh needed; the STL loader and slicer
are pure Python.

```bash
# Make a couple of sample meshes to try
python tools/make_sample_meshes.py

# Wrap a continuous spiral around one, with non-planar waves
python generate.py --stl examples/star_prism.stl --z-amp 2 --z-waves 5 \
    --filament "R3D PETG" --out star_mesh.gcode
```

| Flag | Meaning |
|------|---------|
| `--stl PATH` | STL mesh to wrap (binary or ASCII; should be Z-up and roughly watertight) |
| `--stl-scale` | scale factor (use to fit the bed) |
| `--stl-points` | points sampled per layer around the contour |

The part is auto-centered on the bed by its bounding box. Each height is sliced to
its outer contour, resampled, seam-aligned to the layer below (verified to 0.000mm
wander), and printed as one unbroken spiral. `--z-amp` / `--z-waves` add the same
non-planar modulation as the parametric generator. Interior holes are ignored
(single-wall vase style). The same safety checks (footprint, top Z, Z-rate,
volumetric limit) all apply.

## Non-planar surface-following

Pave a curved surface with a continuous spiral whose Z rides the surface — a
conformal shell with no stair-stepping (the other half of the Alex Scott look).

```bash
# A domed shell
python generate.py --surface dome --radius 40 --surface-amp 15 --out dome.gcode

# Concentric ripples, 3 stacked shells for thickness (one continuous path)
python generate.py --surface ripple --radius 45 --surface-amp 6 \
    --surface-wavelength 18 --surface-shells 3 --out ripple.gcode

# Drape a conformal shell over the TOP surface of a real STL
python generate.py --surface stl --surface-stl model.stl --out drape.gcode
```

Surfaces: `dome`, `ripple`, `saddle`, `waves`, or `stl` (samples a mesh's top into
a height grid). `--surface-amp` sets height, `--surface-wavelength` the ripple/wave
period, `--surface-shells` stacks conformal layers (alternating out/in so the whole
thing stays one continuous bead). Verified: the dome shell matches the ideal
surface to within 0.004 mm. Steep surfaces are auto-slowed to honour the Z-rate
limit, like everything else.

## FullControl integration

[FullControl](https://fullcontrol.xyz) designs a print as a list of point-by-point
state-change steps — the same philosophy as this tool. Any shape you generate here
can be exported as a ready-to-run **FullControl Python script**:

```bash
python generate.py --shape star --xy-twist 2 --z-amp 3 --format fullcontrol --out star_fc.py
python generate.py --surface dome --radius 40 --format fullcontrol --out dome_fc.py
```

The script contains every move as `fc.Point` (with the Z-velocity and volumetric
clamps already baked into the feedrates), your `PRINT_START`/`PRINT_END` macros as
`fc.ManualGcode`, and both `fc.transform(..., 'gcode')` and `'plot'` calls. Two ways
to use it:

- **No install:** paste it into the in-browser editor at
  [fullcontrol.xyz](https://fullcontrol.xyz) — it runs there and gives you
  FullControl's G-code and Plotly 3D preview.
- **Local:** `pip install fullcontrol`, then `python star_fc.py`.

Going the other way is free too: any G-code FullControl produces drops straight
into this project's viewer. The export uses only verified FullControl API
(`fc.Point/Printer/Extruder/ExtrusionGeometry/Fan/ManualGcode`, `GcodeControls`
with `primer='no_primer'`, `relative_e=True`).

> For the canonical print on your Trident, `--format gcode` (the default) is still
> the cleanest path — it integrates your macros directly. The FullControl export is
> for living in the FullControl ecosystem / its preview.

### Importing an external design (FullControl or any slicer)

To bring a design *in* — e.g. a shared model from fullcontrol.xyz — export its
G-code from the web app (the **Download GCode** button), then:

```bash
python analyze.py the_design.gcode     # Trident safety report
```

`analyze.py` reports footprint, height, filament, **estimated print time**, peak
Z-rate, **peak Z-accel demand**, flow, and an **unsupported-extrusion** count, and
**flags anything that won't fit your bed or exceeds the machine limits** —
important because externally-generated G-code wasn't run through this tool's
clamps. Then drag the same file onto `viewer/index.html` for the full 3D preview +
playback. Exit code is `0` when the file is clean and `3` when any warning fires.

Three model-based checks, all driven from the parsed toolpath:

- **Estimated print time.** Each move's effective speed is the commanded feedrate
  clamped by `max_velocity`, a short-segment acceleration limit
  (`v <= sqrt(dist * max_accel)`), and the Z axis: the Z component can't exceed
  `max_z_velocity` nor accelerate over its own Z distance faster than `max_z_accel`
  (`v*zf <= sqrt(|dz| * max_z_accel)`, where `zf = |dz|/dist`). Time is
  `sum(dist / effective_speed)`, shown as e.g. `est. print time : 31m 42s`. It
  includes travels, so it can differ from the viewer's path-only estimate.
- **Z-accel demand.** The commanded Z velocity of consecutive moves is
  finite-differenced (`|vz2 - vz1| / dt`, `dt` = duration of the first move) to
  find how hard wave crests ask the Z axis to swing. The peak is reported against
  `max_z_accel` (500 mm/s^2 on this machine); a warning fires when it exceeds that
  by more than 20% — Klipper would throttle those moves (slower prints, possible
  blobbing).
- **Unsupported extrusion.** Every extruding segment above `z = 1.0 mm` is checked
  against a spatial hash of prior extrusion midpoints: it is *unsupported* if
  nothing was previously laid within 1.0 mm in XY and 0.2-2.0 mm below it (i.e. it
  is printing into mid-air). The count and percentage are reported, and a warning
  fires above 2% — conformal shells with no base flag heavily here; add a
  base/body under the design.
- **Probe keep-out (critical for non-planar!).** The Omron inductive probe rides
  at **nozzle + (3, 29) mm** (your `printer.cfg` `[probe]` offsets) with its body
  bottom only a few mm above the nozzle tip. Non-planar printing dives the nozzle
  into wave troughs while already-printed **crests pass under the probe** — a
  tall crest strikes the probe body. The analyzer simulates this exactly: for
  every extruding move it checks whether earlier-printed material within the
  probe's footprint rises above `nozzle_z + probe_clearance`, reporting the risky
  move count and worst violation depth.

  **Measure your real clearance** (default assumption 2.0 mm): bring the nozzle
  to Z=0 on the bed, then measure the gap under the probe body with feeler
  gauges. Check any file against your number:

  ```bash
  python analyze.py design.gcode --probe-clearance 2.5
  ```

  Rule of thumb for wave prints: **max z-amp <= clearance / 2** (crest-to-trough
  span is 2x the amplitude). At the machine's measured 3.8 mm clearance that
  means z-amp <= 1.9 mm; `examples/01b_probe_safe_vase.gcode` (z-amp 0.9) is
  pre-generated and verified 0 risky moves. Surface-mode domes are far over any
  realistic clearance — do not print them with the probe mounted. To get big
  amplitudes back: remount the probe higher, remove it for non-planar jobs, or
  switch to a dockable probe (Klicky-style).

## Using your OrcaSlicer filament settings

You don't have to re-enter filament settings — the generator can import them
straight from your OrcaSlicer profiles (it resolves Orca's multi-level
inheritance automatically):

```bash
# See what profiles are available
python -m trident_gcode.orca --list

# Inspect one (resolved through its whole inheritance chain)
python -m trident_gcode.orca "R3D PETG"

# Generate using that filament's temps / flow / volumetric limit
python generate.py --filament "R3D PETG" --shape star --xy-twist 2 --out star.gcode
```

`--filament` imports **nozzle + bed temps, flow ratio, and
`max_volumetric_speed`**, and feeds the temps into your `PRINT_START`. The
volumetric limit is enforced per move: e.g. R3D PETG caps at 17 mm³/s, so a thick
1.2 mm bead is automatically slowed to never exceed that — no more guessing
whether a thick continuous print will out-run the hotend.

## Texture patterns (OGcode-style, probe-safe)

Seven wall textures displace the **radius** instead of Z — they cost zero
Z-velocity budget and **cannot hit the probe**, so unlike the Z-waves (capped at
1.9 mm) these can be bold: `vwave` (vertical ridges), `hwave` (horizontal
rings), `ripple` (diagonal), `diamond`, `bubbles`, `pleats`, `hammered`.

```bash
python generate.py --pattern ripple --pattern-amp 1.2 --pattern-waves 14 \
    --pattern-bands 8 --z-amp 2 --filament "R3D PETG" --out textured.gcode
```

`--pattern-amp` = depth (mm), `--pattern-waves` = repeats around,
`--pattern-bands` = cycles over the height, `--pattern-twist` = rotation over
height, `--pattern-fade-in/out` = keep the base/rim clean. Textures combine
freely with Z-waves, twist, and the silhouette envelope. Practical texture-depth
limit is overhang, not the probe — the analyzer's unsupported check flags
excessive per-turn radial shifts.

## Design in the app

You don't have to touch the command line to make a vase. Run the bundled design
server and design one interactively in the viewer:

```bash
python serve.py
```

then open **http://localhost:8777/viewer/index.html**. `serve.py` is pure
standard library — it serves the project statically on port 8777 and adds a tiny
JSON API (`GET /api/filaments`, `POST /api/generate`).

A **Design a vase** panel sits at the top of the sidebar. Pick a shape
(circle / star / square), set radius, height, layer height, waves, twist, base
layers, brim, squish and print speed, and choose a filament (populated from your
OrcaSlicer profiles). Then shape the print with **two draggable curve editors**,
each plotting a value from the bottom of the print (left) to the top (right):

- **Wave amplitude over height** — how strong the non-planar wave texture is at
  each height (mm). Drag the six control points up/down. A red dashed line marks
  the **1.9 mm probe limit**; the amplitude defaults to 0 at the very bottom so
  the first layers stay flat and adhere.
- **Silhouette (radius scale)** — a multiplier on the radius at each height, for
  bulges and tapers. `1.0` (the dashed reference) is the plain outline; push a
  point above 1.0 to bulge, below to pinch.

Click **Generate & preview**: the design is generated through the same
machine-safe pipeline as the CLI (`GcodeWriter` + `analyze_gcode`), loaded
straight into the 3D viewer with full playback and telemetry, and the Trident
safety report is shown beneath the button. A **Download .gcode** button saves the
result.

**The 1.9 mm probe ceiling is enforced, not just suggested.** The wave amplitude
is clamped to `[0, 1.9] mm` both in the curve editor and again on the server, so
even a hand-crafted request can't ask for a print tall enough to strike the
toolhead body/Omron probe behind the nozzle (crest-to-trough span is 2x the
amplitude; 1.9 mm keeps a safety margin under the 3.8 mm measured clearance).
Radius scale is clamped to `[0.2, 1.5]`. Requests that wouldn't fit the bed or
exceed Z max are rejected with a clear error in the status line.

## The printer view

`viewer/index.html` draws a recognisable Voron Trident — aluminium frame, bed
plate, and gantry beam — sized from your build volume, so you can see your part
sitting in the machine the way OrcaSlicer shows it. Toggle the frame, bed grid,
height-coloring, travels, and auto-spin from the panel.

**Print-process playback:** a timeline bar at the bottom replays the print the way
it's actually laid down — drag the slider to scrub, or hit play to watch the path
draw in and a nozzle marker travel along the toolpath. The readout shows the
current Z height, layer/turn number, and percentage; a speed selector (0.5×–4×)
controls playback. Keyboard: **space** = play/pause, **← →** step one spiral turn
at a time, **Home/End** = jump to start/finish. This is the layer-by-layer /
nozzle-movement preview.

**Live telemetry:** a panel updates as you scrub/play to show the values *at the
playhead* — print speed, **volumetric flow rate** (flagged ⚠ if it exceeds the
~17 mm³/s melt ceiling), Z height, layer height, and line width (read from the
G-code header). This is the quickest way to sanity-check a thick print before
running it.

**Seeing individual layers:** at sub-millimetre layer heights the spiral turns sit
so close they can merge into a solid skin. Two controls fix this: **Emphasize
layers** shades alternating turns light/dark so each layer reads as a distinct rib,
and the **Line thickness** slider sets the rendered line width — *thinner* shows
more separation on dense prints, thicker is bolder for sparse ones. Zooming in also
separates the turns.

## Key options (`python generate.py --help`)

| Flag | Meaning |
|------|---------|
| `--shape` | `circle`, `star`, or `square` base outline |
| `--radius` / `--height` | base radius and total height (mm) |
| `--layer-height` | spiral pitch — Z rise per turn (mm) |
| `--line-width` | bead width; go wide (1–2 mm) for genuinely *thick* continuous prints |
| `--xy-twist` | rotate the cross-section over height (turns) — twisted column |
| `--z-amp` | non-planar wave amplitude (mm); `0` = flat top |
| `--z-waves` | number of waves around the perimeter |
| `--z-twist` | how much the wave pattern rotates over the height (turns) |
| `--print-speed` / `--first-layer-speed` | speeds in mm/s (auto-clamped for Z) |
| `--first-layer-squish` | print the first layer at `FRAC x layer_height` off the bed while extruding full-height volume (presses plastic in). `1.0` = off (default), `0.75` recommended |
| `--first-layer-flow` | extra flow multiplier over the first turn for adhesion (default `1.1`; only applies when the first-layer package is active) |
| `--base-layers` | stacked solid base disks under the wall, one continuous bead (default `0`) |
| `--brim` | outward brim loops beyond the outline, printed first (default `0`) |
| `--fan PCT` | part-cooling fan speed override (0-100 percent; 100 = full). Default: full speed after the first layer |
| `--no-fan` | force fan off for the entire print (overrides `--fan` and filament profile) |
| `--config FILE.json` | load default settings from a JSON config file (explicit CLI flags still win) |
| `--save-config FILE.json` | write all fully-resolved settings to a JSON config, then continue generating |

## Fan and retraction

The generator now wires fan, retraction, and pressure-advance settings from your
OrcaSlicer profile straight into the emitted G-code:

- The part-cooling fan is **off during the first turn** (`M107` in the header) and
  turned on at full filament-profile speed after the first layer completes.
- If `--filament` imports a profile that has a `pressure_advance` value, a
  `SET_PRESSURE_ADVANCE ADVANCE=<value>` line is emitted right after `G92 E0`.
- Retraction length (and, for imported profiles, the profile's value) is used for
  every retract/unretract at the start and end of the print.
- `--fan PCT` overrides the fan speed (0-100); `--no-fan` forces it off entirely
  (useful for, e.g., ABS with an enclosure).

## Config files

Save and reuse any set of generate.py settings as a JSON file:

```bash
# Save a config while generating
python generate.py --shape star --z-amp 3 --save-config my_star.json --out star.gcode

# Reproduce exactly (byte-identical output)
python generate.py --config my_star.json --out star2.gcode

# Override a saved setting on the fly
python generate.py --config my_star.json --z-amp 5 --out star_big.gcode
```

The JSON uses the same key names as the CLI flags (hyphens become underscores,
no leading dashes). An optional `"machine"` object can override `PrinterProfile`
fields (e.g. `"max_z_velocity"`, `"bed_size_x"`). Unknown keys cause a clear
error listing them and exit 1.

### Gap-aware extrusion (variable local layer height)

With `--z-twist` / `--z-amp`, the true vertical gap between the bead and the turn
directly below it varies around each loop. The generator now extrudes for that
**local** layer height (the analytic gap between a point and the point one turn
below), so you get even walls instead of systematic under/over-extrusion where the
geometry is most interesting. The extra flow is folded into the volumetric-flow
speed cap too, so a thick wavy bead is still auto-slowed under the melt limit. If a
local layer height ever falls outside `[0.25, 1.5]x` nominal it is clamped and a
`; WARNING:` line is written into the G-code (and printed by `generate.py`). With
`--z-amp 0 --z-twist 0` the output is byte-for-byte identical to before, so flat
prints are unaffected.

## Safety model

The generator refuses to emit unsafe motion:

- Footprint must fit the **safe print area** (X 30–208, Y 30–185 — your bed-mesh range).
- Top Z must be ≤ 160 mm.
- No extruding move may go **below the bed** (wave amplitude ramps in from the base so
  the first layers stay planar and adhere).
- Every feedrate is clamped so Z velocity ≤ `max_z_velocity`.

It calls your existing `PRINT_START` / `PRINT_END` macros and emits relative
extrusion (`M83`), matching your `printer.cfg`.

## First-layer adhesion

Borrowing FullControl's first-layer design patterns, `--first-layer-squish FRAC`
prints the very first layer at `FRAC x layer_height` off the bed (so the nozzle
squishes the bead into the plate) while extruding the **full** nominal layer
height's volume — the standard squish trick that presses plastic down for adhesion
without starving the line. It also lays a **flat priming loop** (one full turn of
the base outline at the squish height) before the spiral begins, and scales flow
over the first turn by `--first-layer-flow` (default `1.1`) at the first-layer
speed. From the second turn on, flow returns to nominal automatically. The whole
package is off by default (`--first-layer-squish 1.0`); `0.75` is a good starting
value.

```bash
python generate.py --first-layer-squish 0.75 --out vase.gcode
```

## Solid base and brim

For a genuinely stuck-down part you can pave a **solid bottom** and a **brim**, all
as one continuous bead with no travel moves anywhere in the printed body:

```bash
python generate.py --base-layers 2 --brim 3 --first-layer-squish 0.75 --out pot.gcode
```

- `--base-layers N` fills the bottom with `N` stacked disks. Each disk is an
  Archimedean spiral *warped to the part outline* (`r(theta)*s`, `s` sweeping
  0->1), with successive loops spaced one line width apart. Disks alternate
  in->out / out->in so the nozzle never lifts between them, and the last pass ends
  exactly on the outline seam where the wall spiral begins.
- `--brim N` adds `N` outward loops beyond the outline at the base height, printed
  **first**; the brim spirals inward and hands straight off to the base fill, which
  fills inward and back out, which hands off to the wall. Brim -> base -> wall is a
  single unbroken path. The footprint check accounts for the brim's extra width.

Both work for the parametric shapes and for STL-wrapped meshes (the mesh's bottom
contour is treated as the outline). Preset `01_gentle_wave_vase` now ships with a
2-layer solid base for a rock-solid first print.

> ⚠️ **Always preview in the viewer first**, and for the first real print keep the
> amplitude modest and watch the first few layers. These are unusual toolpaths.

## Calibration suite

Three small, fast prints that help you dial in the key process variables before
committing to a long decorative print.  All use PLA temps (205 C / 60 C bed):

```bash
python calibrate.py     # writes into examples/cal/
```

| File | What it prints | Dial-in target |
|------|---------------|----------------|
| `cal_first_layer.gcode` | 40 mm spiral disk, 1 layer, squish 0.75 | Live-Z offset |
| `cal_flow_ladder.gcode` | Single-wall cylinder r=25, 30 mm tall, 5 x 6 mm flow bands (0.90 / 0.95 / 1.00 / 1.05 / 1.10) | Flow multiplier (`--flow`) |
| `cal_zamp_ladder.gcode` | Single-wall cylinder r=25, 36 mm tall, 4 x 9 mm Z-amp bands (0 / 0.8 / 1.4 / 1.9 mm) | Maximum safe `--z-amp` |

Each file is checked by `analyze.py` (exit 0) and takes under 20 minutes.  A
`; CAL BAND flow=X.XX` or `; CAL BAND z_amp=X.Xmm` comment is written at the
start of each band so you can visually match the print to the G-code.

## Project layout

```
generate.py                     CLI entry point
calibrate.py                    calibration print suite (live-Z / flow / z-amp)
presets.py                      curated, machine-safe presets
tools/make_sample_meshes.py     writes sample STLs into examples/
trident_gcode/
  profile.py                    machine + material limits (from printer.cfg)
  paths.py                      2D base shapes + continuous spiral geometry
  extrusion.py                  volumetric E math + Z feedrate clamping
  gcode.py                      Klipper-flavoured G-code emitter + safety checks
  config.py                     JSON config file load / save / validation
  mesh.py                       pure-Python STL load + horizontal slicing
  surface.py                    non-planar height fields (+ STL top sampling)
  orca.py                       OrcaSlicer filament profile import
  fullcontrol_export.py         export any toolpath as a FullControl script
  generators/
    continuous_spiral.py        parametric spiral -> bed-placed G-code
    mesh_spiral.py              wrap a spiral around an STL silhouette
    surface_spiral.py           conformal shell following a surface
    base_fill.py                solid base disk + brim as one continuous bead
viewer/index.html               drag-drop Three.js viewer + playback
examples/cal/                   calibration G-code files (generated by calibrate.py)
```

## Roadmap

- [x] Mesh import (STL) → cross-section → continuous spiral wrapping
- [x] Non-planar *surface following* (conformal shell over a curved surface / STL top)
- [x] Variable (gap-aware) layer height along the path
- [x] Wire Orca filament fields (fan, retraction, pressure advance) into emission
- [x] Config/profile system (`--config` / `--save-config`)
- [x] Calibration suite (`calibrate.py`) — live-Z disk, flow ladder, Z-amp ladder
- [ ] Variable line width along the path
- [ ] Feasibility study + prototype for true dynamic tri-Z bed tilt
