(function(){
  'use strict';
  var PROBE_LIMIT = 0.95;  // wave amplitude ceiling (mm) -- user limit < 1mm
  var AMP_MAX = 0.95, RAD_LO = 0.5, RAD_HI = 1.3;

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
    print_speed: 40, filament: "", line_width: null,
    pattern: "", pattern_amp: 1.0, pattern_waves: 12,
    pattern_bands: 6, pattern_twist: 0, pattern_phase: 0,
    pattern_fade_in: 0.10, pattern_fade_out: 0, pattern_alternate: false,
    amp_profile: [[0,0],[0.2,0.3],[0.4,0.6],[0.6,0.8],[0.8,0.8],[1.0,0.5]],
    radius_profile: [[0,1],[0.2,1],[0.4,1],[0.6,1],[0.8,1],[1.0,1]],
    radius_profile_smooth: false,
    sil3d: false,
    sil_mode: "sym",
    cage: null,
    nozzle: "",
    blob_enable: false,
    blob_style: "dots",
    blob_per_turn: 6,
    blob_spacing_mm: 0,
    blob_turn_stride: 2,
    blob_stagger: true,
    blob_align: "stagger",
    blob_jitter: 0.5,
    blob_volume: 3.0,
    blob_vol_start: 1.0,
    blob_vol_end: 1.0,
    blob_dwell: 400,
    blob_fade_in: 0.15,
    blob_fade_out: 0.05,
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
    overhang_flow_k: 0.0
  };

  // Legacy migration: designs saved before blob_align existed only carry
  // blob_stagger. Derive the equivalent alignment so old saves keep behaving
  // the same way instead of silently reverting to the "stagger" default.
  function deriveLegacyBlobAlign(loaded){
    if(loaded && typeof loaded === 'object' &&
       loaded.hasOwnProperty('blob_stagger') && !loaded.hasOwnProperty('blob_align')){
      design.blob_align = loaded.blob_stagger ? 'stagger' : 'column';
    }
  }

  // Legacy migration: designs saved before blobs/loops became pattern-dropdown
  // choices carry blob_enable as a separate flag. Fold that into `pattern` so
  // old saves keep rendering blobs instead of silently losing the texture.
  function migrateBlobPattern(){
    if(design.blob_enable && (!design.pattern || design.pattern === '')) design.pattern = 'blobs';
  }

  // Load persisted state, if any (merge over defaults so new fields survive).
  try {
    var saved = JSON.parse(localStorage.getItem('design-state') || 'null');
    if(saved && typeof saved === 'object'){
      for(var k in saved){ if(saved.hasOwnProperty(k)) design[k] = saved[k]; }
      deriveLegacyBlobAlign(saved);
      migrateBlobPattern();
      // Migrate legacy sil3d boolean (pre mode-bar UI) to the new sil_mode field.
      // Only fires for old saves that predate sil_mode -- once it's saved with
      // a sil_mode value, this branch never re-triggers.
      if(saved.sil3d === true && !saved.sil_mode) design.sil_mode = 'asym';
    }
  } catch(e){ /* ignore corrupt state */ }

  // ---- undo / redo history --------------------------------------------------
  // Every persistDesign() call that actually changed the design pushes the
  // PREVIOUS state onto the undo stack. Undo/redo restore whole snapshots.
  var HIST_MAX = 50;
  var undoStack = [], redoStack = [];
  var histSuppress = false;                       // true while applying undo/redo
  var lastSnap = JSON.stringify(design);          // state as of last persist

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

  function persistDesign(){
    var snap = JSON.stringify(design);
    if(snap !== lastSnap){
      designRev++;
      if(!histSuppress){
        undoStack.push(lastSnap);
        if(undoStack.length > HIST_MAX) undoStack.shift();
        redoStack.length = 0;
      }
    }
    lastSnap = snap;
    updateHistButtons();
    updateStaleBadge();
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
    if(!undoStack.length) return;
    redoStack.push(JSON.stringify(design));
    restoreSnapshot(undoStack.pop());
  }
  function doRedo(){
    if(!redoStack.length) return;
    undoStack.push(JSON.stringify(design));
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
  // Debug hook (harmless in production).
  window.__hist = function(){ return { undo: undoStack.length, redo: redoStack.length }; };

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
        var pts = generatePreview(design);
        if(pts && pts.length > 0){
          window.showPreview(pts);
          var ov = document.getElementById('overlay');
          if(ov) ov.style.display = 'none';
          if(design.sil3d) refreshShapeCage();
        }
      }
    }, 100);
  }

  // Arm the draft preview on the first real user interaction with any design
  // or printer control (init-time fetch callbacks never arm it).
  ['printer-group', 'design-group'].forEach(function(id){
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
    var saved = parseFloat(localStorage.getItem('panel-width'));
    var startW = (saved && saved >= MIN_W && saved <= MAX_W) ? saved : DEFAULT_W;
    panel.style.width = startW + 'px';
    panel.style.flexBasis = startW + 'px';

    var dragging = false, startX = 0, startPanelW = 0;
    function onMove(e){
      if(!dragging) return;
      var dx = startX - e.clientX;   // dragging left grows the panel
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
    function activateMode(name){
      if(!panels[name]) name = 'design';
      btns.forEach(function(btn){
        var active = btn.dataset.mode === name;
        btn.classList.toggle('active', active);
      });
      for(var k in panels){
        if(panels[k]) panels[k].classList.toggle('active', k === name);
      }
      try { localStorage.setItem('app-mode', name); } catch(e){}
      if(name === 'viewer'){
        // Viewing the generated G-code: drop the live blue draft so the
        // rainbow toolpath is unobstructed.
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
    var savedMode = null;
    try { savedMode = localStorage.getItem('app-mode'); } catch(e){}
    activateMode((savedMode && panels[savedMode]) ? savedMode : 'design');
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
    var savedStep = null;
    try { savedStep = localStorage.getItem('designer-step'); } catch(e){}
    activate((savedStep && panels[savedStep]) ? savedStep : 'model');
    return activate;
  })();

  // ---- printer dropdown ---------------------------------------------------
  var printerSel = document.getElementById('d-printer');
  var PRINTER_BEDS = {};   // key -> [bed_x, bed_y]
  var PRINTER_ZCAP = {};   // key -> max Z excursion below printed material (mm)

  // The machine's z-amp ceiling caps how far loop stitches may dip (and with
  // it the useful row height). Reflect it on the inputs so the arrows stop at
  // the real limit; the server clamps authoritatively either way.
  function applyPrinterCaps(){
    var cap = PRINTER_ZCAP[design.printer];
    if(!cap) return;
    var up = document.getElementById('d-loop-up');
    var row = document.getElementById('d-loop-row');
    if(up) up.max = cap;
    if(row) row.max = Math.round(Math.max(cap - 0.3, 0.4) * 100) / 100;
    var note = document.getElementById('loop-zcap');
    if(note){
      note.textContent = 'This printer allows loops up to ' + cap +
        'mm tall' + (cap < 1.5 ? ' (probe keep-out limit)' : '') + '.';
    }
  }

  if(printerSel){
    fetch('/api/printers').then(function(r){ return r.json(); }).then(function(j){
      printerSel.innerHTML = '';
      // Group options like Bambu Studio / OGcode: brand-prefixed printers get
      // their own optgroup, everything else falls into "Other".
      var groups = {}, groupOrder = [];
      function groupFor(name){
        if(name.indexOf('Bambu Lab') === 0) return 'Bambu Lab';
        if(name.indexOf('Voron') === 0) return 'Voron';
        return 'Other';
      }
      (j.printers||[]).forEach(function(p){
        PRINTER_BEDS[p.key] = p.bed;
        PRINTER_ZCAP[p.key] = p.z_amp_max || 0.95;
        var gname = groupFor(p.name || '');
        if(!groups[gname]){
          groups[gname] = document.createElement('optgroup');
          groups[gname].label = gname;
          groupOrder.push(gname);
        }
        var o = document.createElement('option');
        o.value = p.key; o.textContent = p.name;
        groups[gname].appendChild(o);
      });
      groupOrder.forEach(function(g){ printerSel.appendChild(groups[g]); });
      var key = design.printer && PRINTER_BEDS[design.printer] ? design.printer : (j.default || 'trident');
      design.printer = key;
      printerSel.value = key;
      if(PRINTER_BEDS[key] && typeof window.setPreviewBedSize === 'function'){
        window.setPreviewBedSize(PRINTER_BEDS[key][0], PRINTER_BEDS[key][1]);
      }
      if(PRINTER_BEDS[key]) design.bed_center = [PRINTER_BEDS[key][0]/2, PRINTER_BEDS[key][1]/2];
      applyPrinterCaps();
      persistDesign();
      schedulePreview();
    }).catch(function(){ /* keep static option, if any */ });

    printerSel.addEventListener('change', function(){
      design.printer = printerSel.value;
      if(PRINTER_BEDS[design.printer] && typeof window.setPreviewBedSize === 'function'){
        window.setPreviewBedSize(PRINTER_BEDS[design.printer][0], PRINTER_BEDS[design.printer][1]);
      }
      if(PRINTER_BEDS[design.printer]){
        design.bed_center = [PRINTER_BEDS[design.printer][0]/2, PRINTER_BEDS[design.printer][1]/2];
      }
      applyPrinterCaps();
      persistDesign();
      schedulePreview();
    });
  }

  // ---- filament dropdown -------------------------------------------------
  var famSel = document.getElementById('d-filament');
  fetch('/api/filaments').then(function(r){ return r.json(); }).then(function(j){
    famSel.innerHTML = '';
    var opt = document.createElement('option'); opt.value=''; opt.textContent='(generic PLA)';
    famSel.appendChild(opt);
    (j.filaments||[]).forEach(function(n){
      var o=document.createElement('option'); o.value=n; o.textContent=n; famSel.appendChild(o);
    });
    if(design.filament){ famSel.value = design.filament; }
    else if(j.default){ famSel.value = j.default; design.filament = j.default; }
  }).catch(function(){ /* no orca: keep generic PLA */ });

  // ---- curve editor ------------------------------------------------------
  // Variable-count control points {t, v}. First point pinned to t=0, last to
  // t=1.0. Points may be added (double-click), removed (right-click, min 2),
  // and dragged in both X and Y (except the pinned endpoints, which are Y-only).
  var MAX_PTS = 24;
  function defaultsToPts(defaults, ts){
    return defaults.map(function(v,i){ return {t: ts[i], v: v}; });
  }
  function makeEditor(canvasId, lo, hi, defaults, refVal, refLabel){
    var cv = document.getElementById(canvasId);
    var ctx = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    var PADL = 4, PADR = 4, PADT = 8, PADB = 12;
    var defaultTs = [0, 0.2, 0.4, 0.6, 0.8, 1.0];
    var pts = defaultsToPts(defaults, defaultTs);
    var dragging = -1;

    function css(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
    function px(t){ return PADL + t*(W-PADL-PADR); }
    function py(v){ return PADT + (1-(v-lo)/(hi-lo))*(H-PADT-PADB); }
    function toVal(y){ var v = lo + (1-(y-PADT)/(H-PADT-PADB))*(hi-lo); return Math.min(hi, Math.max(lo, v)); }
    function toT(x){ var t = (x-PADL)/(W-PADL-PADR); return Math.min(1, Math.max(0, t)); }

    function sortPts(){ pts.sort(function(a,b){ return a.t - b.t; }); }

    function draw(){
      ctx.clearRect(0,0,W,H);
      var accent = css('--accent') || '#4cc2ff';
      // filled area under the polyline
      ctx.beginPath();
      ctx.moveTo(px(pts[0].t), py(pts[0].v));
      for(var i=1;i<pts.length;i++){ ctx.lineTo(px(pts[i].t), py(pts[i].v)); }
      ctx.lineTo(px(pts[pts.length-1].t), H-PADB);
      ctx.lineTo(px(pts[0].t), H-PADB);
      ctx.closePath();
      ctx.fillStyle = 'rgba(76,194,255,0.14)';
      ctx.fill();
      // polyline
      ctx.beginPath();
      ctx.moveTo(px(pts[0].t), py(pts[0].v));
      for(var j=1;j<pts.length;j++){ ctx.lineTo(px(pts[j].t), py(pts[j].v)); }
      ctx.strokeStyle = accent; ctx.lineWidth = 1.6; ctx.stroke();
      // reference (dashed) line
      ctx.setLineDash([4,3]);
      ctx.beginPath();
      ctx.moveTo(PADL, py(refVal)); ctx.lineTo(W-PADR, py(refVal));
      ctx.strokeStyle = (refLabel.indexOf('probe')>=0) ? 'rgba(224,101,79,0.85)' : (css('--muted')||'#8b97a7');
      ctx.lineWidth = 1; ctx.stroke();
      ctx.setLineDash([]);
      // control points
      for(var k=0;k<pts.length;k++){
        ctx.beginPath(); ctx.arc(px(pts[k].t), py(pts[k].v), 3.2, 0, Math.PI*2);
        ctx.fillStyle = accent; ctx.fill();
      }
      // labels (ASCII)
      ctx.fillStyle = css('--muted')||'#8b97a7';
      ctx.font = '9px sans-serif';
      ctx.fillText(refLabel, PADL+2, py(refVal)-2);
      ctx.fillText('bottom', PADL, H-2);
      ctx.fillText('top', W-18, H-2);
      // numeric readout for the point being dragged
      if(dragging >= 0 && pts[dragging]){
        var dp = pts[dragging];
        ctx.fillStyle = accent;
        ctx.font = '10px sans-serif';
        var label = 't=' + dp.t.toFixed(2) + ' v=' + dp.v.toFixed(2);
        var lx = Math.min(px(dp.t) + 8, W - 78);
        var ly = Math.max(py(dp.v) - 8, 12);
        ctx.fillText(label, lx, ly);
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
      if(dragging >= 0){ dragging=-1; draw(); }   // clear the readout
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
      draw(); onChange();
    });

    // Right-click: remove nearest point (never the endpoints, min 2 points).
    cv.addEventListener('contextmenu', function(e){
      e.preventDefault();
      if(pts.length <= 2) return;
      var p = evtXY(e);
      var idx = nearest(p[0], p[1]);
      if(idx <= 0 || idx >= pts.length-1) return; // can't remove endpoints
      pts.splice(idx, 1);
      draw(); onChange();
    });

    var onChange = function(){};

    return {
      draw: draw,
      profile: function(){
        sortPts();
        return pts.map(function(p){ return [p.t, p.v]; });
      },
      reset: function(){ pts = defaultsToPts(defaults, defaultTs); draw(); onChange(); },
      setChangeHandler: function(fn){ onChange = fn; },
      setProfile: function(prof){
        if(!prof || prof.length < 2) return;
        pts = prof.map(function(p){ return {t: p[0], v: p[1]}; });
        sortPts();
        pts[0].t = 0;
        pts[pts.length-1].t = 1.0;
        draw();
      }
    };
  }

  // Default amp curve peaks at 1.6mm: with the default 5 waves on r=32 that is
  // wave slope 0.25 - the empirically printable ceiling on this machine
  // (2026-07-05 print: slopes beyond ~0.25 collapsed above half height).
  var ampEditor = makeEditor('amp-curve', 0, AMP_MAX, [0, 0.3, 0.6, 0.8, 0.8, 0.5],
                             PROBE_LIMIT, 'amp limit 0.95');
  var silEditor = makeEditor('sil-curve', RAD_LO, RAD_HI, [1,1,1,1,1,1],
                             1.0, '1.0');
  // Restore persisted curve shapes.
  ampEditor.setProfile(design.amp_profile);
  silEditor.setProfile(design.radius_profile);
  ampEditor.draw(); silEditor.draw();
  ampEditor.setChangeHandler(function(){ design.amp_profile = ampEditor.profile(); persistDesign(); updateSlope(); schedulePreview(); });
  silEditor.setChangeHandler(function(){ design.radius_profile = silEditor.profile(); persistDesign(); updateSlope(); schedulePreview(); refreshShapeCage(); });
  document.getElementById('amp-reset').addEventListener('click', function(){ ampEditor.reset(); });
  document.getElementById('sil-reset').addEventListener('click', function(){ silEditor.reset(); });

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
    }, function(i, j, s){
      // A cage-handle drag happens on the 3D viewport canvas, outside the
      // side-panel arming listeners — so arm the live preview here or the
      // blue draft would never draw for a user who only touched the model.
      previewArmed = true;
      design.cage[i][j] = s;
      persistDesign();
      schedulePreview();
    });
    updateCageNote();
  }

  // Warns (in the warning color) when unsymmetrical mode is active over the
  // 'loops' pattern, since the per-point cage deformation doesn't apply to
  // loop fabric geometry.
  function updateCageNote(){
    var noteEl = document.getElementById('cage-note');
    if(noteEl) noteEl.style.color = (design.pattern === 'loops') ? '#ff8a7a' : '';
  }

  // ---- Silhouette mode: Symmetrical (curve editor) vs Unsymmetrical/3D (cage) ----
  function activateSilMode(name){
    document.querySelectorAll('.sil-mode-btn').forEach(function(btn){
      btn.classList.toggle('active', btn.getAttribute('data-silmode') === name);
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
      persistDesign();
      schedulePreview();
      refreshShapeCage();
    });
  })();

  // ---- live wave-slope readout --------------------------------------------
  // Peak wall slope = amp(t) * z_waves / (radius * silhouette(t)). Empirical
  // print-quality ceiling on this machine: ~0.25 (about 14 deg). The probe
  // limit caps amplitude; THIS caps printability - waves steeper than it
  // collapsed above half height on the 2026-07-05 test print.
  var SLOPE_LIMIT = 0.25;
  function lerpProfile(prof, t){
    for(var i=1;i<prof.length;i++){
      if(t <= prof[i][0]){
        var t0=prof[i-1][0], v0=prof[i-1][1], t1=prof[i][0], v1=prof[i][1];
        return t1-t0 < 1e-9 ? v1 : v0 + (t-t0)/(t1-t0)*(v1-v0);
      }
    }
    return prof[prof.length-1][1];
  }
  function updateSlope(){
    var el = document.getElementById('slope-read');
    if(!el) return;
    var waves = Math.round(design.z_waves);
    var radius = design.radius;
    var amp = ampEditor.profile(), sil = silEditor.profile();
    var peak = 0;
    for(var t=0; t<=1.0001; t+=0.02){
      var s = lerpProfile(amp, t) * waves / Math.max(radius * lerpProfile(sil, t), 1e-6);
      if(s > peak) peak = s;
    }
    var over = peak > SLOPE_LIMIT + 1e-9;
    el.textContent = 'peak wave slope: ' + peak.toFixed(2) + ' / ' + SLOPE_LIMIT.toFixed(2) +
      (over ? '  TOO STEEP - waves may collapse' : '  ok');
    el.style.color = over ? '#e0654f' : '#5fd08a';
  }

  // ---- bind inputs to design state ----------------------------------------
  // Simple 2-way binding table: element id -> {field, type, show?}
  var NUM = 'number', INT = 'int', STR = 'string';

  function bindNumber(id, field, isInt){
    var el = document.getElementById(id);
    if(!el) return;
    el.value = design[field];
    el.addEventListener('input', function(){
      var v = parseFloat(el.value);
      if(Number.isNaN(v)) return;
      design[field] = isInt ? Math.round(v) : v;
      persistDesign();
      updateSlope();
      schedulePreview();
    });
  }
  function bindSelect(id, field){
    var el = document.getElementById(id);
    if(!el) return;
    if(design[field]) el.value = design[field];
    el.addEventListener('change', function(){
      design[field] = el.value;
      persistDesign();
      schedulePreview();
    });
  }

  // Shape tab.
  bindSelect('d-shape', 'shape');
  bindNumber('d-radius', 'radius');
  bindNumber('d-height', 'height');
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
      persistDesign();
    });
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

  // Blob texture controls.

  // Named presets bundled into the "Blob style" dropdown. Selecting one
  // writes every listed field into `design` in one shot; the per-turn count
  // is left alone for spacing-driven styles (spacing_mm overrides it anyway).
  var BLOB_STYLES = {
    dots:    { blob_per_turn:6,  blob_spacing_mm:0,   blob_turn_stride:2, blob_align:'stagger',
               blob_volume:3.0,  blob_dwell:400, blob_vol_start:1.0, blob_vol_end:1.0 },
    pearls:  { blob_spacing_mm:3.0, blob_turn_stride:1, blob_align:'stagger',
               blob_volume:1.2,  blob_dwell:150, blob_vol_start:1.0, blob_vol_end:1.0 },
    columns: { blob_per_turn:10, blob_spacing_mm:0,   blob_turn_stride:2, blob_align:'column',
               blob_volume:2.5,  blob_dwell:350, blob_vol_start:1.0, blob_vol_end:1.0 },
    spikes:  { blob_per_turn:5,  blob_spacing_mm:0,   blob_turn_stride:3, blob_align:'stagger',
               blob_volume:8.0,  blob_dwell:800, blob_vol_start:1.0, blob_vol_end:1.0 },
    organic: { blob_per_turn:9,  blob_spacing_mm:0,   blob_turn_stride:2, blob_align:'jitter',
               blob_jitter:0.8,  blob_volume:2.0, blob_dwell:300, blob_vol_start:0.6, blob_vol_end:1.4 }
  };

  function refreshBlobJitterRow(){
    var row = document.getElementById('row-blob-jitter');
    if(row) row.style.display = design.blob_align === 'jitter' ? '' : 'none';
  }

  // Pushes the current design.blob_* fields into their DOM controls (used on
  // style-bundle apply and whenever the whole design is reloaded).
  function updateBlobControlsFromDesign(){
    var styleSel = document.getElementById('d-blob-style');
    if(styleSel) styleSel.value = design.blob_style || 'dots';
    function setVal(id, v){ var el = document.getElementById(id); if(el) el.value = v; }
    setVal('d-blob-per-turn', design.blob_per_turn);
    setVal('d-blob-spacing', design.blob_spacing_mm);
    setVal('d-blob-stride', design.blob_turn_stride);
    var alignSel = document.getElementById('d-blob-align');
    if(alignSel) alignSel.value = design.blob_align;
    setVal('d-blob-jitter', design.blob_jitter);
    setVal('d-blob-volume', design.blob_volume);
    setVal('d-blob-volstart', design.blob_vol_start);
    setVal('d-blob-volend', design.blob_vol_end);
    setVal('d-blob-dwell', design.blob_dwell);
    setVal('d-blob-fadein', design.blob_fade_in);
    setVal('d-blob-fadeout', design.blob_fade_out);
    refreshBlobJitterRow();
  }

  function applyBlobStyle(styleName){
    design.blob_style = styleName;
    var bundle = BLOB_STYLES[styleName];
    if(bundle){
      for(var k in bundle){ if(bundle.hasOwnProperty(k)) design[k] = bundle[k]; }
    }
    updateBlobControlsFromDesign();
    persistDesign();
    schedulePreview();
  }

  // Any manual edit to an individual blob control detaches it from the
  // selected style bundle (switches the dropdown to "Custom").
  function markBlobCustom(){
    if(design.blob_style !== 'custom'){
      design.blob_style = 'custom';
      var styleSel = document.getElementById('d-blob-style');
      if(styleSel) styleSel.value = 'custom';
      persistDesign();
    }
  }
  function bindBlobNumber(id, field, isInt){
    bindNumber(id, field, isInt);
    var el = document.getElementById(id);
    if(el) el.addEventListener('input', markBlobCustom);
  }
  function bindBlobSelect(id, field){
    bindSelect(id, field);
    var el = document.getElementById(id);
    if(el) el.addEventListener('change', markBlobCustom);
  }

  (function(){
    var styleSel = document.getElementById('d-blob-style');
    if(!styleSel) return;
    styleSel.value = design.blob_style || 'dots';
    styleSel.addEventListener('change', function(){
      applyBlobStyle(styleSel.value);
    });
  })();

  bindBlobNumber('d-blob-per-turn', 'blob_per_turn', true);
  bindBlobNumber('d-blob-spacing', 'blob_spacing_mm');
  bindBlobNumber('d-blob-stride', 'blob_turn_stride', true);
  bindBlobSelect('d-blob-align', 'blob_align');
  (function(){
    var alignSel = document.getElementById('d-blob-align');
    if(alignSel) alignSel.addEventListener('change', refreshBlobJitterRow);
  })();
  bindBlobNumber('d-blob-jitter', 'blob_jitter');
  bindBlobNumber('d-blob-volume', 'blob_volume');
  bindBlobNumber('d-blob-volstart', 'blob_vol_start');
  bindBlobNumber('d-blob-volend', 'blob_vol_end');
  bindBlobNumber('d-blob-dwell', 'blob_dwell', true);
  bindBlobNumber('d-blob-fadein', 'blob_fade_in');
  bindBlobNumber('d-blob-fadeout', 'blob_fade_out');
  refreshBlobJitterRow();

  // Loop texture controls (mirrors the blob style-bundle mechanism above).

  var LOOP_STYLES = {
    tiedspikes:{ loop_spacing_mm:4.0, loop_per_turn:0, loop_align:'stagger',
                 loop_row:2.5, loop_up:3.2, loop_out:0,
                 loop_flow:1.2, loop_speed:10, loop_cuff:3,
                 loop_wave_amp:0, loop_waves:12,
                 loop_mode:'spike', loop_dwell:400, loop_lean:20, loop_coast:0.8 },
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

  function applyLoopStyle(styleName){
    design.loop_style = styleName;
    var bundle = LOOP_STYLES[styleName];
    if(bundle){
      for(var k in bundle){ if(bundle.hasOwnProperty(k)) design[k] = bundle[k]; }
    }
    updateLoopControlsFromDesign();
    persistDesign();
    schedulePreview();
  }

  // Any manual edit to an individual loop control detaches it from the
  // selected style bundle (switches the dropdown to "Custom").
  function markLoopCustom(){
    if(design.loop_style !== 'custom'){
      design.loop_style = 'custom';
      var styleSel = document.getElementById('d-loop-style');
      if(styleSel) styleSel.value = 'custom';
      persistDesign();
    }
  }
  function bindLoopNumber(id, field, isInt){
    bindNumber(id, field, isInt);
    var el = document.getElementById(id);
    if(el) el.addEventListener('input', markLoopCustom);
  }
  function bindLoopSelect(id, field){
    bindSelect(id, field);
    var el = document.getElementById(id);
    if(el) el.addEventListener('change', markLoopCustom);
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
      persistDesign();
    });
  })();

  // Print tab.
  bindSelect('d-nozzle', 'nozzle');
  bindNumber('d-speed', 'print_speed');
  bindSelect('d-filament', 'filament');
  (function(){
    var el = document.getElementById('d-lwoverride');
    if(!el) return;
    if(design.line_width != null) el.value = design.line_width;
    el.addEventListener('input', function(){
      var v = parseFloat(el.value);
      design.line_width = (el.value === '' || Number.isNaN(v)) ? null : v;
      persistDesign();
    });
  })();

  // ---- show/hide dependent rows -------------------------------------------
  function refreshShapeRows(){
    var isStar = document.getElementById('d-shape').value === 'star';
    var starRows = document.getElementById('star-rows');
    if(starRows) starRows.style.display = isStar ? '' : 'none';

    var isOpen = design.bottom === 'open';
    var baseRow = document.getElementById('row-base');
    var squishRow = document.getElementById('row-squish');
    var spacingRow = document.getElementById('row-spacing');
    if(baseRow) baseRow.style.display = isOpen ? 'none' : '';
    var baseStyleRow = document.getElementById('row-basestyle');
    if(baseStyleRow) baseStyleRow.style.display = isOpen ? 'none' : '';
    if(squishRow) squishRow.style.display = isOpen ? 'none' : '';
    var flhHint = document.getElementById('flh-hint');
    if(flhHint) flhHint.style.display = isOpen ? 'none' : '';
    if(spacingRow) spacingRow.style.display = isOpen ? 'none' : '';
  }
  document.getElementById('d-shape').addEventListener('change', refreshShapeRows);

  // Bottom type radios.
  var bottomRadios = document.querySelectorAll('input[name="d-bottom"]');
  bottomRadios.forEach(function(r){
    if(r.value === design.bottom) r.checked = true;
    r.addEventListener('change', function(){
      if(!r.checked) return;
      design.bottom = r.value;
      if(design.bottom === 'open') design.base_layers = 0;
      persistDesign();
      refreshShapeRows();
      schedulePreview();
    });
  });

  // Show pattern controls only when a texture is selected: blobs and loops
  // each have their own row group, wave patterns share #pattern-rows.
  function refreshPatternRows(){
    var val = document.getElementById('d-pattern').value;
    var waveRows = document.getElementById('pattern-rows');
    var blobRows = document.getElementById('blob-rows');
    var loopRows = document.getElementById('loop-rows');
    if(waveRows) waveRows.style.display = (val && val !== 'blobs' && val !== 'loops') ? 'block' : 'none';
    if(blobRows) blobRows.style.display = (val === 'blobs') ? 'block' : 'none';
    if(loopRows) loopRows.style.display = (val === 'loops') ? 'block' : 'none';
  }
  document.getElementById('d-pattern').addEventListener('change', refreshPatternRows);

  refreshShapeRows();
  refreshPatternRows();
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
      // Presets define a complete shape, so drop any asymmetric cage
      // deformation from the previous design (applyDesignToUI -> refreshShapeCage
      // rebuilds a neutral all-1.0 grid if the 3D cage stays enabled).
      design.cage = null;
      applyDesignToUI();
      sel.value = '';
    });
  })();

  function applyDesignToUI(){
    if(printerSel && design.printer){
      printerSel.value = design.printer;
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
    var bottomRadios = document.querySelectorAll('input[name="d-bottom"]');
    bottomRadios.forEach(function(r){ r.checked = r.value === design.bottom; });
    var nozzleEl = document.getElementById('d-nozzle');
    if(nozzleEl) nozzleEl.value = design.nozzle || '';
    var smoothEl = document.getElementById('sil-smooth');
    if(smoothEl) smoothEl.checked = !!design.radius_profile_smooth;
    // Blob + loop controls
    updateBlobControlsFromDesign();
    updateLoopControlsFromDesign();
    // Overhang flow
    var ohSlider = document.getElementById('d-overhang-k');
    if(ohSlider){ ohSlider.value = design.overhang_flow_k || 0; }
    var ohRead = document.getElementById('overhang-k-read');
    if(ohRead) ohRead.textContent = (design.overhang_flow_k || 0).toFixed(2);
    ampEditor.setProfile(design.amp_profile);
    silEditor.setProfile(design.radius_profile);
    ampEditor.draw(); silEditor.draw();
    persistDesign();
    refreshShapeRows();
    refreshPatternRows();
    updateSlope();
    schedulePreview();
    activateSilMode(design.sil_mode || 'sym');
  }

  // Save design as JSON file download.
  document.getElementById('save-design').addEventListener('click', function(){
    var data = JSON.parse(JSON.stringify(design));
    data.amp_profile = ampEditor.profile();
    data.radius_profile = silEditor.profile();
    var blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'trident_design_' + design.shape + '_' + design.height + 'mm.json';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  });

  // Load design from JSON file.
  document.getElementById('load-design').addEventListener('click', function(){
    document.getElementById('load-design-file').click();
  });
  document.getElementById('load-design-file').addEventListener('change', function(e){
    if(!e.target.files.length) return;
    var reader = new FileReader();
    reader.onload = function(ev){
      try {
        var loaded = JSON.parse(ev.target.result);
        if(!loaded || typeof loaded !== 'object') throw new Error('not an object');
        for(var k in loaded){ if(design.hasOwnProperty(k)) design[k] = loaded[k]; }
        deriveLegacyBlobAlign(loaded);
        migrateBlobPattern();
        applyDesignToUI();
      } catch(err){
        alert('Could not load design: ' + err.message);
      }
    };
    reader.readAsText(e.target.files[0]);
    e.target.value = '';
  });

  // ---- STL mesh import (Import tab) ---------------------------------------
  var MESH_MAX_MB = 50;
  var meshState = { mesh_id: null, filename: null, info: null };

  var stlDrop = document.getElementById('stl-drop');
  var stlFile = document.getElementById('stl-file');

  stlDrop.addEventListener('click', function(){ stlFile.click(); });
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

  function uploadSTL(file){
    if(file.size > MESH_MAX_MB * 1024 * 1024){
      alert('File too large (max ' + MESH_MAX_MB + ' MB).');
      return;
    }
    var reader = new FileReader();
    reader.onload = function(e){
      fetch('/api/upload_mesh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': file.name },
        body: e.target.result
      })
      .then(function(r){ return r.json(); })
      .then(function(data){
        if(data.error){ alert('Upload failed: ' + data.error); return; }
        meshState.mesh_id = data.mesh_id;
        meshState.filename = file.name;
        meshState.info = data;
        showMeshInfo(data, file.name);
      })
      .catch(function(err){ alert('Upload failed: ' + err); });
    };
    reader.readAsArrayBuffer(file);
  }

  function showMeshInfo(data, filename){
    document.getElementById('stl-drop').style.display = 'none';
    document.getElementById('mesh-info').style.display = 'block';
    document.getElementById('mesh-name').textContent = filename;
    document.getElementById('mesh-tris').textContent = data.triangles;
    var dx = data.bounds.max[0] - data.bounds.min[0];
    var dy = data.bounds.max[1] - data.bounds.min[1];
    document.getElementById('mesh-size').textContent = dx.toFixed(1) + ' x ' + dy.toFixed(1);
    document.getElementById('mesh-height').textContent = data.height.toFixed(1);
  }

  document.getElementById('mesh-clear').addEventListener('click', function(){
    meshState.mesh_id = null;
    meshState.filename = null;
    meshState.info = null;
    document.getElementById('stl-drop').style.display = '';
    document.getElementById('mesh-info').style.display = 'none';
    stlFile.value = '';
  });

  // ---- generate ----------------------------------------------------------
  var genBtn = document.getElementById('gen-btn');
  var statusEl = document.getElementById('gen-status');
  var reportEl = document.getElementById('gen-report');
  var dlBtn = document.getElementById('dl-btn');
  var lastGcode = null, lastName = null;

  genBtn.addEventListener('click', function(){
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
      base_layers: design.bottom === 'open' ? 0 : Math.round(design.base_layers),
      brim: Math.round(design.brim),
      squish: design.squish,
      first_layer_height: design.first_layer_height || 0,
      spine_mm: design.spine_mm || 0,
      spine_deg: design.spine_deg || 0,
      ovality: design.ovality || 0,
      base_style: design.base_style || 'spiral',
      skirt: Math.round(design.skirt || 0),
      first_layer_spacing_factor: design.spacing_factor,
      print_speed: design.print_speed,
      filament: design.filament || null,
      amp_profile: ampEditor.profile(),
      radius_profile: silEditor.profile(),
      radius_profile_smooth: !!design.radius_profile_smooth,
      pattern: null,
      overhang_flow_k: design.overhang_flow_k || 0,
      nozzle: design.nozzle || null
    };
    if(design.cage && design.cage.some(function(r){ return r.some(function(v){ return Math.abs(v-1) > 1e-6; }); })){
      body.cage = design.cage;
    }
    // Route texture params by the pattern dropdown: blobs and loops are
    // site-based textures (server pattern stays null), wave patterns send
    // the pattern_* fields (and only those get pattern_alternate).
    var patternVal = design.pattern || '';
    if(patternVal === 'blobs'){
      body.blob_per_turn = design.blob_per_turn;
      body.blob_spacing_mm = design.blob_spacing_mm;
      body.blob_turn_stride = design.blob_turn_stride;
      body.blob_align = design.blob_align;
      body.blob_jitter = design.blob_jitter;
      body.blob_volume = design.blob_volume;
      body.blob_vol_start = design.blob_vol_start;
      body.blob_vol_end = design.blob_vol_end;
      body.blob_dwell = design.blob_dwell;
      body.blob_fade_in = design.blob_fade_in;
      body.blob_fade_out = design.blob_fade_out;
    } else if(patternVal === 'loops'){
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

    genBtn.disabled = true;
    statusEl.className = ''; statusEl.textContent = 'generating...';
    dlBtn.style.display = 'none';
    fetch('/api/generate', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    }).then(function(resp){
      return resp.json().then(function(j){ return {ok:resp.ok, j:j}; });
    }).then(function(res){
      genBtn.disabled = false;
      if(!res.ok){
        statusEl.className = 'err';
        statusEl.textContent = res.j && res.j.error ? res.j.error : 'generation failed';
        return;
      }
      var j = res.j;
      lastGcode = j.gcode; lastName = j.filename;
      if(window.clearPreview) window.clearPreview();
      if(window.loadGcode){ window.loadGcode(j.filename, j.gcode); }
      if(window.setAppMode) window.setAppMode('viewer');
      generatedRev = designRev;         // loaded gcode now matches the design
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
    }).catch(function(err){
      genBtn.disabled = false;
      statusEl.className = 'err';
      statusEl.textContent = 'request failed: ' + err;
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
})();
