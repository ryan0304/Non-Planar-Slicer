# Trident Non-Planar G-code Generator & Viewer

A standalone G-code generator + 3D viewer for exploring **continuous, non-planar
"thick" prints** on a Voron Trident — the kind of seamless, wavy, single-bead
sculptures in the style of [Alex Scott Creations](https://www.instagram.com/alexscottcreations/).

It does **not** post-process a slicer's output — it generates toolpaths directly
from parametric geometry (and, later, from meshes), so Z can vary continuously
along an XY path instead of being locked to flat layers.

> ### Read before you print
>
> **This output has only ever been verified on one machine: the author's Voron
> Trident.** Everything here is built to refuse unsafe motion, and those guards
> are real — but "it passed the validator" is not "it was tested on your
> printer". Nobody has run this on yours.
>
> Non-planar printing moves the nozzle close to parts of the bed a normal slicer
> never approaches, so the failure mode is a nozzle or probe striking the plate,
> not a stringy print. Two consequences worth taking literally:
>
> - **Read the G-code, or at least dry-run it with the extruder cold and the Z
>   offset raised, before you print for real.**
> - **The limits are only as good as the profile they come from.** Import your
>   own `printer.cfg` rather than running someone else's profile, and check
>   every value in the review dialog — the parser is paranoid about what it
>   reads, but it cannot know what your machine actually is.
>
> Specific example of why this matters: `z_amp_max` on the Trident profile is
> `0.95 mm`, and that number is not a specification. It was revised **down,
> empirically, after a probe strike**. It describes one machine's probe
> clearance and says nothing about yours.
>
> If you are using a **hosted instance** rather than running it yourself, see
> [Running it for other people](#running-it-for-other-people).

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

**Picking up where you left off:** the design you were working on is kept in the
browser's local storage, so it survives a reload or a closed tab. On load, if
that stored design differs from a fresh one, the app asks rather than silently
reinstating it — **Continue previous session** restores it, **Start new design**
resets to defaults. Coming back days later and unknowingly inheriting a
half-finished vase is a good way to generate G-code you didn't mean to, so the
choice is explicit. Two things "Start new" deliberately does *not* touch: your
imported printers (a `.cfg` you had to find and import is an asset, not session
scratch) and the printer you have selected — silently putting a Bambu user back
on the default Trident would hand them a design carrying another machine's
limits. The reset also goes on the undo stack, so **Ctrl+Z** brings the old
design straight back. Nothing is asked on a first visit, or when the stored
design is identical to a fresh one.

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

Every change is autosaved to the browser (`localStorage`) and undoable --
**Ctrl+Z** / **Ctrl+Shift+Z** (or the undo/redo buttons) step back and forward
through up to 50 recent edits, and **Save design** / **Load design** export
or import the full parameter set as JSON so a design survives a browser
restart or moves between machines.

**The 1.9 mm probe ceiling is enforced, not just suggested.** The wave amplitude
is clamped to `[0, 1.9] mm` both in the curve editor and again on the server, so
even a hand-crafted request can't ask for a print tall enough to strike the
toolhead body/Omron probe behind the nozzle (crest-to-trough span is 2x the
amplitude; 1.9 mm keeps a safety margin under the 3.8 mm measured clearance).
Radius scale is clamped to `[0.2, 1.5]`. Requests that wouldn't fit the bed or
exceed Z max are rejected with a clear error in the status line.

## Asymmetric shaping (app only)

Beyond the symmetric wave/silhouette curves, the app has a **3D control cage**
for asymmetric deformation: switch the silhouette editor to **Freeform (3D)**
mode and drag any of a 5-row x 8-column grid of handles directly in the 3D
view to bulge or pinch specific angular regions at specific heights (each
handle scales local radius, clamped to `[0.5, 1.5]x`). Combine with a **spine
offset** (`spine_mm` / `spine_deg` -- leans the whole silhouette off-axis by a
distance and direction) and **ovality** (elongates the cross-section along one
axis) for organic, non-radially-symmetric forms. All three are app-only today
(no `generate.py` CLI flags yet) and flow through the same safety-clamped
pipeline as everything else. Cage deformation does not apply to the loop
fabric pattern (see below) since it isn't a radius-displacement texture.

## Blob and loop-fabric textures (app only)

Two more wall textures, alongside the seven radius-displacement patterns
(`trident_gcode/blobs.py`, `trident_gcode/generators/loop_fabric.py`):

- **Blobs** -- discrete raised bumps placed at intervals around and up the
  wall, with named presets (`dots`, `pearls`, `columns`, `spikes`, `organic`)
  controlling spacing, jitter, and per-height volume envelope. Good for
  textured/tactile surfaces that read as deliberate bumps rather than a
  continuous wave.
- **Loop fabric** -- a knitted/chainmail-style wall built from small looped
  excursions rather than a plain spiral pass, with presets (`tiedspikes`,
  `chainmail`, `fineknit`, `opennet`, `ribs`, `zigzag`, `scallops`). This
  replaces the normal wall generator entirely for that print (it routes
  through `build_loop_fabric` instead of `build_continuous_spiral`), so it
  doesn't combine with the Z-wave/silhouette-cage controls the way the other
  patterns do.

Both are configured and previewed in the app (`serve.py` + the designer's
Texture step); like the cage, they don't yet have `generate.py` CLI flags.

## Multi-printer profiles

`GET /api/printers` lists every configured `PrinterProfile` (bed size, Z max,
Z-amplitude ceiling) and the app's printer dropdown lets you generate against
any of them, not just the default Voron Trident -- useful if you're
prototyping settings for a second machine. Each profile carries its own
safety limits (footprint, Z-rate, probe keep-out where applicable), enforced
the same way regardless of which one is selected.

Besides the Voron Trident and five Bambu Lab machines (A1, A1 Mini, P1S, P2S,
X1C), nine stock Creality
printers are built in: Ender 3 / Pro, Ender 3 V2, Ender 3 S1 / S1 Pro, Ender 3
V3 SE, Ender 3 V3 KE, Ender 5 Plus, CR-10, K1 / K1C, and K1 Max. **Read this
before picking one for a non-planar print**: stock Marlin Creality firmware
caps Z feedrate at ~5 mm/s (`DEFAULT_MAX_FEEDRATE { 500, 500, 5, 25 }`) --
five times slower than the Trident's 25 mm/s -- because this app's entire
premise is non-planar Z modulation, that one number changes the character of
every print on these machines: feedrates get clamped hard and estimated
print times run substantially longer than the same design on a Trident or a
Bambu machine. The built-in profiles use the real stock figure, not a
rounded-up "looks faster" number, so the time estimate you see is honest. If
your Ender 3 is flashed to Klipper with higher limits, raise `max_z_velocity`
for it in the review dialog after importing your own `printer.cfg`. The three
Klipper-firmware models (Ender 3 V3 KE, K1 / K1C, K1 Max) call the stock
`START_PRINT` / `END_PRINT` macros the way the Trident profile calls
`PRINT_START` / `PRINT_END`.

**On the Bambu profiles specifically**: build volume, temperatures and nozzle
come from Bambu's published specs, but **Bambu does not publish a Z-axis
feedrate or Z acceleration for any of their machines**, and those are exactly
the numbers this app leans on hardest. `max_z_velocity` (30 mm/s) and
`max_z_accel` are shared conservative values across the whole family rather
than per-model measurements. `z_amp_max` (4.0 mm) is not a vendor figure at
all — it is this app's own maximum Z excursion below already-printed
material, assuming the probe-less toolhead geometry these machines have. It
has not been print-tested on a Bambu by anyone. Start well under it and work
up, or import your own machine profile from Bambu Studio and adjust in the
review dialog.

## Adding your own printer

Import a real config file and the app builds a validated profile from it:

```bash
# Klipper printer.cfg, OrcaSlicer/Bambu Studio/Creality Print machine .json,
# Cura/Creality Slicer printer .def.json, PrusaSlicer/SuperSlicer .ini, or a
# printer .json exported from this app
python generate.py --import-printer ~/printer.cfg
python generate.py --list-printers
python generate.py --printer custom_my_voron --shape star --height 80
```

Creality owners: Creality Print 5.x machine profiles are an OrcaSlicer fork
and parse through the same OrcaSlicer/Bambu Studio path above. Creality
Slicer and older Creality Print are Cura-derived, so their printer definition
(`.def.json`) is also supported directly -- point `--import-printer` at it
the same way. Two Cura quirks are handled explicitly rather than guessed at:
a `machine_center_is_zero: true` definition (delta-style centred origin) gets
its safe print area left at the default inset with a note instead of a wrong
guess, since this app assumes a front-left origin; and Cura's motion
placeholders (`fdmprinter` ships 299792458000 -- the speed of light in mm/s --
for any limit a definition never set) are treated as **missing** rather than
clamped. That distinction matters: clamping a sentinel lands on this app's
safety ceiling of 100 mm/s, which on the Ender 3 those definitions describe
would be twenty times the real 5 mm/s. Dropping it instead falls back to the
conservative default, so "unset" never quietly becomes "as fast as this app
allows".

In the app, **Printer -> + Add custom printer** does the same thing with a
review dialog: drop the file, check every parsed value, fix anything flagged,
save. Custom profiles never shadow the built-ins.

Where they are kept differs between the two, on purpose:

| | Stored in | Cleared by |
|---|---|---|
| **App** (browser) | your browser's `localStorage`, replayed into a per-session store on the server at page load | clearing your browser data — then re-import the config |
| **CLI** (`generate.py --import-printer`) | a per-user data directory (`%APPDATA%\TridentGcode\printers`, `~/Library/Application Support/...`, `$XDG_DATA_HOME/...`) | deleting the `.json` file there |

The two do not share printers. The app's copy is browser-owned so its
lifetime is one you can predict and control, and so that two people using the
same server never see — or delete — each other's machines. Every profile is
re-validated from scratch on every load, not trusted because it was saved
once.

The parse is deliberately paranoid, because a wrong number here is a broken
printer rather than a bad print:

- **A missing value becomes a conservative default, never the Trident's.**
  An unknown `max_z_velocity` becomes 10 mm/s, not 25 — silently inheriting
  another machine's limits is exactly how a gantry gets wrecked.
- **Every field has an absolute range** independent of what the file claims.
  `max_z_velocity: 9999` is clamped and flagged; a bed size of zero is a hard
  error. Non-finite values (`NaN`, `Infinity`) are refused outright — NaN
  defeats every guard it passes through instead of tripping them.
- **Cross-checks**: `max_z_velocity <= max_velocity`, safe area inside the
  bed, `z_amp_max` derived from probe clearance rather than guessed. With no
  bed mesh in the file, the safe area is inset from the bed edge, not assumed
  to be the whole bed.
- **Cura's `value` field is never evaluated.** A Cura printer definition's
  `value` holds a Python expression (e.g. `"=machine_width / 2"`) that Cura
  itself evaluates at slice time -- it is not a literal. Only `default_value`
  is ever read; a setting that has only a `value` expression is skipped and
  named in a note rather than parsed, let alone executed.
- **Start/end G-code is sanitized, never passed through.** Foreign slicer
  placeholders (`{first_layer_temperature[0]}`, `[bed_temperature]`) are
  mapped onto ours and anything left over is escaped, so a template can never
  blow up mid-generation. `M502`, `M500`, `M851`, `M303`,
  `SET_KINEMATIC_POSITION` and friends are stripped unless you explicitly opt
  back in. **Start G-code with no `G28` is a hard error** — an unhomed first
  move is the classic way to drive a nozzle through a bed — and an explicit
  `M82` is too, since the generator emits relative `E` deltas that an
  absolute-mode printer would read as absolute targets.
- **Hotend and bed `max_temp` are read from the config** and clamped in the
  emitted G-code, so a filament profile can't ask for more than the hardware
  allows.

Klipper `PRINT_START` macros are called rather than inlined, with the
parameter names read from the macro body (`EXTRUDER` vs `EXTRUDER_TEMP` vs
`HOTEND`) so temperatures actually reach it.

Run `python tools/test_printer_import.py` to exercise the whole pipeline,
including an adversarial `hostile.cfg` fixture.

## The printer view

`viewer/index.html` draws a recognisable Voron Trident — aluminium frame, bed
plate, and gantry beam — sized from your build volume, so you can see your part
sitting in the machine the way OrcaSlicer shows it. Toggle the frame, bed grid,
height-coloring, travels, and auto-spin from the panel.

**Print-process playback:** a timeline bar at the bottom replays the print the way
it's actually laid down — drag the slider to scrub, or hit play to watch the path
draw in and a nozzle marker travel along the toolpath. The readout shows the
current Z height, layer/turn number, and elapsed/total print time (e.g.
`4:12 / 17:30`); a speed selector (1×–64×, where 1× is real print time)
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

**Orientation cube:** a small cube in the bottom-left corner of the viewport mirrors
the camera's orientation, and clicking a face snaps the view to it — Top, Bottom,
Front, Back, Left, Right — keeping your current zoom. Labels are the *printer's*
axes, not the renderer's (the scene's Y is the machine's Z), so "Front" is the
Y=0 edge of the bed, the side you stand at. It has its own canvas rather than
sharing the main one, so clicking it can never be mistaken for an orbit drag.

**Measuring the model:** a tool rail sits on the right edge of the canvas, beside
the panel — the panel is where you change what gets printed, the rail is where you
inspect what's already there. Click **Measure** (or press **M**) for two modes:

- **Distance** — click two points for the straight-line span between them, broken
  out into ΔX / ΔY / ΔZ and the horizontal (XY) component.
- **Diameter** — click one point on the wall for the radius out to it and the
  diameter straight across the model at that height.

Picks snap to the toolpath, so clicking the same spot twice gives the same number,
and dragging still orbits — a point is only placed if the pointer didn't move.
**Esc** clears the measurement, **Esc** again closes the tool.

Two things it's careful about, both worth knowing before you cut a lid to fit:

- Every figure is the **toolpath centreline**, not the outside of the printed wall.
  The bead straddles that line, so the real outer wall stands half a line width
  further out. The card shows the derived outer/inner diameters next to the
  measured one and labels them derived rather than folding the correction in
  silently.
- The diameter is measured across the section's **outer wall**, traced in 72
  angular sectors. That matters on anything solid or infilled — a nearest-point
  search across from the pick will happily grab an infill line near the middle and
  report 36 mm across a part that's 64 mm across. The card also prints how many
  sectors it found a wall in and the range of outer radii, so a section that isn't
  round says so instead of implying a single honest diameter exists.

The reading is only ever taken from the part of the print that's currently drawn,
so scrubbing the timeline back and measuring won't quietly report geometry that
isn't on screen. `viewer/dev_smoke.html?selftest=1` checks the arithmetic against
a model whose true dimensions are known from its own G-code.

## Key options (`python generate.py --help`)

| Flag | Meaning |
|------|---------|
| `--shape` | `circle`, `star`, or `square` base outline |
| `--radius` / `--height` | base radius and total height (mm) |
| `--layer-height` | spiral pitch — Z rise per turn (mm) |
| `--line-width` | bead width; go wide (1–2 mm) for genuinely *thick* continuous prints |
| `--line-width-curve` | JSON `[[t,mult],...]` multiplying `--line-width` over height (see "Variable line width along the path") |
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

### Variable line width along the path

Bead width can be curved over height too, independent of the fixed `--line-width`
value -- a wide base for grip, a thin waist, a flared rim, or anything a curve
can express:

```bash
python generate.py --line-width-curve "[[0,1.0],[0.5,1.6],[1,0.7]]" --out vase.gcode
```

The curve is `[[t, multiplier], ...]` control points (`t` = height fraction
`0..1`), piecewise-linear interpolated, multiplying the nominal `--line-width`
at that height. In the app it's a third draggable curve editor on the Print
step, right beside the amplitude and silhouette curves. Either way, width is
clamped to `[0.5, 2.0]x` nominal before it reaches the G-code -- and that
clamp is the same one the E-volume calculation itself uses, so the
volumetric-flow speed cap always tracks the *actual* bead cross-section, not
the nominal one: a widened region is auto-slowed to respect the melt limit
exactly the way a thick fixed-width print already is. A flat curve (or
omitting `--line-width-curve` entirely) reproduces byte-for-byte identical
output to before, so existing prints are unaffected.

## Safety model

The generator refuses to emit unsafe motion:

- Footprint must fit the **safe print area** (X 30–208, Y 30–185 — your bed-mesh range).
- Top Z must be ≤ 160 mm.
- No extruding move may go **below the bed** (wave amplitude ramps in from the base so
  the first layers stay planar and adhere).
- Every feedrate is clamped so Z velocity ≤ `max_z_velocity`.
- Nozzle and bed temperatures are clamped to the profile's `max_nozzle_temp` /
  `max_bed_temp`, with a `; NOTE:` line in the output when a clamp bites.

It calls your existing `PRINT_START` / `PRINT_END` macros and emits relative
extrusion (`M83`), matching your `printer.cfg`.

The numbers above are the *Trident's*. Every limit comes from the selected
`PrinterProfile`, so an imported custom printer is held to its own — see
[Adding your own printer](#adding-your-own-printer) for how those values are
validated before they ever reach the generator.

What the safety model does **not** cover is worth stating as plainly as what
it does. It enforces the limits it is given; it cannot tell you those limits
describe your machine. It has been print-tested on exactly one printer. Treat
published specs, conservative defaults and derived clearances as informed
guesses until you have run the machine yourself.

## Running it for other people

The server binds `127.0.0.1` and is a single-user tool by default. It can be
hosted (there is a `render.yaml` blueprint in the repo), but understand what
that changes before you do it:

- **A hosted instance is public and unauthenticated.** There is no login.
  Anyone with the URL can import printer configs and generate G-code. Setting
  `TRIDENT_BIND=0.0.0.0` is what makes this true, which is why it is opt-in
  and why the server warns on every start when it is set.
- **Custom printers are per-browser, not per-account.** They live in each
  visitor's `localStorage` and are replayed into a server-side session that
  dies with the process. That keeps one visitor's machine limits away from
  another's — but it is isolation, not access control, and it is not a
  security boundary.
- **Nothing on the server persists.** Restart it and every session's custom
  printers are gone until each browser reloads and replays its own.
- **The safety note at the top of this file applies to your users, not just
  to you.** G-code generated by a hosted instance for someone else's printer
  is unverified by anyone. If you hand the link to strangers, tell them to
  read it.

To run it locally instead — the mode this is actually designed for:

```bash
git clone https://github.com/ryan0304/Non-Planar-Slicer
cd Non-Planar-Slicer
python serve.py     # loopback only; open http://localhost:8777/viewer/index.html
```

No dependencies to install: the generator is standard library only.

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
serve.py                        static file server + JSON API for the browser app
tools/make_sample_meshes.py     writes sample STLs into examples/
tools/check_regression.py       byte-compares generated output against regression_ref/
tools/test_printer_import.py    tests the custom-printer parser/validator (incl. a hostile config)
calibrate.py                    calibration print suite (live-Z / flow / z-amp)
presets.py                      curated, machine-safe presets
trident_gcode/
  profile.py                    machine + material limits (from printer.cfg); multi-printer profiles
  paths.py                      2D base shapes + continuous spiral geometry + radius-texture patterns
  extrusion.py                  volumetric E math + Z feedrate clamping
  gcode.py                      Klipper/Marlin G-code emitter + safety checks
  config.py                     JSON config file load / save / validation
  mesh.py                       pure-Python STL load + horizontal slicing
  surface.py                    non-planar height fields (+ STL top sampling)
  profile_stack.py              unified contour-stack format (parametric shapes + sliced meshes)
  orca.py                       OrcaSlicer filament profile import
  printer_import.py             parse a Klipper/Orca/Cura/Prusa printer config (no validation -- raw only)
  printer_validate.py           the safety core: limits, clamps, G-code sanitation
  printer_store.py              custom printer profiles on disk (custom_printers/)
  blobs.py                      blob-texture and loop-fabric site placement
  fullcontrol_export.py         export any toolpath as a FullControl script
  generators/
    continuous_spiral.py        parametric spiral -> bed-placed G-code
    profile_spiral.py           unified generator over any contour stack (app-only asymmetric cage/spine/ovality)
    mesh_spiral.py              wrap a spiral around an STL silhouette
    surface_spiral.py           conformal shell following a surface
    base_fill.py                solid base disk + brim as one continuous bead
    loop_fabric.py              knitted/chainmail wall texture generator
viewer/index.html               3D viewer + browser design app shell
viewer/viewer.js                Three.js scene, playback, telemetry
viewer/designer.js              design wizard, curve editors, cage editor, undo/redo/persistence
examples/cal/                   calibration G-code files (generated by calibrate.py)
regression_ref/                 reference G-code checked by tools/check_regression.py
```

## Roadmap

- [x] Mesh import (STL) → cross-section → continuous spiral wrapping
- [x] Non-planar *surface following* (conformal shell over a curved surface / STL top)
- [x] Variable (gap-aware) layer height along the path
- [x] Wire Orca filament fields (fan, retraction, pressure advance) into emission
- [x] Config/profile system (`--config` / `--save-config`)
- [x] Calibration suite (`calibrate.py`) — live-Z disk, flow ladder, Z-amp ladder
- [x] Asymmetric shaping (3D control cage, spine offset, ovality), blob and loop-fabric
      textures, multi-printer profiles, mesh upload, undo/redo — app-only so far
- [x] Variable line width along the path
- [x] Custom printer import (Klipper cfg / Orca / Prusa) with a validating parser
- [ ] Feasibility study + prototype for true dynamic tri-Z bed tilt
