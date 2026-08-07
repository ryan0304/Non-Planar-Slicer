/* =========================================================================
   Orca-style parameter help content -- one entry per sidebar control.
   Keyed on the control's own DOM id (the input/select inside a .drow
   or label.row). Consumed by the tooltip engine in designer.js; loaded
   before designer.js so window.PARAM_HELP exists when that engine runs.

   Each entry: { desc, param, def }
     desc  -- 1-2 plain-English sentences, what the control actually does.
     param -- the real field name the browser sends to /api/generate (or a
              note when the control is UI-only and nothing is sent).
     def   -- the shipped default, taken from designer.js's design state
              object or the control's own value/checked attribute in
              index.html. Never a machine limit -- those come from the
              selected PrinterProfile and are described qualitatively.

   Do not add machine-limit numbers here (see CLAUDE.md: "No machine limit
   may be a module constant"). Where a control is capped by the printer
   profile, say so in words instead of naming a figure.
   ========================================================================= */
window.PARAM_HELP = {

  // ---- Step 1: Shape --------------------------------------------------
  "d-shape": {
    desc: "The outline traced at every height before twist, lean, or texture are applied. Circle is a plain cylinder; star and square add corners for the wave and texture patterns to play off.",
    param: "shape", def: "circle"
  },
  "d-radius": {
    desc: "Base radius of the outline in millimeters, measured before ovality, the silhouette curve, or texture displacement are applied.",
    param: "radius", def: "32 mm"
  },
  "d-height": {
    desc: "Total print height in millimeters, from the first layer to the top of the wall.",
    param: "height", def: "60 mm"
  },
  "d-lh": {
    desc: "Vertical distance the nozzle rises per full turn of the spiral. Also feeds the First layer height \"auto\" calculation (75% of this value).",
    param: "layer_height", def: "0.30 mm"
  },
  "d-xytwist": {
    desc: "How many full turns the whole outline rotates from bottom to top, on top of the spiral's natural advance -- twists the vase around its own axis.",
    param: "xy_twist", def: "0 turns"
  },

  // ---- Step 1: Asymmetry ------------------------------------------------
  "d-spine": {
    desc: "Bends the vase so the top shifts sideways by this many millimeters, leaning the whole silhouette off-axis.",
    param: "spine_mm", def: "0 mm"
  },
  "d-spinedeg": {
    desc: "Compass direction the lean (Lean mm) tips toward, in degrees around the vase.",
    param: "spine_deg", def: "0 deg"
  },
  "d-ovality": {
    desc: "Squashes the circular cross-section into an ellipse; positive and negative values stretch opposite axes.",
    param: "ovality", def: "0"
  },
  "d-starpoints": {
    desc: "Number of points on the star outline. Only takes effect when Shape is set to star.",
    param: "star_points", def: "5"
  },
  "d-stardepth": {
    desc: "How deep the star's points cut in toward the center, as a fraction of the radius. Only takes effect when Shape is star.",
    param: "star_depth", def: "0.35"
  },
  "d-bottom": {
    desc: "Whether the base is closed off with solid layers (Solid) or left open the way vase mode usually prints (Open). Open sends Base layers as 0 for this print; your Base layers setting itself is unchanged and returns when you pick Solid again.",
    param: "bottom (client-side only -- sends base_layers 0 with the request when Open)", def: "solid"
  },
  "d-base": {
    desc: "Number of stacked solid disks printed at the bottom before the spiral wall begins, for a build-plate-adhering, watertight base.",
    param: "base_layers", def: "2"
  },
  "d-brim": {
    desc: "Extra flat outline loops printed around the base to help it stick to the plate. 0 = no brim.",
    param: "brim", def: "0"
  },
  "d-basestyle": {
    desc: "How the solid base disks are filled: Spiral disk is a single warped Archimedean spiral, Concentric rings are closed nested loops.",
    param: "base_style", def: "spiral"
  },
  "d-skirt": {
    desc: "Outline-following loops printed clear of the model before the print starts, used to prime the nozzle and check bed adhesion without touching the part.",
    param: "skirt", def: "0"
  },
  "d-flh": {
    desc: "Height of the very first layer, printed pressed harder into the plate for a firmer grip. Lower = firmer grip.",
    param: "first_layer_height", def: "auto (75% of layer height)"
  },
  "d-spacing": {
    desc: "Multiplies the line spacing of the base/skirt/brim relative to the normal bead width -- widens gaps between passes for a more open first layer, or tightens them for denser adhesion.",
    param: "first_layer_spacing_factor", def: "1.25"
  },

  // ---- Step 1: STL import (mesh info/readouts) --------------------------
  "mesh-name": {
    desc: "Filename of the imported STL, once one is loaded. Display only.",
    param: "UI display only, not sent to the API", def: "- (none until a file is loaded)"
  },
  "mesh-tris": {
    desc: "Triangle count of the imported mesh. Display only.",
    param: "UI display only, not sent to the API", def: "- (none until a file is loaded)"
  },
  "mesh-size": {
    desc: "Bounding-box width and depth (X x Y) of the imported mesh, in millimeters, before Scale is applied. Display only.",
    param: "UI display only, not sent to the API", def: "- (none until a file is loaded)"
  },
  "mesh-height": {
    desc: "Bounding-box height (Z) of the imported mesh, in millimeters, before Scale is applied. Display only.",
    param: "UI display only, not sent to the API", def: "- (none until a file is loaded)"
  },
  "mesh-scale": {
    desc: "Uniform scale factor applied to the imported mesh before slicing.",
    param: "scale", def: "1.0"
  },
  "mesh-lh": {
    desc: "Layer height used when slicing the imported mesh -- overrides the Shape tab's layer height for STL mode.",
    param: "layer_height (mesh mode)", def: "0.3 mm"
  },

  // ---- Step 2: Z waves ----------------------------------------------------
  "d-waves": {
    desc: "Number of full up-down wave cycles the non-planar Z modulation completes over the print's height. Shape and size of each wave come from the amplitude curve above.",
    param: "z_waves", def: "5"
  },
  "d-ztwist": {
    desc: "Extra rotation applied to the Z-wave pattern itself as it climbs, independent of the XY twist on the outline.",
    param: "z_twist", def: "0 turns"
  },

  // ---- Step 2: Texture pattern --------------------------------------------
  "d-pattern": {
    desc: "Surface texture applied to the wall: raised dots, hanging loop strands, or a radial ridge/ring pattern. Leave as none for a plain wall.",
    param: "pattern", def: "none"
  },
  "d-pamp": {
    desc: "How far the radial texture displaces the wall sideways, in millimeters. Sideways only, never Z -- no probe risk.",
    param: "pattern_amp", def: "1.0 mm"
  },
  "d-pwaves": {
    desc: "How many times the texture pattern repeats around the perimeter of each turn.",
    param: "pattern_waves", def: "12"
  },
  "d-pbands": {
    desc: "How many texture cycles repeat over the full height of the print.",
    param: "pattern_bands", def: "6"
  },
  "d-ptwist": {
    desc: "Extra rotation applied to the texture pattern as it climbs, in turns -- independent of the XY/Z twist on the shape.",
    param: "pattern_twist", def: "0 turns"
  },
  "d-pfadein": {
    desc: "Fraction of the height, measured from the bottom, over which the texture ramps up from zero to full strength.",
    param: "pattern_fade_in", def: "0.10"
  },
  "d-pfadeout": {
    desc: "Fraction of the height, measured from the top, over which the texture ramps back down to zero.",
    param: "pattern_fade_out", def: "0"
  },
  "d-palternate": {
    desc: "Alternates turns zigzagging opposite ways so they bond only at crossings, forming an open net instead of a solid wall. Print slow with max cooling; fragile by design.",
    param: "pattern_alternate", def: "off"
  },

  // ---- Step 2: Blob texture -----------------------------------------------
  "d-blob-style": {
    desc: "Picks a bundle of blob settings (spacing, size, dwell) matching a look. Choosing a style fills in the fields below; editing any of them switches this to Custom.",
    param: "blob_style (UI only -- selects a bundle of the fields below, not itself sent to the API)", def: "dots"
  },
  "d-blob-per-turn": {
    desc: "How many blob deposits are placed around each turn. Only used when Spacing (mm) below is 0.",
    param: "blob_per_turn", def: "6"
  },
  "d-blob-spacing": {
    desc: "Target distance between blobs along a turn, in millimeters. 0 = ignore this and use Blobs per turn instead.",
    param: "blob_spacing_mm", def: "0 mm (use per-turn count)"
  },
  "d-blob-stride": {
    desc: "Vertical spacing between blob rows, in turns -- places a row of blobs every N turns.",
    param: "blob_turn_stride", def: "2"
  },
  "d-blob-align": {
    desc: "How blob rows line up vertically: Stagger offsets alternating rows for a diamond pattern, Column stacks them directly above each other, Jitter scatters them pseudo-randomly.",
    param: "blob_align", def: "stagger"
  },
  "d-blob-jitter": {
    desc: "Scatter strength for Jitter alignment, as a fraction of the blob spacing. Only takes effect when Alignment is Jitter.",
    param: "blob_jitter", def: "0.5"
  },
  "d-blob-volume": {
    desc: "Material volume deposited per blob, in cubic millimeters. Larger values dwell longer and print a bigger bump.",
    param: "blob_volume", def: "3.0 mm3"
  },
  "d-blob-volstart": {
    desc: "Multiplier on blob Volume at the bottom of the print, letting blobs grow or shrink across the height together with Size at top.",
    param: "blob_vol_start", def: "1.0"
  },
  "d-blob-volend": {
    desc: "Multiplier on blob Volume at the top of the print.",
    param: "blob_vol_end", def: "1.0"
  },
  "d-blob-dwell": {
    desc: "How long the nozzle pauses after depositing each blob, in milliseconds, letting it cool and set before moving on.",
    param: "blob_dwell", def: "400 ms"
  },
  "d-blob-fadein": {
    desc: "Fraction of the height, from the bottom, with no blobs at all -- ramps blobs in gradually above that.",
    param: "blob_fade_in", def: "0.15"
  },
  "d-blob-fadeout": {
    desc: "Fraction of the height, from the top, where blobs fade back out to nothing.",
    param: "blob_fade_out", def: "0.05"
  },

  // ---- Step 2: Loop (knitted) fabric texture -------------------------------
  "d-loop-style": {
    desc: "Picks a bundle of loop-fabric settings matching a knit look. Choosing a style fills in the fields below; editing any of them switches this to Custom.",
    param: "loop_style (UI only -- selects a bundle of the fields below, not itself sent to the API)", def: "chainmail"
  },
  "d-loop-spacing": {
    desc: "Target distance between stitches along a fabric row, in millimeters. 0 = ignore this and use Loops per turn instead.",
    param: "loop_spacing_mm", def: "4.0 mm"
  },
  "d-loop-per-turn": {
    desc: "How many stitches are placed around each fabric row. Only used when Stitch spacing (mm) above is 0.",
    param: "loop_per_turn", def: "0 (use stitch spacing instead)"
  },
  "d-loop-row": {
    desc: "Vertical rise per fabric row -- each spiral turn climbs this many millimeters instead of a normal layer height. Capped by your printer profile's non-planar clearance.",
    param: "loop_row", def: "2.5 mm"
  },
  "d-loop-up": {
    desc: "Loop height: how far each stitch dips down below the row line before rising back up. Must exceed Row height for stitches to hook the row below. Capped by your printer profile's non-planar clearance.",
    param: "loop_up", def: "3.5 mm"
  },
  "d-loop-out": {
    desc: "How far each stitch bows outward at its lowest point, away from the wall.",
    param: "loop_out", def: "0.5 mm"
  },
  "d-loop-align": {
    desc: "How stitch rows line up vertically: Stagger offsets alternating rows brick-style, Column stacks them directly above each other.",
    param: "loop_align", def: "stagger"
  },
  "d-loop-mode": {
    desc: "Dip loops below the row line and hooks the row beneath (uses the full Loop height in probe clearance). Spike rises above the row line and pauses at the peak so the tip hardens as a standing tie, using less clearance.",
    param: "loop_mode", def: "dip"
  },
  "d-loop-dwell": {
    desc: "How long the nozzle pauses at the peak of each spike, in milliseconds, so the tip hardens before descending. Spike mode only.",
    param: "loop_dwell", def: "0 ms"
  },
  "d-loop-lean": {
    desc: "Forward tilt of each spike from vertical, in degrees. A leaning spike partially self-supports and its tip spreads over the gap to the next row. Spike mode only.",
    param: "loop_lean", def: "20 deg"
  },
  "d-loop-coast": {
    desc: "Distance before the peak where extrusion stops, so residual nozzle pressure forms the tip instead of oozing a blob onto it. Spike mode only.",
    param: "loop_coast", def: "0.8 mm"
  },
  "d-loop-retract": {
    desc: "Pulls filament back during the peak pause -- the strongest fix for oozy spike tips. 0 = off. Spike mode only.",
    param: "loop_retract", def: "0 mm"
  },
  "d-loop-waveamp": {
    desc: "Amplitude of an undulation added to the row line itself, phase-flipped on alternate rows so wavy rows cross at their nodes. Adds to the Z budget on top of Loop height -- capped by your printer profile's non-planar clearance.",
    param: "loop_wave_amp", def: "0 mm"
  },
  "d-loop-waves": {
    desc: "How many times the row-line wave (Row wave) repeats around the perimeter.",
    param: "loop_waves", def: "12"
  },
  "d-loop-flow": {
    desc: "Extrusion multiplier for loop strands relative to a normal bead -- thicker strands are stronger but take longer to cool.",
    param: "loop_flow", def: "1.2"
  },
  "d-loop-speed": {
    desc: "Print speed while tracing loop stitches. Slower gives cleaner, better-set loops than the main wall speed.",
    param: "loop_speed", def: "10 mm/s"
  },
  "d-loop-cuff": {
    desc: "Number of solid, non-fabric turns printed at the base to anchor the first fabric row before the open loop-fabric begins.",
    param: "loop_cuff", def: "3"
  },

  // ---- Step 2: Cooling & flow ----------------------------------------------
  "d-overhang-k": {
    desc: "Boosts extrusion on outward-leaning walls to compensate for under-extrusion on overhangs. 0 = off, higher = more extra flow.",
    param: "overhang_flow_k", def: "0"
  },
  "d-fan-min": {
    desc: "Part-cooling fan speed on a vertical wall. The fan ramps from this toward Fan max as the wall leans further outward.",
    param: "fan_min (sent as a 0-1 fraction)", def: "100%"
  },
  "d-fan-max": {
    desc: "Part-cooling fan speed where the wall leans 45 degrees or more outward. Set equal to Fan min for a constant fan speed.",
    param: "fan_max (sent as a 0-1 fraction)", def: "100%"
  },
  "d-fan-off-layers": {
    desc: "Total layers (base + wall) to keep the part-cooling fan off, counted from the very start of the print. 0 = today's default (off through any base, or the wall's first turn if there is no base).",
    param: "fan_off_layers", def: "0"
  },
  "sil-smooth": {
    desc: "Smooths the silhouette curve into a rounder profile instead of straight segments between its control points.",
    param: "radius_profile_smooth", def: "off"
  },

  // ---- Step 3: Print --------------------------------------------------------
  "d-nozzle": {
    desc: "Nozzle diameter to generate for. Auto uses the selected printer profile's nozzle diameter; a specific size overrides it and changes the flow line width shown below.",
    param: "nozzle", def: "auto (profile)"
  },
  "d-speed": {
    desc: "Print speed for the main wall. Capped by your printer profile's maximum travel speed.",
    param: "print_speed", def: "40 mm/s"
  },
  "d-filament": {
    desc: "Filament profile to pull temperature and flow settings from, when an Orca filament library is available on the server. (generic PLA) uses this app's built-in conservative defaults.",
    param: "filament", def: "(generic PLA), or the server's Orca library default if one is configured"
  },
  "d-lwoverride": {
    desc: "Overrides the bead width the generator targets, in millimeters. Leave blank to use the automatic width derived from the nozzle diameter (shown in the hint below).",
    param: "line_width", def: "auto (nozzle diameter x 1.125)"
  },
  "d-nozzletemp": {
    desc: "Leave blank to use the selected filament's temperature; set it to override. Bounded by your printer profile's max nozzle temperature, with a 150 C floor.",
    param: "nozzle_temp", def: "auto (filament temperature)"
  },
  "d-bedtemp": {
    desc: "Leave blank to use the selected filament's bed temperature; set it to override. Unlike nozzle temp, 0 is a valid override here and turns the bed heater off. Bounded by your printer profile's max bed temperature.",
    param: "bed_temp", def: "auto (filament bed temperature)"
  },

  // ---- Point Edit Modifiers modal -------------------------------------------
  "pe-mask-enable": {
    desc: "Exposes or protects points using a procedural channel -- gates every deformation modifier below (Point FFD / Smooth / Radial Push). By itself it moves nothing.",
    param: "point_mask (block is only sent when enabled and meaningful)", def: "off"
  },
  "pe-mask-channel": {
    desc: "Procedural pattern used to decide which points are exposed (fully affected) versus protected (untouched) by the deformation modifiers below.",
    param: "point_mask.channel", def: "checker"
  },
  "pe-mask-scaleu": {
    desc: "How many times the mask channel repeats around the perimeter (theta direction).",
    param: "point_mask.scale_u", def: "8"
  },
  "pe-mask-scalev": {
    desc: "How many times the mask channel repeats over the height (t direction).",
    param: "point_mask.scale_v", def: "6"
  },
  "pe-mask-invert": {
    desc: "Flips which points the mask exposes versus protects.",
    param: "point_mask.invert", def: "off"
  },
  "pe-prot-enable": {
    desc: "Protects top/bottom zones from edits (with a falloff ramp) so deformation modifiers only reach the intended layers. By itself it moves nothing.",
    param: "point_protection (block is only sent when enabled and meaningful)", def: "off"
  },
  "pe-prot-bottom": {
    desc: "Fraction of the height, from the bottom, fully protected from Point Edit deformation.",
    param: "point_protection.protect_bottom", def: "0"
  },
  "pe-prot-top": {
    desc: "Fraction of the height, from the top, fully protected from Point Edit deformation.",
    param: "point_protection.protect_top", def: "0"
  },
  "pe-prot-falloff": {
    desc: "Fraction of the height over which protection ramps from fully protected to fully exposed, instead of cutting off sharply.",
    param: "point_protection.falloff", def: "0.08"
  },
  "pe-ffd-enable": {
    desc: "Point-level cage: each cell pushes the already-sliced wall radially in/out (mm) at that height/azimuth, gated by Point Mask & Protection above.",
    param: "point_ffd (block is only sent when enabled and meaningful)", def: "off"
  },
  "pe-ffd-strength": {
    desc: "Overall multiplier on how strongly the FFD cage's cell values push the wall.",
    param: "point_ffd.strength", def: "1.0"
  },
  "pe-smooth-enable": {
    desc: "Averages each point with its neighbors after slicing -- smooths the deposited path itself without going back to the original geometry.",
    param: "point_smooth (block is only sent when enabled and meaningful)", def: "off"
  },
  "pe-smooth-iter": {
    desc: "How many smoothing passes to run. More passes smooth harder but can wash out fine detail.",
    param: "point_smooth.iterations", def: "2"
  },
  "pe-smooth-theta": {
    desc: "How much each point blends toward its neighbors around the same turn (theta direction), from 0 (no blend) to 1 (full blend).",
    param: "point_smooth.theta_amount", def: "0.5"
  },
  "pe-smooth-t": {
    desc: "How much each point blends toward the points on the turn above and below (t direction), from 0 (no blend) to 1 (full blend).",
    param: "point_smooth.t_amount", def: "0.5"
  },
  "pe-smooth-strength": {
    desc: "Overall multiplier on how strongly the smoothing blend is applied.",
    param: "point_smooth.strength", def: "1.0"
  },
  "pe-push-enable": {
    desc: "Pushes or pulls every gated point along its own radial direction, sculpting the actual deposited path.",
    param: "point_radial_push (block is only sent when enabled and meaningful)", def: "off"
  },
  "pe-push-amp": {
    desc: "Distance to push points outward (positive) or pull them inward (negative), in millimeters, before Strength and the mask/protection gating are applied.",
    param: "point_radial_push.amp_mm", def: "1.0 mm"
  },
  "pe-push-strength": {
    desc: "Overall multiplier on how strongly the radial push is applied.",
    param: "point_radial_push.strength", def: "1.0"
  },

  // ---- G-code Viewer mode: Display (client-side rendering only) ------------
  "t-colormode": {
    desc: "How the loaded G-code path is colored in the 3D preview: by height, by overhang angle, or a single plain color.",
    param: "viewer display only, not sent to the API", def: "height"
  },
  "t-xray": {
    desc: "Renders the model semi-transparent in the preview so you can see through the wall.",
    param: "viewer display only, not sent to the API", def: "off"
  },
  "t-bands": {
    desc: "Highlights individual layer bands in the preview for easier reading of the non-planar Z motion.",
    param: "viewer display only, not sent to the API", def: "on"
  },
  "t-risk": {
    desc: "Overrides segment color to bright red for moves the analyzer flags as risky (e.g. a steep Z-rate). Display only -- does not change the print.",
    param: "viewer display only, not sent to the API", def: "off"
  },
  "t-travel": {
    desc: "Shows or hides non-extruding travel moves in the preview.",
    param: "viewer display only, not sent to the API", def: "on"
  },
  "t-printer": {
    desc: "Shows or hides the printer outline/gantry model in the preview.",
    param: "viewer display only, not sent to the API", def: "on"
  },
  "t-bed": {
    desc: "Shows or hides the bed grid in the preview.",
    param: "viewer display only, not sent to the API", def: "on"
  },
  "t-spin": {
    desc: "Auto-rotates the camera around the model in the preview.",
    param: "viewer display only, not sent to the API", def: "off"
  },
  "t-truewidth": {
    desc: "Draws each path segment at its actual printed line width instead of a fixed pixel thickness.",
    param: "viewer display only, not sent to the API", def: "on"
  },
  "t-width": {
    desc: "Pixel thickness of the path lines in the preview when True bead width above is off.",
    param: "viewer display only, not sent to the API", def: "2 px"
  }
};
