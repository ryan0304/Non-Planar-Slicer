# How-to-use guide assets

Frames for the "How to Use Trident" modal (`#guide-modal` in `viewer/index.html`,
driven by `viewer/guide.js`).

## What these are

Every numbered directory is one slide's **recording**: a short sequence of PNG
frames captured from this app while a real click was driven through it. They
are photographs of the running app, not illustrations of it. In the frames you
are seeing the app's own hover states, its own progress bar, its own readouts
changing:

| Directory | Frames | What actually happened |
| --- | --- | --- |
| `01-fastest-start/` | 6 | Pointer travels to **Generate & preview**, clicks it, the button shows `Building toolpath... 40%`, and the real safety report lands: `safe [OK] - 2.91 m, design_circle_60mm.gcode` with the full stats block. |
| `02-four-steps/` | 5 | The four step tabs clicked in order; the panel below really swaps between Z waves, nozzle/speed/filament, and PRINT IT. |
| `03-shaping/` | 5 | A control point on **Wave amplitude over height** is dragged upward. The hover tooltip reads `amp 0.60 mm @ H 24.0 mm`, then `amp 0.87 mm @ H 23.3 mm` after the drag, and `peak wave slope` moves 0.13 -> 0.14 of 0.25. |
| `04-textures/` | 4 | Texture pattern changed `none` -> `blobs (raised dots)` -> `loops (hanging strands)`; each pattern's own controls appear underneath. |
| `05-after-generating/` | 4 | The playback timeline scrubbed across a generated 229-layer print, the nozzle marker moving with it (`L119/229`, `L170/229`, `L222/229`). |

`00-read-first.png` is a single still. That slide is scene-setting and has no
interaction to record.

The only thing added on top of the app is the pointer and its click ring: a
screen capture cannot see the OS cursor, so a small overlay was injected that
draws a pointer at the real `mousemove` position and a ring at the real
`mousedown` position. It reports where the input actually went; it never moves
on its own.

## Frames, not a GIF

The dark UI is full of soft gradients that band badly at a GIF's 256 colours,
and a full-frame GIF of the same footage came out around 1.2 MB for five
frames. The whole PNG set is ~640 kB for 24 frames, stays sharp, and can be
replayed on demand rather than looping in the corner of the reader's eye.

`guide.js` builds the `<img>` stack the first time the guide is opened, so a
user who never opens it never downloads any of this.

## Re-shooting a sequence

Numbering is `01.png`, `02.png`, ... and must be contiguous; `data-count` on
the slide's `.guide-fig` in `index.html` has to match the number of files.

1. `python serve.py`, then open the viewer in Chrome. Use the **`127.0.0.1`**
   origin, not `localhost` -- Chrome stores page zoom per origin, and anything
   other than 100% silently rescales every capture.
2. Inject the pointer overlay (draws a cursor at `mousemove`, a ring at
   `mousedown`; keep exactly one ring alive at a time or a frame reads as
   several clicks at once).
3. Drive the real controls and capture a fixed 16:10 crop after each step.
   Keep the crop rectangle identical across a sequence -- the player
   cross-switches frames in place, so a shifted crop reads as a jump cut.
4. Do **not** click a native `<select>` while capturing. The OS dropdown is not
   in the page, it never appears in a frame, and it blocks screenshot capture
   until dismissed. Focus the select and drive it with arrow keys instead.

All five sequences here were shot at 100% zoom in a 1920x855 viewport; frame
sizes differ per slide (256x160 up to 939x587) because each crop is sized to
the control it is about. That is fine -- the figure is a fixed 16:10 box and
every sequence is internally consistent.
