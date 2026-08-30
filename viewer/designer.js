(function(){
  'use strict';

  // ---- browser-owned custom printers -------------------------------------
  // Custom printers live HERE, in this browser, and only here. The server
  // keeps them in a per-session dict that dies with the process, so this
  // localStorage copy is the durable one and gets replayed on every load.
  //
  // The consequence is deliberate and worth stating plainly: clearing your
  // browser data deletes your imported printers, and you re-import the .cfg.
  // That is a lifetime a user can actually predict, unlike a file quietly
  // written into %APPDATA% that outlives every "clear cache" they try.
  //
  // The session id is not a credential -- it only picks which bucket of
  // replayed printers this browser sees. The server grants it no authority,
  // and the localhost-only bind is what actually keeps other machines out.
  var SESSION_KEY = 'trident_session';
  var PRINTERS_KEY = 'trident_printers';
  var MAX_STORED_PRINTERS = 32;

  var sessionId = null;
  try { sessionId = localStorage.getItem(SESSION_KEY); } catch(e){}
  if(!/^[a-z0-9]{8,64}$/.test(sessionId || '')){
    sessionId = '';
    // Math.random is fine: this is a namespace, not a secret.
    while(sessionId.length < 24) sessionId += Math.random().toString(36).slice(2);
    sessionId = sessionId.slice(0, 24);
    try { localStorage.setItem(SESSION_KEY, sessionId); } catch(e){}
  }

  // Every /api/ call goes through this so no call site can forget the header
  // and silently fall back to "no custom printers".
  function apiFetch(url, opts){
    opts = opts || {};
    var headers = {};
    if(opts.headers){ for(var k in opts.headers){ if(Object.prototype.hasOwnProperty.call(opts.headers, k)) headers[k] = opts.headers[k]; } }
    headers['X-Trident-Session'] = sessionId;
    opts.headers = headers;
    return fetch(url, opts);
  }

  function loadStoredPrinters(){
    var arr;
    try { arr = JSON.parse(localStorage.getItem(PRINTERS_KEY) || '[]'); } catch(e){ arr = []; }
    return Array.isArray(arr) ? arr : [];
  }
  function writeStoredPrinters(arr){
    try { localStorage.setItem(PRINTERS_KEY, JSON.stringify(arr.slice(0, MAX_STORED_PRINTERS))); } catch(e){}
  }
  function upsertStoredPrinter(key, profile, meta){
    if(!key || !profile) return;
    var arr = loadStoredPrinters().filter(function(p){ return p && p.key !== key; });
    arr.push({ key: key, profile: profile, meta: meta || {} });
    writeStoredPrinters(arr);
  }
  function removeStoredPrinter(key){
    writeStoredPrinters(loadStoredPrinters().filter(function(p){ return p && p.key !== key; }));
  }

  // Push this browser's saved printers back into the server session. Resolves
  // even on failure -- a replay that cannot reach the server must not stop
  // the app from loading with its built-in printers.
  function replayStoredPrinters(){
    var stored = loadStoredPrinters();
    if(!stored.length) return Promise.resolve(null);
    return apiFetch('/api/printer/session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ printers: stored })
    }).then(function(r){ return r.json(); }).then(function(j){
      // A printer the server refuses is gone for good -- keeping it in
      // localStorage would retry the same rejection on every single load.
      // Drop it here and tell the user why once, rather than silently.
      if(j && j.rejected && j.rejected.length){
        j.rejected.forEach(function(x){ removeStoredPrinter(x.key); });
        var lines = j.rejected.map(function(x){ return '  - ' + (x.key || '(unnamed)') + ': ' + x.reason; });
        alert('These saved custom printers could no longer be validated and have been '
              + 'removed. Re-import the printer config to use them again:\n\n'
              + lines.join('\n'));
      }
      return j;
    }).catch(function(){ return null; });
  }

  // Bootstrap-only ceiling used for the very first synchronous render of the
  // amp curve editor, before /api/printers has resolved (the default printer
  // is "trident", whose z_amp_max happens to be this same 0.95, so there is
  // no visible flash). Every printer switch after that goes through
  // applyPrinterCaps() -> ampEditor.setRange(), driven entirely by the
  // server's per-printer z_amp_max -- this constant is never consulted again.
  var PROBE_LIMIT = 0.95;
  var AMP_MAX = 0.95, RAD_LO = 0.5, RAD_HI = 1.3;

  // Bootstrap-only floor for the wave-slope readout (see "live wave-slope
  // readout" below), used for the exact same reason and in the exact same
  // way as PROBE_LIMIT/AMP_MAX just above: the very first synchronous
  // updateSlope() runs before /api/printers has resolved. It also covers a
  // served printer entry that omits quality_slope_max outright (an old
  // cache, a hand-rolled dev response) -- see slopeCapFor() below. Once a
  // real printer entry is on hand, PRINTER_SLOPE[design.printer] always
  // wins; this constant is never consulted again. 0.25 is conservative
  // because it is currently the tightest (and only) value any shipped
  // profile declares -- if a future printer ever needs to declare something
  // stricter, it must SEND that number; this floor does not stand in for it.
  var SLOPE_LIMIT_FALLBACK = 0.25;

  // Formats a millimeter ceiling the way the machine bar / notices / hints
  // want it: "0.95", "4.0", "0.40" -- two decimals, trimmed to one only when
  // the second is a redundant zero (x.y0 stays, x.00 becomes x.0).
  function fmtMm(v){
    var s = (Math.round(v * 100) / 100).toFixed(2);
    if(s.slice(-2) === '00') s = s.slice(0, -1);
    return s;
  }

  // ---- header brand mark (ambient busy echo) -------------------------------
  // See viewer/style.css's brand-mark block and the logo spec it implements.
  // Only #brand-mark-header ever animates -- the empty-state splash mark is
  // idle-only. Two mutually-exclusive states on that one element:
  //   is-busy-indeterminate: driven here, from the shared busy-line helper
  //     below, whenever any of #stl-status / #pm-drop-status /
  //     #printer-meta-status is showing its spinner. A >0 counter because
  //     more than one of those three can in principle overlap.
  //   is-busy-determinate: driven from showGenProgress/hideGenProgress
  //     (search "gen-progress-fill"), in step with the real progress bar.
  var headerMarkEl = document.getElementById('brand-mark-topbar');
  var MARK_BUSY_STATUS_IDS = { 'stl-status':1, 'pm-drop-status':1, 'printer-meta-status':1 };
  var markBusyCount = 0;
  function markBusyBegin(){
    markBusyCount++;
    if(headerMarkEl && markBusyCount === 1 && !headerMarkEl.classList.contains('is-busy-determinate')){
      headerMarkEl.classList.add('is-busy-indeterminate');
    }
  }
  function markBusyEnd(){
    if(markBusyCount > 0) markBusyCount--;
    if(headerMarkEl && markBusyCount === 0) headerMarkEl.classList.remove('is-busy-indeterminate');
  }

  // ---- shared busy-state helper -------------------------------------------
  // One small system for every "this network call takes a moment" spot in
  // this file: a disabled trigger control (when there is a single one) and
  // a status line carrying a spinner. Before this helper existed, each of
  // these was either completely silent or invented its own wording -- this
  // is the one place that decides what "working" looks like, so every
  // instance of it looks and reads the same way. The Generate flow is the
  // one exception (it gets its own real progress bar -- see genBtn below).
  //
  // btn:      control to disable while busy. Nullable -- background loads
  //           like the printers/filaments dropdowns have no single button
  //           to disable.
  // statusEl: element that receives the spinner + label. Nullable.
  // label:    text shown while busy.
  //
  // Returns endBusy(finalText, isError), to be called exactly once when the
  // operation settles. finalText may be omitted/null to just clear the busy
  // state silently (e.g. when the caller is about to render its own richer
  // UI, or the operation's result speaks for itself).
  function beginBusy(btn, statusEl, label){
    if(btn) btn.disabled = true;
    var marksBrand = !!(statusEl && MARK_BUSY_STATUS_IDS[statusEl.id]);
    if(marksBrand) markBusyBegin();
    if(statusEl){
      statusEl.textContent = '';
      statusEl.className = 'busy-line';
      statusEl.style.display = '';
      var spin = document.createElement('span');
      spin.className = 'busy-spin';
      spin.setAttribute('aria-hidden', 'true');
      statusEl.appendChild(spin);
      statusEl.appendChild(document.createTextNode(label || 'Working...'));
    }
    var settled = false;
    return function endBusy(finalText, isError){
      if(settled) return;
      settled = true;
      if(btn) btn.disabled = false;
      if(marksBrand) markBusyEnd();
      if(!statusEl) return;
      if(finalText == null){
        statusEl.style.display = 'none';
        statusEl.textContent = '';
        return;
      }
      statusEl.textContent = finalText;
      statusEl.className = 'busy-line busy-line-' + (isError ? 'fail' : 'done');
    };
  }

  // ---- central design state ----------------------------------------------
  var design = {
    printer: "trident",
    shape: "circle", radius: 32, height: 60, layer_height: 0.30,
    z_waves: 5, xy_twist: 0, z_twist: 0,
    base_layers: 2, brim: 0, squish: 0.75, spacing_factor: 1.25,
    first_layer_height: null,
    spine_mm: 0, spine_deg: 0, ovality: 0,
    base_style: "spiral", skirt: 0,
    bottom: "solid",
    star_points: 5, star_depth: 0.35,
    print_speed: 40, filament: "", line_width: null, nozzle_temp: null, bed_temp: null,
    pattern: "", pattern_amp: 1.0, pattern_waves: 12,
    pattern_bands: 6, pattern_twist: 0, pattern_phase: 0,
    pattern_fade_in: 0.10, pattern_fade_out: 0, pattern_alternate: false,
    amp_profile: [[0,0],[0.2,0.3],[0.4,0.6],[0.6,0.8],[0.8,0.8],[1.0,0.5]],
    radius_profile: [[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]],
    radius_profile_smooth: false,
    width_profile: [[0,1],[1,1]],
    sil3d: false,
    sil_mode: "sym",
    cage: null,
    nozzle: "",
    loop_style: "chainmail",
    loop_per_turn: 0,
    loop_spacing_mm: 4.0,
    loop_turn_stride: 1,
    loop_align: "stagger",
    loop_jitter: 0.5,
    loop_row: 2.5,
    loop_up: 3.5,
    loop_out: 0.5,
    loop_rejoin: 2.0,
    loop_dwell: 0,
    loop_flow: 1.2,
    loop_speed: 10,
    loop_cuff: 3,
    loop_mode: "dip",
    loop_lean: 20,
    loop_coast: 0.8,
    loop_retract: 0,
    loop_wave_amp: 0,
    loop_waves: 12,
    loop_fade_in: 0.10,
    loop_fade_out: 0,
    overhang_flow_k: 0.0,
    // Overhang-adaptive part-cooling fan (0-100%, UI convention -- converted
    // to a 0..1 fraction in the /api/generate body). Both at 100 = off
    // (today's constant full-fan default, byte-identical output).
    fan_overhang_min: 100,
    fan_overhang_max: 100,
    // Total layers (base + wall) to keep the fan off from the start of the
    // print. 0 = today's default (off through any base, or the wall's first
    // turn otherwise) -- see _fan_on_threshold() in continuous_spiral.py.
    fan_off_layers: 0,
    // ---- Point Edit Modifiers (post-slice deformation; see point_edit.py) ----
    point_mask_enable: false,
    point_mask_channel: 'checker',
    point_mask_scale_u: 8,
    point_mask_scale_v: 6,
    point_mask_invert: false,
    point_protection_enable: false,
    point_protection_bottom: 0,
    point_protection_top: 0,
    point_protection_falloff: 0.08,
    point_ffd_enable: false,
    point_ffd_grid: null,
    point_ffd_strength: 1.0,
    point_smooth_enable: false,
    point_smooth_iterations: 2,
    point_smooth_theta: 0.5,
    point_smooth_t: 0.5,
    point_smooth_strength: 1.0,
    point_radial_push_enable: false,
    point_radial_push_amp: 1.0,
    point_radial_push_strength: 1.0,
    // ---- Zone Overrides (height-band GENERATION overrides; see paths.py's
    // ZoneOverride -- a different subsystem from Point Edit above, which
    // deforms the already-sliced path instead). Each entry:
    //   { enabled, t_lo, t_hi, blend, pattern, pattern_amp, xy_twist }
    // pattern: '' means "(use global)" (inherit); any real pattern name is an
    // override; there is no separate "explicit smooth" option in the UI (the
    // server accepts one via pattern:"" in the request, but exposing a
    // fourth state -- inherit / a pattern / explicitly none -- in one select
    // was judged not worth the confusion for v1). pattern_amp/xy_twist: null
    // means inherit; a number overrides.
    zone_overrides: []
  };

  // Snapshot of the shipped defaults, taken before any saved state is merged
  // in below -- lets the active-summary chip row (see updateActiveSummary())
  // tell "user changed this" apart from "this just happens to have a value".
  var DEFAULT_DESIGN = JSON.parse(JSON.stringify(design));

  // Load persisted state, if any (merge over defaults so new fields survive).
  //
  // `cachedDesign` records what was found so the session-restore prompt at the
  // bottom of this file can offer it back explicitly.
  //
  // The comparison against DEFAULT_DESIGN ignores keys the app derives for
  // itself, because those differ on a first run where the user has touched
  // nothing -- prompting then would be a dialog offering to restore a design
  // the user never made. Measured on a wiped profile, init writes back:
  //   printer / bed_center  the machine, which "Start new" keeps regardless,
  //                         so both buttons would do the same thing
  //   filament              auto-picked from the server's filament list
  //   loop_row / loop_up    clamped down to the printer's z_amp_max (0.65 and
  //                         0.95 on a Trident, against 2.5/3.5 shipped)
  //   point_ffd_grid        structural: null becomes a zeroed grid
  // Trade-off worth knowing: a session whose ONLY change was one of these
  // won't prompt. That errs toward not nagging, and costs nothing -- the
  // design is still restored exactly as it was before this dialog existed.
  var RESTORE_IGNORED_KEYS = { printer:1, bed_center:1, filament:1,
                               loop_row:1, loop_up:1, point_ffd_grid:1 };
  var cachedDesign = null;
  try {
    var saved = JSON.parse(localStorage.getItem('design-state') || 'null');
    if(saved && typeof saved === 'object'){
      for(var k in saved){ if(saved.hasOwnProperty(k)) design[k] = saved[k]; }
      // Migrate legacy sil3d boolean (pre mode-bar UI) to the new sil_mode field.
      // Only fires for old saves that predate sil_mode -- once it's saved with
      // a sil_mode value, this branch never re-triggers.
      if(saved.sil3d === true && !saved.sil_mode) design.sil_mode = 'asym';
      for(var ck in saved){
        if(!saved.hasOwnProperty(ck) || RESTORE_IGNORED_KEYS[ck]) continue;
        if(JSON.stringify(saved[ck]) !== JSON.stringify(DEFAULT_DESIGN[ck])){
          cachedDesign = saved;
          break;
        }
      }
    }
  } catch(e){ /* ignore corrupt state */ }

  // ---- undo / redo history --------------------------------------------------
  // Every persistDesign() call that actually changed the design pushes the
  // PREVIOUS state onto the undo stack. Undo/redo restore whole snapshots.
  var HIST_MAX = 50;
  var undoStack = [], redoStack = [];
  var histSuppress = false;                       // true while applying undo/redo
  var lastSnap = JSON.stringify(design);          // state as of last persist

  // Coalescing. A continuous edit -- dragging a range slider, holding a number
  // spinner, dragging a curve point or a cage handle -- fires `input` once per
  // pixel of travel. Pushing a snapshot per event made one drag land as dozens
  // of history entries, so Ctrl+Z crawled the value back a step at a time
  // instead of returning to where the control stood before the drag. Callers
  // that fire in a stream pass a stable `coalesceKey` (one per control): the
  // FIRST persist of a run pushes the pre-edit snapshot and every later persist
  // in the same run folds into that one entry, so a single undo jumps straight
  // back to the previously settled value.
  //
  // A run ends when: the control commits (change/blur -- bindNumber calls
  // endHistRun() there), a DIFFERENT key or an un-keyed discrete change takes
  // over, the pointer is released (below), or HIST_IDLE_MS passes with no
  // further edits. The idle timer is the backstop for controls with no commit
  // event at all (the curve editors and the shape cage, which only ever report
  // through a change handler).
  var HIST_IDLE_MS = 700;
  var histRunKey = null, histRunTimer = null;

  function endHistRun(){
    // Ignores any event arg (this is passed directly as a pointerup/
    // pointercancel/change listener in several places) -- flushes whatever
    // logical edit was open using the CURRENT lastSnap, which histFinish's
    // own callers below guarantee is the correct post-state at every call
    // site. Must run before histRunKey is cleared: histFinish reads only
    // histPre, not histRunKey, but keeping this ordering matches
    // persistDesign's own "close the previous run before opening the next
    // one" sequencing.
    histFinish(lastSnap);
    histRunKey = null;
    if(histRunTimer){ clearTimeout(histRunTimer); histRunTimer = null; }
  }
  function armHistRun(key){
    histRunKey = key;
    if(histRunTimer) clearTimeout(histRunTimer);
    histRunTimer = setTimeout(endHistRun, HIST_IDLE_MS);
  }
  // Letting go of the pointer ends a manipulation, whatever was being
  // manipulated -- slider, curve canvas or cage handle. Registered on the
  // capture phase so it still lands if a handler stops propagation, and on
  // `pointerup` (mouse, pen and touch alike) rather than `mouseup`.
  document.addEventListener('pointerup', endHistRun, true);
  document.addEventListener('pointercancel', endHistRun, true);
  // Safety net for the zone-edge drag's deferred persist (setZoneEdge,
  // further down this file, a hoisted function declaration so naming it
  // here -- before its textual definition -- is fine): a pending persist
  // must never outlive the gesture that created it, or a refresh mid-drag
  // loses the edit. Covers pointercancel on the modal axis path too, which
  // that path's own listeners don't. A flush with nothing pending is a
  // no-op.
  document.addEventListener('pointerup', flushZoneEdgePersist, true);
  document.addEventListener('pointercancel', flushZoneEdgePersist, true);

  // Stale-gcode tracking: designRev bumps on every real design change;
  // generatedRev records the revision the loaded G-code was generated from.
  var designRev = 0, generatedRev = -1;

  function updateStaleBadge(){
    var stale = generatedRev >= 0 && designRev !== generatedRev;
    var badge = document.getElementById('stale-badge');
    if(badge) badge.style.display = stale ? 'block' : 'none';
    var btn = document.getElementById('gen-btn');
    if(btn) btn.classList.toggle('stale', stale);
  }

  // `coalesceKey` (optional): a stable id for the control being edited. Passing
  // one folds a stream of edits from that control into a single undo entry --
  // see the coalescing note above. Omit it for discrete, one-shot changes
  // (a select, a checkbox, a preset apply); each of those gets its own entry.
  function persistDesign(coalesceKey){
    var key = coalesceKey || null;
    // A different control (or any un-keyed discrete change) taking over closes
    // the previous run, so the next push starts a fresh entry.
    if(key === null || key !== histRunKey) endHistRun();
    var snap = JSON.stringify(design);
    if(snap !== lastSnap){
      designRev++;
      if(!histSuppress){
        // histRunKey is non-null only mid-run, and mid-run the entry already on
        // top of the stack IS the pre-edit state -- keep it, don't stack another.
        if(histRunKey === null){
          undoStack.push(lastSnap);
          if(undoStack.length > HIST_MAX) undoStack.shift();
          redoStack.length = 0;
          // Same guard as the undo push right above (histRunKey === null,
          // !histSuppress, snap actually changed) -- captured here, not
          // read back off undoStack later, so the edit-history log's
          // granularity is provably identical to the undo stack's, without
          // undoStack's own HIST_MAX shifting or doUndo/doRedo's pushes
          // ever being mistaken for "the pre-state of the run now closing".
          histPre = histLogArmed ? lastSnap : null;
        }
        if(key !== null) armHistRun(key);
      }
    }
    lastSnap = snap;
    // Discrete (un-keyed) edits -- a <select>, a checkbox, a preset apply --
    // never open a coalescing run for endHistRun() to later close, so they
    // must finalize their own history entry right here, on arrival. Keyed
    // edits (histRunKey !== null) are still mid-run and skip this; their
    // entry lands when the run ends (endHistRun, above).
    if(histRunKey === null){
      // An explicit label (import/preset/reset) names an EVENT, not a value
      // change -- it must still record even when the loaded/applied design
      // happens to be byte-identical to what was already live (e.g.
      // importing a file that round-trips your current design exactly),
      // which is exactly the case histFinish's histPre-gate below would
      // otherwise drop. histPre is cleared here rather than left for a
      // later, unrelated endHistRun() to find and misattribute: it may have
      // been armed by the undo-push above if snap DID change, but has
      // nothing left to auto-diff once the explicit label has spoken for
      // this edit.
      if(histLogArmed && histLabelOnce !== null){
        histPre = null;
        histAppend(histLabelOnce);
      } else {
        histFinish(snap);
      }
    }
    histLabelOnce = null;   // consume-or-discard: never leaks onto the
                             // user's NEXT, unrelated edit.
    updateHistButtons();
    updateStaleBadge();
    updateActiveSummary();
    try { localStorage.setItem('design-state', snap); } catch(e){}
  }

  function restoreSnapshot(snap){
    try {
      var s = JSON.parse(snap);
      for(var k in s){ if(s.hasOwnProperty(k)) design[k] = s[k]; }
      histSuppress = true;
      previewArmed = true;             // undo/redo is a real user interaction
      applyDesignToUI();               // refreshes controls + persists + preview
      histSuppress = false;
      lastSnap = snap;
      updateHistButtons();
    } catch(e){
      histSuppress = false;
      console.error('undo/redo restore failed:', e);
    }
  }

  function doUndo(){
    // Close any run first: the entry we are about to pop must be complete, and
    // the next edit must not fold itself into a pre-undo run. This also
    // flushes any pending edit-history entry (chronologically before the
    // undo's own entry below).
    endHistRun();
    if(!undoStack.length) return;
    var cur = JSON.stringify(design);
    // Logged here, not through persistDesign/histPre: restoreSnapshot below
    // sets histSuppress = true, which is exactly the flag that stops
    // histPre from ever being set (persistDesign's own guard), so this is
    // the only place an undo's own entry can be recorded.
    histAppend('undo -- ' + (describeDesignDiff(cur, undoStack[undoStack.length - 1]) || 'restored previous state'));
    redoStack.push(cur);
    restoreSnapshot(undoStack.pop());
  }
  function doRedo(){
    endHistRun();
    if(!redoStack.length) return;
    var cur = JSON.stringify(design);
    histAppend('redo -- ' + (describeDesignDiff(cur, redoStack[redoStack.length - 1]) || 'reapplied next state'));
    undoStack.push(cur);
    restoreSnapshot(redoStack.pop());
  }
  function updateHistButtons(){
    var u = document.getElementById('undo-btn'), r = document.getElementById('redo-btn');
    if(u) u.disabled = undoStack.length === 0;
    if(r) r.disabled = redoStack.length === 0;
  }

  (function(){
    var u = document.getElementById('undo-btn'), r = document.getElementById('redo-btn');
    if(u) u.addEventListener('click', doUndo);
    if(r) r.addEventListener('click', doRedo);
  })();
  // Debug hook (harmless in production). `run` is the open coalescing key, if
  // any -- dev_smoke.html asserts a drag stays in one run and one entry.
  window.__hist = function(){
    return { undo: undoStack.length, redo: redoStack.length, run: histRunKey };
  };
  // Test-only: force a coalescing run closed without waiting out HIST_IDLE_MS.
  window.__histEndRun = endHistRun;

  // ---- edit-history log ------------------------------------------------
  // A human-readable log of what changed, exported inside a .trident file
  // (buildGenerateBody's Save handler, further down) so a design shared
  // with someone else carries its own "here's what happened" record, not
  // just the final numbers. NOT the same thing as #telemetry-card, which is
  // the live G-code playback HUD (speed/flow under the playhead) -- naming
  // here deliberately says "history", never "telemetry", to keep the two
  // apart at a glance.
  //
  // One entry per COMPLETED edit, riding the exact same coalescing the undo
  // stack already does (a whole drag, a whole field commit) -- never one
  // per pointermove or per keystroke. See histFinish()/the two hook sites
  // in endHistRun() and persistDesign() below for how that is guaranteed.
  var HIST_LOG_KEY = 'design-edit-log';   // own localStorage key -- never overloads 'design-state'
  var HIST_LOG_MAX = 200;                 // ~110 bytes/entry * 200 =~ 22KB, well under quota;
                                           // roughly 4x a dense session's completed-edit count, so
                                           // several export/import/edit rounds accumulate without
                                           // the oldest context falling off, and the modal stays a
                                           // list, not a wall.
  var TRIDENT_FORMAT = 'trident-design';
  var TRIDENT_FORMAT_VERSION = 1;

  var histLog = [];        // [{at: ISO8601 string, summary: string}, ...], oldest first
  var histPre = null;      // pre-edit snapshot STRING of the logical edit currently in flight
  var histLabelOnce = null; // explicit summary for the next logged entry (load/preset/reset/undo/redo);
                             // consumed-or-discarded by persistDesign so it can never leak onto an
                             // unrelated later edit.
  var histLogArmed = false; // true once the user has actually touched the page. NOT previewArmed --
                             // the printer combobox (a custom listbox, see selectPrinter) fires no
                             // change/input on #design-group, so previewArmed would miss a real
                             // printer change; and loadPrinterOptions() persists on every page load
                             // (RESTORE_IGNORED_KEYS above documents exactly which fields that
                             // touches), which would otherwise log bogus entries on every reload.
  document.addEventListener('pointerdown', function(){ histLogArmed = true; }, true);
  document.addEventListener('keydown', function(){ histLogArmed = true; }, true);

  try {
    var savedHist = JSON.parse(localStorage.getItem(HIST_LOG_KEY) || '[]');
    if(Array.isArray(savedHist)) histLog = savedHist.slice(-HIST_LOG_MAX);
  } catch(e){ /* ignore corrupt log */ }

  function saveHistLog(){
    try { localStorage.setItem(HIST_LOG_KEY, JSON.stringify(histLog)); } catch(e){}
  }

  function historyModalOpen(){
    var m = document.getElementById('history-modal');
    return !!(m && m.style.display !== 'none');
  }

  // The single place an entry is actually recorded. Deliberately takes only
  // {at, summary} -- a loaded file's entries are passed through verbatim
  // (see the Load handler below) rather than rebuilt, and renderHistList()
  // reads only these two keys and ignores anything else, which is what
  // keeps a future field (e.g. print/test notes) addable later without a
  // format break.
  function histAppend(summary){
    if(!summary) return;
    histLog.push({ at: new Date().toISOString(), summary: String(summary) });
    if(histLog.length > HIST_LOG_MAX) histLog.shift();
    saveHistLog();
    if(historyModalOpen() && typeof renderHistList === 'function') renderHistList();
  }

  // The single finalization point for a logical edit. Reads and clears ONLY
  // histPre -- never undoStack/redoStack/histRunKey/histRunTimer/lastSnap/
  // histSuppress/designRev -- which is the entire safety argument for why
  // this cannot affect undo/redo: it observes the same state those already
  // maintain, but never writes to any of it.
  function histFinish(postSnap){
    if(histPre === null) return;
    var pre = histPre;
    histPre = null;   // clear first: re-entrancy safe, a stray second call is a no-op
    var s = histLabelOnce || describeDesignDiff(pre, postSnap);
    histAppend(s);
  }

  // Pure string-in/string-out: two design JSON snapshots -> one short,
  // human-readable line describing what changed between them. Never reads
  // live `design` -- persistDesign's "a different control takes over" path
  // can call endHistRun() (which calls this via histFinish) AFTER design
  // has already been mutated to the NEW run's value while lastSnap still
  // holds the OLD run's final state, so only the two snapshot strings are
  // trustworthy here.
  var HIST_FIELD_LABELS = {
    xy_twist:'XY twist', z_twist:'Z twist', layer_height:'layer height',
    line_width:'line width', z_waves:'Z waves', pattern_amp:'texture depth',
    pattern_twist:'pattern twist', spine_mm:'spine offset', spine_deg:'spine angle',
    ovality:'ovality', first_layer_height:'first layer height', squish:'first layer squish',
    overhang_flow_k:'overhang flow', nozzle_temp:'nozzle temp', bed_temp:'bed temp',
    base_layers:'base layers', brim:'brim', skirt:'skirt', print_speed:'print speed',
    spacing_factor:'first layer spacing', fan_overhang_min:'min overhang fan',
    fan_overhang_max:'max overhang fan', fan_off_layers:'fan-off layers',
    loop_row:'loop row height', loop_up:'loop height', loop_style:'loop style',
    loop_align:'loop alignment', loop_mode:'loop mode', star_points:'star points',
    lean_mm:'lean', lean_deg:'lean direction', base_style:'base style',
    bottom:'bottom style', sil_mode:'silhouette mode'
  };
  var HIST_FIELD_UNITS = {
    xy_twist:'', z_twist:'', layer_height:'mm', line_width:'mm', radius:'mm',
    height:'mm', pattern_amp:'mm', pattern_twist:'', spine_mm:'mm', spine_deg:'deg',
    first_layer_height:'mm', squish:'%', overhang_flow_k:'', nozzle_temp:'C',
    bed_temp:'C', base_layers:'', brim:'mm', skirt:'mm', print_speed:'mm/s',
    spacing_factor:'', loop_row:'mm', loop_up:'mm', lean_mm:'mm', lean_deg:'deg'
  };
  // Fields whose CONTENTS must never appear in a summary -- named only, and
  // only when the change is a genuine edit (both sides non-null). One side
  // null <-> non-null is a DERIVED transition (activateSilMode's ensureCage,
  // applyDesignToUI's own re-derivation), not something the user typed, so
  // it is skipped entirely rather than reported as "adjusted".
  var HIST_OPAQUE_FIELDS = {
    cage:'shape cage adjusted', point_ffd_grid:'point-edit cage adjusted',
    amp_profile:'wave amplitude curve edited', radius_profile:'silhouette curve edited',
    width_profile:'line width curve edited'
  };
  // Derived, never user intent -- always skipped.
  var HIST_SKIP_FIELDS = { bed_center:1, sil3d:1 };

  function histFmtNum(n){
    if(typeof n !== 'number' || !isFinite(n)) return String(n);
    var s = n.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
    return s === '' || s === '-0' ? '0' : s;
  }
  function histFieldPart(key, a, b){
    if(HIST_SKIP_FIELDS[key]) return null;
    if(HIST_OPAQUE_FIELDS.hasOwnProperty(key)){
      if(a == null || b == null) return null;   // derived null<->grid transition, not an edit
      return HIST_OPAQUE_FIELDS[key];
    }
    var label = HIST_FIELD_LABELS[key] || key.replace(/_/g, ' ');
    if(typeof a === 'boolean' || typeof b === 'boolean'){
      return label + ' ' + (b ? 'on' : 'off');
    }
    if(a === null || b === null){
      // null is this app's own "auto"/"inherit" convention (first_layer_height,
      // line_width, nozzle_temp, bed_temp, pattern fields, ...).
      var known = a === null ? 'auto' : histFmtNum(a) + (HIST_FIELD_UNITS[key] || '');
      var next = b === null ? 'auto' : histFmtNum(b) + (HIST_FIELD_UNITS[key] || '');
      return label + ' ' + known + '->' + next;
    }
    if(typeof a === 'number' && typeof b === 'number'){
      var unit = HIST_FIELD_UNITS[key] || '';
      return label + ' ' + histFmtNum(a) + unit + '->' + histFmtNum(b) + unit;
    }
    if(typeof a === 'string' && typeof b === 'string'){
      var av = a === '' ? '(none)' : a, bv = b === '' ? '(none)' : b;
      return label + ' changed to ' + bv;
    }
    if(Array.isArray(a) || Array.isArray(b) || typeof a === 'object' || typeof b === 'object'){
      // Standing safety net: any array/object field not named in
      // HIST_OPAQUE_FIELDS above still never has its contents emitted.
      return label + ' changed';
    }
    return label + ' changed to ' + b;
  }

  function histZoneMm(t, height){ return (height > 0) ? t * height : 0; }
  // color_idx is deliberately never compared -- assigned once at creation
  // (nextZoneColorIdx), it never changes as a user edit.
  function histPluralZones(n){ return n + (n === 1 ? ' zone' : ' zones'); }
  function describeZoneOverridesDiff(preZones, postZones, postHeight){
    preZones = preZones || []; postZones = postZones || [];
    if(postZones.length > preZones.length) return 'zone added (' + histPluralZones(postZones.length) + ')';
    if(postZones.length < preZones.length){
      return postZones.length === 0 ? 'all zones cleared' : 'zone removed (' + histPluralZones(postZones.length) + ')';
    }
    var changedIdx = [];
    for(var i = 0; i < postZones.length; i++){
      if(JSON.stringify(preZones[i]) !== JSON.stringify(postZones[i])) changedIdx.push(i);
    }
    if(changedIdx.length === 0) return null;
    if(changedIdx.length > 2) return changedIdx.length + ' zones changed';
    var parts = [];
    changedIdx.forEach(function(i){
      var a = preZones[i] || {}, b = postZones[i] || {};
      var n = i + 1;
      if(a.t_lo !== b.t_lo || a.t_hi !== b.t_hi){
        parts.push('zone ' + n + ' band moved to ' +
          histFmtNum(histZoneMm(b.t_lo, postHeight)) + '-' + histFmtNum(histZoneMm(b.t_hi, postHeight)) + 'mm');
      }
      if(a.blend !== b.blend){
        parts.push('zone ' + n + ' blend ' + histFmtNum(a.blend) + '->' + histFmtNum(b.blend));
      }
      if(a.pattern !== b.pattern){
        parts.push('zone ' + n + ' texture changed to ' + (b.pattern || '(global)'));
      }
      if(a.pattern_amp !== b.pattern_amp){
        parts.push('zone ' + n + ' depth ' + (b.pattern_amp == null ? 'set to inherit' : histFmtNum(a.pattern_amp) + '->' + histFmtNum(b.pattern_amp)));
      }
      if(a.pattern_twist !== b.pattern_twist){
        parts.push('zone ' + n + ' pattern twist ' + (b.pattern_twist == null ? 'set to inherit' : histFmtNum(a.pattern_twist) + '->' + histFmtNum(b.pattern_twist)));
      }
      if(a.xy_twist !== b.xy_twist){
        parts.push('zone ' + n + ' XY twist ' + (b.xy_twist == null ? 'set to inherit' : histFmtNum(a.xy_twist) + '->' + histFmtNum(b.xy_twist)));
      }
      if(a.enabled !== b.enabled){
        parts.push('zone ' + n + ' ' + (b.enabled ? 'enabled' : 'disabled'));
      }
    });
    return parts.join('; ');
  }

  function describeDesignDiff(preStr, postStr){
    var a, b;
    try { a = JSON.parse(preStr); b = JSON.parse(postStr); } catch(e){ return ''; }
    if(!a || !b || typeof a !== 'object' || typeof b !== 'object') return '';
    var keys = {}, k;
    for(k in a){ if(a.hasOwnProperty(k)) keys[k] = 1; }
    for(k in b){ if(b.hasOwnProperty(k)) keys[k] = 1; }
    var parts = [];
    for(k in keys){
      if(!keys.hasOwnProperty(k)) continue;
      if(k === 'zone_overrides'){
        var zp = describeZoneOverridesDiff(a[k], b[k], b.height);
        if(zp) parts.push(zp);
        continue;
      }
      if(JSON.stringify(a[k]) === JSON.stringify(b[k])) continue;
      var part = histFieldPart(k, a[k], b[k]);
      if(part) parts.push(part);
    }
    if(parts.length === 0) return '';
    var out;
    if(parts.length > 8) out = parts.length + ' settings changed';
    else if(parts.length > 3) out = parts.slice(0, 3).join('; ') + ' (+' + (parts.length - 3) + ' more)';
    else out = parts.join('; ');
    return out.length > 140 ? out.slice(0, 137) + '...' : out;
  }

  document.addEventListener('keydown', function(e){
    // Leave native text-field undo alone while typing in an input.
    var tag = e.target && e.target.tagName;
    if(tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    var mod = e.ctrlKey || e.metaKey;
    if(!mod) return;
    if(e.key === 'z' || e.key === 'Z'){
      e.preventDefault();
      if(e.shiftKey) doRedo(); else doUndo();
    } else if(e.key === 'y' || e.key === 'Y'){
      e.preventDefault();
      doRedo();
    }
  });

  // ---- debounced live preview -----------------------------------------------
  // The draft preview stays OFF until the user actually touches a design
  // control — a fresh page load shows the empty bed + drop-gcode card.
  var previewArmed = false;
  var previewTimer = null;
  function schedulePreview(){
    if(!previewArmed) return;
    if(previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(function(){
      if(typeof generatePreview === 'function' && typeof window.showPreview === 'function'){
        var pts = generatePreview(design, effectiveBaseSpec());
        if(pts && pts.length > 0){
          window.showPreview(pts);
          var ov = document.getElementById('overlay');
          if(ov) ov.style.display = 'none';
          if(design.sil3d) refreshShapeCage();
          // Don't rebuild zone rings while a drag is live -- it causes
          // flickering. The drag-end path (zoneEndDrag / flushZoneEdgePersist)
          // already calls refreshZoneRings() once the gesture is over.
          if(typeof refreshZoneRings === 'function' && !window.__zoneRingDragActive) refreshZoneRings();
        }
        // The fabric draft resolves build_loop_fabric's own clamps; report
        // anything it had to cut. No-op for every non-fabric design.
        refreshLoopFabricNote();
      }
    }, 100);
  }

  // Arm the draft preview on the first real user interaction with any design
  // or printer control (init-time fetch callbacks never arm it).
  // The machine bar lives inside #design-group now (above the step wizard,
  // not its own #printer-group), so a single listener already covers it.
  ['design-group'].forEach(function(id){
    var g = document.getElementById(id);
    if(!g) return;
    // Capture phase: arm BEFORE the control's own change handler runs its
    // schedulePreview(), or the very first interaction would be swallowed.
    ['change', 'input', 'dblclick'].forEach(function(ev){
      g.addEventListener(ev, function(){ previewArmed = true; }, true);
    });
    // Curve editors drag with the mouse without firing change/input.
    g.addEventListener('mousedown', function(e){
      if(e.target && e.target.tagName === 'CANVAS') previewArmed = true;
    }, true);
  });

  // ---- resizable splitter -------------------------------------------------
  (function(){
    var splitter = document.getElementById('splitter');
    var panel = document.getElementById('panel');
    var MIN_W = 280, MAX_W = 500, DEFAULT_W = 340;
    // Below the responsive breakpoint the stylesheet stacks the panel
    // full-width; an inline width would override that media query, so only
    // apply (and keep) the persisted width on wide viewports.
    var narrowMq = window.matchMedia('(max-width: 900px)');
    function applyPanelWidth(w){
      if(narrowMq.matches){
        panel.style.width = '';
        panel.style.flexBasis = '';
        return;
      }
      panel.style.width = w + 'px';
      panel.style.flexBasis = w + 'px';
    }
    var saved = parseFloat(localStorage.getItem('panel-width'));
    var startW = (saved && saved >= MIN_W && saved <= MAX_W) ? saved : DEFAULT_W;
    applyPanelWidth(startW);
    narrowMq.addEventListener('change', function(){
      var s = parseFloat(localStorage.getItem('panel-width'));
      applyPanelWidth((s && s >= MIN_W && s <= MAX_W) ? s : DEFAULT_W);
      if(window.__viewerResize) window.__viewerResize();
    });

    var dragging = false, startX = 0, startPanelW = 0;
    function onMove(e){
      if(!dragging) return;
      var dx = e.clientX - startX;   // dragging right grows the panel (left sidebar)
      var w = Math.min(MAX_W, Math.max(MIN_W, startPanelW + dx));
      panel.style.width = w + 'px';
      panel.style.flexBasis = w + 'px';
      if(window.__viewerResize) window.__viewerResize();
    }
    function onUp(){
      if(!dragging) return;
      dragging = false;
      splitter.classList.remove('dragging');
      try { localStorage.setItem('panel-width', parseFloat(panel.style.width)); } catch(e){}
    }
    splitter.addEventListener('mousedown', function(e){
      dragging = true; startX = e.clientX; startPanelW = panel.getBoundingClientRect().width;
      splitter.classList.add('dragging');
      e.preventDefault();
    });
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);

    // Trigger an initial resize once layout has settled.
    requestAnimationFrame(function(){ if(window.__viewerResize) window.__viewerResize(); });
  })();

  // ---- top-level mode bar (Design vs G-code Viewer) ------------------------
  (function(){
    var btns = Array.prototype.slice.call(document.querySelectorAll('.mode-btn'));
    var panels = {
      design: document.getElementById('mode-design'),
      viewer: document.getElementById('mode-viewer')
    };
    // #step-nav lives outside #panel-scroll now (pinned to the bottom of
    // #panel, see index.html/style.css) so it is no longer a .mode-panel
    // that display:none/.active toggling covers automatically -- drive it
    // from the same activateMode() switch so it still only shows in Design.
    var stepNavEl = document.getElementById('step-nav');
    function activateMode(name){
      if(!panels[name]) name = 'design';
      btns.forEach(function(btn){
        var active = btn.dataset.mode === name;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      for(var k in panels){
        if(panels[k]) panels[k].classList.toggle('active', k === name);
      }
      if(stepNavEl) stepNavEl.classList.toggle('active', name === 'design');
      // Not persisted: every load starts in Design (see activateMode's call
      // site below). Writing a key nothing reads would just resurrect the
      // stale value this change exists to get rid of.
      // The measure tool's rail lives on the canvas, which is visible in both
      // modes, so it cannot hide itself off the panel's .active class.
      // Canvas-floating chrome (measure rail, telemetry card) lives on the
      // canvas, which is visible in both modes, so it cannot follow the
      // panel's .active class -- viewer.js re-syncs it from here.
      if(window.__syncCanvasChrome) window.__syncCanvasChrome();
      else if(window.__measureAppMode) window.__measureAppMode();
      if(name === 'viewer'){
        // Viewing the generated G-code: drop the live blue draft so the
        // rainbow toolpath is unobstructed. Also cancel any in-flight
        // schedulePreview() debounce -- without this, an edit made just
        // before switching (or just before clicking Generate, which calls
        // this same branch) can still be sitting in its 100ms setTimeout;
        // left alive, it fires AFTER clearPreview() above, redrawing the
        // stale zone-tinted draft (a different, possibly differently-shaped
        // revision of the design) as a ghost detached from the real toolpath
        // now on screen. Clearing it here is safe unconditionally: going
        // back to Design re-arms via schedulePreview() in the else branch.
        if(previewTimer){ clearTimeout(previewTimer); previewTimer = null; }
        if(window.clearPreview) window.clearPreview();
        if(window.__viewerResize) window.__viewerResize();
      } else {
        // Back to designing: re-show the live blue draft of the current shape
        // (schedulePreview no-ops until the user has interacted at least once).
        schedulePreview();
      }
    }
    btns.forEach(function(btn){
      btn.addEventListener('click', function(){ activateMode(btn.dataset.mode); });
    });
    window.setAppMode = activateMode;
    // A load always starts in Design, never in the G-code Viewer.
    //
    // The mode used to be replayed from localStorage, but it is not a
    // preference -- it is a consequence. A successful Generate switches to
    // 'viewer' (see the generate handler), which persisted, so the NEXT load
    // opened the viewer. The generated G-code only ever lives in memory, so
    // after a reload there is nothing to view: the user landed on an empty
    // drop-zone panel instead of the design they were working on.
    //
    // Opening a session is opening a project, and a project starts at the
    // design. The stale 'app-mode' key is cleared rather than left behind,
    // so an older browser profile does not keep a value nothing reads.
    activateMode('design');
    try { localStorage.removeItem('app-mode'); } catch(e){}
  })();

  // ---- design wizard step bar (Model / Texture / Print / Generate) --------
  var activateStep = (function(){
    var STEPS = ['model', 'texture', 'print', 'generate'];
    var items = Array.prototype.slice.call(document.querySelectorAll('.step-item'));
    var panels = {};
    STEPS.forEach(function(name){ panels[name] = document.getElementById('step-' + name); });
    var backBtn = document.getElementById('step-back');
    var nextBtn = document.getElementById('step-next');
    var current = 'model';

    function activate(name){
      if(STEPS.indexOf(name) < 0) name = 'model';
      current = name;
      var idx = STEPS.indexOf(name);
      items.forEach(function(btn){
        var i = STEPS.indexOf(btn.dataset.step);
        btn.classList.toggle('active', btn.dataset.step === name);
        btn.classList.toggle('done', i < idx);
        if(btn.dataset.step === name) btn.setAttribute('aria-current', 'step');
        else btn.removeAttribute('aria-current');
      });
      STEPS.forEach(function(s){
        var p = panels[s];
        if(p) p.classList.toggle('active', s === name);
      });
      if(backBtn) backBtn.disabled = idx <= 0;
      if(nextBtn) nextBtn.disabled = idx >= STEPS.length - 1;
      try { localStorage.setItem('designer-step', name); } catch(e){}
      // Curve editor canvases live inside hidden panels; redraw once visible
      // so they pick up correct layout/CSS colors.
      if(name === 'texture'){
        if(typeof ampEditor !== 'undefined' && ampEditor) ampEditor.draw();
        if(typeof silEditor !== 'undefined' && silEditor) silEditor.draw();
      }
    }
    items.forEach(function(btn){
      btn.addEventListener('click', function(){ activate(btn.dataset.step); });
    });
    if(backBtn) backBtn.addEventListener('click', function(){
      var idx = STEPS.indexOf(current);
      if(idx > 0) activate(STEPS[idx - 1]);
    });
    if(nextBtn) nextBtn.addEventListener('click', function(){
      var idx = STEPS.indexOf(current);
      if(idx < STEPS.length - 1) activate(STEPS[idx + 1]);
    });
    // Same reasoning as the mode above: a load starts the wizard at step 1
    // (Model), not wherever the last session happened to stop. Landing on
    // step 4 (Generate) with a restored design and no way to see what shape
    // it is reads as broken, and "which step was I on" is not a decision
    // worth persisting across a reload -- the design itself is, and that is
    // restored separately (see the session-restore prompt).
    activate('model');
    try { localStorage.removeItem('designer-step'); } catch(e){}
    return activate;
  })();

  // ---- collapsible sidebar sections ---------------------------------------
  // Every h3.section-heading / .group h2 that owns a '#sec-body-*' (or, for
  // the STL importer, the pre-existing '#import-panel') container becomes a
  // real keyboard-operable disclosure toggle. Open/closed state persists
  // under one localStorage key so a user's choices survive a reload; a
  // section not yet visited falls back to the `defaultOpen` passed at
  // registration time. Collapsing uses display:none on the BODY container
  // only -- the heading (and its bound listeners) and every input inside the
  // body stay in the DOM untouched, so nothing about live-preview wiring
  // changes.
  var SECTIONS_KEY = 'trident_sections';
  var sectionState = {};
  try { sectionState = JSON.parse(localStorage.getItem(SECTIONS_KEY) || '{}') || {}; } catch(e){ sectionState = {}; }
  var sectionsByKey = {};
  var sectionList = [];

  function persistSectionState(){
    try { localStorage.setItem(SECTIONS_KEY, JSON.stringify(sectionState)); } catch(e){}
  }

  function setSectionOpen(sec, open, skipPersist){
    sec.headingEl.setAttribute('aria-expanded', open ? 'true' : 'false');
    sec.headingEl.classList.toggle('sec-closed', !open);
    sec.bodyEl.style.display = open ? '' : 'none';
    if(!skipPersist){
      sectionState[sec.key] = open;
      persistSectionState();
    }
  }

  function registerSection(key, headingEl, bodyEl, defaultOpen){
    if(!headingEl || !bodyEl) return null;
    headingEl.classList.add('sec-toggle');
    headingEl.setAttribute('role', 'button');
    if(!headingEl.hasAttribute('tabindex')) headingEl.setAttribute('tabindex', '0');
    if(bodyEl.id) headingEl.setAttribute('aria-controls', bodyEl.id);
    var chevron = document.createElement('span');
    chevron.className = 'sec-chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.innerHTML = '&#9662;';
    headingEl.appendChild(chevron);

    var sec = { key: key, headingEl: headingEl, bodyEl: bodyEl };
    var open = sectionState.hasOwnProperty(key) ? !!sectionState[key] : !!defaultOpen;
    setSectionOpen(sec, open, true);

    function toggle(){ setSectionOpen(sec, sec.bodyEl.style.display === 'none'); }
    headingEl.addEventListener('click', toggle);
    headingEl.addEventListener('keydown', function(e){
      if(e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar'){
        e.preventDefault();
        toggle();
      }
    });

    sectionsByKey[key] = sec;
    sectionList.push(sec);
    return sec;
  }

  // Force a section open by its registration key (used by the active-summary
  // chip row -- clicking a chip must reveal the control it summarizes).
  function expandSectionByKey(key){
    var sec = sectionsByKey[key];
    if(sec) setSectionOpen(sec, true);
  }

  // Force open whichever registered section(s) contain the given element (used
  // by the parameter search -- jumping to a control inside a collapsed
  // section must expand that section first).
  function expandSectionsContaining(el){
    if(!el) return;
    for(var i = 0; i < sectionList.length; i++){
      if(sectionList[i].bodyEl.contains(el)) setSectionOpen(sectionList[i], true);
    }
  }

  registerSection('shape', document.getElementById('sec-head-shape'), document.getElementById('sec-body-shape'), true);
  registerSection('asymmetry', document.getElementById('sec-head-asymmetry'), document.getElementById('sec-body-asymmetry'), false);
  registerSection('importstl', document.getElementById('sec-head-importstl'), document.getElementById('import-panel'), false);
  registerSection('silhouette', document.getElementById('sec-head-silhouette'), document.getElementById('sec-body-silhouette'), false);
  registerSection('zwaves', document.getElementById('sec-head-zwaves'), document.getElementById('sec-body-zwaves'), false);
  registerSection('texturepattern', document.getElementById('sec-head-texturepattern'), document.getElementById('sec-body-texturepattern'), true);
  registerSection('cooling', document.getElementById('sec-head-cooling'), document.getElementById('sec-body-cooling'), true);
  // No 'printer' section anymore -- the printer now lives in the always-on
  // #machine-bar, not a collapsible group (see B.1 in the machine-limits spec).
  registerSection('printstats', document.getElementById('sec-head-printstats'), document.getElementById('sec-body-printstats'), true);
  registerSection('display', document.getElementById('sec-head-display'), document.getElementById('sec-body-display'), true);

  // ---- hint-density toggle --------------------------------------------------
  // Every .hint block defaults visible -- hiding them is opt-in and
  // persisted, so nothing changes for existing users unless they click it.
  (function(){
    var HINTS_KEY = 'trident_hints_visible';
    var btn = document.getElementById('toggle-hints');
    if(!btn) return;
    var visible = true;
    try {
      var saved = localStorage.getItem(HINTS_KEY);
      if(saved !== null) visible = saved === '1';
    } catch(e){}
    function apply(){
      document.body.classList.toggle('hints-hidden', !visible);
      btn.setAttribute('aria-pressed', visible ? 'true' : 'false');
    }
    apply();
    btn.addEventListener('click', function(){
      visible = !visible;
      apply();
      try { localStorage.setItem(HINTS_KEY, visible ? '1' : '0'); } catch(e){}
    });
  })();

  // ---- active-summary chip row ---------------------------------------------
  // Compact read-only chips at the top of the Design panel: one per NON-
  // DEFAULT / active setting, so the user can see what's switched on without
  // hunting through (possibly collapsed) sections. Recomputed from
  // updateActiveSummary() below, called at the end of persistDesign() --
  // the single choke point every real design mutation already funnels
  // through -- rather than inventing a second update path.
  function computeActiveChips(){
    var chips = [];
    if(design.shape !== DEFAULT_DESIGN.shape || design.radius !== DEFAULT_DESIGN.radius ||
       design.height !== DEFAULT_DESIGN.height){
      chips.push({ text: design.shape + ' ' + design.radius + '×' + design.height + 'mm',
                   step: 'model', section: 'shape' });
    }
    if(design.pattern){
      chips.push({ text: 'pattern: ' + design.pattern, step: 'texture', section: 'texturepattern' });
    }
    if(Math.round(design.z_waves || 0) !== Math.round(DEFAULT_DESIGN.z_waves || 0)){
      var zw = Math.round(design.z_waves || 0);
      chips.push({ text: zw + ' Z-wave' + (zw === 1 ? '' : 's'), step: 'texture', section: 'zwaves' });
    }
    if(design.sil_mode === 'asym'){
      chips.push({ text: 'asymmetric cage', step: 'texture', section: null });
    }
    if(design.spine_mm){
      chips.push({ text: 'lean ' + design.spine_mm + 'mm', step: 'model', section: 'asymmetry' });
    }
    if(design.ovality){
      chips.push({ text: 'oval ' + design.ovality, step: 'model', section: 'asymmetry' });
    }
    if(typeof pointEditAnyEnabled === 'function' && pointEditAnyEnabled()){
      var peCount = [pointEditMaskMeaningful(), pointEditProtectionMeaningful(), pointEditFFDMeaningful(),
                     pointEditSmoothMeaningful(), pointEditRadialPushMeaningful()].filter(Boolean).length;
      chips.push({ text: peCount + ' point-edit mod' + (peCount === 1 ? '' : 's'),
                   step: 'texture', openPE: true });
    }
    if(typeof zoneOverridesAnyEnabled === 'function' && zoneOverridesAnyEnabled()){
      var zoCount = (design.zone_overrides || []).filter(zoneOverrideMeaningful).length;
      chips.push({ text: zoCount + ' zone override' + (zoCount === 1 ? '' : 's'),
                   step: 'texture', openZO: true });
    }
    if(typeof meshState !== 'undefined' && meshState && meshState.mesh_id){
      chips.push({ text: 'STL: ' + (meshState.filename || 'imported'), step: 'model', section: 'importstl' });
    }
    return chips.slice(0, 8);
  }

  function updateActiveSummary(){
    var host = document.getElementById('active-summary');
    if(!host) return;
    var chips = computeActiveChips();
    host.innerHTML = '';
    chips.forEach(function(c){
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip';
      btn.textContent = c.text;
      btn.addEventListener('click', function(){
        if(window.setAppMode) window.setAppMode('design');
        if(c.step) activateStep(c.step);
        if(c.section) expandSectionByKey(c.section);
        if(c.openPE && window.__openPointEditModal) window.__openPointEditModal();
        if(c.openZO && window.__openZoneModal) window.__openZoneModal();
      });
      host.appendChild(btn);
    });
  }

  // ---- printer combobox -----------------------------------------------------
  // A custom listbox, not a native <select> -- the printer list is now ~17
  // rows (14 printers + 3 group headers, more with custom printers added),
  // and a native select's OS-drawn popup runs past the bottom of a short
  // window with no way to fix it from CSS. Built on the same bounded/
  // scrolling/keyboard-navigable listbox pattern as .param-search-results.
  var printerComboBtn = document.getElementById('printer-combo-btn');
  var printerComboLabel = document.getElementById('printer-combo-label');
  var printerComboList = document.getElementById('printer-combo-list');
  var printerCombo = document.getElementById('printer-combo');
  var printerComboOptions = [];   // flat, header-free, in DOM order: [{key,name,el}]
  var printerComboActiveIdx = -1; // keyboard/hover highlighted row, NOT necessarily the selection
  var printerComboOpenState = false;
  var PRINTER_BEDS = {};   // key -> [bed_x, bed_y]
  var PRINTER_ZCAP = {};   // key -> max Z excursion below printed material (mm)
  var PRINTER_SLOPE = {};  // key -> quality_slope_max, the peak printable wave wall slope
  var PRINTER_META = {};   // key -> full /api/printers entry (custom, source_format, warnings)

  // The amp ceiling a printer entry is allowed to contribute to the UI.
  //
  // This was `p.z_amp_max || 0.95`, which treats a printer declaring ZERO
  // non-planar tolerance as if it had declared nothing, and hands it the
  // Trident's own measured 0.95mm constant. That is the precise mistake
  // CLAUDE.md names twice -- "no machine limit may be a module constant" and
  // "missing config values become conservative defaults, never another
  // machine's" -- and it was reproduced live: a custom printer saved with
  // z_amp_max=0 still showed "max 0.95 mm" in the amp curve editor and let
  // control points sit at 0.8mm. On a machine that declares no clearance,
  // that is a probe strike.
  //
  // Absent or non-finite falls back to 0.4, matching
  // _Z_AMP_MAX_UNKNOWN_DEFAULT in trident_gcode/printer_validate.py. Keep the
  // two in step; never reintroduce a fallback that names another printer's
  // number. Pinned by window.__zAmpCapFor below.
  function zAmpCapFor(v){
    return (typeof v === 'number' && isFinite(v)) ? v : 0.4;
  }
  // Exposed so the invariant is testable without standing up the whole
  // custom-printer flow -- see the assertions in viewer/dev_smoke.html.
  window.__zAmpCapFor = zAmpCapFor;

  // The per-entry fallback for quality_slope_max, mirrored on zAmpCapFor's
  // shape above but NOT its number: zAmpCapFor deliberately falls back to a
  // 0.4 that belongs to no real printer, because z_amp_max varies widely
  // across profiles (0.40 to 4.0) and defaulting to any one of them would be
  // "silently inheriting a different machine's limit". quality_slope_max
  // does not have that problem today -- every shipped profile currently
  // declares the same 0.25 -- so an entry that omits the field reuses
  // SLOPE_LIMIT_FALLBACK rather than inventing a third constant. If a
  // printer ever needs a slope ceiling other than 0.25, the fix is for its
  // profile to send one, not for this fallback to guess.
  function slopeCapFor(v){
    return (typeof v === 'number' && isFinite(v)) ? v : SLOPE_LIMIT_FALLBACK;
  }
  // The active printer's slope ceiling. Reads PRINTER_SLOPE[design.printer]
  // (populated in loadPrinterOptions(), same funnel as PRINTER_ZCAP) so the
  // wave-slope readout and the amp-editor reference line -- see "live
  // wave-slope readout" below -- always agree on which number is live. Falls
  // back to SLOPE_LIMIT_FALLBACK only when the printer list has not loaded
  // yet (no entry at all for design.printer), never as a stand-in for a
  // printer's real, declared value.
  function activeSlopeLimit(){
    var v = PRINTER_SLOPE[design.printer];
    return (typeof v === 'number' && isFinite(v)) ? v : SLOPE_LIMIT_FALLBACK;
  }

  // Test-only injection point for viewer/dev_smoke.html's printer-dependent
  // assertions: stands up a fake printer entry (any subset of the usual
  // /api/printers fields -- bed, z_amp_max, quality_slope_max, has_probe,
  // name) without a real network round trip. Registers the entry only --
  // does NOT switch to it (see window.__testSelectPrinter right below) --
  // so injecting a fake printer never disturbs whichever one is currently
  // selected, and never derives a fallback bed/cap from "whatever happens to
  // be selected right now" the way that would silently corrupt a REAL
  // entry's data if this were reused to restore the original selection.
  window.__testInjectPrinter = function(entry){
    if(!entry || !entry.key) return;
    PRINTER_BEDS[entry.key] = entry.bed || [235, 235];
    PRINTER_ZCAP[entry.key] = zAmpCapFor(entry.z_amp_max);
    PRINTER_SLOPE[entry.key] = slopeCapFor(entry.quality_slope_max);
    PRINTER_META[entry.key] = entry;
  };
  // Test-only printer switch: the same design.printer + applyPrinterCaps()
  // path a real printer switch uses (applyPrinterCaps() is "the one function
  // every printer switch ... funnels through", per its own comment below),
  // without selectPrinter()'s combo-box/notice/persistDesign side effects --
  // chrome the assertions don't need. Works on any key already known to
  // PRINTER_BEDS, real or injected via __testInjectPrinter above, so it also
  // doubles as the restore path back to whatever was selected before a test.
  window.__testSelectPrinter = function(key){
    if(!PRINTER_BEDS.hasOwnProperty(key)) return;
    design.printer = key;
    applyPrinterCaps();
  };
  // Test-only curve override for the amp/silhouette editors -- mirrors the
  // "Load design as JSON" path's ampEditor.setProfile()/silEditor.setProfile()
  // calls. The wave-slope readout depends on the exact curve shape, and
  // localStorage may be carrying a curve left over from an earlier manual
  // session in this same browser profile, so an assertion that needs a KNOWN
  // shape cannot just assume the shipped default is still in place. Pass
  // null for either profile to leave that curve untouched.
  window.__testSetCurves = function(ampProfile, silProfile){
    if(ampProfile){ ampEditor.setProfile(ampProfile); design.amp_profile = ampEditor.profile(); }
    if(silProfile){ silEditor.setProfile(silProfile); design.radius_profile = silEditor.profile(); }
    updateSlope();
  };

  // Prefers the server-computed loop_row_max/loop_up_max (see /api/printers)
  // so the client never re-derives the formula and the two can never drift.
  // Falls back to deriving from z_amp_max with the same formula the server
  // uses, in case an older /api/printers response doesn't carry them yet.
  function loopCapsFor(meta, cap){
    var up = (meta && typeof meta.loop_up_max === 'number') ? meta.loop_up_max : cap;
    var row = (meta && typeof meta.loop_row_max === 'number') ? meta.loop_row_max : Math.max(0.4, cap - 0.3);
    // Server-published floor (see _printer_entry_json's loop_min_mm). The
    // fallback mirrors LOOP_MIN_MM only for an older /api/printers response
    // that predates the field, and is capped the same way the server caps it.
    var min = (meta && typeof meta.loop_min_mm === 'number')
      ? meta.loop_min_mm : Math.min(0.5, row, up);
    return { up: up, row: row, min: min };
  }

  // Set by applyLoopStyle() when a style bundle asked for more loop/row height
  // than this machine allows, and by the fabric draft when build_loop_fabric's
  // OWN clamps (loop height, and the wave amplitude that rides on top of it)
  // cut something further. Both are appended to the ceiling note below so a
  // trim is stated rather than silently applied.
  var loopStyleTrimmed = null;     // style name, or null
  var loopWaveTrimmed = false;

  // Single writer for #loop-zcap. Called from applyPrinterCaps() (which knows
  // the caps and profile meta) and re-called whenever a clamp flag changes, so
  // the two facts can never overwrite each other.
  var _loopNoteCaps = null, _loopNoteMeta = null;
  function refreshLoopZcapNote(caps, meta){
    if(caps){ _loopNoteCaps = caps; _loopNoteMeta = meta; }
    caps = _loopNoteCaps; meta = _loopNoteMeta;
    var note = document.getElementById('loop-zcap');
    if(!note || !caps) return;
    var mname = (meta && meta.name) || 'This printer';
    // caps.up < 1.5 is real signal that something is tightly limiting loop
    // height, but it is only a PROBE if this printer actually declares
    // one (meta.has_probe). The P1S has none -- its low ceiling is just
    // z_amp_max, and saying "probe keep-out" on a probe-less machine is a
    // false attribution the reader has no way to catch on their own.
    var loopLimitNote = caps.up >= 1.5 ? ''
      : ((meta && meta.has_probe) ? ' (probe keep-out limit)' : ' (z-amp ceiling)');
    var text = mname + ' allows loops up to ' + fmtMm(caps.up) +
      'mm tall' + loopLimitNote + '.';
    if(loopStyleTrimmed){
      text += ' The "' + loopStyleTrimmed + '" preset asks for more, so its'
        + ' whole stitch was scaled down to fit (its row-to-loop proportion is'
        + ' kept).';
    }
    if(loopWaveTrimmed){
      text += ' The stitch wave does not fit under that ceiling on top of the'
        + ' loop height, so it was flattened out.';
    }
    note.textContent = text;
  }

  // Reads the resolved-fabric report the draft leaves behind (see
  // preview_math.js's __loopFabricPreview) and reflects build_loop_fabric's
  // own clamps into the note. Called after each fabric draft.
  function refreshLoopFabricNote(){
    var info = window.__loopFabricPreview;
    // `clamped` covers loop height AND wave amplitude; the height half is
    // already carried by loopStyleTrimmed, so only report a wave that the
    // design asked for and the machine removed.
    var wanted = Math.max(0, parseFloat(design.loop_wave_amp) || 0);
    var trimmed = !!(info && info.clamped && wanted > 0 && info.wave_amp < wanted);
    if(trimmed !== loopWaveTrimmed){
      loopWaveTrimmed = trimmed;
      refreshLoopZcapNote();
    }
  }

  // The machine's z-amp ceiling is the single source of truth for every Z-
  // excursion control: the loop row/height inputs' min/max AND the amp curve
  // editor's scale + control points. Reflect it everywhere so the UI can
  // never suggest a value the server would reject or silently clamp -- and
  // clamp anything already sitting out of range (switching TO a stricter
  // printer must not leave stale over-limit values in the inputs). Returns
  // the number of values actually changed, so the caller can tell the user.
  function applyPrinterCaps(){
    var cap = PRINTER_ZCAP[design.printer];
    // cap can legitimately BE 0 (a printer with zero non-planar tolerance);
    // `if(!cap)` would skip applying it and leave the previous printer's
    // stale, looser amp/loop limits in the UI. Only bail when there is
    // genuinely no entry yet (printer list not loaded) or the value is
    // non-finite.
    if(cap == null || typeof cap !== 'number' || !isFinite(cap)) return 0;
    var meta = PRINTER_META[design.printer];
    var caps = loopCapsFor(meta, cap);
    var changed = 0;

    // The live draft clamps wave amplitude too, and it must clamp to the SAME
    // ceiling the server will (serve.py's amp_ceiling). preview_math.js used
    // to carry its own hardcoded 0.95, so on any printer declaring something
    // else the draft silently stopped responding to amplitude edits partway
    // up the curve -- the user drags a point, the request and the printed
    // part change, the picture does not. Pushed from here because this is the
    // one function every printer switch and the initial /api/printers load
    // both funnel through, the same reasoning as the max_z_velocity contract
    // below.
    if(typeof window.setPreviewAmpMax === 'function') window.setPreviewAmpMax(cap);
    // Same contract for the loop-fabric draft: it re-runs _parse_loop_spec's
    // and build_loop_fabric's clamps client-side, so it needs this machine's
    // row/loop ceilings and whether it has a trailing probe (spike mode's wave
    // clamp is gated on that). Pushed from here for the same reason as the
    // amplitude ceiling above -- this is the one funnel every printer switch
    // and the initial /api/printers load both pass through.
    if(typeof window.setPreviewLoopCaps === 'function'){
      window.setPreviewLoopCaps(caps.up, caps.row, !!(meta && meta.has_probe),
                                caps.min);
    }

    var up = document.getElementById('d-loop-up');
    var row = document.getElementById('d-loop-row');
    // The floor is the server's loop_min_mm, NOT the old min(1.0, cap). That
    // expression equals the ceiling on any machine capped at or below 1.0 --
    // ten of the fifteen shipped profiles -- so it pinned both inputs to a
    // single value, re-clamped every loop style back onto that same number
    // (undoing fitLoopStyleToMachine's spread) and left the user unable to
    // type anything finer. See _parse_loop_spec's docstring for the full
    // account; the CEILINGS below are unchanged.
    var floorUp = Math.min(caps.min, caps.up);
    var floorRow = Math.min(caps.min, caps.row);
    if(up){
      up.min = floorUp;
      up.max = Math.round(caps.up * 100) / 100;
      var uv = parseFloat(up.value);
      if(!isNaN(uv)){
        var cu = Math.min(caps.up, Math.max(floorUp, uv));
        if(cu !== uv){ up.value = cu; design.loop_up = cu; changed++; }
      }
    }
    if(row){
      row.min = floorRow;
      row.max = Math.round(caps.row * 100) / 100;
      var rv = parseFloat(row.value);
      if(!isNaN(rv)){
        var cr = Math.min(caps.row, Math.max(floorRow, rv));
        if(cr !== rv){ row.value = cr; design.loop_row = cr; changed++; }
      }
    }
    refreshLoopZcapNote(caps, meta);

    // Nozzle/bed temp ceilings: server-computed (see _printer_entry_json) so
    // the client never re-derives max_nozzle_temp's 320 C absolute backstop
    // and the two can never drift apart -- same shape as loop_up/loop_row
    // above. Clamp a now-out-of-range current value back down, same as those.
    var nozzleT = document.getElementById('d-nozzletemp');
    if(nozzleT && meta && typeof meta.max_nozzle_temp === 'number'){
      nozzleT.max = meta.max_nozzle_temp;
      var ntv = parseFloat(nozzleT.value);
      if(!isNaN(ntv) && ntv > meta.max_nozzle_temp){
        nozzleT.value = meta.max_nozzle_temp;
        design.nozzle_temp = meta.max_nozzle_temp;
        changed++;
      }
    }
    var bedT = document.getElementById('d-bedtemp');
    if(bedT && meta && typeof meta.max_bed_temp === 'number'){
      bedT.max = meta.max_bed_temp;
      var btv = parseFloat(bedT.value);
      if(!isNaN(btv) && btv > meta.max_bed_temp){
        bedT.value = meta.max_bed_temp;
        design.bed_temp = meta.max_bed_temp;
        changed++;
      }
    }

    // Amp curve: rescale the editor to the new ceiling and re-clamp every
    // control point into it, not just a number input -- the amp value lives
    // entirely in amp_profile's control points, there is no separate field.
    // setRange() here carries ONLY the hard hi/lo ceiling -- it no longer
    // takes a reference value/label at all (see its own comment in
    // makeEditor()). updateSlope(), called immediately after, derives both
    // ceiling labels from the same slope math as the readout (via
    // ampEditor.setHardWallLabel()/setSoftLimit()) and always wins, so the
    // panel can never show different numbers than the editor.
    if(typeof ampEditor !== 'undefined' && ampEditor){
      changed += ampEditor.setRange(cap);
      design.amp_profile = ampEditor.profile();
      updateSlope();
    }
    var ampHint = document.getElementById('amp-limit-hint');
    if(ampHint){
      // Same false-attribution fix as loop-zcap above: this is a probe
      // figure only when the selected printer actually has a probe.
      var ampIsProbeLimit = !!(meta && meta.has_probe);
      ampHint.textContent = 'max ' + fmtMm(cap) + ' mm - ' +
        ((meta && meta.name) || 'selected printer') +
        (ampIsProbeLimit ? ' probe keep-out' : ' z-amp ceiling');
    }

    // Cross-agent contract (see CLAUDE.md: "no machine limit may be a
    // module constant"): viewer.js needs the active printer's Z-velocity
    // ceiling and previously hardcoded the Trident's 25.1 mm/s for every
    // printer. Publish it here, in the one function every printer switch
    // AND the initial /api/printers load both already funnel through, so
    // it can never drift from whichever printer is actually selected.
    window.__printerLimits = {
      key: design.printer,
      name: (meta && meta.name) || design.printer,
      max_z_velocity: (meta && typeof meta.max_z_velocity === 'number' && isFinite(meta.max_z_velocity))
        ? meta.max_z_velocity : 10.0, // conservative default, matches printer_validate.py's unknown-limit fallback
      // Same contract, same funnel, for the wave-slope ceiling: designer.js
      // used to hardcode the Trident's own 0.25 for every printer (see
      // SLOPE_LIMIT_FALLBACK's comment above for why that number in
      // particular is no longer a module constant). activeSlopeLimit()
      // resolves PRINTER_SLOPE[design.printer], set from this same printer's
      // /api/printers entry a few lines above.
      quality_slope_max: activeSlopeLimit()
    };

    return changed;
  }

  // Line 2 of the machine bar: bed WxD, Z<z_max>, amp <= <z_amp_max>mm --
  // every value comes straight from the /api/printers entry for the
  // selected key, never a hardcoded constant.
  function updateMachineSummary(){
    var el = document.getElementById('mb-summary');
    if(!el) return;
    var meta = PRINTER_META[design.printer];
    var bed = (meta && meta.bed) || PRINTER_BEDS[design.printer];
    var cap = PRINTER_ZCAP[design.printer];
    if(!bed && typeof cap !== 'number'){ el.textContent = '–'; return; }
    var bedTxt = bed ? (bed[0] + 'x' + bed[1]) : '?';
    var zTxt = (meta && typeof meta.z_max === 'number') ? meta.z_max : '?';
    var ampTxt = (typeof cap === 'number') ? fmtMm(cap) : '?';
    el.textContent = bedTxt + ' — Z' + zTxt + ' — amp <= ' + ampTxt + 'mm';
  }

  // Brief, dismissable "what changed" notice in the machine bar -- shown
  // whenever a printer switch either clamped values to fit or opened up new
  // headroom. Never a window.alert.
  var machineNoticeTimer = null;
  function hideMachineNotice(){
    var el = document.getElementById('mb-notice');
    if(!el) return;
    if(machineNoticeTimer){ clearTimeout(machineNoticeTimer); machineNoticeTimer = null; }
    el.style.display = 'none';
    el.textContent = '';
  }
  function showMachineNotice(prevName, newName, prevCap, newCap, changedCount){
    var el = document.getElementById('mb-notice');
    if(!el) return;
    var msg, warn;
    if(newCap < prevCap && changedCount > 0){
      warn = true;
      msg = 'Switched to ' + newName + ' — amplitude ceiling ' + fmtMm(prevCap) + ' -> ' +
        fmtMm(newCap) + ' mm. ' + changedCount + (changedCount === 1 ? ' value was' : ' values were') +
        ' clamped to fit.';
    } else if(newCap > prevCap){
      warn = false;
      msg = 'Switched to ' + newName + ' — amplitude ceiling ' + fmtMm(prevCap) + ' -> ' +
        fmtMm(newCap) + ' mm. More headroom is available in the Texture step.';
    } else if(changedCount > 0){
      warn = true;
      msg = 'Switched to ' + newName + ' — ' + changedCount +
        (changedCount === 1 ? ' value was' : ' values were') + ' clamped to fit.';
    } else {
      // Ceiling unchanged and nothing needed clamping -- nothing to say about
      // THIS switch. Clear any notice still standing from a previous one:
      // leaving it up would caption the newly-selected machine with another
      // machine's transition (seen switching Ender 3 -> K1 Max, which share a
      // 1.0 mm ceiling: the Ender 3 notice stayed on screen).
      hideMachineNotice();
      return;
    }
    el.className = 'mb-notice ' + (warn ? 'mb-notice-warn' : 'mb-notice-ok');
    el.innerHTML = '';
    var span = document.createElement('span');
    span.className = 'mb-notice-msg';
    span.textContent = msg;
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'mb-notice-close';
    closeBtn.setAttribute('aria-label', 'Dismiss');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function(){ el.style.display = 'none'; });
    el.appendChild(span);
    el.appendChild(closeBtn);
    el.style.display = '';
    if(machineNoticeTimer) clearTimeout(machineNoticeTimer);
    machineNoticeTimer = setTimeout(function(){ el.style.display = 'none'; }, 8000);
  }

  var FORMAT_LABELS = {
    klipper_cfg: 'printer.cfg', orca_json: 'Orca/Bambu Studio JSON',
    prusa_ini: 'PrusaSlicer INI', trident_json: 'Trident export'
  };

  // Shown only when the selected printer is custom: source format, a
  // warnings chip, and the Edit/Export/Delete row.
  function updatePrinterMeta(){
    var host = document.getElementById('printer-meta');
    if(!host) return;
    var meta = PRINTER_META[design.printer];
    if(!meta || !meta.custom){
      host.style.display = 'none';
      return;
    }
    host.style.display = '';
    var src = document.getElementById('printer-meta-source');
    if(src) src.textContent = 'from ' + (FORMAT_LABELS[meta.source_format] || meta.source_format || 'unknown source');
    var warn = document.getElementById('printer-meta-warn');
    if(warn){
      if(meta.warnings > 0){
        warn.style.display = '';
        warn.textContent = meta.warnings + (meta.warnings === 1 ? ' warning' : ' warnings');
      } else {
        warn.style.display = 'none';
      }
    }
  }

  // Updates the button label and each option's aria-selected/checkmark to
  // reflect `key` as the current value -- pure display, no side effects
  // (bed size, caps, persistence, notices). Used wherever the old code did
  // `printerSel.value = key` without going through the change handler:
  // populating/repopulating the list and restoring a saved design.
  function setPrinterComboValue(key){
    var found = null;
    printerComboOptions.forEach(function(o){
      var selected = o.key === key;
      o.el.setAttribute('aria-selected', selected ? 'true' : 'false');
      if(selected) found = o;
    });
    printerComboLabel.textContent = found ? found.name :
      ((PRINTER_META[key] && PRINTER_META[key].name) || key || '–');
  }

  function printerComboIndexOf(key){
    for(var i = 0; i < printerComboOptions.length; i++){
      if(printerComboOptions[i].key === key) return i;
    }
    return -1;
  }

  // Positions the open list so it always fits the viewport: opens downward
  // by default, flips upward when there is more room above the button than
  // below (the actual bug -- a 17-row list opened from a button mid-panel
  // ran past the bottom of a short window). max-height is capped at the
  // lesser of 320px and whatever room is actually available, so the list
  // itself never runs off-screen even in the direction it opens toward.
  // Recomputed on resize while open.
  function positionPrinterCombo(){
    if(!printerComboOpenState) return;
    var btnRect = printerComboBtn.getBoundingClientRect();
    var spaceBelow = window.innerHeight - btnRect.bottom - 8;
    var spaceAbove = btnRect.top - 8;
    var openUp = spaceAbove > spaceBelow;
    var avail = Math.max(60, openUp ? spaceAbove : spaceBelow);
    printerComboList.style.maxHeight = Math.min(320, avail) + 'px';
    if(openUp){
      printerComboList.style.bottom = 'calc(100% + 4px)';
      printerComboList.style.top = 'auto';
    } else {
      printerComboList.style.top = 'calc(100% + 4px)';
      printerComboList.style.bottom = 'auto';
    }
  }

  // Keyboard/hover "virtual focus" row -- distinct from the persistent
  // aria-selected value (see setPrinterComboValue). Skips group headers
  // automatically since only real options are ever indexed here.
  function setPrinterComboActive(idx){
    if(!printerComboOptions.length) return;
    if(idx < 0) idx = 0;
    if(idx > printerComboOptions.length - 1) idx = printerComboOptions.length - 1;
    printerComboOptions.forEach(function(o){ o.el.classList.remove('active'); });
    printerComboActiveIdx = idx;
    var o = printerComboOptions[idx];
    o.el.classList.add('active');
    printerComboBtn.setAttribute('aria-activedescendant', o.el.id);
    if(o.el.scrollIntoView) o.el.scrollIntoView({ block: 'nearest' });
  }

  function closePrinterCombo(){
    if(!printerComboOpenState) return;
    printerComboOpenState = false;
    printerComboList.classList.remove('open');
    printerComboBtn.setAttribute('aria-expanded', 'false');
    printerComboBtn.removeAttribute('aria-activedescendant');
    document.removeEventListener('mousedown', onPrinterComboDocMouseDown, true);
    window.removeEventListener('resize', positionPrinterCombo);
  }

  function onPrinterComboDocMouseDown(e){
    if(printerCombo.contains(e.target)) return;
    closePrinterCombo();
  }

  function openPrinterCombo(){
    if(printerComboOpenState || !printerComboOptions.length) return;
    printerComboOpenState = true;
    printerComboList.classList.add('open');
    printerComboBtn.setAttribute('aria-expanded', 'true');
    positionPrinterCombo();
    setPrinterComboActive(printerComboIndexOf(design.printer));
    document.addEventListener('mousedown', onPrinterComboDocMouseDown, true);
    window.addEventListener('resize', positionPrinterCombo);
  }

  function printerComboTypeAhead(letter){
    var lower = letter.toLowerCase();
    var n = printerComboOptions.length;
    for(var step = 1; step <= n; step++){
      var idx = (printerComboActiveIdx + step) % n;
      if(printerComboOptions[idx].name.toLowerCase().indexOf(lower) === 0){
        setPrinterComboActive(idx);
        return;
      }
    }
  }

  // The one true "user picked a printer" handler -- runs exactly the logic
  // the old native-select 'change' listener ran (bed size, caps, clamping,
  // notice, meta, persist, preview). Called from both ways an option can be
  // chosen: clicking it, and Enter/Space on the keyboard-active row. Mirrors
  // a native <select> in not doing anything when the value doesn't actually
  // change (reselecting the current printer fires no 'change' event either).
  function selectPrinter(key){
    if(!PRINTER_BEDS.hasOwnProperty(key) || key === design.printer) return;
    // Snapshot the outgoing printer's name/ceiling before switching -- the
    // post-switch notice needs both the old and new numbers to say what
    // changed (B.3: clamped-down vs. more-headroom read very differently).
    var prevKey = design.printer;
    var prevCap = PRINTER_ZCAP[prevKey];
    var prevName = (PRINTER_META[prevKey] && PRINTER_META[prevKey].name) || prevKey;

    design.printer = key;
    setPrinterComboValue(key);
    if(PRINTER_BEDS[design.printer] && typeof window.setPreviewBedSize === 'function'){
      window.setPreviewBedSize(PRINTER_BEDS[design.printer][0], PRINTER_BEDS[design.printer][1]);
    }
    if(PRINTER_BEDS[design.printer]){
      design.bed_center = [PRINTER_BEDS[design.printer][0]/2, PRINTER_BEDS[design.printer][1]/2];
    }
    var changed = applyPrinterCaps();
    updatePrinterMeta();
    updateMachineSummary();
    persistDesign();
    schedulePreview();

    var newCap = PRINTER_ZCAP[design.printer];
    var newName = (PRINTER_META[design.printer] && PRINTER_META[design.printer].name) || design.printer;
    if(typeof prevCap === 'number' && typeof newCap === 'number' && prevKey !== design.printer){
      showMachineNotice(prevName, newName, prevCap, newCap, changed || 0);
    }
  }

  // Repopulates the printer list from /api/printers (built-ins grouped by
  // brand as before, plus a "Custom" group last for saved custom printers).
  // Shared by initial load and by the add/edit/delete modal so a save or
  // delete refreshes the list without a page reload. preferKey, if given
  // and still valid after the refetch, becomes the new selection (used right
  // after a save); otherwise the previous design.printer is kept if it still
  // exists, falling back to the server default (used after a delete).
  function loadPrinterOptions(preferKey){
    return apiFetch('/api/printers').then(function(r){ return r.json(); }).then(function(j){
      printerComboList.innerHTML = '';
      printerComboOptions = [];
      // Group like Bambu Studio / OGcode: brand-prefixed printers get their
      // own group, everything else falls into "Other"; custom printers
      // always get their own group, placed last.
      var byGroup = {}, groupOrder = [];
      function groupFor(p){
        if(p.custom) return 'Custom';
        if((p.name || '').indexOf('Bambu Lab') === 0) return 'Bambu Lab';
        if((p.name || '').indexOf('Creality') === 0) return 'Creality';
        if((p.name || '').indexOf('Voron') === 0) return 'Voron';
        return 'Other';
      }
      PRINTER_BEDS = {}; PRINTER_ZCAP = {}; PRINTER_SLOPE = {}; PRINTER_META = {};
      (j.printers||[]).forEach(function(p){
        PRINTER_BEDS[p.key] = p.bed;
        PRINTER_ZCAP[p.key] = zAmpCapFor(p.z_amp_max);
        PRINTER_SLOPE[p.key] = slopeCapFor(p.quality_slope_max);
        PRINTER_META[p.key] = p;
        var gname = groupFor(p);
        if(!byGroup[gname]){ byGroup[gname] = []; groupOrder.push(gname); }
        byGroup[gname].push(p);
      });
      var customIdx = groupOrder.indexOf('Custom');
      if(customIdx !== -1){ groupOrder.splice(customIdx, 1); groupOrder.push('Custom'); }
      groupOrder.forEach(function(gname){
        var head = document.createElement('div');
        head.className = 'mb-combo-group';
        head.textContent = gname;
        printerComboList.appendChild(head);
        byGroup[gname].forEach(function(p){
          var opt = document.createElement('div');
          opt.className = 'mb-combo-option';
          opt.id = 'printer-combo-opt-' + p.key;
          opt.textContent = p.name;
          opt.setAttribute('role', 'option');
          opt.setAttribute('aria-selected', 'false');
          opt.dataset.key = p.key;
          opt.addEventListener('click', function(){
            selectPrinter(p.key);
            closePrinterCombo();
            printerComboBtn.focus();
          });
          printerComboList.appendChild(opt);
          printerComboOptions.push({ key: p.key, name: p.name, el: opt });
        });
      });
      var wanted = preferKey || design.printer;
      var key = wanted && PRINTER_BEDS[wanted] ? wanted : (j.default || 'trident');
      design.printer = key;
      setPrinterComboValue(key);
      if(PRINTER_BEDS[key] && typeof window.setPreviewBedSize === 'function'){
        window.setPreviewBedSize(PRINTER_BEDS[key][0], PRINTER_BEDS[key][1]);
      }
      if(PRINTER_BEDS[key]) design.bed_center = [PRINTER_BEDS[key][0]/2, PRINTER_BEDS[key][1]/2];
      applyPrinterCaps();
      updatePrinterMeta();
      updateMachineSummary();
      persistDesign();
      schedulePreview();
      return j;
    });
  }

  if(printerComboBtn && printerComboList){
    // Replay first: /api/printers only reports what this session holds, so
    // asking before the replay lands would show the built-ins alone and
    // reset design.printer away from the user's custom machine.
    replayStoredPrinters()
      .then(function(){ return loadPrinterOptions(); })
      .catch(function(){ /* keep static option, if any */ });

    printerComboBtn.addEventListener('click', function(){
      if(printerComboOpenState) closePrinterCombo(); else openPrinterCombo();
    });

    printerComboBtn.addEventListener('keydown', function(e){
      switch(e.key){
        case 'ArrowDown':
          e.preventDefault();
          if(!printerComboOpenState) openPrinterCombo();
          else setPrinterComboActive(printerComboActiveIdx + 1);
          break;
        case 'ArrowUp':
          e.preventDefault();
          if(!printerComboOpenState) openPrinterCombo();
          else setPrinterComboActive(printerComboActiveIdx - 1);
          break;
        case 'Home':
          if(printerComboOpenState){ e.preventDefault(); setPrinterComboActive(0); }
          break;
        case 'End':
          if(printerComboOpenState){ e.preventDefault(); setPrinterComboActive(printerComboOptions.length - 1); }
          break;
        case 'Enter':
        case ' ':
        case 'Spacebar':
          e.preventDefault();
          if(printerComboOpenState){
            var active = printerComboOptions[printerComboActiveIdx];
            if(active) selectPrinter(active.key);
            closePrinterCombo();
          } else {
            openPrinterCombo();
          }
          break;
        case 'Escape':
          if(printerComboOpenState){ e.preventDefault(); closePrinterCombo(); }
          break;
        case 'Tab':
          closePrinterCombo();
          break;
        default:
          if(printerComboOpenState && e.key && e.key.length === 1 && /[a-z0-9]/i.test(e.key)){
            printerComboTypeAhead(e.key);
          }
      }
    });
  }

  // ---- custom printer import/edit modal -----------------------------------
  // Parses a Klipper/Orca/Prusa config (or a previously exported Trident
  // printer JSON) via /api/printer/parse, walks the user through the safety
  // report + field review, live-revalidates via /api/printer/validate as
  // they edit, and saves via /api/printer/save. Mirrors the Point Edit
  // modal's open/close plumbing but uses its own .pm-* chrome (--accent
  // blue, not --accent-purple -- that is reserved for Point Edit).
  (function(){
    var modal = document.getElementById('printer-modal');
    var addBtn = document.getElementById('printer-add-btn');
    if(!modal || !addBtn) return;
    var card = modal.querySelector('.pm-modal-card');
    var backdrop = modal.querySelector('.pm-modal-backdrop');
    var closeBtn = document.getElementById('pm-modal-close');
    var cancelBtn = document.getElementById('pm-cancel-btn');
    var saveBtn = document.getElementById('pm-save-btn');
    var editBtn = document.getElementById('printer-edit-btn');
    var exportBtn = document.getElementById('printer-export-btn');
    var deleteBtn = document.getElementById('printer-delete-btn');
    var backBtn = document.getElementById('pm-back-btn');
    var titleEl = document.getElementById('pm-modal-title');
    var dropStage = document.getElementById('pm-stage-drop');
    var reviewStage = document.getElementById('pm-stage-review');
    var clearanceStage = document.getElementById('pm-stage-clearance');
    var clearanceInput = document.getElementById('pm-clearance-input');
    var clearanceTag = document.getElementById('pm-clearance-tag');
    var dropZone = document.getElementById('pm-drop');
    var fileInput = document.getElementById('pm-file');
    var parseErrorEl = document.getElementById('pm-parse-error');
    var dropStatusEl = document.getElementById('pm-drop-status');
    var saveStatusEl = document.getElementById('pm-save-status');
    var metaStatusEl = document.getElementById('printer-meta-status');
    var nameInput = document.getElementById('pm-name');
    var formatBadge = document.getElementById('pm-format-badge');
    var reportEl = document.getElementById('pm-report');
    var fieldsEl = document.getElementById('pm-fields');
    var startGcodeEl = document.getElementById('pm-start-gcode');
    var endGcodeEl = document.getElementById('pm-end-gcode');
    var strippedBlock = document.getElementById('pm-stripped');
    var strippedToggle = document.getElementById('pm-stripped-toggle');
    var strippedList = document.getElementById('pm-stripped-list');
    var strippedCount = document.getElementById('pm-stripped-count');
    var repairedBlock = document.getElementById('pm-repaired');
    var repairedToggle = document.getElementById('pm-repaired-toggle');
    var repairedList = document.getElementById('pm-repaired-list');
    var repairedCount = document.getElementById('pm-repaired-count');
    var allowRawEl = document.getElementById('pm-allow-raw');

    var PARSE_MAX_MB = 2;
    var GROUP_ORDER = ['identity','volume','area','motion','hardware','probe','thermal','gcode'];
    var GROUP_LABELS = {
      identity: 'Identity', volume: 'Build volume', area: 'Safe print area',
      motion: 'Motion limits', hardware: 'Hardware', probe: 'Probe & keep-out',
      thermal: 'Temperature limits', gcode: 'G-code'
    };
    // Rendered elsewhere (name input header, textareas below, the dedicated
    // clearance stage) or purely derived server-side (pa_gcode_style always
    // tracks firmware) -- not shown as a generic field row. z_amp_max in
    // particular must appear exactly once across the whole dialog, so it is
    // skipped here and rendered only in #pm-stage-clearance (see B.5).
    var FIELD_SKIP = { name: 1, start_gcode: 1, end_gcode: 1, pa_gcode_style: 1, z_amp_max: 1 };
    var FIELD_BOOL = { has_probe: 1 };
    var FIELD_SELECT = { firmware: ['klipper', 'marlin', 'bambu_marlin'] };

    var pmState = null;
    var pmSeq = 0;
    var pmParseSeq = 0;   // guards /api/printer/parse the same way pmSeq guards /validate
    var pmDebounceTimer = null;
    var deleteConfirming = false;

    function resetState(){
      pmState = { mode: 'add', key: null, detectedFormat: null, sourceFile: null,
        profile: null, fields: [], issues: [], strippedGcode: [], stage: 'drop' };
      dropStage.style.display = '';
      reviewStage.style.display = 'none';
      clearanceStage.style.display = 'none';
      titleEl.textContent = 'Add custom printer';
      hideParseError();
      fileInput.value = '';
      allowRawEl.checked = false;
      saveBtn.textContent = 'Next';
      saveBtn.disabled = true;
      saveBtn.title = '';
      backBtn.style.display = 'none';
      cancelBtn.style.display = '';
      resetDeleteConfirm();
    }

    function showParseError(msg){ parseErrorEl.textContent = msg; parseErrorEl.style.display = ''; }
    function hideParseError(){ parseErrorEl.style.display = 'none'; parseErrorEl.textContent = ''; }

    function openModal(){
      resetState();
      modal.style.display = 'flex';
      closeBtn.focus();
    }
    function closeModal(){
      modal.style.display = 'none';
      if(pmDebounceTimer){ clearTimeout(pmDebounceTimer); pmDebounceTimer = null; }
      addBtn.focus();
    }

    addBtn.addEventListener('click', openModal);
    if(closeBtn) closeBtn.addEventListener('click', closeModal);
    if(cancelBtn) cancelBtn.addEventListener('click', closeModal);
    if(backdrop) backdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', function(e){
      if(modal.style.display === 'none') return;
      if(e.key === 'Escape'){ closeModal(); return; }
      if(e.key === 'Tab') trapFocus(e);
    });

    function trapFocus(e){
      var all = card.querySelectorAll('button, input, select, textarea, [tabindex]');
      var focusables = Array.prototype.filter.call(all, function(el){
        return !el.disabled && el.tabIndex !== -1 && el.offsetParent !== null;
      });
      if(!focusables.length) return;
      var first = focusables[0], last = focusables[focusables.length - 1];
      if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
    }

    // ---- Stage A: drop / choose ------------------------------------------
    dropZone.addEventListener('click', function(){ fileInput.click(); });
    dropZone.addEventListener('keydown', function(e){
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); fileInput.click(); }
    });
    fileInput.addEventListener('change', function(e){
      if(e.target.files.length) handleFile(e.target.files[0]);
      e.target.value = '';
    });
    dropZone.addEventListener('dragover', function(e){
      e.preventDefault(); e.stopPropagation(); dropZone.classList.add('hot');
    });
    dropZone.addEventListener('dragleave', function(){ dropZone.classList.remove('hot'); });
    dropZone.addEventListener('drop', function(e){
      e.preventDefault(); e.stopPropagation();
      dropZone.classList.remove('hot');
      var files = e.dataTransfer.files;
      if(files.length) handleFile(files[0]);
    });

    function handleFile(file){
      hideParseError();
      if(file.size > PARSE_MAX_MB * 1024 * 1024){
        showParseError('File too large (max ' + PARSE_MAX_MB + ' MB).');
        return;
      }
      // Sequence guard: two rapid drops must let only the LATER one win,
      // same pattern as runRevalidate()'s pmSeq below.
      var mySeq = ++pmParseSeq;
      var end = beginBusy(null, dropStatusEl, 'Parsing ' + file.name + '...');
      var reader = new FileReader();
      reader.onload = function(e){
        if(mySeq !== pmParseSeq) return; // superseded before the request even went out
        apiFetch('/api/printer/parse', {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': file.name },
          body: e.target.result
        }).then(function(r){
          return r.json().then(function(j){ return { status: r.status, body: j }; });
        }).then(function(res){
          if(mySeq !== pmParseSeq) return; // a newer parse's response already won
          end(null);
          if(res.status !== 200 || !res.body || !res.body.profile){
            showParseError((res.body && res.body.error) || 'Could not parse this file.');
            return;
          }
          pmState.mode = 'add';
          pmState.key = null;
          pmState.detectedFormat = res.body.detected_format;
          pmState.sourceFile = file.name;
          applyServerResult(res.body);
          enterReviewStage();
        }).catch(function(err){
          if(mySeq !== pmParseSeq) return;
          end(null);
          showParseError('Upload failed: ' + err);
        });
      };
      reader.onerror = function(){
        if(mySeq !== pmParseSeq) return;
        end(null);
        showParseError('Could not read file: ' + (reader.error ? reader.error.message : 'unknown error'));
      };
      reader.readAsArrayBuffer(file);
    }

    // ---- Stage B: review ---------------------------------------------------
    function applyServerResult(resp){
      pmState.profile = resp.profile;
      pmState.fields = resp.fields || [];
      pmState.issues = resp.issues || [];
      pmState.strippedGcode = resp.stripped_gcode || [];
    }

    // Snapshot the provenance the IMPORT established. /api/printer/validate is
    // handed a plain profile dict with no memory of where each number came
    // from, so it can only ever answer "parsed" -- meaning one keystroke
    // anywhere would otherwise relabel every conservative default and every
    // clamped value as though the user's own config had supplied it. That is
    // exactly the distinction this dialog exists to show, so it is pinned here
    // and re-applied on every sync.
    function snapshotProvenance(){
      pmState.importedSource = {};
      pmState.importedValue = {};
      (pmState.fields || []).forEach(function(f){
        pmState.importedSource[f.name] = f.source;
        pmState.importedValue[f.name] = f.value;
      });
      // Same reason the tags are pinned: "clamped from 99999" and "not found,
      // using a conservative default" can only be said about the ORIGINAL file.
      // Re-validation sees a complete, in-range profile and reports almost
      // nothing, so the import-time list is kept for the saved record.
      pmState.importWarnings = (pmState.issues || [])
        .filter(function(i){ return i.severity === 'warn'; })
        .map(function(i){ return i.message; });
    }

    // A live clamp is always authoritative (it just happened). Otherwise a
    // value the user changed reads "edited", and an untouched one keeps
    // whatever the import said about it.
    function effectiveSource(f){
      if(f.source === 'clamped') return 'clamped';
      if(!pmState.importedSource || !(f.name in pmState.importedSource)) return f.source;
      var before = pmState.importedValue[f.name];
      if(String(f.value) !== String(before)) return 'edited';
      return pmState.importedSource[f.name];
    }

    function enterReviewStage(){
      pmState.stage = 'review';
      dropStage.style.display = 'none';
      reviewStage.style.display = '';
      clearanceStage.style.display = 'none';
      titleEl.textContent = pmState.mode === 'edit' ? 'Edit printer' : 'Review imported printer';
      formatBadge.textContent = FORMAT_LABELS[pmState.detectedFormat] || pmState.detectedFormat || 'unknown format';
      nameInput.value = (pmState.profile && pmState.profile.name) || '';
      saveBtn.textContent = 'Next';
      backBtn.style.display = 'none';
      cancelBtn.style.display = '';
      snapshotProvenance();
      renderFields();
      refreshFromState();
      nameInput.focus();
    }

    // ---- Stage C: non-planar clearance -- z_amp_max's one and only field --
    function findField(name){
      var list = pmState.fields || [];
      for(var i = 0; i < list.length; i++){ if(list[i].name === name) return list[i]; }
      return null;
    }

    // Keeps the clearance input + its provenance tag in sync with the latest
    // validated field (called from the initial stage entry and from every
    // debounced revalidation afterwards, same as the review-stage rows).
    function syncClearanceField(f){
      if(!f || !clearanceInput) return;
      if(document.activeElement !== clearanceInput) clearanceInput.value = (f.value == null ? '' : f.value);
      if(clearanceTag){
        var src = effectiveSource(f);
        clearanceTag.className = 'pm-tag pm-tag-' + src;
        clearanceTag.textContent = src;
        clearanceTag.title = src === 'edited'
          ? 'you changed this from the imported value ' + pmState.importedValue[f.name]
          : (f.note || '');
      }
    }

    function enterClearanceStage(){
      pmState.stage = 'clearance';
      reviewStage.style.display = 'none';
      clearanceStage.style.display = '';
      titleEl.textContent = 'Non-planar clearance';
      saveBtn.textContent = 'Save printer';
      backBtn.style.display = '';
      cancelBtn.style.display = 'none';
      syncClearanceField(findField('z_amp_max'));
      if(clearanceInput) clearanceInput.focus();
    }

    function backToReviewStage(){
      pmState.stage = 'review';
      clearanceStage.style.display = 'none';
      reviewStage.style.display = '';
      titleEl.textContent = pmState.mode === 'edit' ? 'Edit printer' : 'Review imported printer';
      saveBtn.textContent = 'Next';
      backBtn.style.display = 'none';
      cancelBtn.style.display = '';
    }
    if(backBtn) backBtn.addEventListener('click', backToReviewStage);

    // Rebuilds the field rows (the field SET is static -- the server always
    // returns every PrinterProfile field, just with different sources/notes
    // -- so this only needs to run once per stage-B entry; live revalidation
    // afterwards updates values/tags in place via syncFields()).
    function renderFields(){
      fieldsEl.innerHTML = '';
      var byGroup = {};
      (pmState.fields || []).forEach(function(f){
        if(FIELD_SKIP[f.name]) return;
        if(!byGroup[f.group]) byGroup[f.group] = [];
        byGroup[f.group].push(f);
      });
      GROUP_ORDER.forEach(function(g){
        var list = byGroup[g];
        if(!list || !list.length) return;
        var h = document.createElement('div');
        h.className = 'pm-group-head';
        h.textContent = GROUP_LABELS[g] || g;
        fieldsEl.appendChild(h);
        list.forEach(function(f){ fieldsEl.appendChild(renderFieldRow(f)); });
      });
    }

    function renderFieldRow(f){
      var row = document.createElement('div');
      row.className = 'pm-field-row';
      row.id = 'pm-field-' + f.name;
      var label = document.createElement('span');
      label.className = 'pm-field-label';
      label.textContent = f.label + (f.unit ? ' (' + f.unit + ')' : '');
      row.appendChild(label);

      var control = document.createElement('span');
      control.className = 'pm-field-control';
      var input;
      if(FIELD_BOOL[f.name]){
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = !!f.value;
        input.addEventListener('change', scheduleRevalidate);
      } else if(FIELD_SELECT[f.name]){
        input = document.createElement('select');
        FIELD_SELECT[f.name].forEach(function(opt){
          var o = document.createElement('option'); o.value = opt; o.textContent = opt;
          input.appendChild(o);
        });
        input.value = f.value;
        input.addEventListener('change', scheduleRevalidate);
      } else {
        input = document.createElement('input');
        input.type = 'number';
        input.step = 'any';
        input.value = (f.value == null ? '' : f.value);
        input.addEventListener('input', scheduleRevalidate);
      }
      input.className = 'pm-field-input';
      input.setAttribute('data-field', f.name);
      // These hold machine values, not prose -- "klipper", "corexy" and the
      // like otherwise get a red spellcheck squiggle that reads as an error.
      input.spellcheck = false;
      control.appendChild(input);

      var tag = document.createElement('span');
      var src0 = effectiveSource(f);
      tag.className = 'pm-tag pm-tag-' + src0;
      tag.textContent = src0;
      if(f.note) tag.title = f.note;
      control.appendChild(tag);

      row.appendChild(control);
      return row;
    }

    // Updates values (on fields whose input is not currently focused, so a
    // debounced response never steals mid-typing focus/caret) and source
    // tags in place -- does not rebuild the DOM (the field set never
    // changes shape, only values/sources/notes).
    function syncFields(fields){
      (fields || []).forEach(function(f){
        if(f.name === 'start_gcode' || f.name === 'end_gcode'){
          var ta = f.name === 'start_gcode' ? startGcodeEl : endGcodeEl;
          if(ta && document.activeElement !== ta) ta.value = f.value;
          return;
        }
        // Lives in the dedicated clearance stage, not a generic field row --
        // still needs to track live revalidation the same way every other
        // field does, just into its own input/tag instead of a pm-field-row.
        if(f.name === 'z_amp_max'){ syncClearanceField(f); return; }
        if(FIELD_SKIP[f.name]) return;
        var row = document.getElementById('pm-field-' + f.name);
        if(!row) return;
        var tag = row.querySelector('.pm-tag');
        if(tag){
          var src = effectiveSource(f);
          tag.className = 'pm-tag pm-tag-' + src;
          tag.textContent = src;
          tag.title = src === 'edited'
            ? 'you changed this from the imported value ' + pmState.importedValue[f.name]
            : (f.note || '');
        }
        var input = row.querySelector('[data-field="' + f.name + '"]');
        if(input && document.activeElement !== input){
          if(input.type === 'checkbox') input.checked = !!f.value;
          else input.value = (f.value == null ? '' : f.value);
        }
      });
    }

    function renderReport(){
      reportEl.innerHTML = '';
      var issues = pmState.issues || [];
      if(!issues.length){
        var ok = document.createElement('div');
        ok.className = 'pm-report-ok';
        ok.textContent = 'All safety checks passed.';
        reportEl.appendChild(ok);
        return;
      }
      var errors = issues.filter(function(i){ return i.severity === 'error'; });
      var warns = issues.filter(function(i){ return i.severity !== 'error'; });
      if(errors.length){
        reportEl.appendChild(reportHead(errors.length + ' error(s)', 'pm-report-head-error'));
        errors.forEach(function(i){ reportEl.appendChild(reportRow(i)); });
      }
      if(warns.length){
        reportEl.appendChild(reportHead(warns.length + ' warning(s)', 'pm-report-head-warn'));
        warns.forEach(function(i){ reportEl.appendChild(reportRow(i)); });
      }
      reportEl.appendChild(reportCopyButton(errors, warns));
    }

    // Plain-text dump of the whole report. These messages are the useful thing
    // to paste into a forum post or a bug report when a config will not import,
    // and they are otherwise unselectable -- each row is a <button>, so a drag
    // selects nothing.
    function reportText(errors, warns){
      var lines = [];
      var name = (nameInput && nameInput.value) || 'custom printer';
      lines.push(name + ' -- ' + (pmState.detectedFormat || 'unknown format'));
      lines.push('');
      if(errors.length){
        lines.push(errors.length + ' error(s):');
        errors.forEach(function(i){ lines.push('  - ' + i.message); });
        lines.push('');
      }
      if(warns.length){
        lines.push(warns.length + ' warning(s):');
        warns.forEach(function(i){ lines.push('  - ' + i.message); });
      }
      var stripped = pmState.strippedGcode || [];
      if(stripped.length){
        lines.push('');
        lines.push(stripped.length + ' G-code line(s) removed:');
        stripped.forEach(function(s){
          lines.push('  - ' + (typeof s === 'string' ? s : (s.line + ' -- ' + s.reason)));
        });
      }
      return lines.join('\n');
    }

    function reportCopyButton(errors, warns){
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pm-report-copy';
      btn.textContent = 'Copy report';
      btn.addEventListener('click', function(){
        var text = reportText(errors, warns);
        function done(ok){
          btn.textContent = ok ? 'Copied' : 'Press Ctrl+C';
          btn.classList.toggle('pm-report-copy-done', ok);
          setTimeout(function(){
            btn.textContent = 'Copy report';
            btn.classList.remove('pm-report-copy-done');
          }, 2000);
        }
        if(navigator.clipboard && navigator.clipboard.writeText){
          navigator.clipboard.writeText(text).then(function(){ done(true); },
                                                    function(){ fallback(text, done); });
        } else {
          fallback(text, done);
        }
      });
      return btn;
    }

    // Clipboard API needs a secure context; if it is unavailable, drop the
    // text into a selected textarea so Ctrl+C still works.
    function fallback(text, done){
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', 'readonly');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
      document.body.removeChild(ta);
      done(ok);
    }
    function reportHead(text, cls){
      var h = document.createElement('div');
      h.className = 'pm-report-head ' + cls;
      h.textContent = text;
      return h;
    }
    function reportRow(issue){
      // A clickable "jump to field" element and the quick-fix buttons must be
      // siblings, never a button nested inside a button (invalid HTML -- the
      // browser silently un-nests it and breaks this DOM).
      var clickable = !!issue.field;
      var el = document.createElement('div');
      el.className = 'pm-issue pm-issue-' + issue.severity;
      var text = document.createElement(clickable ? 'button' : 'span');
      if(clickable) text.type = 'button';
      text.className = 'pm-issue-msg';
      text.textContent = issue.message;
      if(clickable) text.addEventListener('click', function(){ focusField(issue.field); });
      el.appendChild(text);
      if(issue.field === 'start_gcode'){
        if(issue.message.indexOf('Add G28') !== -1){
          el.appendChild(makeQuickFix('Insert G28', function(){
            insertStartLine('G28                ; home all axes');
          }));
        }
        if(issue.message.indexOf('M83') !== -1){
          el.appendChild(makeQuickFix('Insert M83', function(){
            // Appended, not prepended: M83 only has to be in effect for the
            // moves WE emit, which follow the whole start block. Putting it
            // first would also switch any purge line inside the user's own
            // start G-code into relative E and silently change how much it
            // extrudes.
            appendStartLine('M83                ; relative extrusion');
          }));
        }
      }
      return el;
    }
    function makeQuickFix(label, onClick){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'pm-quickfix';
      b.textContent = label;
      b.addEventListener('click', onClick);
      return b;
    }
    // G28 must precede all motion, so homing is prepended.
    function insertStartLine(line){
      startGcodeEl.value = line + '\n' + startGcodeEl.value;
      scheduleRevalidate();
    }
    function appendStartLine(line){
      var cur = startGcodeEl.value.replace(/\s+$/, '');
      startGcodeEl.value = (cur ? cur + '\n' : '') + line;
      scheduleRevalidate();
    }
    function focusField(fieldName){
      if(fieldName === 'start_gcode'){ startGcodeEl.scrollIntoView({block:'center'}); startGcodeEl.focus(); return; }
      if(fieldName === 'end_gcode'){ endGcodeEl.scrollIntoView({block:'center'}); endGcodeEl.focus(); return; }
      var row = document.getElementById('pm-field-' + fieldName);
      if(!row) return;
      row.scrollIntoView({block:'center'});
      var input = row.querySelector('[data-field]');
      if(input) input.focus();
    }

    function renderStripped(){
      var names = pmState.strippedGcode || [];
      if(!names.length){ strippedBlock.style.display = 'none'; return; }
      strippedBlock.style.display = '';
      strippedCount.textContent = names.length;
      strippedList.innerHTML = '';
      strippedList.style.display = 'none';
      strippedToggle.setAttribute('aria-expanded', 'false');
      var reasons = {};
      (pmState.issues || []).forEach(function(i){
        var m = /^removed '([^']+)': (.+)$/.exec(i.message);
        if(m) reasons[m[1]] = m[2];
      });
      names.forEach(function(line){
        var li = document.createElement('li');
        var cmd = line.split(/\s+/)[0];
        li.textContent = line + (reasons[cmd] ? ' - ' + reasons[cmd] : '');
        strippedList.appendChild(li);
      });
    }
    strippedToggle.addEventListener('click', function(){
      var open = strippedList.style.display !== 'none';
      strippedList.style.display = open ? 'none' : '';
      strippedToggle.setAttribute('aria-expanded', open ? 'false' : 'true');
    });

    // Import-time auto-repair notice (see printer_validate._repair_missing_
    // heating). No separate API field for this -- every line the repair adds
    // carries a literal '[auto-added]' tag in its own comment, so it can be
    // found straight in the start G-code text the user already sees and can
    // edit. That also makes the notice self-clearing: once the user deletes
    // an auto-added line (or its tag), it stops appearing here, matching
    // repair being a one-time, editable suggestion rather than something
    // that keeps coming back (re-validation never re-runs repair -- see
    // sanitize_gcode's docstring).
    var AUTO_ADDED_TAG = '[auto-added]';
    function renderRepaired(){
      var lines = (startGcodeEl.value || '').split('\n').filter(function(ln){
        return ln.indexOf(AUTO_ADDED_TAG) !== -1;
      });
      if(!lines.length){ repairedBlock.style.display = 'none'; return; }
      repairedBlock.style.display = '';
      repairedCount.textContent = lines.length;
      repairedList.innerHTML = '';
      repairedList.style.display = 'none';
      repairedToggle.setAttribute('aria-expanded', 'false');
      lines.forEach(function(line){
        var li = document.createElement('li');
        li.textContent = line.trim();
        repairedList.appendChild(li);
      });
    }
    repairedToggle.addEventListener('click', function(){
      var open = repairedList.style.display !== 'none';
      repairedList.style.display = open ? 'none' : '';
      repairedToggle.setAttribute('aria-expanded', open ? 'false' : 'true');
    });

    function updateSaveButtonState(){
      var hasError = (pmState.issues || []).some(function(i){ return i.severity === 'error'; });
      saveBtn.disabled = hasError;
      saveBtn.title = hasError ? 'Fix the blocking error(s) in the safety report before saving.' : '';
    }

    function refreshFromState(){
      syncFields(pmState.fields);
      renderReport();
      renderStripped();
      renderRepaired();
      updateSaveButtonState();
    }

    // ---- live revalidation (debounced, out-of-order-safe) ------------------
    function collectProfileFromForm(){
      var p = {};
      if(pmState.profile) for(var k in pmState.profile) p[k] = pmState.profile[k];
      p.name = nameInput.value;
      // Queried from the whole card, not just #pm-fields -- the clearance
      // stage's z_amp_max input lives outside #pm-fields (see B.5) but still
      // carries a [data-field] attribute so it is picked up the same way.
      var inputs = card.querySelectorAll('[data-field]');
      Array.prototype.forEach.call(inputs, function(el){
        var name = el.getAttribute('data-field');
        if(el.type === 'checkbox') p[name] = el.checked;
        else if(el.tagName === 'SELECT') p[name] = el.value;
        else p[name] = el.value === '' ? null : parseFloat(el.value);
      });
      p.start_gcode = startGcodeEl.value;
      p.end_gcode = endGcodeEl.value;
      return p;
    }

    function scheduleRevalidate(){
      if(pmDebounceTimer) clearTimeout(pmDebounceTimer);
      pmDebounceTimer = setTimeout(runRevalidate, 350);
    }
    function runRevalidate(){
      var mySeq = ++pmSeq;
      var body = { profile: collectProfileFromForm(), allow_raw_gcode: !!allowRawEl.checked };
      apiFetch('/api/printer/validate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      }).then(function(r){ return r.json(); }).then(function(j){
        if(mySeq !== pmSeq) return; // a newer request already landed -- drop this stale one
        applyServerResult(j);
        refreshFromState();
      }).catch(function(){ /* transient network error -- keep last known state */ });
    }
    if(nameInput) nameInput.addEventListener('input', scheduleRevalidate);
    if(startGcodeEl) startGcodeEl.addEventListener('input', scheduleRevalidate);
    if(endGcodeEl) endGcodeEl.addEventListener('input', scheduleRevalidate);
    if(allowRawEl) allowRawEl.addEventListener('change', scheduleRevalidate);
    if(clearanceInput) clearanceInput.addEventListener('input', scheduleRevalidate);

    // ---- next / save ----------------------------------------------------
    // saveBtn is the one primary action button in the footer, but it means
    // two different things depending on stage: "Next" (review -> clearance)
    // or "Save printer" (clearance -> actually saves). See B.5.
    saveBtn.addEventListener('click', function(){
      if(saveBtn.disabled) return;
      if(pmState.stage !== 'clearance'){
        enterClearanceStage();
        return;
      }
      saveBtn.disabled = true;
      var body = {
        profile: collectProfileFromForm(),
        allow_raw_gcode: !!allowRawEl.checked,
        meta: {
          source_format: pmState.detectedFormat,
          source_file: pmState.sourceFile || '',
          warnings: (pmState.importWarnings || []).concat(
            (pmState.issues || [])
              .filter(function(i){ return i.severity === 'warn'; })
              .map(function(i){ return i.message; }))
        }
      };
      if(pmState.mode === 'edit' && pmState.key) body.key = pmState.key;
      apiFetch('/api/printer/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      }).then(function(r){
        return r.json().then(function(j){ return { status: r.status, body: j }; });
      }).then(function(res){
        if(res.status !== 200 || !res.body.ok){
          // The server re-validates from scratch and is authoritative -- if
          // it disagrees with our last debounced snapshot, surface why. The
          // report that explains it lives in the review stage, so send the
          // user back there to see it rather than leaving them stranded on
          // the clearance screen with no visible error.
          pmState.issues = res.body.issues || pmState.issues;
          backToReviewStage();
          refreshFromState();
          return;
        }
        // Mirror the server's OWN validated profile, not the form snapshot:
        // validation clamps and repairs values, and storing the pre-clamp
        // version would replay something the server never accepted.
        upsertStoredPrinter(res.body.key, res.body.profile, res.body.meta);
        return loadPrinterOptions(res.body.key).then(function(){ closeModal(); });
      }).catch(function(err){
        alert('Save failed: ' + err);
        updateSaveButtonState();
      });
    });

    // ---- edit / export / delete (sidebar meta row) --------------------
    function openEditForKey(key){
      resetState();
      modal.style.display = 'flex';
      closeBtn.focus();
      apiFetch('/api/printer?key=' + encodeURIComponent(key)).then(function(r){ return r.json(); }).then(function(j){
        if(!j.ok){ alert('Could not load printer: ' + (j.error || 'unknown error')); closeModal(); return; }
        pmState.mode = 'edit';
        pmState.key = key;
        pmState.detectedFormat = j.meta && j.meta.source_format;
        pmState.sourceFile = j.meta && j.meta.source_file;
        return apiFetch('/api/printer/validate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile: j.profile, allow_raw_gcode: false })
        }).then(function(r2){ return r2.json(); }).then(function(vr){
          applyServerResult(vr);
          enterReviewStage();
        });
      }).catch(function(err){ alert('Could not load printer: ' + err); closeModal(); });
    }
    if(editBtn) editBtn.addEventListener('click', function(){ openEditForKey(design.printer); });
    // Fetched rather than navigated to: a location change cannot carry the
    // session header, and the custom printer being exported only exists in
    // this session. Downloading the blob keeps the session id out of URLs
    // (and out of the browser history) as a side benefit.
    if(exportBtn) exportBtn.addEventListener('click', function(){
      var key = design.printer;
      apiFetch('/api/printer/export?key=' + encodeURIComponent(key)).then(function(r){
        if(!r.ok) return r.json().then(function(j){ throw new Error(j.error || ('HTTP ' + r.status)); });
        return r.blob();
      }).then(function(blob){
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = key + '.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }).catch(function(err){ alert('Export failed: ' + err.message); });
    });

    function resetDeleteConfirm(){
      if(!deleteBtn) return;
      deleteConfirming = false;
      deleteBtn.textContent = 'Delete';
      deleteBtn.classList.remove('pm-link-confirm');
    }
    if(deleteBtn){
      deleteBtn.addEventListener('click', function(){
        if(!deleteConfirming){
          deleteConfirming = true;
          deleteBtn.textContent = 'Confirm?';
          deleteBtn.classList.add('pm-link-confirm');
          setTimeout(function(){ if(deleteConfirming) resetDeleteConfirm(); }, 3000);
          return;
        }
        var key = design.printer;
        resetDeleteConfirm();
        apiFetch('/api/printer/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: key })
        }).then(function(r){ return r.json(); }).then(function(j){
          if(!j.ok){ alert('Delete failed: ' + (j.error || 'unknown error')); return; }
          // Drop the durable copy too, or the next page load would replay it
          // straight back in.
          removeStoredPrinter(key);
          // Falls back to the server default automatically -- loadPrinterOptions
          // drops any key that no longer exists in the refetched list.
          loadPrinterOptions(null);
        }).catch(function(err){ alert('Delete failed: ' + err); });
      });
    }
  })();

  // ---- filament dropdown -------------------------------------------------
  var famSel = document.getElementById('d-filament');
  apiFetch('/api/filaments').then(function(r){ return r.json(); }).then(function(j){
    famSel.innerHTML = '';
    var opt = document.createElement('option'); opt.value=''; opt.textContent='(generic PLA)';
    famSel.appendChild(opt);
    (j.filaments||[]).forEach(function(n){
      var o=document.createElement('option'); o.value=n; o.textContent=n; famSel.appendChild(o);
    });
    if(design.filament){ famSel.value = design.filament; }
    else if(j.default){ famSel.value = j.default; design.filament = j.default; }
    syncFilamentTitle();
  }).catch(function(){ /* no orca: keep generic PLA */ });

  // Orca filament names run past 400px, wider than the whole panel, and a
  // <select> cannot ellipsize its own closed state -- so mirror the selection
  // into the title attribute, making the full name reachable on hover.
  function syncFilamentTitle(){
    var o = famSel.options[famSel.selectedIndex];
    famSel.title = o ? o.textContent : '';
  }
  famSel.addEventListener('change', syncFilamentTitle);

  // ---- filament combo: a button+list mirror of the real <select> above,
  // same pattern as #printer-combo but flat (no groups) -- this never holds
  // its own state, only reads/writes famSel, so the two can never disagree.
  (function(){
    var btn = document.getElementById('filament-combo-btn');
    var label = document.getElementById('filament-combo-label');
    var list = document.getElementById('filament-combo-list');
    var combo = document.getElementById('filament-combo');
    if(!btn || !label || !list || !combo) return;

    function syncLabel(){
      var o = famSel.options[famSel.selectedIndex];
      label.textContent = o ? o.textContent : '(generic PLA)';
    }
    function closeList(){
      list.classList.remove('open');
      btn.setAttribute('aria-expanded', 'false');
    }
    function openList(){
      list.innerHTML = '';
      Array.prototype.forEach.call(famSel.options, function(o, i){
        var item = document.createElement('div');
        item.className = 'mb-combo-option';
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', i === famSel.selectedIndex ? 'true' : 'false');
        item.textContent = o.textContent;
        item.addEventListener('click', function(){
          famSel.selectedIndex = i;
          famSel.dispatchEvent(new Event('change'));
          syncLabel();
          closeList();
          btn.focus();
        });
        list.appendChild(item);
      });
      list.classList.add('open');
      btn.setAttribute('aria-expanded', 'true');
    }
    btn.addEventListener('click', function(){
      if(list.classList.contains('open')) closeList(); else openList();
    });
    document.addEventListener('pointerdown', function(e){
      if(list.classList.contains('open') && !combo.contains(e.target)) closeList();
    });
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape' && list.classList.contains('open')) closeList();
    });
    famSel.addEventListener('change', syncLabel);
    syncLabel();
  })();

  // ---- filament settings modal: temperature/cooling overrides for the
  // currently selected filament (#d-filament above, plus the overhang/fan
  // fields bound further down). No server-side custom-filament storage
  // exists yet (unlike printers' /api/printer/*) so "Save as custom" has
  // nothing to save into -- hidden rather than left clickable-but-inert.
  (function(){
    var modal = document.getElementById('filament-modal');
    var editBtn = document.getElementById('filament-edit-btn');
    var addBtn = document.getElementById('filament-add-btn');
    var closeBtn = document.getElementById('fm-modal-close');
    var doneBtn = document.getElementById('fm-modal-done');
    var backdrop = modal ? modal.querySelector('.pm-modal-backdrop') : null;
    var customRow = document.getElementById('fm-custom-row');
    if(!modal) return;
    if(customRow) customRow.style.display = 'none';

    function openModal(){ modal.style.display = 'flex'; }
    function closeModal(){ modal.style.display = 'none'; }
    if(editBtn) editBtn.addEventListener('click', openModal);
    if(addBtn) addBtn.addEventListener('click', openModal);
    if(closeBtn) closeBtn.addEventListener('click', closeModal);
    if(doneBtn) doneBtn.addEventListener('click', closeModal);
    if(backdrop) backdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape' && modal.style.display !== 'none') closeModal();
    });

    function bindReset(btnId, fieldId){
      var rbtn = document.getElementById(btnId);
      var field = document.getElementById(fieldId);
      if(!rbtn || !field) return;
      rbtn.addEventListener('click', function(){
        field.value = '';
        field.dispatchEvent(new Event('input'));
        field.dispatchEvent(new Event('change'));
      });
    }
    bindReset('fm-reset-nozzle', 'd-nozzletemp');
    bindReset('fm-reset-bed', 'd-bedtemp');
  })();

  // ---- shared hover tooltip for .fm-hover-target / .fm-info-btn, both
  // marked up with data-tip (plain text) or data-tip-html (the info-button
  // form, which needs the <br>/<code>/<span> already baked into the string).
  // Positioning mirrors the parameter tooltip's own position() -- flip off
  // the viewport edge rather than overflow it.
  (function(){
    var tip = document.getElementById('fm-tooltip');
    if(!tip) return;
    var targets = document.querySelectorAll('.fm-hover-target, .fm-info-btn');
    function show(el){
      var html = el.getAttribute('data-tip-html');
      var text = el.getAttribute('data-tip');
      if(html){ tip.innerHTML = html; }
      else if(text){ tip.textContent = text; }
      else { return; }
      tip.style.display = 'block';
      var ar = el.getBoundingClientRect();
      var margin = 8, gap = 6;
      tip.style.left = '0px'; tip.style.top = '0px';
      var tw = tip.offsetWidth, th = tip.offsetHeight;
      var x = ar.left;
      if(x + tw + margin > window.innerWidth) x = window.innerWidth - tw - margin;
      x = Math.max(margin, x);
      var y = ar.bottom + gap;
      if(y + th + margin > window.innerHeight) y = ar.top - gap - th;
      tip.style.left = Math.round(x) + 'px';
      tip.style.top = Math.round(Math.max(margin, y)) + 'px';
    }
    function hide(){ tip.style.display = 'none'; }
    targets.forEach(function(el){
      el.addEventListener('mouseenter', function(){ show(el); });
      el.addEventListener('mouseleave', hide);
      el.addEventListener('focus', function(){ show(el); });
      el.addEventListener('blur', hide);
    });
  })();

  // ---- curve editor ------------------------------------------------------
  // Variable-count control points {t, v}. First point pinned to t=0, last to
  // t=1.0. Points may be added (double-click), removed (right-click, min 2),
  // and dragged in both X and Y (except the pinned endpoints, which are Y-only).
  var MAX_PTS = 24;
  function defaultsToPts(defaults, ts){
    return defaults.map(function(v,i){ return {t: ts[i], v: v}; });
  }
  // readoutFmt (optional): function(pt) -> {mm: string, sub: string}, called
  // with the active {t,v} control point to describe it in real units. Each
  // editor's Y axis means something different (radius scale, mm of wave
  // amplitude, width multiplier), so the caller supplies its own formatter
  // rather than this shared factory guessing units. readoutElId (optional):
  // id of a persistent DOM node (with .cv-readout-mm / .cv-readout-sub
  // children) that mirrors the on-canvas label and survives mouseup.
  function makeEditor(canvasId, lo, hi, defaults, refVal, refLabel, readoutFmt, readoutElId, hardWall){
    var cv = document.getElementById(canvasId);
    var ctx = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    var PADL = 4, PADR = 4, PADT = 8, PADB = 12;
    var defaultTs = [0, 0.2, 0.4, 0.6, 0.8, 1.0];
    var pts = defaultsToPts(defaults, defaultTs);
    var dragging = -1;
    var hoverIdx = -1;
    // Last point actually interacted with (drag or hover), kept as an object
    // reference rather than an index -- indices shift on add/remove/sort, but
    // the point object itself stays valid until it is removed.
    var lastTouchedPt = null;
    var readoutEl = readoutElId ? document.getElementById(readoutElId) : null;
    var readoutMmEl = readoutEl ? readoutEl.querySelector('.cv-readout-mm') : null;
    var readoutSubEl = readoutEl ? readoutEl.querySelector('.cv-readout-sub') : null;

    // ---- two-ceiling model (see the block comment above updateSlope()) -----
    // `hardWall` (bool, only true for the amp-curve editor) switches draw()
    // between two entirely different ceiling treatments:
    //
    //  - false (sil-curve, width-curve): the ORIGINAL plain dashed line at
    //    refVal/refLabel, unchanged from before this rework. Neither editor
    //    has a printer-derived ceiling, so there is nothing to distinguish.
    //
    //  - true (amp-curve): hi IS the printer's z_amp_max -- a physical wall
    //    control points are clamped against (see setRange()) -- so the wall
    //    is drawn at the axis TOP, always, on every printer, labelled with
    //    wallLabel. A SEPARATE, optional soft limit (the print-quality slope
    //    cap) can additionally be set via setSoftLimit(); it is advisory
    //    only -- the server warns but still generates -- so it never moves
    //    hi and never clamps a point, and is drawn only while it actually
    //    falls inside the axis (softVal < hi). refVal/refLabel are UNUSED in
    //    this mode (wallLabel below is the amp editor's own label channel,
    //    seeded from refLabel at construction only so the very first
    //    synchronous render -- before /api/printers resolves -- still shows
    //    a sane bootstrap value, same trick AMP_MAX/PROBE_LIMIT already use).
    var wallLabel = refLabel;
    var softVal = null, softLabel = null;
    // Last-drawn label geometry, canvas-pixel space -- test-only, refreshed
    // every draw() so viewer/dev_smoke.html can assert a label rectangle
    // never leaves the canvas (the exact regression this rework fixes: see
    // ceilings() near the bottom of this factory).
    var lastWallRect = null, lastSoftRect = null;

    function css(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
    function px(t){ return PADL + t*(W-PADL-PADR); }
    function py(v){ return PADT + (1-(v-lo)/(hi-lo))*(H-PADT-PADB); }
    function toVal(y){ var v = lo + (1-(y-PADT)/(H-PADT-PADB))*(hi-lo); return Math.min(hi, Math.max(lo, v)); }
    function toT(x){ var t = (x-PADL)/(W-PADL-PADR); return Math.min(1, Math.max(0, t)); }

    function sortPts(){ pts.sort(function(a,b){ return a.t - b.t; }); }

    // Fixed label-chip dimensions, shared between labelChip() itself and
    // draw()'s "is there room above the line" hint below -- kept as named
    // constants rather than two copies of the same numbers so the hint can
    // never drift out of sync with what labelChip() actually needs to fit.
    var LABEL_BOX_H = 13, LABEL_GAP = 3;

    // Draws a filled chip (panel-background plate) behind `text` so it stays
    // legible over the plot fill, the curve and (for the amp editor) the
    // soft-limit band, then draws the text on top in `color`. Returns the
    // chip's rectangle in canvas-pixel space so draw() can stash it for
    // dev_smoke's off-canvas/clipping assertions.
    //
    // anchorRight: true pins the chip's right edge near the plot's right
    // edge, false pins its left edge near the plot's left edge. The wall
    // label and the soft-limit label are pinned to OPPOSITE edges (see their
    // call sites in draw()) specifically so the two can never overlap each
    // other, even when their lines sit only a few pixels apart -- the one
    // geometry this canvas is too small to resolve by vertical spacing alone.
    //
    // preferAbove: try to sit the chip above lineY first; if that would run
    // the chip off the top of the canvas (the exact clipping bug this
    // rework fixes -- the old code drew the "amp limit" label at
    // py(refVal)-2 with refVal===hi, i.e. right at the canvas edge), flip it
    // below instead. The reverse flip (prefer below, flip up if it would run
    // off the bottom) is available for symmetry even though nothing calls it
    // with preferAbove:false today, keeping the helper reusable.
    function labelChip(text, lineY, anchorRight, preferAbove, color){
      ctx.font = '9px sans-serif';
      var tw = ctx.measureText(text).width;
      var padX = 3, boxW = tw + padX*2, boxH = LABEL_BOX_H, gap = LABEL_GAP;
      var above = preferAbove;
      var y = above ? (lineY - gap - boxH) : (lineY + gap);
      if(above && y < 0){ above = false; y = lineY + gap; }
      else if(!above && y + boxH > H){ above = true; y = lineY - gap - boxH; }
      // Final defensive clamp: whichever side was chosen still has to fit.
      // Reachable only in a degenerate axis (e.g. hi very close to lo) where
      // neither side has boxH of room -- better a slightly overlapping label
      // than one with a coordinate that pushes it off the canvas entirely.
      y = Math.max(0, Math.min(y, H - boxH));
      var x = anchorRight ? (W - PADR - boxW) : PADL;
      x = Math.max(0, Math.min(x, W - boxW));
      ctx.fillStyle = css('--panel') || '#1e2124';
      ctx.fillRect(x, y, boxW, boxH);
      ctx.fillStyle = color;
      ctx.fillText(text, x + padX, y + boxH - 4);
      return { x:x, y:y, w:boxW, h:boxH };
    }

    function draw(){
      ctx.clearRect(0,0,W,H);
      var accent = css('--accent') || '#2997ff';
      var muted = css('--muted') || '#9aa0a6';
      var warn = css('--warn') || '#ffb454';

      // Soft-limit "may collapse" band, drawn FIRST so the curve fill/stroke
      // and control points layer on top of it and stay the most legible
      // thing on the chart (requirement: the tint must not bury the curve).
      // Fills from the slope cap up to the axis top (hi) -- the region where
      // amplitude is still inside the printer's hard z_amp_max wall but over
      // the print-quality slope advisory. Clamp softVal into [lo, hi] before
      // computing pixels: slopeAmpCap() cannot go negative for sane inputs,
      // but a rect built from an unclamped value is exactly the kind of edge
      // case that produces an inverted rect the moment an input changes.
      var bandTop = null, bandBottom = null, clampedSoft = null;
      if(hardWall && softVal !== null && isFinite(softVal) && softVal < hi){
        clampedSoft = Math.min(hi, Math.max(lo, softVal));
        bandTop = py(hi);
        bandBottom = py(clampedSoft);
        ctx.globalAlpha = 0.16;
        ctx.fillStyle = warn;
        ctx.fillRect(PADL, bandTop, W-PADL-PADR, Math.max(0, bandBottom - bandTop));
        ctx.globalAlpha = 1;
      }

      // filled area under the polyline
      ctx.beginPath();
      ctx.moveTo(px(pts[0].t), py(pts[0].v));
      for(var i=1;i<pts.length;i++){ ctx.lineTo(px(pts[i].t), py(pts[i].v)); }
      ctx.lineTo(px(pts[pts.length-1].t), H-PADB);
      ctx.lineTo(px(pts[0].t), H-PADB);
      ctx.closePath();
      ctx.fillStyle = 'rgba(47,107,255,0.14)';
      ctx.fill();
      // polyline
      ctx.beginPath();
      ctx.moveTo(px(pts[0].t), py(pts[0].v));
      for(var j=1;j<pts.length;j++){ ctx.lineTo(px(pts[j].t), py(pts[j].v)); }
      ctx.strokeStyle = accent; ctx.lineWidth = 1.6; ctx.stroke();

      var wallRect = null, softRect = null, softLineY = null;
      if(hardWall){
        // Hard wall: always the axis top (py(hi) === PADT), on every
        // printer -- see the block comment above wallLabel's declaration.
        // Drawn muted/dashed like the old plain reference line (same visual
        // weight as sil-curve/width-curve's ceiling), just repositioned to a
        // coordinate that can never clip: the label is placed BELOW its
        // line by labelChip()'s preferAbove:false, because "above" here
        // would be off the top of the canvas by construction.
        ctx.setLineDash([4,3]);
        ctx.beginPath();
        ctx.moveTo(PADL, py(hi)); ctx.lineTo(W-PADR, py(hi));
        ctx.strokeStyle = muted; ctx.lineWidth = 1; ctx.stroke();
        ctx.setLineDash([]);

        // Soft limit line: a dash pattern visually distinct from the hard
        // wall's, in --warn (the reserved "risk flag" token -- a collapse
        // risk is exactly the safety state that token is for).
        if(bandTop !== null){
          softLineY = bandBottom;
          ctx.setLineDash([2,2]);
          ctx.beginPath();
          ctx.moveTo(PADL, softLineY); ctx.lineTo(W-PADR, softLineY);
          ctx.strokeStyle = warn; ctx.lineWidth = 1; ctx.stroke();
          ctx.setLineDash([]);
        }
      } else {
        // Plain reference line -- sil-curve/width-curve, unchanged from
        // before this rework. Never gets a band; never gets the hard-wall
        // top-of-axis treatment. The old "probe" colour branch lived here
        // and is gone: no label has said "probe" in a long time (that text
        // now lives in the amp-limit hint elsewhere in the panel), so it was
        // dead code a future reader could easily mistake for live behaviour.
        ctx.setLineDash([4,3]);
        ctx.beginPath();
        ctx.moveTo(PADL, py(refVal)); ctx.lineTo(W-PADR, py(refVal));
        ctx.strokeStyle = muted; ctx.lineWidth = 1; ctx.stroke();
        ctx.setLineDash([]);
      }

      // control points
      for(var k=0;k<pts.length;k++){
        ctx.beginPath(); ctx.arc(px(pts[k].t), py(pts[k].v), 3.2, 0, Math.PI*2);
        ctx.fillStyle = accent; ctx.fill();
      }

      // labels (ASCII)
      if(hardWall){
        wallRect = labelChip(wallLabel, py(hi), true, false, muted);
        if(softLineY !== null){
          // Mirrors labelChip()'s own "does above fit" test (y = lineY -
          // gap - boxH >= 0) so this hint can never disagree with what the
          // helper actually decides -- it only saves labelChip() a redundant
          // flip-then-reclamp pass in the common case.
          var fitsAbove = (softLineY - LABEL_GAP - LABEL_BOX_H) >= 0;
          softRect = labelChip(softLabel, softLineY, false, fitsAbove, warn);
        }
      } else {
        ctx.fillStyle = muted;
        ctx.font = '9px sans-serif';
        ctx.fillText(refLabel, PADL+2, py(refVal)-2);
      }
      lastWallRect = wallRect;
      lastSoftRect = softRect;
      ctx.fillStyle = muted;
      ctx.font = '9px sans-serif';
      ctx.fillText('bottom', PADL, H-2);
      ctx.fillText('top', W-18, H-2);
      // Numeric readout for the active point (dragging, else hovered) --
      // drawn right next to the dot so the eye never has to leave the point.
      var activeIdx = dragging >= 0 ? dragging : hoverIdx;
      if(activeIdx >= 0 && pts[activeIdx]){
        var dp = pts[activeIdx];
        lastTouchedPt = dp;
        var label = readoutFmt ? readoutFmt(dp).mm
                                : ('t=' + dp.t.toFixed(2) + ' v=' + dp.v.toFixed(2));
        ctx.font = '10px sans-serif';
        var tw = ctx.measureText(label).width;
        var lx = px(dp.t) + 8;
        if(lx + tw + 4 > W) lx = px(dp.t) - tw - 8;   // flip left near the right edge
        lx = Math.max(2, Math.min(lx, W - tw - 2));   // clamp inside the canvas
        var ly = py(dp.v) - 8;
        ly = Math.max(11, Math.min(ly, H - PADB - 3));
        // Backing plate so the label stays legible over the filled curve area.
        ctx.fillStyle = 'rgba(10,11,13,0.72)';
        ctx.fillRect(lx - 3, ly - 10, tw + 6, 13);
        ctx.fillStyle = accent;
        ctx.fillText(label, lx, ly);
      }
      // Persistent readout: the active point, or the last one touched, so the
      // value survives mouseup instead of vanishing with the cursor. Recomputed
      // from readoutFmt on every draw(), so a redraw after design.radius /
      // design.height changes (see the d-radius/d-height hooks below) is
      // enough to bring it current -- no separate "refresh" path needed.
      if(readoutMmEl && readoutSubEl){
        if(lastTouchedPt && readoutFmt){
          var info = readoutFmt(lastTouchedPt);
          readoutMmEl.textContent = info.mm;
          readoutSubEl.textContent = info.sub ? ('  ' + info.sub) : '';
        } else {
          readoutMmEl.textContent = '--';
          readoutSubEl.textContent = 'hover or drag a point';
        }
      }
    }

    function nearest(mx, my){
      var best=-1, bd=1e9;
      for(var i=0;i<pts.length;i++){
        var dx=px(pts[i].t)-mx, dy=py(pts[i].v)-my, d=dx*dx+dy*dy;
        if(d<bd){ bd=d; best=i; }
      }
      return bd <= 18*18 ? best : -1;
    }
    function evtXY(e){
      var r = cv.getBoundingClientRect();
      var cx = (e.touches?e.touches[0].clientX:e.clientX) - r.left;
      var cy = (e.touches?e.touches[0].clientY:e.clientY) - r.top;
      return [cx*(W/r.width), cy*(H/r.height)];
    }

    cv.addEventListener('mousedown', function(e){
      var p=evtXY(e); dragging = nearest(p[0], p[1]);
      if(dragging>=0){
        pts[dragging].v = toVal(p[1]);
        if(dragging !== 0 && dragging !== pts.length-1){
          pts[dragging].t = toT(p[0]);
        }
        draw(); onChange();
      }
    });
    window.addEventListener('mousemove', function(e){
      if(dragging<0) return;
      var p=evtXY(e);
      // Shift = fine adjustment: ease 20% toward the cursor per event.
      var fine = e.shiftKey ? 0.2 : 1.0;
      var targetV = toVal(p[1]);
      pts[dragging].v += (targetV - pts[dragging].v) * fine;
      if(dragging !== 0 && dragging !== pts.length-1){
        // clamp t between neighbours so ordering can't flip
        var tMin = pts[dragging-1].t + 1e-3;
        var tMax = pts[dragging+1].t - 1e-3;
        var t = toT(p[0]);
        var targetT = Math.min(tMax, Math.max(tMin, t));
        pts[dragging].t += (targetT - pts[dragging].t) * fine;
      }
      draw(); onChange();
    });
    window.addEventListener('mouseup', function(){
      // Stop dragging and clear the on-canvas label; the persistent readout
      // keeps showing this point (lastTouchedPt) until another is touched.
      if(dragging >= 0){ dragging=-1; draw(); }
    });

    // Hover (no button held): cheap on a canvas this small, and it is what
    // lets the mm readout appear before the user commits to a drag.
    cv.addEventListener('mousemove', function(e){
      if(dragging >= 0) return;   // the window-level drag listener handles this
      var p = evtXY(e);
      var idx = nearest(p[0], p[1]);
      if(idx !== hoverIdx){ hoverIdx = idx; draw(); }
    });
    cv.addEventListener('mouseleave', function(){
      if(hoverIdx !== -1){ hoverIdx = -1; draw(); }
    });

    // Double-click: insert a new point at the clicked position.
    cv.addEventListener('dblclick', function(e){
      if(pts.length >= MAX_PTS) return;
      var p = evtXY(e);
      var t = toT(p[0]);
      var v = toVal(p[1]);
      // Don't insert exactly on top of an existing point.
      var hit = nearest(p[0], p[1]);
      if(hit >= 0) return;
      pts.push({t:t, v:v});
      sortPts();
      hoverIdx = -1;   // point positions shifted; stale index would mislabel
      draw(); onChange();
    });

    // Right-click: remove nearest point (never the endpoints, min 2 points).
    cv.addEventListener('contextmenu', function(e){
      e.preventDefault();
      if(pts.length <= 2) return;
      var p = evtXY(e);
      var idx = nearest(p[0], p[1]);
      if(idx <= 0 || idx >= pts.length-1) return; // can't remove endpoints
      if(pts[idx] === lastTouchedPt) lastTouchedPt = null;
      pts.splice(idx, 1);
      hoverIdx = -1;
      draw(); onChange();
    });

    var onChange = function(){};

    return {
      draw: draw,
      profile: function(){
        sortPts();
        return pts.map(function(p){ return [p.t, p.v]; });
      },
      reset: function(){
        pts = defaultsToPts(defaults, defaultTs);
        // `defaults` is a fixed array captured at construction time, in the
        // ORIGINAL bootstrap scale (0..AMP_MAX=0.95 for the amp editor) --
        // it does not know about a printer switch that has since lowered
        // `hi`. Without this, resetting the amp curve on any printer whose
        // z_amp_max is below the default peak (0.8mm) put points ABOVE the
        // wall: same bug setRange() already guards against for a printer
        // switch, just reachable through a different button.
        for(var i = 0; i < pts.length; i++){
          pts[i].v = Math.min(hi, Math.max(lo, pts[i].v));
        }
        hoverIdx = -1; lastTouchedPt = null;
        draw(); onChange();
      },
      setChangeHandler: function(fn){ onChange = fn; },
      setProfile: function(prof){
        if(!prof || prof.length < 2) return;
        pts = prof.map(function(p){ return {t: p[0], v: p[1]}; });
        sortPts();
        pts[0].t = 0;
        pts[pts.length-1].t = 1.0;
        // The old point objects are gone; drop any reference to them.
        hoverIdx = -1; lastTouchedPt = null;
        draw();
      },
      // Changes the editor's HARD ceiling (e.g. a new printer's z_amp_max)
      // and re-clamps every existing control point into the new [lo, hi]
      // range -- not just a paired number input, since the amp value lives
      // entirely in these points. Deliberately takes ONLY newHi: the old
      // 3-argument form also carried a reference value/label, but every real
      // call site (applyPrinterCaps()) already passed just the ceiling and
      // relied on the updateSlope() call immediately after to set the
      // label -- see setHardWallLabel()/setSoftLimit() below, which is now
      // that funnel. Returns how many points actually moved, so a caller can
      // report it.
      setRange: function(newHi){
        hi = newHi;
        var moved = 0;
        for(var i = 0; i < pts.length; i++){
          var clamped = Math.min(hi, Math.max(lo, pts[i].v));
          if(clamped !== pts[i].v){ pts[i].v = clamped; moved++; }
        }
        draw();
        return moved;
      },
      // Moves ONLY the plain dashed reference line and its label (sil-curve/
      // width-curve's ceiling) -- never touches hi/lo, never re-clamps a
      // control point. No shipped call site uses this today (both plain-line
      // editors are constructed with a fixed refVal that never moves), but
      // it is kept as the general "move the plain reference line" entry
      // point the constructor's refVal/refLabel imply exists, parallel to
      // setHardWallLabel()/setSoftLimit() below for the amp editor's two-
      // ceiling model. hardWall-mode editors should use those instead --
      // this setter's plain line is never drawn while hardWall is true.
      setRef: function(newRefVal, newRefLabel){
        if(typeof newRefVal === 'number') refVal = newRefVal;
        if(typeof newRefLabel === 'string') refLabel = newRefLabel;
        draw();
      },
      // ---- hardWall-mode ceiling setters (amp-curve only) ------------------
      // Sets the hard-wall label. Its VALUE is deliberately not a parameter:
      // hi already IS the wall (see the block comment above wallLabel's
      // declaration), so the only thing left to carry is the text --
      // duplicating hi here would just be a second number that could drift
      // from the first. Called every updateSlope() run in designer.js,
      // same as setSoftLimit()/clearSoftLimit() below, so the three can
      // never disagree about which printer they are describing.
      setHardWallLabel: function(label){
        if(typeof label === 'string'){ wallLabel = label; draw(); }
      },
      // Sets the advisory slope-cap line/band/label. Never touches hi/lo and
      // never re-clamps a control point -- see the CLAUDE.md-driven design
      // note above updateSlope() in designer.js for why that must stay true
      // (the server only warns on this ceiling, it does not reject; clamping
      // here would silently discard geometry the print would actually run).
      // draw() itself re-checks val < hi before showing anything, so passing
      // a val that has drifted above hi (e.g. the printer switched under a
      // stale slope-cap number) safely produces no band rather than a bad
      // one -- this setter does not need to duplicate that guard.
      setSoftLimit: function(val, label){
        if(typeof val !== 'number' || !isFinite(val)){ softVal = null; softLabel = null; draw(); return; }
        softVal = val;
        softLabel = (typeof label === 'string') ? label : '';
        draw();
      },
      clearSoftLimit: function(){
        if(softVal !== null){ softVal = null; softLabel = null; draw(); }
      },
      // Test-only introspection of the two-ceiling model's current numbers
      // AND drawn geometry -- the lines/bands/label chips are drawn to a
      // <canvas>, which has no DOM to read back, so viewer/dev_smoke.html's
      // ceiling assertions go through this instead of the pixels. Rects are
      // canvas-pixel space (the same W x H coordinate system draw() uses),
      // so a test can assert e.g. `rect.y >= 0 && rect.y + rect.h <= H` --
      // exactly the clipping regression that motivated this rework.
      ceilings: function(){
        return {
          hi: hi,
          wall: { val: hi, label: wallLabel, rect: lastWallRect },
          soft: (softVal !== null && isFinite(softVal) && softVal < hi)
            ? { val: softVal, label: softLabel,
                lineY: py(Math.min(hi, Math.max(lo, softVal))),
                bandTop: py(hi), bandBottom: py(Math.min(hi, Math.max(lo, softVal))),
                rect: lastSoftRect }
            : null
        };
      }
    };
  }

  // ---- per-editor mm readouts ---------------------------------------------
  // Each curve editor's Y axis means something different (a radius scale
  // factor, mm of wave amplitude, a line-width multiplier), so each gets its
  // own formatter rather than the shared makeEditor() factory guessing units.
  // Height mm is always t * design.height (a point's X position is the
  // height fraction); degenerate design.radius/height/nozzle (unset, zero,
  // non-finite) fall back to "--" rather than printing garbage.
  function fmtHeightMm(t){
    var h = design.height;
    return (typeof h === 'number' && isFinite(h) && h > 0) ? (t * h) : null;
  }
  function silReadout(pt){
    var r = design.radius;
    var mm = (typeof r === 'number' && isFinite(r) && r > 0) ? (r * pt.v) : null;
    var h = fmtHeightMm(pt.t);
    return {
      mm: mm != null ? ('R ' + mm.toFixed(1) + ' mm') : 'R --',
      sub: pt.v.toFixed(2) + 'x' + (h != null ? ('  @ H ' + h.toFixed(1) + ' mm') : '')
    };
  }
  function ampReadout(pt){
    // This editor's Y axis is already millimetres of wave amplitude
    // (0..z_amp_max), not a scale factor -- no multiplication needed.
    var h = fmtHeightMm(pt.t);
    return {
      mm: 'amp ' + pt.v.toFixed(2) + ' mm',
      sub: h != null ? ('@ H ' + h.toFixed(1) + ' mm') : ''
    };
  }
  function widthReadout(pt){
    // This editor's Y axis is a multiplier on the nominal bead width, which
    // is either the explicit override (design.line_width) or nozzle*1.125 --
    // the same formula the "Flow line width" hint on the Print tab uses.
    var nz = (design.nozzle === '' || design.nozzle == null) ? 0.4 : parseFloat(design.nozzle);
    var nominal = design.line_width != null ? design.line_width : (isFinite(nz) ? nz * 1.125 : null);
    var mm = (nominal != null && isFinite(nominal)) ? (pt.v * nominal) : null;
    var h = fmtHeightMm(pt.t);
    return {
      mm: mm != null ? ('line ' + mm.toFixed(2) + ' mm') : 'line --',
      sub: pt.v.toFixed(2) + 'x' + (h != null ? ('  @ H ' + h.toFixed(1) + ' mm') : '')
    };
  }

  // Default amp curve peaks at 1.6mm: with the default 5 waves on r=32 that is
  // wave slope 0.25 - the empirically printable ceiling on this machine
  // (2026-07-05 print: slopes beyond ~0.25 collapsed above half height).
  // hardWall:true -- the amp editor is the one editor whose axis top is a
  // physical printer limit (z_amp_max); see the block comment above
  // wallLabel's declaration inside makeEditor(). refVal/refLabel here only
  // seed the bootstrap label shown before /api/printers resolves.
  var ampEditor = makeEditor('amp-curve', 0, AMP_MAX, [0, 0.3, 0.6, 0.8, 0.8, 0.5],
                             PROBE_LIMIT, 'amp limit 0.95', ampReadout, 'amp-readout', true);
  var silEditor = makeEditor('sil-curve', RAD_LO, RAD_HI, [1,1,1,1,1,1],
                             1.0, '1.0', silReadout, 'sil-readout');
  var widthEditor = makeEditor('width-curve', 0.6, 1.8, [1,1,1,1,1,1],
                               1.0, '1.0', widthReadout, 'width-readout');
  // Test-only: see makeEditor()'s ceilings() and viewer/dev_smoke.html's
  // amp-editor two-ceiling assertions.
  window.__testAmpCeilings = function(){ return ampEditor.ceilings(); };
  // Restore persisted curve shapes.
  ampEditor.setProfile(design.amp_profile);
  silEditor.setProfile(design.radius_profile);
  widthEditor.setProfile(design.width_profile);
  ampEditor.draw(); silEditor.draw(); widthEditor.draw();
  // Curve editors report per mousemove while a point is dragged, so each gets
  // its own coalescing key: one drag of one curve = one undo entry, closed by
  // the document-level pointerup ender (these canvases have no commit event).
  ampEditor.setChangeHandler(function(){ design.amp_profile = ampEditor.profile(); persistDesign('curve:amp'); updateSlope(); schedulePreview(); });
  silEditor.setChangeHandler(function(){ design.radius_profile = silEditor.profile(); persistDesign('curve:sil'); updateSlope(); schedulePreview(); refreshShapeCage(); });
  widthEditor.setChangeHandler(function(){ design.width_profile = widthEditor.profile(); persistDesign('curve:width'); schedulePreview(); });
  document.getElementById('amp-reset').addEventListener('click', function(){ ampEditor.reset(); });
  document.getElementById('sil-reset').addEventListener('click', function(){ silEditor.reset(); });
  document.getElementById('width-reset').addEventListener('click', function(){ widthEditor.reset(); });

  // Defensive no-op in the normal case (the /api/printers fetch above always
  // resolves after this synchronous block finishes, and its own .then()
  // already calls these) -- but if PRINTER_ZCAP somehow already has data by
  // now, ampEditor exists here for the first time and should be synced to it
  // immediately rather than showing the bootstrap 0.95 scale a tick longer
  // than necessary. setRange()/re-clamping is idempotent either way.
  applyPrinterCaps();
  updateMachineSummary();

  // Smooth-curve checkbox for the silhouette editor.
  (function(){
    var el = document.getElementById('sil-smooth');
    if(!el) return;
    el.checked = !!design.radius_profile_smooth;
    el.addEventListener('change', function(){
      design.radius_profile_smooth = el.checked;
      persistDesign();
      updateSlope();
      schedulePreview();
    });
  })();

  // ---- 3D shape cage (draggable grid of points on the draft preview) --------
  var CAGE_ROWS = 5, CAGE_COLS = 8;

  // Ensures design.cage is a valid rows x cols grid of scale factors (all 1.0
  // = no deformation). Called before the cage is shown or reset so old saves
  // / fresh designs always get a usable grid.
  function ensureCage(){
    if(Array.isArray(design.cage) && design.cage.length && Array.isArray(design.cage[0])) return;
    var grid = [];
    for(var i = 0; i < CAGE_ROWS; i++){
      var row = [];
      for(var j = 0; j < CAGE_COLS; j++) row.push(1.0);
      grid.push(row);
    }
    design.cage = grid;
  }

  // Linear sample of a [t,v] control-point profile (same shape as
  // silEditor.profile()) at an arbitrary t -- used to compute the per-row
  // base radius (silhouette scale) the cage handles sit on.
  function linearSample(profile, t){
    if(!profile || !profile.length) return 1.0;
    if(t <= profile[0][0]) return profile[0][1];
    for(var i = 1; i < profile.length; i++){
      if(t <= profile[i][0]){
        var t0 = profile[i-1][0], v0 = profile[i-1][1];
        var t1 = profile[i][0],   v1 = profile[i][1];
        var frac = (t1 - t0) < 1e-9 ? 1.0 : (t - t0) / (t1 - t0);
        return v0 + frac * (v1 - v0);
      }
    }
    return profile[profile.length-1][1];
  }

  // Rebuilds the on-model cage handle group from the current design.radius /
  // silhouette / design.cage state. Skipped while a drag is in progress
  // (window.__silDragActive) so a preview refresh mid-drag doesn't rebuild
  // the group out from under the active handle.
  function refreshShapeCage(){
    if(!design.sil3d || !window.showShapeCage) return;
    if(window.__silDragActive) return;
    ensureCage();
    var cage = design.cage;
    var rows = cage.length, cols = cage[0].length;
    var silProfile = silEditor.profile();
    var base = [];
    for(var i = 0; i < rows; i++){
      var t = rows > 1 ? i/(rows-1) : 0;
      var silScale = linearSample(silProfile, t);
      var row = [];
      for(var j = 0; j < cols; j++) row.push(design.radius * silScale);
      base.push(row);
    }
    window.showShapeCage({
      rows: rows, cols: cols, height: design.height,
      base: base, scales: cage
    }, function(changes){
      // A cage-handle drag happens on the 3D viewport canvas, outside the
      // side-panel arming listeners -- so arm the live preview here or the
      // blue draft would never draw for a user who only touched the model.
      // `changes` covers the whole dragged selection at once (viewer.js
      // fires this once per move event, not once per handle) -- write every
      // entry, then persist/schedule exactly once.
      previewArmed = true;
      for(var k = 0; k < changes.length; k++){
        design.cage[changes[k].i][changes[k].j] = changes[k].scale;
      }
      // One cage drag = one undo entry (fires per move event, like the curve
      // editors above); the pointerup ender closes the run on release.
      persistDesign('cage');
      schedulePreview();
      updateCageResetState();
    });
    updateCageNote();
    updateCageResetState();
  }

  // True when any cage handle is off the neutral 1.0, i.e. there is actually
  // something for "Reset all points" to undo.
  function cageHasEdits(){
    if(!Array.isArray(design.cage)) return false;
    for(var i = 0; i < design.cage.length; i++){
      var row = design.cage[i];
      if(!Array.isArray(row)) continue;
      for(var j = 0; j < row.length; j++){
        if(Math.abs(row[j] - 1.0) > 1e-6) return true;
      }
    }
    return false;
  }

  // Enables "Reset all points" only when edits exist. Runs on load too, so a
  // design restored from localStorage with deformation already in it shows an
  // enabled button before the user has touched anything.
  function updateCageResetState(){
    var el = document.getElementById('cage-reset');
    if(el) el.disabled = !cageHasEdits();
  }

  // Warns (in the warning color) when freeform mode is active over the
  // 'loops' pattern, since the per-point cage deformation doesn't apply to
  // loop fabric geometry.
  function updateCageNote(){
    var noteEl = document.getElementById('cage-note');
    // Mirrors --warn in style.css (safety/constraint-state color, not decoration).
    if(noteEl) noteEl.style.color = (design.pattern === 'loops') ? '#ffb454' : '';
  }

  // ---- Silhouette mode: Symmetrical (curve editor) vs Freeform/3D (cage) ----
  // NOTE: the visible label is "Freeform (3D)" but the persisted mode value
  // stays "asym" -- it is written into saved designs and localStorage, so
  // renaming it would orphan every design already on disk.
  function activateSilMode(name){
    document.querySelectorAll('.sil-mode-btn').forEach(function(btn){
      var on = btn.getAttribute('data-silmode') === name;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var symPanel = document.getElementById('sil-sym-panel');
    var asymPanel = document.getElementById('sil-asym-panel');
    if(symPanel) symPanel.style.display = name === 'asym' ? 'none' : '';
    if(asymPanel) asymPanel.style.display = name === 'asym' ? '' : 'none';
    design.sil3d = (name === 'asym');
    if(name === 'asym'){
      ensureCage();
      refreshShapeCage();
      updateCageNote();
    } else if(window.hideShapeCage){
      window.hideShapeCage();
    }
    // Covers entering freeform mode on a restored design, and keeps the
    // button honest if the mode is switched back and forth.
    updateCageResetState();
  }

  (function(){
    document.querySelectorAll('.sil-mode-btn').forEach(function(btn){
      btn.addEventListener('click', function(){
        previewArmed = true;   // switching silhouette mode is a design action
        design.sil_mode = btn.getAttribute('data-silmode');
        activateSilMode(design.sil_mode);   // sets design.sil3d before we persist it
        persistDesign();
        schedulePreview();
      });
    });
    activateSilMode(design.sil_mode || 'sym');
  })();

  // Reset-cage button: clears all deformation back to 1.0 everywhere.
  (function(){
    var el = document.getElementById('cage-reset');
    if(!el) return;
    el.addEventListener('click', function(){
      ensureCage();
      for(var i = 0; i < design.cage.length; i++){
        for(var j = 0; j < design.cage[i].length; j++) design.cage[i][j] = 1.0;
      }
      // Arm the preview, exactly as the cage-drag callback does. schedulePreview
      // is a no-op while previewArmed is false, and that flag starts false on
      // every page load -- so without this, a reset straight after a refresh
      // updated design.cage but never redrew, and the button looked dead until
      // the user happened to touch the canvas.
      previewArmed = true;
      persistDesign();
      schedulePreview();
      refreshShapeCage();
      updateCageResetState();
    });
  })();

  // Selection-size readout + enable/disable for the "reset selected points"
  // button. Driven by viewer.js: cageRestyle() there calls this every time a
  // cage handle's hover/selection state is touched, with the current
  // selection size. Guarded with typeof since viewer.js may call it before
  // this file has run (module load order), or not at all if viewer.js failed
  // to load.
  window.onCageSelectionChange = function(n){
    var el = document.getElementById('cage-selcount');
    if(el){
      el.textContent = n === 0 ? 'No points selected'
        : (n === 1 ? '1 point selected' : (n + ' points selected'));
      el.classList.toggle('has-sel', n > 0);
    }
    var btn = document.getElementById('cage-reset-sel');
    if(btn) btn.disabled = (n === 0);
    var clr = document.getElementById('cage-clear-sel');
    if(clr) clr.disabled = (n === 0);
  };

  // Clear-selection button: a pointer-only path to the same thing Esc does,
  // for when a keyboard shortcut isn't reaching the viewport listener.
  (function(){
    var el = document.getElementById('cage-clear-sel');
    if(!el) return;
    el.addEventListener('click', function(){
      if(window.clearCageSelection) window.clearCageSelection();
    });
  })();

  // Reset-selected-points button: resets only the handles in the current
  // cage selection back to 1.0, leaving the rest of the cage untouched.
  (function(){
    var el = document.getElementById('cage-reset-sel');
    if(!el) return;
    el.addEventListener('click', function(){
      if(!window.getCageSelection) return;
      var sel = window.getCageSelection();
      if(!sel.length) return;
      ensureCage();
      for(var k = 0; k < sel.length; k++) design.cage[sel[k].i][sel[k].j] = 1.0;
      previewArmed = true;   // same reason as the reset-all handler above
      persistDesign();
      schedulePreview();
      refreshShapeCage();
      updateCageResetState();
    });
  })();

  // ---- live wave-slope readout --------------------------------------------
  // Peak wall slope = amp(t) * z_waves / (radius * silhouette(t)). The
  // Trident's own empirical print-quality ceiling is ~0.25 (about 14 deg,
  // from the 2026-07-05 test print where waves steeper than it collapsed
  // above half height) -- but per CLAUDE.md ("no machine limit may be a
  // module constant") that ceiling now lives on the PrinterProfile as
  // quality_slope_max and arrives over /api/printers like every other
  // machine limit. See PRINTER_SLOPE / activeSlopeLimit() near the top of
  // this file. z_amp_max caps amplitude outright; THIS caps printability of
  // whatever amplitude is still inside that ceiling.
  function lerpProfile(prof, t){
    for(var i=1;i<prof.length;i++){
      if(t <= prof[i][0]){
        var t0=prof[i-1][0], v0=prof[i-1][1], t1=prof[i][0], v1=prof[i][1];
        return t1-t0 < 1e-9 ? v1 : v0 + (t-t0)/(t1-t0)*(v1-v0);
      }
    }
    return prof[prof.length-1][1];
  }

  // The amplitude at which the slope check itself trips, for the CURRENT
  // wave count / radius / silhouette curve: solving slope = amp*waves /
  // (radius*sil) for amp at slope==slopeLimit, using the silhouette's
  // narrowest point (its smallest v) because a narrower waist reads the
  // same amplitude as a steeper slope -- the worst case, and therefore the
  // amplitude that trips the check soonest as amp rises from 0. Returns null
  // when there are no waves at all: no waves means no slope to constrain.
  // This is the one place both the amp-editor's dashed reference line and
  // the readout's explanatory hint compute this number, so they can never
  // disagree about why a design got flagged.
  // The narrowest effective wall radius (radius * the silhouette's smallest
  // v). Both the cap below and the readout's hint quote THIS rather than the
  // nominal radius: on a design waisted to 0.5 the nominal figure overstates
  // the available amplitude headroom by 2x, and the hint would then name a
  // radius the cap was not computed from. Mirrors serve.py's
  // _min_wall_radius() so the browser and the server explain a flagged
  // design with the same two numbers.
  function minWallRadius(radius, sil){
    var minSil = sil[0][1];
    for(var i=1;i<sil.length;i++){ if(sil[i][1] < minSil) minSil = sil[i][1]; }
    var r = (typeof radius === 'number' && isFinite(radius)) ? radius : 0;
    return r * minSil;
  }
  function slopeAmpCap(slopeLimit, radius, waves, sil){
    if(!waves) return null;
    return slopeLimit * minWallRadius(radius, sil) / waves;
  }

  function updateSlope(){
    var el = document.getElementById('slope-read');
    if(!el) return;
    var waves = Math.round(design.z_waves);
    var radius = design.radius;
    var amp = ampEditor.profile(), sil = silEditor.profile();
    var slopeLimit = activeSlopeLimit();
    var peak = 0;
    for(var t=0; t<=1.0001; t+=0.02){
      var s = lerpProfile(amp, t) * waves / Math.max(radius * lerpProfile(sil, t), 1e-6);
      if(s > peak) peak = s;
    }
    var over = peak > slopeLimit + 1e-9;

    // Explain WHY, not just THAT: a printer's advertised z_amp_max can sit
    // far above what the current wave geometry can actually reach once the
    // slope check bites (a P1S's 4.0mm ceiling is unreachable at the default
    // 5 waves / r32, which read as a flat contradiction before this line
    // existed). Only computed when the design is actually over the limit --
    // there is nothing to explain otherwise.
    var hint = '';
    if(over){
      var ampCapHint = slopeAmpCap(slopeLimit, radius, waves, sil);
      if(ampCapHint != null && isFinite(ampCapHint)){
        hint = ' (' + waves + ' waves at r' +
          minWallRadius(radius, sil).toFixed(0) + ' caps amp at ' +
          ampCapHint.toFixed(2) + 'mm)';
      }
    }
    el.textContent = 'peak wave slope: ' + peak.toFixed(2) + ' / ' + slopeLimit.toFixed(2) +
      (over ? '  TOO STEEP - waves may collapse' + hint : '  ok');
    // Safety-state readout: --danger / --ok are reserved for exactly this
    // (style.css's token comment names "risk flags" explicitly) -- toggle
    // the classes that spend them rather than setting style.color to a
    // literal hex that can silently drift from the tokens it claims to
    // mirror. See .cv-note.err / .cv-note.ok in style.css.
    el.classList.toggle('err', over);
    el.classList.toggle('ok', !over);

    // Amp-editor ceilings: TWO of them, not one -- see the block comment
    // above wallLabel's declaration in makeEditor() and the two-ceiling
    // design note in this file's header comment. The hard wall (z_amp_max,
    // the editor's axis top -- setRange() already put it there) is always
    // labelled, on every printer. The slope cap is a SEPARATE, advisory
    // ceiling drawn only while it actually falls inside that wall
    // (slopeCap < zCap) -- setSoftLimit()/clearSoftLimit() never touch hi/lo
    // and never re-clamp a control point, unlike setRange(), because this
    // ceiling is print-quality advice the server warns about but still
    // generates past, not a hard stop.
    if(typeof ampEditor !== 'undefined' && ampEditor){
      var zCap = PRINTER_ZCAP[design.printer];
      if(typeof zCap === 'number' && isFinite(zCap)){
        ampEditor.setHardWallLabel('amp limit ' + fmtMm(zCap));
        var slopeCap = slopeAmpCap(slopeLimit, radius, waves, sil);
        if(slopeCap != null && isFinite(slopeCap) && slopeCap < zCap){
          ampEditor.setSoftLimit(slopeCap, 'slope cap ' + slopeCap.toFixed(2) + ' (' + waves + ' waves)');
        } else {
          ampEditor.clearSoftLimit();
        }
      }
    }
  }

  // ---- bind inputs to design state ----------------------------------------
  // Simple 2-way binding table: element id -> {field, type, show?}
  var NUM = 'number', INT = 'int', STR = 'string';

  // Clamp to the field's own min/max. Those attributes are not decoration:
  // applyPrinterCaps() rewrites them from the SELECTED PRINTER's profile, so
  // they carry that machine's real ceilings. Without this the panel would
  // hold, preview and SEND a value the server silently clamps -- typing 5 into
  // a row height whose max is 0.7 drew a 5mm spike in the draft and put 5 in
  // the request body. That is exactly what the comment above applyPrinterCaps
  // forbids: the UI must never suggest a value the server would reject or
  // clamp. The server clamp still stands behind this (it must -- a browser is
  // not a safety device); this stops the UI lying about what will be printed.
  //
  // Only the value the DESIGN uses is clamped, not the text being typed, so an
  // intermediate "0" on the way to "0.9" is not fought. The field snaps to the
  // clamped number on commit (change/blur).
  function bindNumber(id, field, isInt){
    var el = document.getElementById(id);
    if(!el) return;
    el.value = design[field];
    function applyValue(commit){
      var raw = parseFloat(el.value);
      if(Number.isNaN(raw)) return;
      var v = raw;
      var lo = parseFloat(el.min), hi = parseFloat(el.max);
      if(isFinite(lo)) v = Math.max(lo, v);
      if(isFinite(hi)) v = Math.min(hi, v);
      if(isInt) v = Math.round(v);
      design[field] = v;
      // --warn is reserved for safety states, and style.css names "clamp
      // events" among them -- a value over this printer's ceiling is one.
      el.classList.toggle('out-of-range', Math.abs(raw - v) > 1e-9);
      if(commit){ el.value = v; el.classList.remove('out-of-range'); }
      // Typing "3" then "0" into a radius, or dragging a range slider, is ONE
      // edit: coalesce the whole stream under this field's id so a single
      // Ctrl+Z returns to the value the field held before the edit started.
      // The commit (change/blur, or a spinner click) folds into the same entry
      // and then closes the run.
      persistDesign('num:' + id);
      if(commit) endHistRun();
      updateSlope();
      schedulePreview();
    }
    el.addEventListener('input', function(){ applyValue(false); });
    el.addEventListener('change', function(){ applyValue(true); });
  }
  // `coalesceKey` (optional) lets a caller that adds a SECOND listener to the
  // same change event (bindLoopSelect, which also flips the style dropdown
  // to "Custom") fold both writes into one undo entry instead of leaving two.
  function bindSelect(id, field, coalesceKey){
    var el = document.getElementById(id);
    if(!el) return;
    if(design[field]) el.value = design[field];
    el.addEventListener('change', function(){
      design[field] = el.value;
      persistDesign(coalesceKey || null);
      schedulePreview();
    });
  }

  // Shape tab.
  bindSelect('d-shape', 'shape');
  bindNumber('d-radius', 'radius');
  bindNumber('d-height', 'height');
  // The silhouette/amp/width mm readouts derive from design.radius and
  // design.height (radius*scale, t*height) but only recompute when an
  // editor redraws -- bindNumber() above never touches those canvases, so a
  // Model-tab radius/height edit would otherwise leave a stale mm figure
  // showing under Texture until the user happened to touch a curve point.
  // draw() re-invokes each editor's formatter against the CURRENT design
  // values every time, so a bare redraw is enough; no extra state to sync.
  (function(){
    var radiusEl = document.getElementById('d-radius');
    var heightEl = document.getElementById('d-height');
    if(radiusEl) radiusEl.addEventListener('input', function(){ silEditor.draw(); });
    if(heightEl) heightEl.addEventListener('input', function(){
      ampEditor.draw(); silEditor.draw(); widthEditor.draw();
    });
  })();
  bindNumber('d-lh', 'layer_height');
  bindNumber('d-xytwist', 'xy_twist');
  bindNumber('d-starpoints', 'star_points', true);
  bindNumber('d-stardepth', 'star_depth');
  bindNumber('d-base', 'base_layers', true);
  bindNumber('d-brim', 'brim', true);
  bindNumber('d-spine', 'spine_mm');
  bindNumber('d-spinedeg', 'spine_deg');
  bindNumber('d-ovality', 'ovality');
  bindSelect('d-basestyle', 'base_style');
  bindNumber('d-skirt', 'skirt', true);
  (function(){
    var el = document.getElementById('d-flh');
    if(!el) return;
    if(design.first_layer_height != null) el.value = design.first_layer_height;
    el.addEventListener('input', function(){
      var v = parseFloat(el.value);
      design.first_layer_height = (el.value === '' || Number.isNaN(v)) ? null : v;
      persistDesign('num:d-flh');
    });
    el.addEventListener('change', endHistRun);
  })();
  bindNumber('d-spacing', 'spacing_factor');

  // Waves tab.
  bindNumber('d-waves', 'z_waves', true);
  bindNumber('d-ztwist', 'z_twist');

  // Texture tab.
  bindSelect('d-pattern', 'pattern');
  bindNumber('d-pamp', 'pattern_amp');
  bindNumber('d-pwaves', 'pattern_waves', true);
  bindNumber('d-pbands', 'pattern_bands');
  bindNumber('d-ptwist', 'pattern_twist');
  bindNumber('d-pfadein', 'pattern_fade_in');
  bindNumber('d-pfadeout', 'pattern_fade_out');

  (function(){
    var en = document.getElementById('d-palternate');
    var hint = document.getElementById('lattice-hint');
    if(!en) return;
    en.checked = !!design.pattern_alternate;
    if(hint) hint.style.display = en.checked ? 'block' : 'none';
    en.addEventListener('change', function(){
      design.pattern_alternate = en.checked;
      if(hint) hint.style.display = en.checked ? 'block' : 'none';
      persistDesign();
      schedulePreview();
    });
  })();

  // Loop texture controls.

  var LOOP_STYLES = {
    tiedspikes:{ loop_spacing_mm:4.0, loop_per_turn:0, loop_align:'stagger',
                 loop_row:2.5, loop_up:3.2, loop_out:0,
                 loop_flow:1.2, loop_speed:10, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 // Peak pause (dwell) without a retract lets the tip ooze at full
                 // melt pressure for the whole hold -- by the time the descent
                 // resumes, pressure has bled off and the first several segments
                 // come out starved (looks like "no extrusion until the bottom").
                 // Pairing the pause with a retract/unretract keeps pressure
                 // controlled through the hold instead.
                 loop_mode:'spike', loop_dwell:400, loop_lean:20, loop_coast:0.8,
                 loop_retract:0.3 },
    chainmail: { loop_spacing_mm:4.0, loop_per_turn:0, loop_align:'stagger',
                 loop_row:2.5, loop_up:3.5, loop_out:0.5,
                 loop_flow:1.2, loop_speed:10, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 loop_mode:'dip', loop_dwell:0 },
    fineknit:  { loop_spacing_mm:2.2, loop_per_turn:0, loop_align:'stagger',
                 loop_row:1.8, loop_up:2.4, loop_out:0.3,
                 loop_flow:1.1, loop_speed:10, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 loop_mode:'dip', loop_dwell:0 },
    opennet:   { loop_spacing_mm:10.0, loop_per_turn:0, loop_align:'stagger',
                 loop_row:3.2, loop_up:3.6, loop_out:0.8,
                 loop_flow:1.3, loop_speed:9, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 loop_mode:'dip', loop_dwell:0 },
    ribs:      { loop_spacing_mm:5.0, loop_per_turn:0, loop_align:'column',
                 loop_row:2.2, loop_up:3.0, loop_out:0.5,
                 loop_flow:1.2, loop_speed:10, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 loop_mode:'dip', loop_dwell:0 },
    zigzag:    { loop_spacing_mm:5.0, loop_per_turn:0, loop_align:'stagger',
                 loop_row:3.0, loop_up:3.4, loop_out:0.6,
                 loop_flow:1.25, loop_speed:9, loop_cuff:3,
                 loop_wave_amp:1.2, loop_waves:10,
                 loop_mode:'dip', loop_dwell:0 },
    scallops:  { loop_spacing_mm:8.0, loop_per_turn:0, loop_align:'stagger',
                 loop_row:4.0, loop_up:5.0, loop_out:1.0,
                 loop_flow:1.3, loop_speed:9, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 loop_mode:'dip', loop_dwell:0 }
  };

  function refreshLoopJitterRow(){
    var row = document.getElementById('row-loop-jitter');
    if(row) row.style.display = design.loop_align === 'jitter' ? '' : 'none';
  }

  // Pushes the current design.loop_* fields into their DOM controls (used on
  // style-bundle apply and whenever the whole design is reloaded).
  function updateLoopControlsFromDesign(){
    var styleSel = document.getElementById('d-loop-style');
    if(styleSel) styleSel.value = design.loop_style || 'chainmail';
    function setVal(id, v){ var el = document.getElementById(id); if(el) el.value = v; }
    setVal('d-loop-per-turn', design.loop_per_turn);
    setVal('d-loop-spacing', design.loop_spacing_mm);
    setVal('d-loop-stride', design.loop_turn_stride);
    var alignSel = document.getElementById('d-loop-align');
    if(alignSel) alignSel.value = design.loop_align;
    setVal('d-loop-jitter', design.loop_jitter);
    setVal('d-loop-row', design.loop_row);
    setVal('d-loop-up', design.loop_up);
    setVal('d-loop-out', design.loop_out);
    setVal('d-loop-cuff', design.loop_cuff);
    var modeSel = document.getElementById('d-loop-mode');
    if(modeSel) modeSel.value = design.loop_mode || 'dip';
    setVal('d-loop-dwell', design.loop_dwell);
    setVal('d-loop-lean', design.loop_lean);
    setVal('d-loop-coast', design.loop_coast);
    setVal('d-loop-retract', design.loop_retract);
    refreshLoopDwellRow();
    setVal('d-loop-waveamp', design.loop_wave_amp);
    setVal('d-loop-waves', design.loop_waves);
    setVal('d-loop-rejoin', design.loop_rejoin);
    setVal('d-loop-dwell', design.loop_dwell);
    setVal('d-loop-flow', design.loop_flow);
    setVal('d-loop-speed', design.loop_speed);
    setVal('d-loop-fadein', design.loop_fade_in);
    setVal('d-loop-fadeout', design.loop_fade_out);
    refreshLoopJitterRow();
  }

  // Widest and narrowest row height any shipped style asks for. Read off the
  // table rather than written down, so adding a style re-spreads the others
  // instead of quietly falling outside the mapping below.
  function authoredRowSpan(){
    var lo = Infinity, hi = -Infinity;
    for(var k in LOOP_STYLES){
      if(!LOOP_STYLES.hasOwnProperty(k)) continue;
      var r = LOOP_STYLES[k].loop_row;
      if(!(r > 0)) continue;
      if(r < lo) lo = r;
      if(r > hi) hi = r;
    }
    return (lo <= hi) ? { lo: lo, hi: hi } : null;
  }

  // Fits a style bundle's Z excursions to the selected machine.
  //
  // The bundles are authored as design INTENT in absolute mm, sized for a
  // machine with the headroom to print them (chainmail wants a 2.5mm row and
  // a 3.5mm loop). Ten of the fifteen shipped profiles have far less -- the
  // Trident allows 0.65mm of row, every Creality 0.7mm -- and EVERY style asks
  // for more than that, so any scheme that clips to the ceiling lands all
  // seven on the identical row and the fabric looks the same whichever style
  // is picked.
  //
  // Scaling each bundle by its own factor does not fix it either, which is
  // worth recording because it looks like it should: the row is always the
  // binding constraint, so every style still pins to the row ceiling, and
  // build_loop_fabric then derives loop_h = max(row + 0.3, up) -- which lands
  // on the SAME number for all of them and discards the scaled `up` entirely.
  //
  // So map the styles' authored row SPREAD onto the machine's usable row
  // range instead. The coarsest style gets the ceiling, the finest gets the
  // floor, the rest land in between: relative order and relative coarseness --
  // what actually makes a style recognisable -- survive on any machine, and
  // loop_h genuinely differs because row does. `up` follows the style's own
  // authored up:row ratio so each keeps its hook depth relative to its pitch.
  //
  // Every result stays inside [loop_min_mm, ceiling], so this only ever moves
  // values DOWN from what the panel used to send -- smaller Z excursions are
  // strictly safer, and the ceiling itself is untouched. applyPrinterCaps()
  // and the server clamp both still stand behind it.
  //
  // Returns null when the bundle already fits as authored, or when the printer
  // list has not loaded yet; the bundle is then applied unchanged.
  function fitLoopStyleToMachine(bundle){
    var cap = PRINTER_ZCAP[design.printer];
    if(cap == null || typeof cap !== 'number' || !isFinite(cap)) return null;
    var caps = loopCapsFor(PRINTER_META[design.printer], cap);
    var row = bundle.loop_row, up = bundle.loop_up;
    if(!(row > 0) || !(up > 0)) return null;
    if(row <= caps.row && up <= caps.up) return null;   // fits as authored
    var span = authoredRowSpan();
    var fittedRow;
    if(!span || span.hi <= span.lo || caps.min >= caps.row){
      // One style, or a machine with no usable range at all: nothing to
      // spread across, so take the ceiling.
      fittedRow = caps.row;
    } else {
      var f = (row - span.lo) / (span.hi - span.lo);       // 0 = finest
      fittedRow = caps.min + f * (caps.row - caps.min);
    }
    var fittedUp = fittedRow * (up / row);                 // keep the hook ratio
    function snap(v, hi){
      // Round to the 0.01mm the inputs step in, then re-clamp -- rounding must
      // never be what pushes a value back over the machine's ceiling.
      return Math.max(caps.min, Math.min(hi, Math.round(v * 100) / 100));
    }
    return { loop_row: snap(fittedRow, caps.row), loop_up: snap(fittedUp, caps.up) };
  }

  function applyLoopStyle(styleName){
    design.loop_style = styleName;
    var bundle = LOOP_STYLES[styleName];
    if(bundle){
      for(var k in bundle){ if(bundle.hasOwnProperty(k)) design[k] = bundle[k]; }
      var fitted = fitLoopStyleToMachine(bundle);
      if(fitted){
        design.loop_row = fitted.loop_row;
        design.loop_up = fitted.loop_up;
      }
    }
    updateLoopControlsFromDesign();
    // Writing the bundle straight into `design` also walked past bindNumber's
    // per-field clamp, so the panel could show a number the request would not
    // carry. applyPrinterCaps() is the funnel a printer switch already uses;
    // running it here keeps the panel, the draft and the G-code in agreement,
    // and stands as the backstop behind the scaling above.
    // Cleared first so its note write starts from a clean slate -- the
    // previous style's trim must not stick to this one.
    loopStyleTrimmed = null;
    applyPrinterCaps();
    if(fitted) loopStyleTrimmed = styleName;
    refreshLoopZcapNote();
    persistDesign();
    schedulePreview();
  }

  // Any manual edit to an individual loop control detaches it from the
  // selected style bundle (switches the dropdown to "Custom"). Takes the
  // caller's coalescing key so the style->Custom switch joins that same undo
  // entry rather than adding one of its own (see persistDesign's coalescing
  // note).
  function markLoopCustom(coalesceKey){
    if(design.loop_style !== 'custom'){
      design.loop_style = 'custom';
      var styleSel = document.getElementById('d-loop-style');
      if(styleSel) styleSel.value = 'custom';
      persistDesign(coalesceKey || null);
    }
  }
  function bindLoopNumber(id, field, isInt){
    bindNumber(id, field, isInt);
    var el = document.getElementById(id);
    if(el) el.addEventListener('input', function(){ markLoopCustom('num:' + id); });
  }
  function bindLoopSelect(id, field){
    var key = 'sel:' + id;
    bindSelect(id, field, key);
    var el = document.getElementById(id);
    if(el) el.addEventListener('change', function(){
      markLoopCustom(key);
      endHistRun();
    });
  }

  function refreshLoopDwellRow(){
    var show = design.loop_mode === 'spike' ? '' : 'none';
    var row = document.getElementById('row-loop-dwell');
    if(row) row.style.display = show;
    var lean = document.getElementById('row-loop-lean');
    if(lean) lean.style.display = show;
    var coast = document.getElementById('row-loop-coast');
    if(coast) coast.style.display = show;
    var retr = document.getElementById('row-loop-retract');
    if(retr) retr.style.display = show;
    var retrHint = document.getElementById('retract-hint');
    if(retrHint) retrHint.style.display = show;
  }

  (function(){
    var styleSel = document.getElementById('d-loop-style');
    if(!styleSel) return;
    styleSel.value = design.loop_style || 'chainmail';
    styleSel.addEventListener('change', function(){
      applyLoopStyle(styleSel.value);
    });
  })();

  bindLoopNumber('d-loop-per-turn', 'loop_per_turn', true);
  bindLoopNumber('d-loop-spacing', 'loop_spacing_mm');
  bindLoopNumber('d-loop-stride', 'loop_turn_stride', true);
  bindLoopSelect('d-loop-align', 'loop_align');
  (function(){
    var alignSel = document.getElementById('d-loop-align');
    if(alignSel) alignSel.addEventListener('change', refreshLoopJitterRow);
  })();
  bindLoopNumber('d-loop-jitter', 'loop_jitter');
  bindLoopNumber('d-loop-row', 'loop_row');
  bindLoopNumber('d-loop-up', 'loop_up');
  bindLoopNumber('d-loop-out', 'loop_out');
  bindLoopNumber('d-loop-cuff', 'loop_cuff', true);
  bindLoopSelect('d-loop-mode', 'loop_mode');
  (function(){
    var modeSel = document.getElementById('d-loop-mode');
    if(modeSel) modeSel.addEventListener('change', refreshLoopDwellRow);
  })();
  bindLoopNumber('d-loop-dwell', 'loop_dwell', true);
  bindLoopNumber('d-loop-lean', 'loop_lean', true);
  bindLoopNumber('d-loop-coast', 'loop_coast');
  bindLoopNumber('d-loop-retract', 'loop_retract');
  bindLoopNumber('d-loop-waveamp', 'loop_wave_amp');
  bindLoopNumber('d-loop-waves', 'loop_waves', true);
  bindLoopNumber('d-loop-rejoin', 'loop_rejoin');
  bindLoopNumber('d-loop-dwell', 'loop_dwell', true);
  bindLoopNumber('d-loop-flow', 'loop_flow');
  bindLoopNumber('d-loop-speed', 'loop_speed', true);
  bindLoopNumber('d-loop-fadein', 'loop_fade_in');
  bindLoopNumber('d-loop-fadeout', 'loop_fade_out');
  refreshLoopJitterRow();

  // Overhang flow slider.
  (function(){
    var slider = document.getElementById('d-overhang-k');
    var read = document.getElementById('overhang-k-read');
    if(!slider) return;
    slider.value = design.overhang_flow_k || 0;
    if(read) read.textContent = parseFloat(slider.value).toFixed(2);
    slider.addEventListener('input', function(){
      design.overhang_flow_k = parseFloat(slider.value);
      if(read) read.textContent = design.overhang_flow_k.toFixed(2);
      persistDesign('num:d-overhang-k');
    });
    slider.addEventListener('change', endHistRun);
  })();

  // Overhang-adaptive fan sliders (min ramps up to max at a steep overhang).
  (function(){
    function bindFanSlider(id, readId, field){
      var slider = document.getElementById(id);
      var read = document.getElementById(readId);
      if(!slider) return;
      slider.value = design[field] != null ? design[field] : 100;
      if(read) read.textContent = slider.value + '%';
      slider.addEventListener('input', function(){
        design[field] = parseFloat(slider.value);
        if(read) read.textContent = slider.value + '%';
        persistDesign('num:' + id);
      });
      slider.addEventListener('change', endHistRun);
    }
    bindFanSlider('d-fan-min', 'fan-min-read', 'fan_overhang_min');
    bindFanSlider('d-fan-max', 'fan-max-read', 'fan_overhang_max');
  })();

  // Fan-off-layers numeric input.
  (function(){
    var el = document.getElementById('d-fan-off-layers');
    if(!el) return;
    el.value = design.fan_off_layers || 0;
    el.addEventListener('input', function(){
      var v = parseInt(el.value, 10);
      design.fan_off_layers = Number.isNaN(v) ? 0 : Math.max(0, Math.min(v, 50));
      persistDesign('num:d-fan-off-layers');
    });
    el.addEventListener('change', endHistRun);
  })();

  // ---- Point Edit Modifiers: popup panel + live-preview wiring -------------
  // "Purple modifiers" (Clay Studio Pro convention): operate on the already-
  // sliced wall polyline, not the source geometry. Mirrors
  // trident_gcode/point_edit.py exactly -- see preview_math.js's
  // applyPointEditsPreview() for the client-side mirror math.
  var PE_FFD_ROWS = 3, PE_FFD_COLS = 6;

  function ensurePointFFDGrid(){
    if(Array.isArray(design.point_ffd_grid) && design.point_ffd_grid.length &&
       Array.isArray(design.point_ffd_grid[0])) return;
    var grid = [];
    for(var i = 0; i < PE_FFD_ROWS; i++){
      var row = [];
      for(var j = 0; j < PE_FFD_COLS; j++) row.push(0);
      grid.push(row);
    }
    design.point_ffd_grid = grid;
  }

  // Single source of truth for "is this modifier actually going to do
  // anything" -- enabled AND holds a non-default value. The POST-body
  // assembly (buildRequestBody, below) gates on these exact same functions,
  // so the rail dot can never show "on" for a modifier that silently sends
  // nothing (e.g. Push enabled with amp_mm still at 0, or Smooth enabled
  // with iterations at 0).
  function pointEditMaskMeaningful(){
    return !!(design.point_mask_enable && design.point_mask_channel &&
              design.point_mask_channel !== 'none');
  }
  function pointEditProtectionMeaningful(){
    return !!(design.point_protection_enable &&
      ((design.point_protection_bottom||0) > 0 || (design.point_protection_top||0) > 0));
  }
  function pointEditFFDMeaningful(){
    return !!(design.point_ffd_enable && design.point_ffd_grid &&
      design.point_ffd_grid.some(function(r){ return r.some(function(v){ return Math.abs(v) > 1e-6; }); }));
  }
  function pointEditSmoothMeaningful(){
    return !!(design.point_smooth_enable && Math.round(design.point_smooth_iterations || 0) > 0);
  }
  function pointEditRadialPushMeaningful(){
    return !!(design.point_radial_push_enable && design.point_radial_push_amp);
  }

  function pointEditAnyEnabled(){
    return pointEditMaskMeaningful() || pointEditProtectionMeaningful() ||
           pointEditFFDMeaningful() || pointEditSmoothMeaningful() ||
           pointEditRadialPushMeaningful();
  }

  function updatePointEditActiveDot(){
    var dot = document.getElementById('pe-active-dot');
    if(dot) dot.classList.toggle('active', pointEditAnyEnabled());
    document.querySelectorAll('.pe-rail-item').forEach(function(btn){
      var tab = btn.getAttribute('data-pe-tab');
      var on = (tab === 'mask' && pointEditMaskMeaningful()) ||
               (tab === 'protection' && pointEditProtectionMeaningful()) ||
               (tab === 'ffd' && pointEditFFDMeaningful()) ||
               (tab === 'smooth' && pointEditSmoothMeaningful()) ||
               (tab === 'radial' && pointEditRadialPushMeaningful());
      btn.classList.toggle('enabled', !!on);
    });
  }

  // Point edit modifiers only reach the parametric wall spiral -- loop fabric
  // and STL import ignore them server-side (mirrors updateCageNote()'s
  // precedent for the same limitation on the asymmetric shape cage).
  function updatePointEditScopeNote(){
    var note = document.getElementById('pe-scope-note');
    var btn = document.getElementById('point-edit-btn');
    var outOfScope = design.pattern === 'loops' || !!(typeof meshState !== 'undefined' && meshState && meshState.mesh_id);
    if(note) note.style.display = outOfScope ? '' : 'none';
    if(btn) btn.classList.toggle('pe-out-of-scope', outOfScope);
  }

  function buildFFDGridUI(){
    ensurePointFFDGrid();
    var container = document.getElementById('pe-ffd-grid');
    if(!container) return;
    container.innerHTML = '';
    var rows = design.point_ffd_grid.length, cols = design.point_ffd_grid[0].length;
    container.style.gridTemplateColumns = 'repeat(' + cols + ', 1fr)';
    // Build top-to-bottom (row rows-1 = top of print) for an intuitive layout.
    for(var i = rows - 1; i >= 0; i--){
      for(var j = 0; j < cols; j++){
        (function(ri, ci){
          var inp = document.createElement('input');
          inp.type = 'number'; inp.step = '0.25'; inp.min = '-4'; inp.max = '4';
          inp.value = design.point_ffd_grid[ri][ci];
          var tFrac = rows > 1 ? ri/(rows-1) : 0;
          inp.title = 'height t=' + tFrac.toFixed(2) + ', azimuth cell ' + ci + ' (mm radial push)';
          inp.addEventListener('input', function(){
            var v = parseFloat(inp.value);
            design.point_ffd_grid[ri][ci] = Number.isNaN(v) ? 0 : Math.max(-4, Math.min(4, v));
            // Per-cell key: editing one FFD cell is one undo entry, and moving
            // to a different cell starts a new one.
            persistDesign('ffd:' + ri + ',' + ci);
            schedulePreview();
            updatePointEditActiveDot();
          });
          inp.addEventListener('change', endHistRun);
          container.appendChild(inp);
        })(i, j);
      }
    }
  }

  // Re-syncs every Point Edit control from `design` -- called on load/undo/
  // redo/preset-apply, mirroring applyDesignToUI()'s job for the rest of the panel.
  function applyPointEditUIFromDesign(){
    var set = function(id, v){ var el = document.getElementById(id); if(el && v != null) el.value = v; };
    var chk = function(id, v){ var el = document.getElementById(id); if(el) el.checked = !!v; };
    chk('pe-mask-enable', design.point_mask_enable);
    set('pe-mask-channel', design.point_mask_channel || 'checker');
    set('pe-mask-scaleu', design.point_mask_scale_u);
    set('pe-mask-scalev', design.point_mask_scale_v);
    chk('pe-mask-invert', design.point_mask_invert);
    chk('pe-prot-enable', design.point_protection_enable);
    set('pe-prot-bottom', design.point_protection_bottom);
    set('pe-prot-top', design.point_protection_top);
    set('pe-prot-falloff', design.point_protection_falloff);
    chk('pe-ffd-enable', design.point_ffd_enable);
    set('pe-ffd-strength', design.point_ffd_strength);
    buildFFDGridUI();
    chk('pe-smooth-enable', design.point_smooth_enable);
    set('pe-smooth-iter', design.point_smooth_iterations);
    set('pe-smooth-theta', design.point_smooth_theta);
    set('pe-smooth-t', design.point_smooth_t);
    set('pe-smooth-strength', design.point_smooth_strength);
    chk('pe-push-enable', design.point_radial_push_enable);
    set('pe-push-amp', design.point_radial_push_amp);
    set('pe-push-strength', design.point_radial_push_strength);
    updatePointEditActiveDot();
    updatePointEditScopeNote();
  }

  (function(){
    var modal = document.getElementById('point-edit-modal');
    var openBtn = document.getElementById('point-edit-btn');
    var closeBtn = document.getElementById('pe-modal-close');
    var doneBtn = document.getElementById('pe-modal-done');
    var backdrop = modal ? modal.querySelector('.pe-modal-backdrop') : null;
    if(!modal || !openBtn) return;

    function openModal(){
      previewArmed = true;
      modal.style.display = 'flex';
    }
    function closeModal(){ modal.style.display = 'none'; }
    // Exposed so the active-summary "N point-edit mods" chip (see
    // updateActiveSummary()) can open the modal directly rather than just
    // switching to the Texture step.
    window.__openPointEditModal = openModal;
    openBtn.addEventListener('click', openModal);
    if(closeBtn) closeBtn.addEventListener('click', closeModal);
    if(doneBtn) doneBtn.addEventListener('click', closeModal);
    if(backdrop) backdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape' && modal.style.display !== 'none') closeModal();
    });

    // Rail tab switching.
    var railBtns = Array.prototype.slice.call(document.querySelectorAll('.pe-rail-item'));
    var panels = {};
    railBtns.forEach(function(btn){
      var name = btn.getAttribute('data-pe-tab');
      panels[name] = document.querySelector('.pe-panel[data-pe-panel="' + name + '"]');
    });
    railBtns.forEach(function(btn){
      btn.addEventListener('click', function(){
        var name = btn.getAttribute('data-pe-tab');
        railBtns.forEach(function(b){ b.classList.toggle('active', b === btn); });
        for(var k in panels){ if(panels[k]) panels[k].classList.toggle('active', k === name); }
      });
    });

    function bindPEBool(id, field){
      var el = document.getElementById(id);
      if(!el) return;
      el.checked = !!design[field];
      el.addEventListener('change', function(){
        design[field] = el.checked;
        previewArmed = true;
        persistDesign();
        schedulePreview();
        updatePointEditActiveDot();
      });
    }
    function bindPENumber(id, field){
      var el = document.getElementById(id);
      if(!el) return;
      if(design[field] != null) el.value = design[field];
      el.addEventListener('input', function(){
        var v = parseFloat(el.value);
        if(Number.isNaN(v)) return;
        design[field] = v;
        previewArmed = true;
        persistDesign('num:' + id);
        schedulePreview();
        updatePointEditActiveDot();
      });
      el.addEventListener('change', endHistRun);
    }
    function bindPESelect(id, field){
      var el = document.getElementById(id);
      if(!el) return;
      if(design[field]) el.value = design[field];
      el.addEventListener('change', function(){
        design[field] = el.value;
        previewArmed = true;
        persistDesign();
        schedulePreview();
      });
    }

    bindPEBool('pe-mask-enable', 'point_mask_enable');
    bindPESelect('pe-mask-channel', 'point_mask_channel');
    bindPENumber('pe-mask-scaleu', 'point_mask_scale_u');
    bindPENumber('pe-mask-scalev', 'point_mask_scale_v');
    bindPEBool('pe-mask-invert', 'point_mask_invert');

    bindPEBool('pe-prot-enable', 'point_protection_enable');
    bindPENumber('pe-prot-bottom', 'point_protection_bottom');
    bindPENumber('pe-prot-top', 'point_protection_top');
    bindPENumber('pe-prot-falloff', 'point_protection_falloff');

    bindPEBool('pe-ffd-enable', 'point_ffd_enable');
    bindPENumber('pe-ffd-strength', 'point_ffd_strength');

    bindPEBool('pe-smooth-enable', 'point_smooth_enable');
    bindPENumber('pe-smooth-iter', 'point_smooth_iterations');
    bindPENumber('pe-smooth-theta', 'point_smooth_theta');
    bindPENumber('pe-smooth-t', 'point_smooth_t');
    bindPENumber('pe-smooth-strength', 'point_smooth_strength');

    bindPEBool('pe-push-enable', 'point_radial_push_enable');
    bindPENumber('pe-push-amp', 'point_radial_push_amp');
    bindPENumber('pe-push-strength', 'point_radial_push_strength');

    buildFFDGridUI();

    var ffdResetBtn = document.getElementById('pe-ffd-reset');
    if(ffdResetBtn) ffdResetBtn.addEventListener('click', function(){
      ensurePointFFDGrid();
      for(var i = 0; i < design.point_ffd_grid.length; i++){
        for(var j = 0; j < design.point_ffd_grid[i].length; j++) design.point_ffd_grid[i][j] = 0;
      }
      buildFFDGridUI();
      persistDesign();
      schedulePreview();
    });

    var resetAllBtn = document.getElementById('pe-reset-all');
    if(resetAllBtn) resetAllBtn.addEventListener('click', function(){
      design.point_mask_enable = false;
      design.point_mask_channel = 'checker';
      design.point_mask_scale_u = 8;
      design.point_mask_scale_v = 6;
      design.point_mask_invert = false;
      design.point_protection_enable = false;
      design.point_protection_bottom = 0;
      design.point_protection_top = 0;
      design.point_protection_falloff = 0.08;
      design.point_ffd_enable = false;
      design.point_ffd_grid = null;
      design.point_ffd_strength = 1.0;
      design.point_smooth_enable = false;
      design.point_smooth_iterations = 2;
      design.point_smooth_theta = 0.5;
      design.point_smooth_t = 0.5;
      design.point_smooth_strength = 1.0;
      design.point_radial_push_enable = false;
      design.point_radial_push_amp = 1.0;
      design.point_radial_push_strength = 1.0;
      applyPointEditUIFromDesign();
      persistDesign();
      schedulePreview();
    });

    updatePointEditActiveDot();
    updatePointEditScopeNote();
  })();

  // Pattern dropdown also affects the Point Edit scope note (loop fabric
  // is out of scope for point edits, same as the asymmetric shape cage).
  document.getElementById('d-pattern').addEventListener('change', updatePointEditScopeNote);

  // ---- Zone Overrides: height-band regions generated with a different
  // texture pattern/depth and/or xy-twist than the rest of the print. A
  // DIFFERENT subsystem from Point Edit Modifiers above: zones change how
  // the wall is GENERATED (see trident_gcode/paths.py's ZoneOverride and
  // spiral_path()'s zone branch), Point Edit deforms the already-sliced
  // path. Mirrors preview_math.js's zoneWeight()/zoneTwistIntegral() for
  // the live client-side draft preview.
  var ZONE_MAX_CLIENT = 8;   // mirrors serve.py's ZONE_MAX; server is the real ceiling
  var ZONE_PATTERNS = ['vwave', 'hwave', 'ripple', 'diamond', 'bubbles', 'pleats', 'hammered'];
  // --zone-c1..c5, mirrored from style.css's :root block (keep the two in
  // step -- see that block's comment for why this palette tops out at 5 and
  // for the reasoning behind each colour choice). Read via getComputedStyle
  // so a future palette edit in CSS needs no matching edit here.
  var ZONE_PALETTE_SIZE = 5;
  function zoneColorVar(colorIdx){
    return '--zone-c' + ((((colorIdx | 0) % ZONE_PALETTE_SIZE) + ZONE_PALETTE_SIZE) % ZONE_PALETTE_SIZE + 1);
  }
  function zoneColorHex(z){
    var name = zoneColorVar(z && z.color_idx != null ? z.color_idx : 0);
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#ff6fd8';
  }
  // Stable per-zone colour: the lowest palette slot no EXISTING zone is
  // using, so deleting a zone never recolours the survivors (an array-index-
  // derived colour would do exactly that on every remove). Falls back to
  // cycling by count once every slot is taken (ZONE_MAX_CLIENT 8 > 5).
  function nextZoneColorIdx(zones){
    for(var c = 0; c < ZONE_PALETTE_SIZE; c++){
      if(!zones.some(function(z){ return z && z.color_idx === c; })) return c;
    }
    return zones.length % ZONE_PALETTE_SIZE;
  }
  // Designs saved before v2 have no color_idx on their zones. Backfill in
  // place -- deliberately WITHOUT persistDesign(), which would push a
  // spurious undo entry just for loading a file; the next real edit persists
  // it naturally.
  function normalizeZoneColors(){
    var zones = design.zone_overrides;
    if(!zones || !zones.length) return;
    zones.forEach(function(z){
      if(z && z.color_idx == null) z.color_idx = nextZoneColorIdx(zones);
    });
  }

  function minZoneSpan(){
    return Math.max(2.0 * (design.layer_height || 0.3) / Math.max(design.height || 60, 1e-6), 1e-4);
  }
  function zoneMmFromT(t){ return t * (design.height || 60); }
  function zoneTFromMm(mm){ return (design.height > 0) ? mm / design.height : 0; }

  // Single source of truth for "is this zone actually going to do anything"
  // -- enabled AND overrides at least one of pattern/depth/twist. The
  // POST-body assembly (buildGenerateBody) gates on this exact same
  // function, so the active dot can never show "on" for a zone that would
  // silently send nothing.
  function zoneOverrideMeaningful(z){
    return !!(z && z.enabled && (z.pattern || z.pattern_amp != null ||
              z.pattern_twist != null || z.xy_twist != null));
  }
  function zoneOverridesAnyEnabled(){
    return (design.zone_overrides || []).some(zoneOverrideMeaningful);
  }
  function updateZoneActiveDot(){
    var dot = document.getElementById('zo-active-dot');
    if(dot) dot.classList.toggle('active', zoneOverridesAnyEnabled());
  }
  // Zone overrides only reach the parametric wall spiral -- loop fabric and
  // STL import ignore them server-side, same limitation (and same UI
  // treatment) as updatePointEditScopeNote() above.
  function updateZoneScopeNote(){
    var note = document.getElementById('zo-scope-note');
    var btn = document.getElementById('zone-btn');
    var outOfScope = design.pattern === 'loops' || !!(typeof meshState !== 'undefined' && meshState && meshState.mesh_id);
    if(note) note.style.display = outOfScope ? '' : 'none';
    if(btn) btn.classList.toggle('zo-out-of-scope', outOfScope);
    if(typeof refreshZoneRings === 'function') refreshZoneRings();
  }
  document.getElementById('d-pattern').addEventListener('change', updateZoneScopeNote);

  function positionZoneAxisEls(idx, band, hLo, hHi, axisH){
    var z = (design.zone_overrides || [])[idx];
    if(!z) return;
    var yTop = axisH * (1 - z.t_hi);
    var yBot = axisH * (1 - z.t_lo);
    band.style.top = yTop + 'px';
    band.style.height = Math.max(0, yBot - yTop) + 'px';
    hHi.style.top = (yTop - 2) + 'px';
    hLo.style.top = (yBot - 2) + 'px';
    // Height-axis mm labels ride the exact same yTop/yBot as the handles
    // they describe, so a label can never disagree with the edge it's next
    // to -- looked up by (zone idx, edge) rather than threaded through as
    // extra params, so every existing call site (initial build AND every
    // live-drag reposition in setZoneEdge) updates them for free.
    var host = band.parentNode;
    if(host){
      var lHi = host.querySelector('.zo-axis-label[data-zone-idx="' + idx + '"][data-edge="hi"]');
      var lLo = host.querySelector('.zo-axis-label[data-zone-idx="' + idx + '"][data-edge="lo"]');
      if(lHi){ lHi.style.top = yTop + 'px'; lHi.textContent = zoneMmFromT(z.t_hi).toFixed(2) + ' mm'; }
      if(lLo){ lLo.style.top = yBot + 'px'; lLo.textContent = zoneMmFromT(z.t_lo).toFixed(2) + ' mm'; }
    }
  }
  var ZONE_AXIS_H = 260;   // matches --zo-axis height in style.css
  var ZO_AXIS_LABEL_GAP_PX = 17;   // ~ one .zo-axis-label's own rendered
                                    // height (11px font * 1.45 line-height +
                                    // padding/border) -- two labels closer
                                    // than this in axis-Y read as overlapping.
  var ZO_AXIS_SCALE_INSET_PX = 9;  // nudges the two track scale labels
                                    // (0 mm / full height) in from the very
                                    // top/bottom of the 260px track so they
                                    // stay clear of the track's own border
                                    // even when a zone edge sits right at 0%/100%.

  function renderZoneAxis(){
    var axisHost = document.getElementById('zo-axis');
    if(!axisHost) return;
    axisHost.innerHTML = '';
    (design.zone_overrides || []).forEach(function(z, i){
      if(!z.enabled) return;   // a disabled zone doesn't crowd the axis
      var color = zoneColorHex(z);
      var band = document.createElement('div');
      band.className = 'zo-axis-band';
      band.setAttribute('data-zone-idx', i);
      band.style.borderTopColor = color;
      band.style.borderBottomColor = color;
      band.style.background = 'color-mix(in srgb, ' + color + ' 32%, transparent)';   // raised
        // from 14% -- same contrast pass as ZONE_TINT_BOOST in viewer.js, so the
        // modal's own axis reads as clearly-coloured as the model does now.
      axisHost.appendChild(band);
      var hHi = document.createElement('div');
      hHi.className = 'zo-axis-handle';
      hHi.setAttribute('data-zone-idx', i);
      hHi.setAttribute('data-edge', 'hi');
      hHi.style.background = color;
      axisHost.appendChild(hHi);
      var hLo = document.createElement('div');
      hLo.className = 'zo-axis-handle';
      hLo.setAttribute('data-zone-idx', i);
      hLo.setAttribute('data-edge', 'lo');
      hLo.style.background = color;
      axisHost.appendChild(hLo);
      var lHi = document.createElement('div');
      lHi.className = 'zo-axis-label';
      lHi.setAttribute('data-zone-idx', i);
      lHi.setAttribute('data-edge', 'hi');
      lHi.style.color = color;
      axisHost.appendChild(lHi);
      var lLo = document.createElement('div');
      lLo.className = 'zo-axis-label';
      lLo.setAttribute('data-zone-idx', i);
      lLo.setAttribute('data-edge', 'lo');
      lLo.style.color = color;
      axisHost.appendChild(lLo);
      positionZoneAxisEls(i, band, hLo, hHi, ZONE_AXIS_H);
    });
    // Track's own base/top marks -- not a zone, so no data-edge/zone-idx to
    // match a handle; always present, same "show the wall's own extent"
    // idea as the 3-D view's .zone-tag-scale chips (viewer.js
    // zoneLabelsRebuild).
    var sTop = document.createElement('div');
    sTop.className = 'zo-axis-label zo-axis-label-scale';
    sTop.setAttribute('data-zone-idx', -1);
    sTop.style.top = ZO_AXIS_SCALE_INSET_PX + 'px';
    sTop.textContent = zoneMmFromT(1).toFixed(2) + ' mm';
    axisHost.appendChild(sTop);
    var sBot = document.createElement('div');
    sBot.className = 'zo-axis-label zo-axis-label-scale';
    sBot.setAttribute('data-zone-idx', -1);
    sBot.style.top = (ZONE_AXIS_H - ZO_AXIS_SCALE_INSET_PX) + 'px';
    sBot.textContent = zoneMmFromT(0).toFixed(2) + ' mm';
    axisHost.appendChild(sBot);
    cullZoneAxisLabels(null);
  }

  // Hides whichever .zo-axis-label chips would visually collide (closer than
  // ZO_AXIS_LABEL_GAP_PX in axis-Y), rather than letting the track fill with
  // overlapping mm text as zones get placed close together. focusIdx (the
  // zone currently hovered/dragged, or null) always wins its own collisions;
  // after that, zone edge labels beat the track's own scale marks, which are
  // the least essential and the first hidden.
  function cullZoneAxisLabels(focusIdx){
    var axisHost = document.getElementById('zo-axis');
    if(!axisHost) return;
    var els = Array.prototype.slice.call(axisHost.querySelectorAll('.zo-axis-label'));
    els.forEach(function(el){ el.style.display = ''; });
    var items = els.map(function(el){
      var zi = +el.getAttribute('data-zone-idx');
      var prio = (focusIdx != null && zi === focusIdx) ? 0 : (zi < 0 ? 2 : 1);
      return { el: el, top: parseFloat(el.style.top) || 0, prio: prio };
    });
    items.sort(function(a, b){ return a.prio - b.prio || a.top - b.top; });
    var placed = [];
    items.forEach(function(it){
      var collide = placed.some(function(p){ return Math.abs(p.top - it.top) < ZO_AXIS_LABEL_GAP_PX; });
      if(collide) it.el.style.display = 'none';
      else placed.push(it);
    });
  }

  function syncZoneRowMmInputs(idx){
    var row = document.querySelector('.zo-row[data-zone-row-idx="' + idx + '"]');
    var z = (design.zone_overrides || [])[idx];
    if(!row || !z) return;
    var fromInp = row.querySelector('[data-help-id="zo-from"] input');
    var toInp = row.querySelector('[data-help-id="zo-to"] input');
    if(fromInp && document.activeElement !== fromInp) fromInp.value = zoneMmFromT(z.t_lo).toFixed(2);
    if(toInp && document.activeElement !== toInp) toInp.value = zoneMmFromT(z.t_hi).toFixed(2);
  }

  // Rebuilds #zo-rows + #zo-axis from `design.zone_overrides` -- called on
  // load/undo/redo/preset-apply (mirrors applyPointEditUIFromDesign()'s job)
  // and after any add/remove. Full rebuild each time, same convention as
  // buildFFDGridUI() above.
  function renderZoneRows(){
    var rowsHost = document.getElementById('zo-rows');
    var emptyHint = document.getElementById('zo-empty-hint');
    if(!rowsHost) return;
    normalizeZoneColors();
    var zones = design.zone_overrides || [];
    rowsHost.innerHTML = '';
    if(emptyHint) emptyHint.style.display = zones.length ? 'none' : '';

    zones.forEach(function(z, i){
      var row = document.createElement('div');
      row.className = 'zo-row' + (z.enabled ? '' : ' disabled')
        + (window.__zoneJustAdded === i ? ' is-new' : '');
      row.setAttribute('data-zone-row-idx', i);
      // Clears itself on the row's own next interaction, not on a timer --
      // the marker's job is "which one did I just add", not "for N seconds".
      if(window.__zoneJustAdded === i){
        row.style.borderLeftColor = zoneColorHex(z);
        row.addEventListener('pointerdown', function clearNew(){
          row.classList.remove('is-new');
          row.removeEventListener('pointerdown', clearNew);
          if(window.__zoneJustAdded === i) window.__zoneJustAdded = null;
        }, { once: true });
      }

      var swatch = document.createElement('span');
      swatch.className = 'zo-row-swatch';
      swatch.style.background = zoneColorHex(z);
      swatch.title = 'zone ' + (i + 1) + ' colour (matches its band on the model)';
      row.appendChild(swatch);

      var enableField = document.createElement('label');
      enableField.className = 'zo-row-field';
      var enableLab = document.createElement('span');
      enableLab.textContent = 'On';
      enableField.appendChild(enableLab);
      var enableChk = document.createElement('input');
      enableChk.type = 'checkbox';
      enableChk.checked = !!z.enabled;
      enableChk.addEventListener('change', function(){
        z.enabled = enableChk.checked;
        row.classList.toggle('disabled', !z.enabled);
        previewArmed = true;
        persistDesign();
        schedulePreview();
        updateZoneActiveDot();
        renderZoneAxis();
        refreshZoneRings();
      });
      enableField.appendChild(enableChk);
      row.appendChild(enableField);

      function numField(labelText, helpId, unit, value, step, min, max, placeholder, onChange){
        var f = document.createElement('div');
        f.className = 'drow zo-row-field';
        f.setAttribute('data-help-id', helpId);
        var lab = document.createElement('span');
        lab.textContent = labelText;
        f.appendChild(lab);
        var inp = document.createElement('input');
        inp.type = 'number';
        if(step != null) inp.step = step;
        if(min != null) inp.min = min;
        if(max != null) inp.max = max;
        if(unit) inp.setAttribute('data-unit', unit);
        if(placeholder) inp.placeholder = placeholder;
        inp.value = (value == null || value === '') ? '' : value;
        inp.addEventListener('input', function(){ onChange(inp); });
        inp.addEventListener('change', endHistRun);
        f.appendChild(inp);
        return f;
      }

      row.appendChild(numField('From (mm)', 'zo-from', 'mm',
        +zoneMmFromT(z.t_lo).toFixed(2), 0.5, 0, design.height, null, function(inp){
          var mm = parseFloat(inp.value);
          if(Number.isNaN(mm)) return;
          z.t_lo = Math.max(0, Math.min(zoneTFromMm(mm), z.t_hi - minZoneSpan()));
          previewArmed = true;
          persistDesign('zone:' + i + ':t_lo');
          schedulePreview();
          renderZoneAxis();
          refreshZoneRings();
        }));
      row.appendChild(numField('To (mm)', 'zo-to', 'mm',
        +zoneMmFromT(z.t_hi).toFixed(2), 0.5, 0, design.height, null, function(inp){
          var mm = parseFloat(inp.value);
          if(Number.isNaN(mm)) return;
          z.t_hi = Math.min(1, Math.max(zoneTFromMm(mm), z.t_lo + minZoneSpan()));
          previewArmed = true;
          persistDesign('zone:' + i + ':t_hi');
          schedulePreview();
          renderZoneAxis();
          refreshZoneRings();
        }));
      row.appendChild(numField('Blend (mm)', 'zo-blend', 'mm',
        +zoneMmFromT(z.blend != null ? z.blend : 0.02).toFixed(2), 0.5, 0, design.height / 2, null, function(inp){
          var mm = parseFloat(inp.value);
          if(Number.isNaN(mm) || mm < 0) return;
          z.blend = zoneTFromMm(mm);
          previewArmed = true;
          persistDesign('zone:' + i + ':blend');
          schedulePreview();
          renderZoneAxis();
          refreshZoneRings();
        }));

      var pf = document.createElement('div');
      pf.className = 'drow zo-row-field';
      pf.setAttribute('data-help-id', 'zo-pattern');
      var pLab = document.createElement('span');
      pLab.textContent = 'Texture';
      pf.appendChild(pLab);
      var pSel = document.createElement('select');
      var optInherit = document.createElement('option');
      optInherit.value = ''; optInherit.textContent = '(use global)';
      pSel.appendChild(optInherit);
      ZONE_PATTERNS.forEach(function(p){
        var o = document.createElement('option');
        o.value = p; o.textContent = p;
        pSel.appendChild(o);
      });
      pSel.value = z.pattern || '';
      pf.appendChild(pSel);
      row.appendChild(pf);

      var depthField = numField('Depth (mm)', 'zo-depth', 'mm',
        z.pattern_amp != null ? z.pattern_amp : '', 0.1, 0, 4, 'inherit', function(inp){
          var v = inp.value === '' ? null : parseFloat(inp.value);
          z.pattern_amp = (v == null || Number.isNaN(v)) ? null : Math.max(0, Math.min(v, 4));
          previewArmed = true;
          persistDesign('zone:' + i + ':pattern_amp');
          schedulePreview();
          updateZoneActiveDot();
        });
      var depthInput = depthField.querySelector('input');
      depthInput.disabled = !z.pattern;
      row.appendChild(depthField);
      pSel.addEventListener('change', function(){
        z.pattern = pSel.value || '';
        depthInput.disabled = !z.pattern;
        previewArmed = true;
        persistDesign();
        schedulePreview();
        updateZoneActiveDot();
      });

      row.appendChild(numField('Pattern twist (turns)', 'zo-ptwist', 'turns',
        z.pattern_twist != null ? z.pattern_twist : '', 0.25, -6, 6, 'inherit', function(inp){
          var v = inp.value === '' ? null : parseFloat(inp.value);
          z.pattern_twist = (v == null || Number.isNaN(v)) ? null : v;
          previewArmed = true;
          persistDesign('zone:' + i + ':pattern_twist');
          schedulePreview();
          updateZoneActiveDot();
        }));

      row.appendChild(numField('XY twist (turns)', 'zo-twist', 'turns',
        z.xy_twist != null ? z.xy_twist : '', 0.25, -6, 6, 'inherit', function(inp){
          var v = inp.value === '' ? null : parseFloat(inp.value);
          z.xy_twist = (v == null || Number.isNaN(v)) ? null : v;
          previewArmed = true;
          persistDesign('zone:' + i + ':xy_twist');
          schedulePreview();
          updateZoneActiveDot();
          updateZoneTwistCautions();
        }));
      var twistCaution = document.createElement('div');
      twistCaution.className = 'hint zo-caution zo-twist-caution';
      twistCaution.style.display = 'none';
      row.appendChild(twistCaution);

      var removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'zo-row-remove';
      removeBtn.textContent = 'Remove';
      removeBtn.addEventListener('click', function(){ removeZone(i); });
      row.appendChild(removeBtn);

      rowsHost.appendChild(row);
    });

    renderZoneAxis();
    refreshZoneRings();
    updateZoneTwistCautions();
  }

  // A zone's xy_twist rotates the WALL CROSS-SECTION's own angular sampling
  // (mirrors paths.py's spiral_path(): shape_angle -= extra_twist * 2*pi) --
  // on a perfectly round `circle` shape that has no effect at all to see,
  // since every angle already has the same radius (r_amp'd radial texture
  // is a SEPARATE control, pattern_twist, unaffected by this). A user typing
  // a value into "XY twist (turns)" and watching nothing move is exactly
  // that: not a bug, just this control genuinely having nothing to rotate on
  // a circle. Told here rather than silently letting it look broken, the
  // same reasoning as #zo-scope-note for loop fabric/STL mode above.
  function zoneTwistNoOpNote(z){
    if(design.shape !== 'circle') return null;
    if(z.xy_twist == null || z.xy_twist === 0) return null;
    return 'No visible effect on a circle shape -- there is no angular ' +
           'asymmetry for a twist to rotate. Try star or square instead.';
  }
  function updateZoneTwistCautions(){
    var zones = design.zone_overrides || [];
    document.querySelectorAll('.zo-row').forEach(function(row){
      var idx = parseInt(row.getAttribute('data-zone-row-idx'), 10);
      var z = zones[idx];
      var el = row.querySelector('.zo-twist-caution');
      if(!el || !z) return;
      var note = zoneTwistNoOpNote(z);
      el.textContent = note || '';
      el.style.display = note ? '' : 'none';
    });
  }
  // Exposed so applyDesignToUI() (load/undo/redo/preset-apply) can re-sync
  // this panel the same way applyPointEditUIFromDesign() re-syncs its own.
  window.__applyZoneUIFromDesign = renderZoneRows;

  // Single removal path -- both the modal row's Remove button and the
  // in-model right-click "Remove zone" entry (viewer.js) call this.
  function removeZone(i){
    design.zone_overrides.splice(i, 1);
    previewArmed = true;
    persistDesign();
    schedulePreview();
    renderZoneRows();
    updateZoneActiveDot();
  }
  window.__zoneRemove = removeZone;

  // Converts a 3-D pick's world Y (== printer Z, see preview_math.js's
  // generatePreview comment on the coordinate convention) into a height
  // fraction `t`, for viewer.js's right-click handler -- kept here rather
  // than in viewer.js because only this closure holds `design` and
  // effectiveBaseSpec(), which previewWallOffset() needs.
  window.__zoneTFromWorldY = function(worldY){
    var off = window.previewWallOffset ? window.previewWallOffset(design, effectiveBaseSpec()) : { wallOff: 0 };
    var t = (worldY - off.wallOff) / Math.max(design.height || 60, 1e-6);
    return Math.max(0, Math.min(1, t));
  };
  // The on-model mm labels' single source of the number they print -- kept
  // here for the same reason __zoneTFromWorldY is (only this closure holds
  // `design`). Deliberately the exact inverse of zoneMmFromT below, but
  // exposed globally since viewer.js has no other way to reach it.
  window.__zoneMmFromT = function(t){ return zoneMmFromT(t); };
  // The exact inverse, exposed for the same reason: the on-model chip's
  // double-click-to-edit (viewer.js's zoneLabelBeginEdit) needs to turn a
  // typed mm value back into a t before it can call window.__zoneRingDrag,
  // and must use the SAME conversion the row's own From/To (mm) inputs use
  // -- not a re-derivation from window.__previewWallMeta's height, which
  // can briefly disagree with design.height mid-debounce.
  window.__zoneTFromMm = function(mm){ return zoneTFromMm(mm); };
  window.__zoneCanAdd = function(){
    return (design.zone_overrides || []).length < ZONE_MAX_CLIENT;
  };
  // Right-click "Add zone here" (viewer.js): adds a zone and shows its rings
  // immediately WITHOUT opening the modal -- see openModal's own comment for
  // why forcing the modal open on every add was the friction being fixed.
  window.__zoneAddAt = function(seedT){
    if(!window.__zoneCanAdd()) return -1;
    addZone(seedT);
    renderZoneRows();
    return design.zone_overrides.length - 1;
  };

  function addZone(seedT){
    var zones = design.zone_overrides || (design.zone_overrides = []);
    if(zones.length >= ZONE_MAX_CLIENT) return;
    var t = seedT != null ? seedT : 0.5;
    var half = 0.06;
    // Bands may overlap (v2) -- no clamp against existing zones here, unlike
    // v1. A fresh zone simply centres on the seed point (or the middle of
    // the wall with no seed) and can be dragged wherever the user wants.
    zones.push({
      enabled: true,
      t_lo: Math.max(0, t - half), t_hi: Math.min(1, t + half),
      blend: 0.02, pattern: '', pattern_amp: null, pattern_twist: null, xy_twist: null,
      color_idx: nextZoneColorIdx(zones)
    });
    window.__zoneJustAdded = zones.length - 1;
    previewArmed = true;
    persistDesign();
    schedulePreview();
  }

  // Single source of truth for "where may this zone's edge legally go" --
  // used by BOTH the modal's #zo-axis drag (below) and the in-model rings
  // (viewer.js, via window.__zoneRingDrag), so the two interactions can
  // never disagree about a bound. Overlap with OTHER zones is allowed (v2)
  // -- only this zone's own minimum span and [0,1] constrain an edge.
  // slideBand=true moves both edges together (the whole band), preserving
  // its span -- the modal's own hint already promises "drag its middle to
  // slide it"; Shift+drag on either UI does the same thing.
  function setZoneEdge(idx, edge, t, slideBand){
    var zones = design.zone_overrides || [];
    var z = zones[idx];
    if(!z) return;
    var minSpan = minZoneSpan();
    if(slideBand){
      var span = Math.max(z.t_hi - z.t_lo, minSpan);
      var half = span / 2;
      var mid = Math.max(half, Math.min(t, 1 - half));
      z.t_lo = mid - half;
      z.t_hi = mid + half;
    } else if(edge === 'lo'){
      z.t_lo = Math.max(0, Math.min(t, z.t_hi - minSpan));
    } else {
      z.t_hi = Math.min(1, Math.max(t, z.t_lo + minSpan));
    }
    z.blend = Math.min(z.blend != null ? z.blend : 0.02, (z.t_hi - z.t_lo) / 2);
    previewArmed = true;
    // A drag streams this function at pointer-event rate (60-120Hz). The
    // EDIT (above) must stay live every move -- that's what makes the ring/
    // handle/label track the pointer -- but persistDesign()'s JSON.stringify
    // of the whole design plus its synchronous localStorage.setItem plus
    // updateActiveSummary()'s chip-rebuild are far too heavy for that frame,
    // and were the actual cause of the drag "glitching" (a synchronous main-
    // thread write on nearly every move). So mid-drag we only bump designRev
    // (keeps updateStaleBadge() honest live -- see its own call below) and
    // record which coalesce key is owed a real persist; the real persist
    // happens exactly once, at drag release, via flushZoneEdgePersist(). One
    // persist per drag is also exactly what the 'zoneaxis:<idx>' coalescing
    // key already collapsed a whole drag's worth of persists down to, so the
    // undo stack still gets exactly one entry holding the pre-drag state.
    if(zoneEdgeDragLive()){
      zoneEdgePersistPending = 'zoneaxis:' + idx;
      designRev++;
      updateStaleBadge();
      // Skip schedulePreview() mid-drag: generatePreview() is heavy
      // synchronous math (full spiral-path recompute) and running it at
      // 60-120 Hz is what causes the ring-drag lag. The preview will fire
      // once the drag ends (via flushZoneEdgePersist -> schedulePreview).
    } else {
      persistDesign('zoneaxis:' + idx);
      schedulePreview();
    }
    var band = document.querySelector('#zo-axis .zo-axis-band[data-zone-idx="' + idx + '"]');
    var hLo = document.querySelector('#zo-axis .zo-axis-handle[data-zone-idx="' + idx + '"][data-edge="lo"]');
    var hHi = document.querySelector('#zo-axis .zo-axis-handle[data-zone-idx="' + idx + '"][data-edge="hi"]');
    if(band && hLo && hHi) positionZoneAxisEls(idx, band, hLo, hHi, ZONE_AXIS_H);
    cullZoneAxisLabels(idx);
    syncZoneRowMmInputs(idx);
    // The modal axis drag has no viewer.js counterpart doing its own live
    // ring update (unlike the in-model drag, see the comment on the return
    // value below), so route it through the cheap in-place ring mover
    // instead of refreshZoneRings()'s full teardown/rebuild -- that rebuild
    // is fine paid once per debounced preview regen, not once per
    // pointermove. Falls back to the full refresh whenever this zone has no
    // live rings to move (out-of-scope design, disabled zone, or the modal
    // just opened and showZoneRings() hasn't built them yet).
    if(!(zoneAxisDragging && window.__zoneRingsMoveZone && window.__zoneRingsMoveZone(idx, z.t_lo, z.t_hi))){
      refreshZoneRings();
    }
    // Returned so viewer.js's in-model ring drag can reposition its OWN ring
    // geometry directly (refreshZoneRings() above is a no-op mid-drag, by
    // design -- see its own guard comment) without a second read of `design`.
    return { t_lo: z.t_lo, t_hi: z.t_hi };
  }
  // Exposed for viewer.js's in-model ring drag -- see setZoneEdge's own
  // comment for why this is the ONLY place either UI mutates a zone's edges.
  window.__zoneRingDrag = setZoneEdge;

  // --- deferred persist for zone-edge drags -----------------------------
  // See the comment inside setZoneEdge above for why this exists. Both drag
  // paths funnel through setZoneEdge, so one predicate covers both: the
  // in-model ring drag sets window.__zoneRingDragActive itself (viewer.js,
  // on pointerdown, strictly before its first pointermove); the modal axis
  // drag sets zoneAxisDragging below.
  var zoneEdgePersistPending = null;   // coalesce key of a persist not yet flushed, or null
  var zoneAxisDragging = false;
  function zoneEdgeDragLive(){ return zoneAxisDragging || !!window.__zoneRingDragActive; }
  function flushZoneEdgePersist(){
    if(!zoneEdgePersistPending) return;
    var key = zoneEdgePersistPending;
    zoneEdgePersistPending = null;   // null first: re-entrancy safe, and a
                                      // stray second flush is a no-op.
    persistDesign(key);
    // persistDesign only ARMS the coalescing run (histRunKey); closing it
    // here, rather than waiting on the idle timer or the capture-phase
    // pointerup/pointercancel listeners above, keeps a flushed drag's undo
    // entry from folding into whatever the user does next.
    endHistRun();
    // schedulePreview() was skipped on every move while the drag was live
    // (see setZoneEdge) to avoid running generatePreview() at 60-120 Hz.
    // Now that the drag is over, fire it once so the 3D preview updates.
    schedulePreview();
  }
  window.__zoneEdgeFlush = flushZoneEdgePersist;

  // Rebuilds the in-model drag rings from current zone state. A no-op until
  // viewer.js defines window.showZoneRings/hideZoneRings (guarded so this
  // file loads and works standalone, e.g. under dev_smoke.html, without
  // viewer.js present). Skips entirely mid-drag (window.__zoneRingDragActive)
  // so the debounced rebuild never yanks a ring out from under the pointer,
  // same guard refreshShapeCage() uses for the FFD cage.
  function refreshZoneRings(){
    if(!window.showZoneRings || !window.hideZoneRings) return;
    if(window.__zoneRingDragActive) return;
    var outOfScope = design.pattern === 'loops' ||
      !!(typeof meshState !== 'undefined' && meshState && meshState.mesh_id);
    var zones = (design.zone_overrides || []);
    var live = [];
    zones.forEach(function(z, idx){
      if(z && z.enabled) live.push({ idx: idx, t_lo: z.t_lo, t_hi: z.t_hi, color: zoneColorHex(z) });
    });
    if(outOfScope || !live.length){ window.hideZoneRings(); return; }
    window.showZoneRings(live);
  }
  window.__refreshZoneRings = refreshZoneRings;

  (function(){
    var modal = document.getElementById('zone-modal');
    var openBtn = document.getElementById('zone-btn');
    var closeBtn = document.getElementById('zo-modal-close');
    var doneBtn = document.getElementById('zo-modal-done');
    var addBtn = document.getElementById('zo-add-zone');
    var resetBtn = document.getElementById('zo-reset-all');
    var backdrop = modal ? modal.querySelector('.zo-modal-backdrop') : null;
    if(!modal || !openBtn) return;

    // seedT (optional): a height fraction to seed a new zone at. No longer
    // used by the right-click flow (viewer.js's "Add zone here" calls
    // window.__zoneAddAt directly and never opens this modal) but kept for
    // the sidebar #zone-btn / "Zone Overrides..." entry, which still opens
    // the all-zones form with nothing pre-added.
    //
    // focusIdx (optional): scrolls to and pulses that zone's row -- used by
    // the right-click "Edit zone N textures..." entry (viewer.js), which
    // reaches an EXISTING zone the user already positioned on the model.
    function openModal(seedT, focusIdx){
      previewArmed = true;
      if(seedT != null) addZone(seedT);
      renderZoneRows();
      modal.style.display = 'flex';
      if(focusIdx != null){
        var row = document.querySelector('.zo-row[data-zone-row-idx="' + focusIdx + '"]');
        if(row){
          row.scrollIntoView({ block: 'nearest' });
          // Same pulse-cue convention as the param-search jump-to-control
          // (designer.js's param-search IIFE): remove-reflow-add so a
          // repeat call restarts the animation instead of no-op'ing.
          row.classList.remove('param-search-hit'); void row.offsetWidth;
          row.classList.add('param-search-hit');
          setTimeout(function(){ row.classList.remove('param-search-hit'); }, 1400);
        }
      }
    }
    function closeModal(){ modal.style.display = 'none'; }
    window.__openZoneModal = openModal;
    // Right-click "Edit zone N textures..." (viewer.js) -- opens the modal
    // scrolled to and pulsing an EXISTING zone, adding nothing new.
    window.__zoneOpenModalAt = function(idx){ openModal(null, idx); };

    openBtn.addEventListener('click', function(){ openModal(null); });
    if(closeBtn) closeBtn.addEventListener('click', closeModal);
    if(doneBtn) doneBtn.addEventListener('click', closeModal);
    if(backdrop) backdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape' && modal.style.display !== 'none') closeModal();
    });
    if(addBtn) addBtn.addEventListener('click', function(){ addZone(0.5); renderZoneRows(); });
    if(resetBtn) resetBtn.addEventListener('click', function(){
      design.zone_overrides = [];
      previewArmed = true;
      renderZoneRows();
      persistDesign();
      schedulePreview();
      updateZoneActiveDot();
    });

    // Height-axis drag: mousedown on a handle, track pointermove/pointerup on
    // the document (not the handle) so the drag survives leaving the small
    // handle's own hit area, same pattern as the FFD cage drag in viewer.js.
    // The actual edge-clamp logic lives in setZoneEdge() (below), shared with
    // the in-model ring drag in viewer.js -- one code path, so the two
    // interactions can never disagree about a bound.
    var axisHost = document.getElementById('zo-axis');
    var dragging = null;   // {idx, edge:'lo'|'hi'|'band', offset?:number}
    if(axisHost){
      axisHost.addEventListener('pointerdown', function(e){
        var t = e.target;
        if(!t.classList) return;
        if(t.classList.contains('zo-axis-handle')) {
          dragging = { idx: parseInt(t.getAttribute('data-zone-idx'), 10), edge: t.getAttribute('data-edge') };
          t.classList.add('dragging');
        } else if(t.classList.contains('zo-axis-band')) {
          var idx = parseInt(t.getAttribute('data-zone-idx'), 10);
          var zones = design.zone_overrides || [];
          var z = zones[idx];
          if(!z) return;
          var mid = (z.t_lo + z.t_hi) / 2;
          var rect = axisHost.getBoundingClientRect();
          var pointerT = Math.max(0, Math.min(1, 1 - (e.clientY - rect.top) / rect.height));
          dragging = { idx: idx, edge: 'band', offset: mid - pointerT };
          t.classList.add('dragging');
        } else {
          return;
        }
        zoneAxisDragging = true;
        e.preventDefault();
        t.setPointerCapture(e.pointerId);
      });
    }
    function tAtClientY(clientY){
      var rect = axisHost.getBoundingClientRect();
      return Math.max(0, Math.min(1, 1 - (clientY - rect.top) / rect.height));
    }
    document.addEventListener('pointermove', function(e){
      if(!dragging) return;
      if (dragging.edge === 'band') {
        setZoneEdge(dragging.idx, 'lo', tAtClientY(e.clientY) + dragging.offset, true);
      } else {
        setZoneEdge(dragging.idx, dragging.edge, tAtClientY(e.clientY), e.shiftKey);
      }
    });
    document.addEventListener('pointerup', function(e){
      if(!dragging) return;
      dragging = null;
      zoneAxisDragging = false;
      if(axisHost) axisHost.querySelectorAll('.zo-axis-handle.dragging, .zo-axis-band.dragging').forEach(function(h){
        h.classList.remove('dragging');
        try { h.releasePointerCapture(e.pointerId); } catch(err){}
      });
      // Flush the persist setZoneEdge deferred during the drag (see its own
      // comment), THEN the one full ring/label re-sync for this drag --
      // mirrors zoneEndDrag's own order in viewer.js for the in-model path.
      flushZoneEdgePersist();
      refreshZoneRings();
      endHistRun();
    });

    renderZoneRows();
    updateZoneActiveDot();
    updateZoneScopeNote();
  })();

  // Print tab.
  bindSelect('d-nozzle', 'nozzle');
  document.getElementById('d-nozzle').addEventListener('change', function(){ widthEditor.draw(); });
  bindNumber('d-speed', 'print_speed');
  bindSelect('d-filament', 'filament');
  (function(){
    var el = document.getElementById('d-lwoverride');
    if(!el) return;
    if(design.line_width != null) el.value = design.line_width;
    el.addEventListener('input', function(){
      var v = parseFloat(el.value);
      design.line_width = (el.value === '' || Number.isNaN(v)) ? null : v;
      persistDesign('num:d-lwoverride');
      widthEditor.draw();   // its mm readout depends on the nominal bead width
    });
    el.addEventListener('change', endHistRun);
  })();

  // Optional temperature override. Blank = auto (the selected filament's own
  // temp); only a real value overrides, and 0 is a meaningful value for the
  // bed (bed off), so blank-vs-zero must stay distinct -- see serve.py's
  // _parse_bed_temp. Shared by both inputs so the melt-ceiling clamp below can
  // never drift between them.
  //
  // The clamp reads the element's own max, which applyPrinterCaps() sets from
  // the selected printer's max_nozzle_temp / max_bed_temp (already reconciled
  // server-side against the 320C absolute backstop). Previously these stored
  // whatever was typed, so the panel could show and send 400C to a printer
  // declaring 260C and let the server quietly cut it back.
  function bindOptionalTemp(id, field, resetBtnId){
    var el = document.getElementById(id);
    if(!el) return;
    var resetBtn = resetBtnId ? document.getElementById(resetBtnId) : null;
    function syncOverrideMark(){
      var overridden = design[field] != null;
      el.classList.toggle('fm-overridden', overridden);
      if(resetBtn) resetBtn.style.display = overridden ? '' : 'none';
    }
    if(design[field] != null) el.value = design[field];
    syncOverrideMark();
    function applyTemp(commit){
      var raw = parseFloat(el.value);
      if(el.value === '' || Number.isNaN(raw)){
        design[field] = null;
        el.classList.remove('out-of-range');
      } else {
        var v = raw;
        var lo = parseFloat(el.min), hi = parseFloat(el.max);
        if(isFinite(lo)) v = Math.max(lo, v);
        if(isFinite(hi)) v = Math.min(hi, v);
        design[field] = v;
        el.classList.toggle('out-of-range', Math.abs(raw - v) > 1e-9);
        if(commit){ el.value = v; el.classList.remove('out-of-range'); }
      }
      syncOverrideMark();
      persistDesign('num:' + id);
      if(commit) endHistRun();
    }
    el.addEventListener('input', function(){ applyTemp(false); });
    el.addEventListener('change', function(){ applyTemp(true); });
  }
  bindOptionalTemp('d-nozzletemp', 'nozzle_temp', 'fm-reset-nozzle');
  bindOptionalTemp('d-bedtemp', 'bed_temp', 'fm-reset-bed');

  // Live "flow line width" readout mirroring serve.py: line_width = round(nozzle*1.125, 3),
  // or the explicit override. Purely informational; stale tracking is already handled
  // by bindSelect('d-nozzle') -> persistDesign().
  (function(){
    var hint = document.getElementById('nozzle-flow-hint');
    var nz = document.getElementById('d-nozzle');
    var lwo = document.getElementById('d-lwoverride');
    if(!hint || !nz) return;
    function fmt(n){ return String(Math.round(n*1000)/1000); }
    function compute(){
      var ov = (lwo && lwo.value !== '') ? parseFloat(lwo.value) : NaN;
      if(!Number.isNaN(ov)){ hint.textContent = 'Flow line width: ' + fmt(ov) + ' mm (override)'; return; }
      // "auto (profile)": every current printer profile defaults to a 0.40 mm nozzle.
      var nd = nz.value === '' ? 0.4 : parseFloat(nz.value);
      hint.textContent = 'Flow line width: ' + fmt(nd*1.125) + ' mm  (nozzle ' + nd + ' × 1.125)';
    }
    nz.addEventListener('change', compute);
    if(lwo) lwo.addEventListener('input', compute);
    compute();
  })();

  // ---- show/hide dependent rows -------------------------------------------
  // True only for parametric loop fabric (a solid-cuff-anchored knitted wall
  // that prints no base/brim/skirt). Mesh-mode loops are a different feature
  // (hanging loop sites on a normal wall via build_profile_spiral) that DOES
  // print a base, so this is deliberately false when a mesh is loaded. Reads
  // the DOM select directly (like refreshPatternRows) so it does not depend
  // on listener order relative to whatever updates `design.pattern`.
  function loopFabricActive(){
    var noMesh = !(typeof meshState !== 'undefined' && meshState && meshState.mesh_id);
    return document.getElementById('d-pattern').value === 'loops' && noMesh;
  }

  // Single source of truth for what actually prints at the bottom of the
  // part: base disks, brim, skirt -- after BOTH the Bottom=Open rule and the
  // loop-fabric rule (parametric loop fabric anchors on its own solid cuff
  // and never gets a base/brim/skirt, see loopFabricActive() above) are
  // applied. buildGenerateBody() (the /api/generate request) and
  // schedulePreview() (the live draft) both read this instead of each
  // re-deriving the same rule, so the draft can never promise a base the
  // request will not actually carry, or vice versa.
  function effectiveBaseSpec(){
    var lf = loopFabricActive();
    // STL mode builds the wall with build_profile_spiral, which takes neither
    // a base style nor a skirt: it always paves the disks as the Archimedean
    // spiral and never lays a skirt. It DOES honour base layers and the brim,
    // so only these two are dropped here. serve.py reports it if a request
    // asks anyway -- this just stops our own UI from asking.
    var mesh = !!(typeof meshState !== 'undefined' && meshState && meshState.mesh_id);
    return {
      base_layers: (design.bottom === 'open' || lf) ? 0 : Math.round(design.base_layers),
      // Bottom=Open zeroes ONLY base_layers -- the server still prints a brim
      // for an open-bottom design (serve.py:660 leaves brim alone). Keep that
      // exact asymmetry: brim is gated on loop-fabric alone.
      brim: lf ? 0 : Math.round(design.brim),
      skirt: (lf || mesh) ? 0 : Math.round(design.skirt || 0),
      base_style_applies: !mesh,
      // Which GENERATOR the request will hit. serve.py sends a parametric
      // loops design to build_loop_fabric(), which replaces the wall outright,
      // so the draft has to draw the fabric rather than a spiral. Carried on
      // the same object for the same reason as the fields above: one place
      // decides, and the request and the draft cannot disagree about it.
      loop_fabric: lf
    };
  }

  function refreshShapeRows(){
    var isStar = document.getElementById('d-shape').value === 'star';
    var starRows = document.getElementById('star-rows');
    if(starRows) starRows.style.display = isStar ? '' : 'none';

    var isOpen = design.bottom === 'open';
    var isLoopFabric = loopFabricActive();
    var baseRow = document.getElementById('row-base');
    // First layer height / squish is governed by Open alone: the loop-fabric
    // cuff genuinely uses first_layer_squish (loop_fabric.py:113), so it
    // still applies to loop fabric even though base/brim/skirt do not. Do
    // not fold this into the loop-fabric condition below.
    var squishRow = document.getElementById('row-squish');
    var spacingRow = document.getElementById('row-spacing');
    if(baseRow) baseRow.style.display = (isOpen || isLoopFabric) ? 'none' : '';
    // STL mode always paves the base as the Archimedean spiral and never lays
    // a skirt (build_profile_spiral takes neither), so those two rows are
    // inert there -- hide them rather than let the panel offer a choice the
    // generator will drop. Base layers and brim DO work in STL mode.
    var meshLoaded = !!(typeof meshState !== 'undefined' && meshState && meshState.mesh_id);
    var baseStyleRow = document.getElementById('row-basestyle');
    if(baseStyleRow) baseStyleRow.style.display = (isOpen || isLoopFabric || meshLoaded) ? 'none' : '';
    if(squishRow) squishRow.style.display = isOpen ? 'none' : '';
    var flhHint = document.getElementById('flh-hint');
    if(flhHint) flhHint.style.display = isOpen ? 'none' : '';
    if(spacingRow) spacingRow.style.display = (isOpen || isLoopFabric) ? 'none' : '';

    var bottomRow = document.getElementById('d-bottom');
    if(bottomRow) bottomRow.style.display = isLoopFabric ? 'none' : '';
    var brimRow = document.getElementById('row-brim');
    if(brimRow) brimRow.style.display = isLoopFabric ? 'none' : '';
    var skirtRow = document.getElementById('row-skirt');
    if(skirtRow) skirtRow.style.display = (isLoopFabric || meshLoaded) ? 'none' : '';
    var loopBaseHint = document.getElementById('loop-base-hint');
    if(loopBaseHint) loopBaseHint.style.display = isLoopFabric ? '' : 'none';
    // A zone's xy_twist no-op note (zoneTwistNoOpNote below) depends on the
    // shape too -- refresh it here rather than only from the field's own
    // input handler, or switching TO circle after already setting a zone's
    // twist would leave the note stale (missing) until the field is touched.
    if(typeof updateZoneTwistCautions === 'function') updateZoneTwistCautions();
  }
  document.getElementById('d-shape').addEventListener('change', refreshShapeRows);

  // Bottom type radios.
  var bottomRadios = document.querySelectorAll('input[name="d-bottom"]');
  bottomRadios.forEach(function(r){
    if(r.value === design.bottom) r.checked = true;
    r.addEventListener('change', function(){
      if(!r.checked) return;
      design.bottom = r.value;
      // Leave the stored Base layers value alone. buildGenerateBody() is the
      // single place that maps Bottom -> base_layers, and it already sends 0
      // for Open. Zeroing it here instead clobbered the user's setting
      // permanently: picking Solid again could not bring it back, while the
      // Base layers input still displayed the old number, so the panel
      // promised a solid base and the G-code had none.
      persistDesign();
      refreshShapeRows();
      schedulePreview();
    });
  });

  // Show pattern controls only when a texture is selected: loops have their
  // own row group, wave patterns share #pattern-rows.
  function refreshPatternRows(){
    var val = document.getElementById('d-pattern').value;
    var waveRows = document.getElementById('pattern-rows');
    var loopRows = document.getElementById('loop-rows');
    if(waveRows) waveRows.style.display = (val && val !== 'loops') ? 'block' : 'none';
    if(loopRows) loopRows.style.display = (val === 'loops') ? 'block' : 'none';
  }
  document.getElementById('d-pattern').addEventListener('change', function(){
    refreshPatternRows();
    refreshShapeRows();
  });

  refreshShapeRows();
  refreshPatternRows();

  // ---- parameter search (jump-to-control) ---------------------------------
  (function(){
    var input = document.getElementById('param-search-input');
    var resultsEl = document.getElementById('param-search-results');
    var scroll = document.getElementById('panel-scroll');
    if(!input || !resultsEl || !scroll) return;

    // conditional block -> the <select> id that controls its visibility
    var BLOCK_CTRL = { 'star-rows':'d-shape', 'pattern-rows':'d-pattern',
                       'loop-rows':'d-pattern' };
    var STEP_LABEL = { model:'Model', texture:'Texture', print:'Print', generate:'Generate' };

    // build index once over every reachable row in the sidebar
    var index = [];
    Array.prototype.forEach.call(scroll.querySelectorAll('.drow, label.row'), function(row){
      var span = row.querySelector(':scope > span');
      var label = span ? (span.textContent || '').trim() : '';
      if(!label) return;
      var mp = row.closest('.mode-panel');
      var mode = (mp && mp.id === 'mode-viewer') ? 'viewer' : 'design';
      var sp = row.closest('.step-panel');
      var step = sp ? sp.id.replace('step-','') : null;
      var ctrl = null;
      for(var b in BLOCK_CTRL){ var el = document.getElementById(b);
        if(el && el.contains(row)){ ctrl = document.getElementById(BLOCK_CTRL[b]); break; } }
      index.push({ row:row, lc:label.toLowerCase(), label:label, mode:mode, step:step, ctrl:ctrl });
    });

    var matches = [], activeIdx = -1;
    function esc(s){ return s.replace(/[&<>"]/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

    function search(q){
      q = q.trim().toLowerCase();
      if(!q){ resultsEl.classList.remove('open'); return; }
      var out = [];
      for(var i=0;i<index.length && out.length<12;i++) if(index[i].lc.indexOf(q)>=0) out.push(index[i]);
      out.sort(function(a,b){ return (a.lc.indexOf(q)===0?0:1) - (b.lc.indexOf(q)===0?0:1); });
      matches = out; activeIdx = out.length ? 0 : -1;
      resultsEl.innerHTML = '';
      out.forEach(function(m,i){
        var it = document.createElement('div');
        it.className = 'param-search-item' + (i===0?' active':'');
        it.setAttribute('role','option');
        it.id = 'psi-' + i;
        it.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
        var crumb = m.mode==='viewer' ? 'Viewer' : (STEP_LABEL[m.step]||'Design');
        it.innerHTML = '<span>'+esc(m.label)+'</span><span class="psi-crumb">'+crumb+'</span>';
        it.addEventListener('mousedown', function(e){ e.preventDefault(); choose(m); });
        it.addEventListener('mouseenter', function(){ setActive(i); });
        resultsEl.appendChild(it);
      });
      resultsEl.classList.toggle('open', out.length>0);
      input.setAttribute('aria-expanded', out.length>0 ? 'true' : 'false');
      input.setAttribute('aria-activedescendant', out.length>0 ? 'psi-0' : '');
    }
    function setActive(i){ var ch = resultsEl.children;
      if(ch[activeIdx]){ ch[activeIdx].classList.remove('active');
        ch[activeIdx].setAttribute('aria-selected','false'); }
      activeIdx = i;
      if(ch[i]){ ch[i].classList.add('active'); ch[i].setAttribute('aria-selected','true');
        input.setAttribute('aria-activedescendant', ch[i].id); } }

    // Shared by the outside-click handler and Escape: empties the query and
    // drops the stale match list, not just the dropdown's open state. Does
    // NOT run on choose() -- picking a result is expected to leave the
    // chosen label sitting in the box, only leaving-without-picking clears it.
    function clearSearch(){
      input.value = '';
      matches = []; activeIdx = -1;
      resultsEl.classList.remove('open');
      input.setAttribute('aria-expanded','false');
      input.removeAttribute('aria-activedescendant');
    }

    function choose(m){
      if(window.setAppMode) window.setAppMode(m.mode);      // reuse existing path
      if(m.mode==='design' && m.step) activateStep(m.step); // reuse existing path
      // A search hit inside a collapsed section would otherwise scroll to a
      // display:none row and flash nothing. Expand first, synchronously, so the
      // measurement below sees the real geometry.
      expandSectionsContaining(m.row);
      if(m.ctrl) expandSectionsContaining(m.ctrl);
      requestAnimationFrame(function(){
        var target = m.row;
        if(m.ctrl && m.row.offsetParent === null)           // hidden conditional block
          target = m.ctrl.closest('.drow') || m.ctrl;
        var cr = scroll.getBoundingClientRect(), er = target.getBoundingClientRect();
        scroll.scrollTop += (er.top - cr.top) - (scroll.clientHeight/2 - er.height/2);
        target.classList.remove('param-search-hit'); void target.offsetWidth;
        target.classList.add('param-search-hit');
        setTimeout(function(){ target.classList.remove('param-search-hit'); }, 1400);
      });
      resultsEl.classList.remove('open'); input.blur();
    }

    input.addEventListener('input', function(){ search(input.value); });
    input.addEventListener('keydown', function(e){
      // Escape must clear unconditionally -- checked before the matches-gate
      // below, otherwise a query with zero results (matches.length === 0)
      // would leave Escape silently doing nothing and the text stuck.
      if(e.key==='Escape'){ clearSearch(); input.blur(); return; }
      if(!matches.length) return;
      if(e.key==='ArrowDown'){ e.preventDefault(); setActive(Math.min(activeIdx+1, matches.length-1)); }
      else if(e.key==='ArrowUp'){ e.preventDefault(); setActive(Math.max(activeIdx-1, 0)); }
      else if(e.key==='Enter'){ e.preventDefault(); if(matches[activeIdx]) choose(matches[activeIdx]); }
    });
    document.addEventListener('click', function(e){
      if(!e.target.closest('#param-search')) clearSearch(); });
  })();

  // ---- parameter help tooltips (Orca-style hover panel) --------------------
  // Content lives in window.PARAM_HELP (viewer/param_help.js, loaded before
  // this file) keyed by control DOM id. One reused floating panel; a handful
  // of delegated listeners on the containers that hold every .drow/label.row
  // in the app -- the sidebar scroll area and the Point Edit Modifiers modal
  // (a separate overlay outside #panel-scroll) -- not one listener per row.
  (function(){
    var HELP = window.PARAM_HELP;
    if(!HELP) return;

    var tip = document.createElement('div');
    tip.id = 'param-tooltip';
    tip.className = 'param-tip';
    tip.setAttribute('role', 'tooltip');
    document.body.appendChild(tip);

    var OPEN_DELAY = 400;
    var showTimer = null;
    var currentCtrl = null;   // control currently wearing aria-describedby
    var currentRow = null;    // row the visible/pending tooltip belongs to

    function findRow(el){
      return (el && el.closest) ? el.closest('.drow, label.row') : null;
    }
    function labelSpan(row){
      return row.querySelector(':scope > span');
    }
    // The id a row's tooltip is keyed on: prefer an explicit data-help-id
    // (dynamically-built rows -- currently only Zone Overrides -- have no
    // stable element id to key PARAM_HELP on, since there can be several of
    // the same row at once), then a real form control, then a plain readout
    // <span id> (the STL mesh-info rows have no input at all), and finally
    // the row's own id (the Bottom radio row has no per-input id -- see
    // id="d-bottom" on that .drow in index.html).
    function helpIdFor(row){
      var explicit = row.getAttribute('data-help-id');
      if(explicit) return explicit;
      var el = row.querySelector('input[id], select[id], textarea[id], span[id]');
      if(el) return el.id;
      return row.id || null;
    }
    function controlFor(row){
      return row.querySelector('input, select, textarea') || row;
    }

    function hide(){
      if(showTimer){ clearTimeout(showTimer); showTimer = null; }
      tip.classList.remove('open');
      if(currentCtrl){ currentCtrl.removeAttribute('aria-describedby'); currentCtrl = null; }
      currentRow = null;
    }

    // Fixed-position, flipped to stay inside the viewport on both axes --
    // the panel is docked at the right edge of the window (so opening
    // rightward would usually overflow) and rows near the bottom of a tall
    // sidebar must not push the tooltip below the fold. Measured, not assumed.
    function position(anchorEl){
      var ar = anchorEl.getBoundingClientRect();
      var tw = tip.offsetWidth, th = tip.offsetHeight;
      var gap = 8, margin = 8;
      var vw = window.innerWidth, vh = window.innerHeight;

      var x = ar.right + gap;
      if(x + tw + margin > vw) x = ar.left - gap - tw;
      x = Math.max(margin, Math.min(vw - tw - margin, x));

      var y = ar.top;
      if(y + th + margin > vh) y = vh - th - margin;
      y = Math.max(margin, y);

      tip.style.left = Math.round(x) + 'px';
      tip.style.top = Math.round(y) + 'px';
    }

    function render(data){
      tip.innerHTML = '';
      var desc = document.createElement('div');
      desc.className = 'param-tip-desc';
      desc.textContent = data.desc;
      tip.appendChild(desc);
      var paramLine = document.createElement('div');
      paramLine.className = 'param-tip-meta';
      paramLine.textContent = 'parameter: ' + data.param;
      tip.appendChild(paramLine);
      var defLine = document.createElement('div');
      defLine.className = 'param-tip-meta';
      defLine.textContent = 'Default: ' + data.def;
      tip.appendChild(defLine);
    }

    function showFor(row, anchorEl, immediate){
      var id = helpIdFor(row);
      var data = id ? HELP[id] : null;
      if(!data) return;   // no entry for this id -- fail silent, never a broken empty box
      function doShow(){
        render(data);
        currentRow = row;
        currentCtrl = controlFor(row);
        if(currentCtrl) currentCtrl.setAttribute('aria-describedby', tip.id);
        position(anchorEl);   // tip is laid out (visibility:hidden) so this measures real content
        tip.classList.add('open');
      }
      if(showTimer){ clearTimeout(showTimer); showTimer = null; }
      if(immediate) doShow();
      else showTimer = setTimeout(doShow, OPEN_DELAY);
    }

    function onMouseOver(e){
      var row = findRow(e.target);
      if(!row) return;
      var span = labelSpan(row);
      if(!span || (e.target !== span && !span.contains(e.target))) return;
      if(currentRow === row && tip.classList.contains('open')) return;
      showFor(row, span, false);
    }
    function onMouseOut(e){
      var row = findRow(e.target);
      if(!row) return;
      var span = labelSpan(row);
      if(!span) return;
      var to = e.relatedTarget;
      if(to && (to === span || span.contains(to))) return;   // still inside the label
      if(currentRow === row || showTimer) hide();
    }
    function onFocusIn(e){
      var ctrl = e.target;
      if(!ctrl || !ctrl.matches || !ctrl.matches('input, select, textarea')) return;
      var row = findRow(ctrl);
      if(!row) return;
      showFor(row, ctrl, true);   // no delay on keyboard focus
    }
    function onFocusOut(e){
      var row = findRow(e.target);
      if(row && row === currentRow) hide();
    }

    var hoverContainers = [document.getElementById('panel-scroll'),
                            document.getElementById('point-edit-modal'),
                            document.getElementById('zone-modal')];
    hoverContainers.forEach(function(c){
      if(!c) return;
      c.addEventListener('mouseover', onMouseOver);
      c.addEventListener('mouseout', onMouseOut);
      c.addEventListener('focusin', onFocusIn);
      c.addEventListener('focusout', onFocusOut);
    });
    // 'scroll' does not bubble, so it needs the actual scrolling elements --
    // #panel-scroll for the sidebar, .pe-panels/.zo-modal-body for each
    // modal's own body.
    var scrollContainers = [document.getElementById('panel-scroll'),
                             document.querySelector('.pe-panels'),
                             document.querySelector('.zo-modal-body')];
    scrollContainers.forEach(function(c){ if(c) c.addEventListener('scroll', hide, {passive:true}); });
    document.addEventListener('keydown', function(e){ if(e.key === 'Escape') hide(); });
    document.addEventListener('click', hide, true);
  })();

  updateSlope();
  window.addEventListener('mousemove', function(){ if(document.getElementById('slope-read')) updateSlope(); });


  // ---- preset gallery + save/load -----------------------------------------
  var PRESETS = [
    { name: 'Gentle Wave Vase', shape:'circle', radius:32, height:45, layer_height:0.30,
      xy_twist:0, z_waves:5, z_twist:0, bottom:'solid', base_layers:2, brim:0,
      squish:0.75, spacing_factor:1.25, print_speed:40, pattern:'',
      amp_profile:[[0,0],[0.2,0.3],[0.4,0.6],[0.6,0.8],[0.8,0.8],[1.0,0.5]],
      radius_profile:[[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]] },
    { name: 'Twisted Star', shape:'star', radius:30, height:50, layer_height:0.30,
      xy_twist:1.5, z_waves:5, z_twist:0, star_points:5, star_depth:0.35,
      bottom:'solid', base_layers:2, brim:0, squish:0.75, spacing_factor:1.25,
      print_speed:45, pattern:'',
      amp_profile:[[0,0],[0.2,0.3],[0.4,0.6],[0.6,0.8],[0.8,0.8],[1.0,0.5]],
      radius_profile:[[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]] },
    { name: 'Twisted Squircle', shape:'square', radius:30, height:55, layer_height:0.32,
      xy_twist:1.0, z_waves:4, z_twist:0, bottom:'solid', base_layers:2, brim:0,
      squish:0.75, spacing_factor:1.25, print_speed:45, pattern:'',
      amp_profile:[[0,0],[0.2,0.3],[0.4,0.65],[0.6,0.9],[0.8,0.9],[1.0,0.5]],
      radius_profile:[[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]] },
    { name: 'Ripple Vase', shape:'circle', radius:34, height:60, layer_height:0.30,
      xy_twist:0, z_waves:6, z_twist:0, bottom:'solid', base_layers:2, brim:0,
      squish:0.75, spacing_factor:1.25, print_speed:40, pattern:'ripple',
      pattern_amp:1.2, pattern_waves:10, pattern_bands:8, pattern_twist:0.5,
      pattern_fade_in:0.10, pattern_fade_out:0,
      amp_profile:[[0,0],[0.15,0.25],[0.4,0.6],[0.7,0.7],[1.0,0.4]],
      radius_profile:[[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]] },
    { name: 'Flared Vase', shape:'circle', radius:28, height:65, layer_height:0.30,
      xy_twist:0, z_waves:5, z_twist:0, bottom:'solid', base_layers:2, brim:0,
      squish:0.75, spacing_factor:1.25, print_speed:40, pattern:'',
      amp_profile:[[0,0],[0.2,0.3],[0.5,0.7],[0.8,0.8],[1.0,0.5]],
      radius_profile:[[0,0.7],[0.15,0.6],[0.4,0.8],[0.7,1.1],[1.0,1.25]] },
    { name: 'Aggressive Twist Star', shape:'star', radius:28, height:55, layer_height:0.30,
      xy_twist:3.0, z_waves:8, z_twist:1.0, star_points:6, star_depth:0.40,
      bottom:'solid', base_layers:2, brim:0, squish:0.75, spacing_factor:1.25,
      print_speed:90, pattern:'',
      amp_profile:[[0,0],[0.2,0.2],[0.5,0.5],[0.8,0.7],[1.0,0.4]],
      radius_profile:[[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]] },
    { name: 'Diamond Mesh Basket', shape:'circle', radius:32, height:50, layer_height:0.3,
      xy_twist:0, z_waves:0, z_twist:0, bottom:'solid', base_layers:2, brim:0,
      squish:0.75, spacing_factor:1.25, print_speed:25, pattern:'vwave',
      pattern_amp:2.5, pattern_waves:24, pattern_bands:6, pattern_twist:0,
      pattern_phase:0, pattern_fade_in:0.08, pattern_fade_out:0, pattern_alternate:true,
      amp_profile:[[0,0],[1,0]],
      radius_profile:[[0,1],[1,1]] },
    { name: 'Loop Fabric Vase', shape:'circle', radius:32, height:45, layer_height:0.3,
      xy_twist:0, z_waves:0, z_twist:0, bottom:'solid', base_layers:2, brim:0,
      squish:0.75, spacing_factor:1.25, print_speed:25, pattern:'loops',
      loop_style:'chainmail', loop_spacing_mm:4, loop_per_turn:0, loop_turn_stride:1,
      loop_align:'stagger', loop_jitter:0.5, loop_row:2.5, loop_up:3.5, loop_out:0.5,
      loop_cuff:3, loop_rejoin:2.0, loop_dwell:0, loop_flow:1.2, loop_speed:10,
      loop_fade_in:0.1, loop_fade_out:0,
      amp_profile:[[0,0],[1,0]],
      radius_profile:[[0,1],[1,1]] },
  ];

  (function(){
    var sel = document.getElementById('preset-select');
    PRESETS.forEach(function(p, i){
      var o = document.createElement('option');
      o.value = i; o.textContent = p.name;
      sel.appendChild(o);
    });
    sel.addEventListener('change', function(){
      if(sel.value === '') return;
      var p = PRESETS[parseInt(sel.value)];
      if(!p) return;
      for(var k in p){ if(k !== 'name' && design.hasOwnProperty(k)) design[k] = p[k]; }
      design.cage = null;
      previewArmed = true;
      applyDesignToUI();
      sel.value = '';
    });
    
    // Populate the right-click preset context menu
    var ctxItems = document.getElementById('preset-ctx-items');
    var ctxMenu = document.getElementById('preset-context-menu');
    if(ctxItems && ctxMenu){
      PRESETS.forEach(function(p, i){
        var btn = document.createElement('button');
        btn.className = 'preset-ctx-item';
        
        var icon = document.createElement('div');
        icon.className = 'pci-icon';
        // Add a placeholder SVG for the icon
        icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>';
        
        var label = document.createElement('span');
        label.textContent = p.name;
        
        btn.appendChild(icon);
        btn.appendChild(label);
        
        btn.addEventListener('click', function(){
          for(var k in p){ if(k !== 'name' && design.hasOwnProperty(k)) design[k] = p[k]; }
          design.cage = null;
          previewArmed = true; // <--- Force preview update
          applyDesignToUI();
          ctxMenu.style.display = 'none';
        });
        ctxItems.appendChild(btn);
      });
      
      // Allow viewer.js to close the menu
      window.__closePresetMenu = function(){
        ctxMenu.style.display = 'none';
      };
      // Allow viewer.js to open the menu
      window.__openPresetMenu = function(x, y){
        const mw = 200; // estimated width
        const margin = 8;
        if(x + mw + margin > window.innerWidth) x = window.innerWidth - mw - margin;
        ctxMenu.style.left = Math.max(margin, Math.round(x)) + 'px';
        ctxMenu.style.top = Math.max(margin, Math.round(y)) + 'px';
        ctxMenu.style.display = 'block';
      };
    }
  })();

  function applyDesignToUI(){
    if(printerComboBtn && design.printer){
      setPrinterComboValue(design.printer);
      if(PRINTER_BEDS[design.printer] && typeof window.setPreviewBedSize === 'function'){
        window.setPreviewBedSize(PRINTER_BEDS[design.printer][0], PRINTER_BEDS[design.printer][1]);
      }
      if(PRINTER_BEDS[design.printer]){
        design.bed_center = [PRINTER_BEDS[design.printer][0]/2, PRINTER_BEDS[design.printer][1]/2];
      }
    }
    document.getElementById('d-shape').value = design.shape;
    document.getElementById('d-radius').value = design.radius;
    document.getElementById('d-height').value = design.height;
    document.getElementById('d-lh').value = design.layer_height;
    document.getElementById('d-xytwist').value = design.xy_twist;
    document.getElementById('d-starpoints').value = design.star_points;
    document.getElementById('d-stardepth').value = design.star_depth;
    document.getElementById('d-base').value = design.base_layers;
    document.getElementById('d-brim').value = design.brim;
    var _set = function(id, v){ var el = document.getElementById(id); if(el) el.value = v; };
    _set('d-spine', design.spine_mm || 0);
    _set('d-spinedeg', design.spine_deg || 0);
    _set('d-ovality', design.ovality || 0);
    _set('d-basestyle', design.base_style || 'spiral');
    _set('d-skirt', design.skirt || 0);
    var flhEl = document.getElementById('d-flh');
    if(flhEl) flhEl.value = design.first_layer_height != null ? design.first_layer_height : '';
    document.getElementById('d-spacing').value = design.spacing_factor;
    document.getElementById('d-waves').value = design.z_waves;
    document.getElementById('d-ztwist').value = design.z_twist;
    document.getElementById('d-pattern').value = design.pattern || '';
    document.getElementById('d-pamp').value = design.pattern_amp;
    document.getElementById('d-pwaves').value = design.pattern_waves;
    document.getElementById('d-pbands').value = design.pattern_bands;
    document.getElementById('d-ptwist').value = design.pattern_twist;
    document.getElementById('d-pfadein').value = design.pattern_fade_in;
    document.getElementById('d-pfadeout').value = design.pattern_fade_out;
    var palternateEl = document.getElementById('d-palternate');
    if(palternateEl) palternateEl.checked = !!design.pattern_alternate;
    var latticeHintEl = document.getElementById('lattice-hint');
    if(latticeHintEl) latticeHintEl.style.display = design.pattern_alternate ? 'block' : 'none';
    document.getElementById('d-speed').value = design.print_speed;
    var lwEl = document.getElementById('d-lwoverride');
    if(lwEl) lwEl.value = design.line_width != null ? design.line_width : '';
    var nozzleTempEl = document.getElementById('d-nozzletemp');
    if(nozzleTempEl) nozzleTempEl.value = design.nozzle_temp != null ? design.nozzle_temp : '';
    var bedTempEl = document.getElementById('d-bedtemp');
    if(bedTempEl) bedTempEl.value = design.bed_temp != null ? design.bed_temp : '';
    var bottomRadios = document.querySelectorAll('input[name="d-bottom"]');
    bottomRadios.forEach(function(r){ r.checked = r.value === design.bottom; });
    var nozzleEl = document.getElementById('d-nozzle');
    if(nozzleEl) nozzleEl.value = design.nozzle || '';
    var smoothEl = document.getElementById('sil-smooth');
    if(smoothEl) smoothEl.checked = !!design.radius_profile_smooth;
    // Loop controls
    updateLoopControlsFromDesign();
    // Overhang flow
    var ohSlider = document.getElementById('d-overhang-k');
    if(ohSlider){ ohSlider.value = design.overhang_flow_k || 0; }
    var ohRead = document.getElementById('overhang-k-read');
    if(ohRead) ohRead.textContent = (design.overhang_flow_k || 0).toFixed(2);
    // Fan min/max (speed selected by wall lean)
    var fanMinSlider = document.getElementById('d-fan-min');
    if(fanMinSlider){ fanMinSlider.value = design.fan_overhang_min != null ? design.fan_overhang_min : 100; }
    var fanMinRead = document.getElementById('fan-min-read');
    if(fanMinRead) fanMinRead.textContent = (design.fan_overhang_min != null ? design.fan_overhang_min : 100) + '%';
    var fanMaxSlider = document.getElementById('d-fan-max');
    if(fanMaxSlider){ fanMaxSlider.value = design.fan_overhang_max != null ? design.fan_overhang_max : 100; }
    var fanMaxRead = document.getElementById('fan-max-read');
    if(fanMaxRead) fanMaxRead.textContent = (design.fan_overhang_max != null ? design.fan_overhang_max : 100) + '%';
    var fanOffEl = document.getElementById('d-fan-off-layers');
    if(fanOffEl) fanOffEl.value = design.fan_off_layers || 0;
    ampEditor.setProfile(design.amp_profile);
    silEditor.setProfile(design.radius_profile);
    widthEditor.setProfile(design.width_profile);
    // Re-apply the SELECTED printer's ceilings, and do it AFTER setProfile so
    // the loaded control points are re-clamped rather than merely displayed.
    // "Load design as JSON" assigns every key it recognises, and `printer` is
    // one of them (see the load handler's `if(design.hasOwnProperty(k))`), so
    // this function can land on a different machine than the one whose caps
    // are currently applied. Without this, loading a design saved on a 4.0mm
    // Bambu while a 0.95mm Trident is selected left the amp editor's ceiling,
    // its control points and the draft's amplitude clamp all on the old
    // machine's numbers -- the UI offering, drawing and SENDING amplitudes
    // the server would clamp away, which is precisely what applyPrinterCaps'
    // own comment forbids. Cheap and idempotent when the printer did not
    // change, which is the common case.
    applyPrinterCaps();
    design.amp_profile = ampEditor.profile();
    ampEditor.draw(); silEditor.draw(); widthEditor.draw();
    if(typeof applyPointEditUIFromDesign === 'function') applyPointEditUIFromDesign();
    if(typeof window.__applyZoneUIFromDesign === 'function') window.__applyZoneUIFromDesign();
    if(typeof updateZoneActiveDot === 'function') updateZoneActiveDot();
    if(typeof updateZoneScopeNote === 'function') updateZoneScopeNote();
    persistDesign();
    refreshShapeRows();
    refreshPatternRows();
    updateSlope();
    schedulePreview();
    activateSilMode(design.sil_mode || 'sym');
  }

  // Save design as a .trident file download -- still plain JSON text under
  // the hood (the extension is a naming/recognition convention only, not a
  // different serialization), wrapped in a small envelope so the edit-
  // history log travels with it. A session that hasn't edited anything
  // exports history: [].
  document.getElementById('save-design').addEventListener('click', function(){
    var data = JSON.parse(JSON.stringify(design));
    data.amp_profile = ampEditor.profile();
    data.radius_profile = silEditor.profile();
    data.width_profile = widthEditor.profile();
    var env = {
      format: TRIDENT_FORMAT,
      format_version: TRIDENT_FORMAT_VERSION,
      exported_at: new Date().toISOString(),
      design: data,
      history: histLog.slice()   // shallow copy: entries pass through verbatim
    };
    var blob = new Blob([JSON.stringify(env, null, 2)], {type:'application/json'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'trident_design_' + design.shape + '_' + design.height + 'mm.trident';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  });

  // Load design from a .trident (or an older plain-design .json) file.
  document.getElementById('load-design').addEventListener('click', function(){
    document.getElementById('load-design-file').click();
  });
  // JSON.parse rejects the bare tokens NaN/Infinity, but NOT a numeral that
  // overflows to one at parse time -- {"height": 1e999} is syntactically
  // valid JSON and silently becomes Infinity. That value then survives
  // straight into `design` (the merge loop below does no range checking)
  // and on into geometry generation, where it doesn't get clamped -- it
  // crashes: `new Float32Array(Infinity)` throws a RangeError out of
  // preview_math.js. Same CLAUDE.md invariant as the server's z_amp_max
  // parsing ("reject non-finite values at the boundary; never clamp them"),
  // just reachable here via a loaded file instead of a config string. Scan
  // BEFORE merging anything into `design` so a bad file can't partially
  // corrupt the current design. Also covers a .trident envelope's own
  // exported_at/history -- deliberately the ONE gate for the whole loaded
  // object (envelope and all), not a second, easier-to-forget-about check
  // scoped to just `design`.
  function findNonFiniteNumber(v, path){
    if(typeof v === 'number') return isFinite(v) ? null : (path || '(root)');
    if(Array.isArray(v)){
      for(var i = 0; i < v.length; i++){
        var hit = findNonFiniteNumber(v[i], (path || '') + '[' + i + ']');
        if(hit) return hit;
      }
      return null;
    }
    if(v && typeof v === 'object'){
      for(var k in v){
        if(!Object.prototype.hasOwnProperty.call(v, k)) continue;
        var hit2 = findNonFiniteNumber(v[k], (path ? path + '.' : '') + k);
        if(hit2) return hit2;
      }
    }
    return null;
  }

  document.getElementById('load-design-file').addEventListener('change', function(e){
    if(!e.target.files.length) return;
    
    var fname = e.target.files[0].name;
    var fext = fname.toLowerCase().split('.').pop();
    
    if (fext === 'stl') {
      uploadSTL(e.target.files[0]);
      e.target.value = '';
      return;
    }
    
    // Captured NOW, synchronously: reader.onload below fires asynchronously,
    // by which point `e.target.value = ''` (also below, runs synchronously
    // right after readAsText) has already cleared e.target.files -- reading
    // e.target.files[0] from inside onload would throw on an empty
    // FileList, which the catch below would swallow into a "could not load"
    // alert, silently skipping applyDesignToUI() (and with it, e.g., a
    // printer switch the loaded file specified).
    var reader = new FileReader();
    reader.onload = function(ev){
      try {
        var loaded = JSON.parse(ev.target.result);
        if(!loaded || typeof loaded !== 'object') throw new Error('not an object');
        var badPath = findNonFiniteNumber(loaded, '');
        if(badPath) throw new Error('non-finite value at ' + badPath + ' (NaN/Infinity are not valid design values)');
        // Detected by SHAPE, never by file extension -- #load-design-file
        // accepts both .trident and legacy .json, and both are read
        // identically as text; only their CONTENTS differ. No key of
        // `design` is named "format" or "design", so this is a safe
        // discriminator against a bare old-format design file.
        var isEnv = loaded.format === TRIDENT_FORMAT && loaded.design && typeof loaded.design === 'object';
        var src = isEnv ? loaded.design : loaded;
        if(isEnv && +loaded.format_version > TRIDENT_FORMAT_VERSION){
          alert('This .trident file was saved by a newer version -- some information may not be shown.');
        }
        // History first, so the "imported <file>" entry below lands AFTER
        // whatever the file itself carried, keeping one continuous log
        // across repeated export/import/edit rounds rather than resetting
        // it each time.
        if(isEnv && Array.isArray(loaded.history)){
          histLog = loaded.history.filter(function(h){
            return h && typeof h === 'object' && typeof h.summary === 'string';
          }).slice(-HIST_LOG_MAX);
          saveHistLog();
          histLabelOnce = 'imported ' + fname;
        } else {
          histLabelOnce = 'imported ' + fname + ' (no edit history in file)';
        }
        for(var k in src){ if(design.hasOwnProperty(k)) design[k] = src[k]; }
        applyDesignToUI();
        if(isEnv && loaded.history && loaded.history.length && typeof openHistoryModal === 'function'){
          openHistoryModal();
        }
      } catch(err){
        alert('Could not load design: ' + err.message);
      }
    };
    reader.onerror = function(){
      // Without this, a failed read leaves onload never firing and the
      // user staring at silence with no idea the load did nothing.
      alert('Could not read file: ' + (reader.error ? reader.error.message : 'unknown error'));
    };
    reader.readAsText(e.target.files[0]);
    e.target.value = '';
  });

  // ---- edit-history modal -------------------------------------------------
  // Read-only view of histLog. Mirrors the open/close/backdrop/Escape
  // pattern the app's other modals (Zone Overrides, Point Edit, printer
  // manager) already use, rather than inventing a new interaction shape.
  // Hoisted function declarations (this whole file is one IIFE), so the
  // Load handler above -- textually earlier -- calling openHistoryModal()
  // is exactly the same "declared later, safe to call earlier" pattern
  // already used throughout this file (e.g. flushZoneEdgePersist, referenced
  // by pointerup listeners well before its own definition).
  function openHistoryModal(){
    var modal = document.getElementById('history-modal');
    if(!modal) return;
    renderHistList();
    modal.style.display = 'flex';
  }
  function closeHistoryModal(){
    var modal = document.getElementById('history-modal');
    if(modal) modal.style.display = 'none';
  }
  (function(){
    var modal = document.getElementById('history-modal');
    if(!modal) return;
    var btn = document.getElementById('history-btn');
    if(btn) btn.addEventListener('click', openHistoryModal);
    var closeBtn = document.getElementById('eh-modal-close');
    if(closeBtn) closeBtn.addEventListener('click', closeHistoryModal);
    var doneBtn = document.getElementById('eh-modal-done');
    if(doneBtn) doneBtn.addEventListener('click', closeHistoryModal);
    var backdrop = modal.querySelector('.eh-modal-backdrop');
    if(backdrop) backdrop.addEventListener('click', closeHistoryModal);
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape' && modal.style.display !== 'none') closeHistoryModal();
    });
  })();

  function renderHistList(){
    var list = document.getElementById('eh-list');
    var empty = document.getElementById('eh-empty');
    if(!list) return;
    list.innerHTML = '';
    if(empty) empty.style.display = histLog.length ? 'none' : '';
    // Newest first -- iterate the persisted (oldest-first) array backwards.
    for(var i = histLog.length - 1; i >= 0; i--){
      var e = histLog[i];
      var li = document.createElement('li');
      li.className = 'eh-item';
      var when = document.createElement('span');
      when.className = 'eh-when';
      var d = new Date(e.at);
      when.textContent = isNaN(d.getTime()) ? String(e.at) :
        d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + '  ' +
        d.toLocaleDateString([], {day:'numeric', month:'short'});
      var what = document.createElement('span');
      what.className = 'eh-what';
      // textContent only, never innerHTML: this renders values out of
      // localStorage AND out of a file someone else wrote and handed you.
      what.textContent = e.summary;
      li.appendChild(when); li.appendChild(what);
      list.appendChild(li);
    }
  }

  // ---- STL mesh import (Import tab) ---------------------------------------
  var MESH_MAX_MB = 50;
  var meshState = { mesh_id: null, filename: null, info: null };

  var stlDrop = document.getElementById('stl-drop');
  var stlFile = document.getElementById('stl-file');
  var stlStatusEl = document.getElementById('stl-status');
  var stlUploadSeq = 0;

  if (stlDrop && stlFile) {
    stlDrop.addEventListener('click', function(){ stlFile.click(); });
    // A div with role="button" gets no automatic Enter/Space activation --
    // mirrors the #pm-drop handler above so both drop zones behave the same.
    stlDrop.addEventListener('keydown', function(e){
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); stlFile.click(); }
    });
    stlFile.addEventListener('change', function(e){
      if(e.target.files.length > 0) uploadSTL(e.target.files[0]);
    });

    stlDrop.addEventListener('dragover', function(e){
      e.preventDefault(); e.stopPropagation();
      stlDrop.classList.add('drag-over');
    });
    stlDrop.addEventListener('dragleave', function(e){
      stlDrop.classList.remove('drag-over');
    });
    stlDrop.addEventListener('drop', function(e){
      e.preventDefault(); e.stopPropagation();
      stlDrop.classList.remove('drag-over');
      var files = e.dataTransfer.files;
      for(var i=0; i<files.length; i++){
        if(files[i].name.toLowerCase().slice(-4) === '.stl'){
          uploadSTL(files[i]);
          return;
        }
      }
    });
  }

  function uploadSTL(file){
    if(file.size > MESH_MAX_MB * 1024 * 1024){
      alert('File too large (max ' + MESH_MAX_MB + ' MB).');
      return;
    }
    // Sequence guard, same pattern as printer-config parsing's
    // runRevalidate() below: two rapid drops/picks must let only the LATER
    // one win, not whichever response happens to land first.
    var mySeq = ++stlUploadSeq;
    var end = beginBusy(null, stlStatusEl, 'Uploading and analyzing mesh...');
    var reader = new FileReader();
    reader.onload = function(e){
      if(mySeq !== stlUploadSeq) return; // superseded before the request even went out
      apiFetch('/api/upload_mesh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': file.name },
        body: e.target.result
      })
      .then(function(r){ return r.json(); })
      .then(function(data){
        if(mySeq !== stlUploadSeq) return; // a newer upload's response already won
        if(data.error){ end('Upload failed: ' + data.error, true); return; }
        end(null); // mesh-info panel below is the confirmation; no need to leave text behind
        meshState.mesh_id = data.mesh_id;
        meshState.filename = file.name;
        meshState.info = data;
        showMeshInfo(data, file.name);
        // Mesh-mode loops print a base again once a mesh is loaded (loops
        // become hanging loop sites on a normal wall, not fabric) - bring
        // the base/brim/skirt rows back.
        refreshShapeRows();
      })
      .catch(function(err){
        if(mySeq !== stlUploadSeq) return;
        end('Upload failed: ' + err, true);
      });
    };
    reader.onerror = function(){
      if(mySeq !== stlUploadSeq) return;
      end('Could not read file: ' + (reader.error ? reader.error.message : 'unknown error'), true);
    };
    reader.readAsArrayBuffer(file);
  }

  function showMeshInfo(data, filename){
    var drop = document.getElementById('stl-drop');
    if (drop) drop.style.display = 'none';
    document.getElementById('mesh-info').style.display = 'block';
    document.getElementById('mesh-name').textContent = filename;
    document.getElementById('mesh-tris').textContent = data.triangles;
    var dx = data.bounds.max[0] - data.bounds.min[0];
    var dy = data.bounds.max[1] - data.bounds.min[1];
    document.getElementById('mesh-size').textContent = dx.toFixed(1) + ' x ' + dy.toFixed(1);
    document.getElementById('mesh-height').textContent = data.height.toFixed(1);
    showMeshCheck(data.vase_check);
    if(typeof updatePointEditScopeNote === 'function') updatePointEditScopeNote();
    if(typeof updateZoneScopeNote === 'function') updateZoneScopeNote();
  }

  // We keep only the largest contour per layer, so holes and islands are dropped
  // silently. The server flags that on upload; surface it here so the user finds
  // out before printing rather than after.
  function showMeshCheck(chk){
    var box = document.getElementById('mesh-check');
    var list = document.getElementById('mesh-check-notes');
    if(!box || !list) return;
    list.innerHTML = '';
    if(!chk || chk.error){
      box.style.display = chk && chk.error ? 'block' : 'none';
      if(chk && chk.error){
        box.className = 'mesh-check warn';
        document.getElementById('mesh-check-icon').textContent = '!';
        document.getElementById('mesh-check-title').textContent = chk.error;
      }
      return;
    }
    var notes = chk.notes || [];
    var bad = !chk.compatible;
    var warn = chk.compatible && notes.length > 0;
    var info = !bad && !warn && chk.solid_cross_section;
    // A clean star-convex vase needs no callout at all -- staying quiet when
    // everything is fine keeps the warning meaningful when it does appear.
    if(!bad && !warn && !info){
      box.style.display = 'none';
      return;
    }
    box.style.display = 'block';
    var level = bad ? 'bad' : (warn ? 'warn' : 'info');
    box.className = 'mesh-check ' + level;
    document.getElementById('mesh-check-icon').textContent = bad ? '✗' : (warn ? '!' : 'i');
    document.getElementById('mesh-check-title').textContent = bad
      ? 'This STL is not vase-compatible'
      : (warn ? 'This STL will print, with caveats' : 'This model will print hollow');
    // Independent of severity: "prints hollow" is about what the user gets, while
    // warn-level notes are about slicing fidelity. A solid part that also trips a
    // caveat still needs to hear the hollow message -- it's the more consequential
    // one -- so this is keyed off the flag itself, not off the info level.
    if(chk.solid_cross_section && !bad){
      var liInfo = document.createElement('li');
      liInfo.textContent = 'This is a solid part, so the single-wall spiral traces only '
        + 'its outline - no infill, open top. Use Export STL below and slice in Orca '
        + 'if you want it solid.';
      list.appendChild(liInfo);
    }
    for(var i = 0; i < notes.length; i++){
      var li = document.createElement('li');
      li.textContent = notes[i];
      list.appendChild(li);
    }
    if(bad && chk.discarded_area_pct > 0){
      var li2 = document.createElement('li');
      li2.textContent = 'Up to ' + chk.discarded_area_pct
        + '% of a layer’s area is dropped (worst at Z '
        + chk.worst_height_mm + ' mm).';
      list.appendChild(li2);
    }
  }

  document.getElementById('mesh-clear').addEventListener('click', function(){
    meshState.mesh_id = null;
    meshState.filename = null;
    meshState.info = null;
    var drop = document.getElementById('stl-drop');
    if (drop) drop.style.display = '';
    document.getElementById('mesh-info').style.display = 'none';
    showMeshCheck(null);
    if (stlFile) stlFile.value = '';
    if(typeof updatePointEditScopeNote === 'function') updatePointEditScopeNote();
    if(typeof updateZoneScopeNote === 'function') updateZoneScopeNote();
    // Clearing the mesh can turn loops back into fabric (loop-fabric hides
    // base/brim/skirt) - re-evaluate those rows.
    refreshShapeRows();
  });

  // ---- generate ----------------------------------------------------------
  var genBtn = document.getElementById('gen-btn');
  var statusEl = document.getElementById('gen-status');
  var reportEl = document.getElementById('gen-report');
  var dlBtn = document.getElementById('dl-btn');
  var lastGcode = null, lastName = null;

  // Shared by the Generate and Export STL buttons -- both send the same
  // design snapshot to the server, just to different endpoints, so the body
  // assembly lives in one place instead of drifting out of sync.
  function buildGenerateBody(){
    var baseSpec = effectiveBaseSpec();
    var body = {
      printer: design.printer || 'trident',
      shape: design.shape,
      radius: design.radius,
      height: design.height,
      layer_height: design.layer_height,
      line_width: design.line_width,
      z_waves: Math.round(design.z_waves),
      xy_twist: design.xy_twist,
      z_twist: design.z_twist,
      base_layers: baseSpec.base_layers,
      brim: baseSpec.brim,
      squish: design.squish,
      first_layer_height: design.first_layer_height || 0,
      spine_mm: design.spine_mm || 0,
      spine_deg: design.spine_deg || 0,
      ovality: design.ovality || 0,
      base_style: baseSpec.base_style_applies ? (design.base_style || 'spiral') : 'spiral',
      skirt: baseSpec.skirt,
      first_layer_spacing_factor: design.spacing_factor,
      print_speed: design.print_speed,
      filament: design.filament || null,
      amp_profile: ampEditor.profile(),
      radius_profile: silEditor.profile(),
      width_profile: widthEditor.profile(),
      radius_profile_smooth: !!design.radius_profile_smooth,
      pattern: null,
      overhang_flow_k: design.overhang_flow_k || 0,
      nozzle: design.nozzle || null
    };
    if(design.cage && design.cage.some(function(r){ return r.some(function(v){ return Math.abs(v-1) > 1e-6; }); })){
      body.cage = design.cage;
    }
    // Overhang-adaptive fan: sliders are 0-100 (UI); only sent when not both
    // at the 100/100 no-op default (server also tolerates them absent).
    if((design.fan_overhang_min != null && design.fan_overhang_min < 100) ||
       (design.fan_overhang_max != null && design.fan_overhang_max < 100)){
      body.fan_min = Math.max(0, Math.min(design.fan_overhang_min != null ? design.fan_overhang_min : 100, 100)) / 100;
      body.fan_max = Math.max(0, Math.min(design.fan_overhang_max != null ? design.fan_overhang_max : 100, 100)) / 100;
    }
    if(design.fan_off_layers > 0){
      body.fan_off_layers = Math.round(design.fan_off_layers);
    }
    // Only sent when the user actually set an override -- absent means
    // "use the profile/filament default", same contract as serve.py's
    // _parse_nozzle_temp().
    if(design.nozzle_temp != null && design.nozzle_temp !== ''){
      body.nozzle_temp = design.nozzle_temp;
    }
    // Same contract via serve.py's _parse_bed_temp(), EXCEPT 0 is a real,
    // meaningful override (bed off) and must be sent, not treated as unset --
    // this check is "!= null", not truthy, so 0 passes through correctly.
    if(design.bed_temp != null && design.bed_temp !== ''){
      body.bed_temp = design.bed_temp;
    }
    // Point Edit Modifiers: each block is only sent when its modifier is
    // enabled AND would actually do something (server tolerates malformed/
    // no-op input gracefully either way, but there's no reason to send inert
    // payloads). Only the parametric wall honors these -- loop fabric and
    // STL mode ignore them server-side and surface a warning in the report.
    if(pointEditMaskMeaningful()){
      body.point_mask = {
        channel: design.point_mask_channel,
        scale_u: design.point_mask_scale_u,
        scale_v: design.point_mask_scale_v,
        invert: !!design.point_mask_invert
      };
    }
    if(pointEditProtectionMeaningful()){
      body.point_protection = {
        protect_bottom: design.point_protection_bottom || 0,
        protect_top: design.point_protection_top || 0,
        falloff: design.point_protection_falloff || 0.08
      };
    }
    if(pointEditFFDMeaningful()){
      body.point_ffd = {
        cage: window.buildFFDCageFromGrid(design.point_ffd_grid),
        strength: design.point_ffd_strength != null ? design.point_ffd_strength : 1.0
      };
    }
    if(pointEditSmoothMeaningful()){
      body.point_smooth = {
        iterations: Math.round(design.point_smooth_iterations),
        theta_amount: design.point_smooth_theta != null ? design.point_smooth_theta : 0.5,
        t_amount: design.point_smooth_t != null ? design.point_smooth_t : 0.5,
        strength: design.point_smooth_strength != null ? design.point_smooth_strength : 1.0
      };
    }
    if(pointEditRadialPushMeaningful()){
      body.point_radial_push = {
        amp_mm: design.point_radial_push_amp,
        strength: design.point_radial_push_strength != null ? design.point_radial_push_strength : 1.0
      };
    }
    // Zone Overrides: only sent when at least one zone would actually do
    // something (mirrors the Point Edit gating above). A different
    // subsystem from Point Edit: zones change how the wall is GENERATED for
    // a height band, not the already-sliced path. Only the parametric wall
    // honors these -- loop fabric and STL mesh mode ignore them server-side
    // and surface a scope warning in the report.
    if(zoneOverridesAnyEnabled()){
      body.zone_overrides = design.zone_overrides.filter(zoneOverrideMeaningful).map(function(z){
        var entry = { t_lo: z.t_lo, t_hi: z.t_hi, blend: z.blend != null ? z.blend : 0.02 };
        if(z.pattern) entry.pattern = z.pattern;
        if(z.pattern_amp != null) entry.pattern_amp = z.pattern_amp;
        if(z.pattern_twist != null) entry.pattern_twist = z.pattern_twist;
        if(z.xy_twist != null) entry.xy_twist = z.xy_twist;
        return entry;
      });
    }
    // Route texture params by the pattern dropdown: loops are a site-based
    // texture (server pattern stays null), wave patterns send the pattern_*
    // fields (and only those get pattern_alternate).
    var patternVal = design.pattern || '';
    if(patternVal === 'loops'){
      if(design.loop_per_turn > 0) body.loop_per_turn = Math.round(design.loop_per_turn);
      if(design.loop_spacing_mm > 0) body.loop_spacing_mm = design.loop_spacing_mm;
      body.loop_turn_stride = Math.round(design.loop_turn_stride);
      body.loop_align = design.loop_align;
      body.loop_jitter = design.loop_jitter;
      body.loop_row = design.loop_row;
      body.loop_up = design.loop_up;
      body.loop_out = design.loop_out;
      body.loop_cuff = Math.round(design.loop_cuff || 3);
      body.loop_mode = design.loop_mode || 'dip';
      body.loop_dwell = Math.round(design.loop_dwell || 0);
      body.loop_lean = design.loop_lean != null ? design.loop_lean : 20;
      body.loop_coast = design.loop_coast != null ? design.loop_coast : 0.8;
      body.loop_retract = design.loop_retract || 0;
      body.loop_wave_amp = design.loop_wave_amp || 0;
      body.loop_waves = Math.round(design.loop_waves || 12);
      body.loop_rejoin = design.loop_rejoin;
      body.loop_flow = design.loop_flow;
      body.loop_speed = design.loop_speed;
      body.loop_fade_in = design.loop_fade_in;
      body.loop_fade_out = design.loop_fade_out;
      // base_layers/brim/skirt are already 0 here when loop fabric is active
      // (mesh_id absent) -- effectiveBaseSpec() at the top of this function
      // zeroed them into `body` already. Loop fabric (as opposed to mesh-mode
      // loops, handled below) anchors itself with its own solid cuff and
      // prints no base/brim/skirt no matter what is asked for. The stored
      // design values are deliberately NOT mutated (same principle as the
      // Bottom radio above): the request is where this decision belongs, so
      // the settings return when the user leaves loops.
    } else if(patternVal){
      body.pattern = patternVal;
      body.pattern_amp = design.pattern_amp;
      body.pattern_waves = Math.round(design.pattern_waves);
      body.pattern_bands = design.pattern_bands;
      body.pattern_twist = design.pattern_twist;
      body.pattern_phase = design.pattern_phase;
      body.pattern_fade_in = design.pattern_fade_in;
      body.pattern_fade_out = design.pattern_fade_out;
      body.pattern_alternate = !!design.pattern_alternate;
    }
    if(design.shape === 'star'){
      body.star_points = Math.round(design.star_points);
      body.star_depth = design.star_depth;
    }

    if(meshState.mesh_id){
      body.mode = 'mesh_texture';
      body.mesh_id = meshState.mesh_id;
      body.scale = parseFloat(document.getElementById('mesh-scale').value) || 1.0;
      // layer_height from the mesh panel overrides the shape panel's value
      body.layer_height = parseFloat(document.getElementById('mesh-lh').value) || 0.3;
      // texture + z-wave params still come from the design object (Texture/Waves tabs)
    }

    return body;
  }
  // Dev/self-test hook: lets dev_smoke.html inspect the exact request body a
  // Generate click would send, without actually POSTing it.
  window.__designSnapshot = buildGenerateBody;

  // ---- generate (streamed progress, with a fallback to the one-shot path) --
  // Human labels for the "stage" token /api/generate_stream sends. Anything
  // not in this table (a future stage, or a typo server-side) still shows
  // something reasonable rather than leaking the raw token to the user.
  var GEN_STAGE_LABELS = { toolpath: 'Building toolpath', verify: 'Verifying against printer limits' };
  function genStageLabel(stage){
    return GEN_STAGE_LABELS[stage] || ('Working (' + stage + ')');
  }

  var genProgressEl = document.getElementById('gen-progress');
  var genProgressFill = document.getElementById('gen-progress-fill');
  function showGenProgress(frac, label){
    var pct = Math.max(0, Math.min(99, Math.round(frac * 100))); // completion only via the 'done' line
    // 'block', not '': .gen-progress carries `display:none` in style.css, so
    // clearing the inline style falls back to that rule and the bar stays
    // invisible for the whole generate -- which is exactly what it did.
    if(genProgressEl){ genProgressEl.style.display = 'block'; genProgressEl.setAttribute('aria-valuenow', String(pct)); }
    if(genProgressFill) genProgressFill.style.width = pct + '%';
    // Header brand mark: same fraction, same clamp-to-99 intent, so the
    // spiral never visually completes before the stream's 'done' line does.
    if(headerMarkEl){
      headerMarkEl.classList.remove('is-busy-indeterminate');
      headerMarkEl.classList.add('is-busy-determinate');
      headerMarkEl.style.setProperty('--mark-progress', pct / 100);
    }
    statusEl.className = '';
    statusEl.textContent = label + '... ' + pct + '%';
  }
  function hideGenProgress(){
    if(genProgressEl) genProgressEl.style.display = 'none';
    if(genProgressFill) genProgressFill.style.width = '0%';
    if(headerMarkEl){
      headerMarkEl.classList.remove('is-busy-determinate');
      headerMarkEl.style.removeProperty('--mark-progress');
    }
  }

  // Applies a successful /api/generate result (the streaming path's "done"
  // line and the legacy one-shot response carry the identical shape) to the
  // UI. Shared so the two paths can never diverge in what "success" means.
  //
  // clickRev is designRev AS OF THE CLICK, not read live here -- see the
  // capture at the bottom of this function's one caller. Nothing disables
  // the design inputs while a generate is in flight, so if the user edits a
  // parameter mid-request, stamping the LIVE designRev would mark G-code
  // built from the OLD design as matching the NEW one: updateStaleBadge()
  // would then hide the "outdated" warning while the loaded/downloadable
  // G-code silently does not match what the panel shows. Given this
  // project's stance on output matching the screen, that is exactly the
  // failure CLAUDE.md's safety-first stance exists to prevent.
  function applyGenerateResult(j, clickRev){
    lastGcode = j.gcode; lastName = j.filename;
    // Pressing Generate is a design action, so arm the draft preview. Without
    // this, a user who generated a restored design without touching a single
    // control had never armed it, and switching back to Design left them on a
    // bare bed: the generated toolpath is (correctly) hidden outside the
    // viewer, and the draft that should replace it never drew.
    previewArmed = true;
    if(window.clearPreview) window.clearPreview();
    if(window.loadGcode){ window.loadGcode(j.filename, j.gcode); }
    if(window.setAppMode) window.setAppMode('viewer');
    generatedRev = clickRev;          // the click-time revision, not the live one -- see above
    updateStaleBadge();
    reportEl.textContent = j.report || '';
    reportEl.style.display = 'block';
    dlBtn.style.display = 'block';
    var nIssues = (j.issues||[]).length;
    if(nIssues){
      statusEl.className = 'err';
      statusEl.textContent = nIssues + ' safety warning(s) - see report';
    } else {
      statusEl.className = 'ok';
      statusEl.textContent = 'safe [OK] - ' + (j.stats ? (j.stats.filament_m + ' m, ' + j.filename) : j.filename);
    }
  }

  // Original one-shot path. Used as a fallback when /api/generate_stream is
  // unreachable (404 against an older server, a network error, or a
  // response with no streaming body) -- indeterminate spinner only, since
  // there is no progress signal to show a real bar for.
  function runLegacyGenerate(body, clickRev){
    statusEl.className = '';
    statusEl.textContent = 'Generating (no progress available)...';
    apiFetch('/api/generate', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    }).then(function(resp){
      return resp.json().then(function(j){ return {ok:resp.ok, j:j}; });
    }).then(function(res){
      genBtn.disabled = false;
      hideGenProgress();
      if(!res.ok){
        statusEl.className = 'err';
        statusEl.textContent = res.j && res.j.error ? res.j.error : 'generation failed';
        return;
      }
      applyGenerateResult(res.j, clickRev);
    }).catch(function(err){
      genBtn.disabled = false;
      hideGenProgress();
      statusEl.className = 'err';
      statusEl.textContent = 'request failed: ' + err;
    });
  }

  genBtn.addEventListener('click', function(){
    var body = buildGenerateBody();
    // Captured together, alongside body: both are frozen at click time so
    // the eventual response can only ever be stamped against the design it
    // was actually built from. See the comment in applyGenerateResult.
    var clickRev = designRev;
    genBtn.disabled = true;
    statusEl.className = ''; statusEl.textContent = 'Starting...';
    dlBtn.style.display = 'none';
    hideGenProgress();

    var usedStream = false;   // true once we've committed to reading the stream
    var settled = false;      // true once a 'done'/'error' line (or legacy response) has landed
    var lastFrac = 0;

    apiFetch('/api/generate_stream', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    }).then(function(resp){
      if(!resp.ok || !resp.body){
        // Old server without this endpoint (404), or a response.body this
        // browser/proxy doesn't support streaming -- fall back below rather
        // than leaving Generate dead against an older deployment. The
        // endpoint always answers 200 even for a generation failure (see
        // its docstring), so a non-ok status here means something more
        // fundamental than "the design was rejected."
        throw new Error('stream unavailable');
      }
      usedStream = true;
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = ''; // carries a partial line across chunk boundaries

      function handleLine(line){
        line = line.trim();
        if(!line) return;
        var obj;
        try { obj = JSON.parse(line); } catch(e){ return; }
        if(obj.type === 'progress'){
          var frac = (typeof obj.frac === 'number' && isFinite(obj.frac)) ? obj.frac : lastFrac;
          if(frac < lastFrac) frac = lastFrac; // never let the bar go backwards
          lastFrac = frac;
          showGenProgress(frac, genStageLabel(obj.stage));
        } else if(obj.type === 'done'){
          settled = true;
          genBtn.disabled = false;
          hideGenProgress();
          applyGenerateResult(obj.result, clickRev);
        } else if(obj.type === 'error'){
          // Status is 200 even here (see the endpoint's docstring) -- the
          // failure lives entirely in this line's "type", never in HTTP
          // status, so branching on resp.ok anywhere in this path would
          // silently show a stale "generating..." forever.
          settled = true;
          genBtn.disabled = false;
          hideGenProgress();
          statusEl.className = 'err';
          statusEl.textContent = obj.error || 'generation failed';
        }
      }

      function pump(){
        return reader.read().then(function(res){
          if(res.done){
            if(buf.trim()) handleLine(buf);
            buf = '';
            if(!settled){
              // The connection closed without a 'done' or 'error' line --
              // do not leave the button/bar stuck busy forever.
              genBtn.disabled = false;
              hideGenProgress();
              statusEl.className = 'err';
              statusEl.textContent = 'generation stream ended unexpectedly';
            }
            return;
          }
          buf += decoder.decode(res.value, {stream: true});
          var lines = buf.split('\n');
          buf = lines.pop(); // last (possibly partial) line waits for the next chunk
          lines.forEach(handleLine);
          if(settled){ try { reader.cancel(); } catch(e){} return; }
          return pump();
        });
      }
      return pump();
    }).catch(function(err){
      if(settled) return; // a real result already landed; a trailing network blip is moot
      if(usedStream){
        // Broke mid-stream after we'd already committed to it (network
        // drop, malformed NDJSON) -- report it directly. Falling back to
        // /api/generate here would double-submit the same generation job.
        genBtn.disabled = false;
        hideGenProgress();
        statusEl.className = 'err';
        statusEl.textContent = 'request failed: ' + err;
        return;
      }
      // Never got a usable stream at all -- fall back so the app still
      // works against an older server.
      runLegacyGenerate(body, clickRev);
    });
  });

  dlBtn.addEventListener('click', function(){
    if(lastGcode==null) return;
    var blob = new Blob([lastGcode], {type:'text/plain'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = lastName || 'design.gcode';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  });

  // ---- export STL ---------------------------------------------------------
  // Slices the same design through /api/export_stl instead of /api/generate,
  // giving a solid mesh of the sculpted surface for users who want to slice
  // it as a filled part in Orca rather than print our single-wall spiral.
  var exportStlBtn = document.getElementById('export-stl-btn');
  if(exportStlBtn){
    exportStlBtn.addEventListener('click', function(){
      // body is a click-time snapshot, same as Generate's -- but unlike
      // Generate, this handler never stamps a "matches the current design"
      // claim anywhere (no generatedRev-equivalent, no stale badge for
      // STL), so there is nothing here for a mid-request edit to make
      // dishonest: the download is simply whatever body says, and that was
      // frozen the moment this click fired.
      var body = buildGenerateBody();
      exportStlBtn.disabled = true;
      statusEl.className = ''; statusEl.textContent = 'exporting STL...';
      apiFetch('/api/export_stl', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
      }).then(function(resp){
        if(!resp.ok){
          return resp.json().then(function(j){
            throw new Error(j && j.error ? j.error : 'STL export failed');
          }).catch(function(e){
            throw (e instanceof Error ? e : new Error('STL export failed'));
          });
        }
        return resp.blob();
      }).then(function(blob){
        exportStlBtn.disabled = false;
        var stlName = (lastName || ('trident_design_' + design.shape + '_' + design.height + 'mm.gcode'))
          .replace(/\.[^.]*$/, '') + '.stl';
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = stlName;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
        statusEl.className = 'ok';
        statusEl.textContent = 'STL exported - ' + stlName;
      }).catch(function(err){
        exportStlBtn.disabled = false;
        statusEl.className = 'err';
        statusEl.textContent = 'STL export failed: ' + (err && err.message ? err.message : err);
      });
    });
  }

  // ---- session restore prompt ----------------------------------------------
  // A saved design is reinstated from localStorage on every load, which is
  // convenient right up until it isn't: coming back days later and silently
  // inheriting a half-finished vase -- its own wave amplitudes, texture and
  // silhouette already in the boxes -- is how someone generates G-code they did
  // not mean to. When there is something restorable, say so and let them pick.
  //
  // Ordering is deliberate. The design is restored BEFORE this asks, so the app
  // is never sitting half-initialised behind a dialog, and "Start new" is then
  // an ordinary edit through applyDesignToUI(): it repaints every control,
  // re-runs the preview, and lands on the undo stack, so Ctrl+Z brings the old
  // design back.
  //
  // The selected PRINTER is deliberately not reset. It is a statement about the
  // hardware on the desk, not about this particular vase -- silently putting a
  // Bambu user back onto the default Trident would hand them a design carrying
  // a machine limit that is not theirs, which is exactly the class of mistake
  // this project treats as a hardware problem rather than a UI one. Imported
  // printers are likewise untouched: those are user assets (a .cfg they had to
  // find and import), not session scratch.
  (function(){
    var modal = document.getElementById('session-modal');
    if(!modal || !cachedDesign) return;

    var summary = document.getElementById('sr-summary');
    if(summary){
      var d = cachedDesign;
      var pick = function(key){ return d[key] != null ? d[key] : DEFAULT_DESIGN[key]; };
      var bits = [];
      var shape = String(pick('shape') || '');
      if(shape) bits.push(shape.charAt(0).toUpperCase() + shape.slice(1));
      bits.push((+pick('radius') * 2).toFixed(0) + ' x ' + (+pick('height')).toFixed(0) + ' mm');
      if(+pick('z_waves')) bits.push(pick('z_waves') + ' Z waves');
      if(+pick('xy_twist')) bits.push(pick('xy_twist') + ' deg twist');
      if(d.pattern) bits.push('texture: ' + d.pattern);
      if(pick('bottom') !== 'open' && +pick('base_layers')) bits.push(pick('base_layers') + ' base layers');
      bits.forEach(function(t){
        var el = document.createElement('span');
        el.className = 'chip';
        el.textContent = t;          // textContent, never innerHTML: this is
        el.style.cursor = 'default'; // rendering values out of localStorage
        summary.appendChild(el);
      });
    }

    function close(){
      modal.style.display = 'none';
      document.removeEventListener('keydown', onKey, true);
    }
    // Esc resolves to "continue" -- the choice that changes nothing. A stray
    // keypress must never be the thing that discards a design.
    function onKey(e){
      if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); continueSession(); }
    }

    // Continuing means "the design from last time is the one I want", so draw
    // it. The draft preview is otherwise armed only by the user touching a
    // control (see previewArmed), which left the bed empty behind a dialog
    // that had just described a vase: the summary chips read "Star, 64 x 60
    // mm, 5 Z waves, texture: ripple" and the canvas underneath was bare, so
    // the one question the dialog exists to ask -- is this the design you
    // meant? -- could only be answered by poking a control. Drawing it is what
    // makes Continue actually continue.
    //
    // This is the DRAFT preview, not a generate: client-side geometry only, no
    // server round trip and no G-code. Kicking off a real generation for a
    // design nobody has looked at yet is the exact failure this whole prompt
    // exists to prevent, and it would also hand the user a Download button for
    // a file they never asked for.
    //
    // Esc routes here too (it resolves to Continue), so the quiet way out of
    // the dialog shows the design as well.
    function continueSession(){
      previewArmed = true;
      schedulePreview();
      close();
    }

    document.getElementById('sr-continue').addEventListener('click', continueSession);
    document.getElementById('sr-new').addEventListener('click', function(){
      var keepPrinter = design.printer;
      for(var k in DEFAULT_DESIGN){
        if(!DEFAULT_DESIGN.hasOwnProperty(k)) continue;
        design[k] = JSON.parse(JSON.stringify(DEFAULT_DESIGN[k]));
      }
      if(keepPrinter) design.printer = keepPrinter;
      applyDesignToUI();                 // repaints, persists, re-previews
      if(typeof activateStep === 'function') activateStep('model');
      close();
    });

    // Deliberately no backdrop-click dismissal: the whole point is an explicit
    // choice, and a misplaced click should not count as one.
    modal.style.display = '';
    document.addEventListener('keydown', onKey, true);
    var primary = document.getElementById('sr-continue');
    if(primary) primary.focus();
  })();
})();
