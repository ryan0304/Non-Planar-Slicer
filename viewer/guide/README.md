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
| `01-fastest-start/` | 6 | Pointer travels to **Generate & preview**, clicks it, the button goes disabled and reads `Starting...`, and the real safety report lands: `safe [OK] - 2.77 m, design_circle_60mm.gcode` with the full stats block below it. |
| `02-four-steps/` | 5 | The four step tabs clicked in order; the panel below really swaps between Shape, Z waves, nozzle/speed/filament, and PRINT IT. |
| `03-shaping/` | 5 | A control point on **Wave amplitude over height** is dragged upward. The readout reads `amp 0.60 mm @ H 24.0 mm`, then `0.83`, then `0.95` as the curve reshapes, and `peak wave slope` moves 0.13 -> 0.15 of 0.25. |
| `04-textures/` | 3 | Texture pattern changed `none` -> `loops (hanging strands)`; the pattern's own controls appear underneath. |
| `05-after-generating/` | 4 | The playback timeline scrubbed across a generated 229-layer print (`L60`, `L115`, `L172`, `L229`). The viewer truncates the model at the scrubbed layer, so the print grows from a single disc to the finished wall and the red nozzle marker climbs with it. |

`00-read-first.png` is a single still. That slide is scene-setting and has no
interaction to record.

`05-after-generating/` shows the print rather than the timeline that is being
dragged, and that is a compromise: the scrub handle and the nozzle marker sit
some 400 px apart vertically in the viewer, so no frame this size holds both,
and the viewer exposes no pan control to bring them together. Framing the
timeline strip instead was tried and rejected -- within one frame width the
handle only travels a few layers, so the numbers barely move and two thirds of
the frame is empty bed. The growing print is the more legible half of the same
interaction.

Its camera is pulled back further than "Fit view to model" leaves it, so the
finished print sits inside the frame with bed around it rather than filling the
edges. The viewer zooms toward the POINTER, so wheel ticks are not uniform
steps: back off one tick at a time from the Fit baseline and check the framing
after each, and expect the camera to jump through the model if you overshoot
(Fit view to model, `F`, is the way back). Because the subject is a 3D view and
not text, its scale is set by that camera rather than by the capture rectangle
-- which is why this sequence can be framed wider than the panel slides without
breaking the one-size rule.

The only thing added on top of the app is the pointer and its click ring: a
screen capture cannot see the OS cursor, so a small overlay was injected that
draws a pointer at the real `mousemove` position and a ring at the real
`mousedown` position. It reports where the input actually went; it never moves
on its own.

## One size, and displayed 1:1

Every file here is **540x338**, stills included, and `dev_smoke.html` asserts
they all match. Two separate things depend on that number.

**Same size, or the guide changes zoom between slides.** The figure box is a
fixed 16:10 and each frame is stretched to fill it, so the crop size *is* the
magnification: a 256-wide crop and a 939-wide crop of the same interface land
on screen at 1.7x and 0.45x, and paging through the guide reads as the app
zooming in and out under the reader.

**This size, or everything is magnified.** The box is 431x270 CSS px. A frame
captured from a 432x270 CSS region therefore shows the interface at exactly the
size it is in the real app -- no blow-up, and none of the softness that comes
with one. An earlier set was captured at 318x200 and stretched 1.36x to fill the
same box, which read as the whole app being zoomed in. 540x338 is that 432x270
region at this machine's DPR of 1.25; the extra pixels are what keep it sharp on
a high-DPI display. If you re-shoot on a DPR 1 machine you will get 432x270 --
resample to 540x338 rather than shipping a mixed set.

A 432 px window is wider than the 340 px control panel, so panel frames also
catch a strip of the 3D viewport down the left edge. That is deliberate: it
shows where the panel sits rather than presenting it as a floating fragment.

## Frames, not a GIF

The dark UI is full of soft gradients that band badly at a GIF's 256 colours,
and a full-frame GIF of the same footage came out around 1.2 MB for five
frames. The whole PNG set is ~950 kB for 25 frames and stays sharp. It is
fetched only when the guide is first opened, and the player decodes a sequence
before stepping it, so the weight lands once and never mid-animation. Chrome
writes captures with almost no PNG row filtering; re-encoding them with
per-row adaptive filters took the set from 1.8 MB to 1.3 MB with no pixel
changed, and is worth redoing after any re-shoot.

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
   Keep the crop rectangle identical across a sequence -- the player fades
   each frame in on top of the previous one (260 ms, a step every 620 ms), so
   a shifted crop reads as the whole scene sliding under the dissolve. Watch
   the panel's SCROLL position too: `#panel-scroll` is the scroller, and a few
   pixels of drift between frames looks exactly like a jump cut. Park it
   explicitly and re-set it after anything that reflows the panel.
4. Do **not** click a native `<select>` while capturing. The OS dropdown is not
   in the page, it never appears in a frame, and it blocks screenshot capture
   until dismissed. Focus the select and drive it with arrow keys instead.
5. On a display at DPR != 1 the capture comes back at device resolution, not at
   the CSS size you asked for: a 318x200 region saves as 390x245 at DPR 1.25.
   Ask for the region in CSS pixels and area-average the result back down to
   318x200. Cropping instead of scaling would keep the wrong magnification --
   it changes what is in frame, not how big it is.
6. The first `left_click_drag` after another action is sometimes swallowed and
   the frame captures the previous state. Check every frame actually differs
   from the one before it; a click on the track jumps a range input just as
   well as a drag does.
