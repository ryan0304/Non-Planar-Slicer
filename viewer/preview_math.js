/* preview_math.js -- client-side spiral path generation for live draft preview.
 * Mirrors trident_gcode/paths.py spiral_path() logic so the 3D viewer can show
 * an instant preview as the user drags sliders -- no server round-trip needed.
 *
 * Plain script (not module) -- loaded before designer.js.
 */
(function(){
  'use strict';

  var TWO_PI = 2.0 * Math.PI;
  var _MAX_AMP_STEP = 0.6;
  // Wave-amplitude clamp. MUST equal the SELECTED printer's z_amp_max -- it
  // mirrors serve.py's amp_ceiling(profile), which is what the real generator
  // clamps amp_profile to. This was a hardcoded 0.95 (the Trident's own
  // measured ceiling) applied to every machine, which is the module-constant
  // machine limit CLAUDE.md forbids, and it broke the draft in the direction
  // that matters: on a Bambu declaring 4.0 the preview stopped growing at
  // 0.95, so dragging an amplitude point from 0.95 to 2.34 changed the
  // request and the printed part but NOT the picture the user was judging it
  // by. Understating is the dangerous direction here, exactly as the
  // RADIUS_SCALE note below says -- the draft's whole job is to show what the
  // server will actually generate. Overridden via setAmpMax() from
  // designer.js's applyPrinterCaps(), the same funnel every other per-printer
  // ceiling comes through; the initial value is only what stands until
  // /api/printers resolves, and the preview does not run before then.
  var AMP_MAX = 0.95;
  // Radius-envelope clamp. MUST match serve.py's RADIUS_SCALE_MIN/MAX, not the
  // narrower [0.5, 1.3] range the silhouette editor limits dragging to: the
  // preview's job is to show what the SERVER will generate. A design loaded from
  // JSON can legitimately carry values the editor itself cannot reach, and
  // clamping tighter here made the draft understate the real wall by up to 9 mm.
  var RADIUS_SCALE_MIN = 0.2, RADIUS_SCALE_MAX = 1.5;
  // Bed centre offset (printer coords -> world coords happen in the viewer).
  // Default matches the Voron Trident's 235x235 bed; designer.js overrides
  // this (via setBedSize / design.bed_center) once /api/printers resolves.
  var BED_CX = 117.5, BED_CY = 117.5;

  // Called by designer.js when the selected printer's amplitude ceiling is
  // known. Non-finite is refused rather than clamped (CLAUDE.md: every
  // comparison against NaN is False, so it would survive the min()/max() in
  // makeInterp and defeat the clamp entirely); a legitimate 0 is accepted,
  // since a printer declaring zero non-planar tolerance really does preview
  // as a flat wall.
  function setAmpMax(v){
    if(typeof v === 'number' && isFinite(v) && v >= 0) AMP_MAX = v;
  }

  // Loop-fabric ceilings for the SELECTED printer, same contract and the same
  // reasoning as AMP_MAX above: these are machine limits, so they arrive from
  // the profile (serve.py's amp_ceiling / loop_row_ceiling, surfaced as
  // loop_up_max / loop_row_max on /api/printers) and are never module
  // constants. `hasProbe` matters because loop_fabric.py gates part of its
  // wave clamp on it. The bootstrap values are the Trident's and only stand
  // until /api/printers resolves; the preview does not run before then.
  var LOOP_UP_MAX = 0.95, LOOP_ROW_MAX = 0.65, LOOP_HAS_PROBE = true;
  function setLoopCaps(upMax, rowMax, hasProbe){
    if(typeof upMax === 'number' && isFinite(upMax) && upMax >= 0) LOOP_UP_MAX = upMax;
    if(typeof rowMax === 'number' && isFinite(rowMax) && rowMax >= 0) LOOP_ROW_MAX = rowMax;
    LOOP_HAS_PROBE = !!hasProbe;
  }

  // Called by designer.js when the selected printer's bed size is known.
  function setBedSize(bedSizeX, bedSizeY){
    if(typeof bedSizeX === 'number' && bedSizeX > 0) BED_CX = bedSizeX / 2.0;
    if(typeof bedSizeY === 'number' && bedSizeY > 0) BED_CY = bedSizeY / 2.0;
  }

  // ---- triangle wave --------------------------------------------------------
  function tri(x){
    return 2.0 / Math.PI * Math.asin(Math.sin(x));
  }

  // ---- shape functions: return r(theta) ------------------------------------
  function circleShape(radius){
    return function(){ return radius; };
  }

  function starShape(radius, points, depth){
    return function(theta){
      return radius * (1.0 - depth * 0.5 * (1 - Math.cos(points * theta)));
    };
  }

  function superellipseShape(radius, n){
    return function(theta){
      var c = Math.pow(Math.abs(Math.cos(theta)), n);
      var s = Math.pow(Math.abs(Math.sin(theta)), n);
      return radius / Math.max(Math.pow(c + s, 1.0 / n), 1e-9);
    };
  }

  // ---- radial pattern library (matches Python _R_PATTERNS exactly) ----------
  var R_PATTERNS = {
    vwave:    function(a, b){ return Math.sin(a); },
    hwave:    function(a, b){ return Math.sin(b); },
    ripple:   function(a, b){ return Math.sin(a + b); },
    diamond:  function(a, b){ return tri(a + b) * tri(a - b); },
    bubbles:  function(a, b){ return Math.pow(Math.max(0, Math.sin(a) * Math.sin(b)), 1.5); },
    pleats:   function(a, b){ return tri(a); },
    hammered: function(a, b){
      return -Math.abs(
        (Math.sin(a) * Math.sin(b)
         + Math.sin(1.618 * a + 2.4) * Math.sin(0.7 * b + 1.3)
         + Math.sin(0.5347 * a + 4.0) * Math.sin(1.93 * b + 2.2)) / 1.8);
    }
  };

  // ---- fade envelope (matches Python _fade_envelope) ------------------------
  function fadeEnvelope(t, fadeIn, fadeOut){
    var e = 1.0;
    if(fadeIn > 0.0 && t < fadeIn){
      e = Math.min(e, t / fadeIn);
    }
    if(fadeOut > 0.0 && t > 1.0 - fadeOut){
      e = Math.min(e, (1.0 - t) / fadeOut);
    }
    return Math.max(0.0, e);
  }

  // ---- piecewise linear interpolation (matches Python _make_interp) ---------
  // points: array of [t, value] pairs, sorted by t.
  // Returns a function(t) -> interpolated value, clamped to [clampLo, clampHi].
  function makeInterp(points, clampLo, clampHi){
    return function(t){
      if(t <= points[0][0]) return Math.min(clampHi, Math.max(clampLo, points[0][1]));
      for(var i = 1; i < points.length; i++){
        if(t <= points[i][0]){
          var t0 = points[i-1][0], v0 = points[i-1][1];
          var t1 = points[i][0],   v1 = points[i][1];
          var frac = (t1 - t0) < 1e-9 ? 1.0 : (t - t0) / (t1 - t0);
          var v = v0 + frac * (v1 - v0);
          return Math.min(clampHi, Math.max(clampLo, v));
        }
      }
      var last = points[points.length - 1][1];
      return Math.min(clampHi, Math.max(clampLo, last));
    };
  }

  // ---- Catmull-Rom smoothed interpolation ------------------------------
  // Same control points as makeInterp, but interpolates through them with a
  // Catmull-Rom spline instead of straight line segments, for a smoother
  // profile. Falls back to clamped endpoints outside the range.
  function catmullRom(p0, p1, p2, p3, frac){
    var f2 = frac * frac, f3 = f2 * frac;
    return 0.5 * (
      (2 * p1) +
      (-p0 + p2) * frac +
      (2*p0 - 5*p1 + 4*p2 - p3) * f2 +
      (-p0 + 3*p1 - 3*p2 + p3) * f3
    );
  }
  function makeSmoothInterp(points, clampLo, clampHi){
    return function(t){
      var n = points.length;
      if(t <= points[0][0]) return Math.min(clampHi, Math.max(clampLo, points[0][1]));
      if(t >= points[n-1][0]) return Math.min(clampHi, Math.max(clampLo, points[n-1][1]));
      for(var i = 1; i < n; i++){
        if(t <= points[i][0]){
          var t0 = points[i-1][0], v0 = points[i-1][1];
          var t1 = points[i][0],   v1 = points[i][1];
          var frac = (t1 - t0) < 1e-9 ? 1.0 : (t - t0) / (t1 - t0);
          var vm1 = (i-2 >= 0) ? points[i-2][1] : v0;
          var v2  = (i+1 < n)  ? points[i+1][1] : v1;
          var v = catmullRom(vm1, v0, v1, v2, frac);
          return Math.min(clampHi, Math.max(clampLo, v));
        }
      }
      var last = points[n-1][1];
      return Math.min(clampHi, Math.max(clampLo, last));
    };
  }

  // ---- shape cage sampling (mirrors trident_gcode's server-side cage sampler
  // EXACTLY -- N x M grid of radius scale factors, rows bottom->top over
  // height, cols at bed angle theta = 2*pi*j/M, periodic in theta, cosine-
  // eased bilinear interpolation in both axes.
  function cageScale(cage, theta, t){
    var n = cage.length, m = cage[0].length;
    var ft = Math.min(Math.max(t,0),1) * (n-1);
    var i0 = Math.min(Math.floor(ft), n-2);
    var u = ft - i0; u = (1 - Math.cos(Math.PI*u))/2;
    var TAU = 2*Math.PI;
    var tau = ((theta % TAU + TAU) % TAU) / TAU * m;
    var j0 = Math.floor(tau) % m;
    var v = tau - Math.floor(tau); v = (1 - Math.cos(Math.PI*v))/2;
    var j1 = (j0+1) % m;
    var a = cage[i0][j0]*(1-v) + cage[i0][j1]*v;
    var b = cage[i0+1][j0]*(1-v) + cage[i0+1][j1]*v;
    return a*(1-u) + b*u;
  }

  // ---- Point Edit Modifiers (mirrors trident_gcode/point_edit.py exactly) --
  // Post-slice deformation of the already-generated wall polyline: mask and
  // protection gate (multiply into one 0..1 weight) how much FFD / Smooth /
  // Radial Push are allowed to move each point. Runs on PRINTER-space (x,y,z)
  // coordinates -- generatePreview() converts to/from its Three.js world-space
  // layout (worldX=printerX, worldY=printerZ/height, worldZ=-printerY, negated
  // to keep the world basis right-handed) around the call into
  // applyPointEditsPreview() below.
  var MASK_CHANNELS = {
    checker:    function(a, b){ return (Math.floor(a/Math.PI) + Math.floor(b/Math.PI)) % 2 === 0 ? 1.0 : 0.0; },
    stripes_v:  function(a, b){ return 0.5 + 0.5*Math.sin(a); },
    stripes_h:  function(a, b){ return 0.5 + 0.5*Math.sin(b); },
    rings:      function(a, b){ return 0.5 + 0.5*Math.sin(b); },
    diamond:    function(a, b){ return 0.5 + 0.5*(tri(a+b)*tri(a-b)); },
    gradient_v: function(a, b){ return ((b % TWO_PI) + TWO_PI) % TWO_PI / TWO_PI; },
    gradient_h: function(a, b){ return ((a % TWO_PI) + TWO_PI) % TWO_PI / TWO_PI; },
    radial:     function(a, b){ return 0.5 + 0.5*Math.sin(a); }
  };

  function pointMaskWeight(design, theta, t){
    if(!design.point_mask_enable) return 1.0;
    var fn = MASK_CHANNELS[design.point_mask_channel];
    if(!fn) return 1.0;
    var su = Math.max(design.point_mask_scale_u || 8, 0.001);
    var sv = Math.max(design.point_mask_scale_v || 6, 0.001);
    var a = theta * su;
    var b = TWO_PI * t * sv;
    var v = Math.min(1, Math.max(0, fn(a, b)));
    return design.point_mask_invert ? (1.0 - v) : v;
  }

  function pointProtectionWeight(design, t){
    if(!design.point_protection_enable) return 1.0;
    var w = 1.0;
    var fo = Math.max(design.point_protection_falloff || 0.08, 1e-6);
    var pb = Math.max(0, Math.min(design.point_protection_bottom || 0, 1));
    var pt = Math.max(0, Math.min(design.point_protection_top || 0, 1));
    if(pb > 0){
      if(t < pb) w = Math.min(w, 0.0);
      else if(t < pb + fo) w = Math.min(w, (t - pb) / fo);
    }
    if(pt > 0){
      var edge = 1.0 - pt;
      if(t > edge) w = Math.min(w, 0.0);
      else if(t > edge - fo) w = Math.min(w, (edge - t) / fo);
    }
    return Math.max(0.0, Math.min(1.0, w));
  }

  function pointEditGate(design, theta, t){
    return pointMaskWeight(design, theta, t) * pointProtectionWeight(design, t);
  }

  // Point FFD cage sample: cosine-eased bilinear, 3 components (dx,dy,dz) --
  // same easing as cageScale() above, generalized from 1 to 3 components.
  function ffdSample(cage, theta, t){
    var n = cage.length, m = cage[0].length;
    var ft = Math.min(Math.max(t,0),1) * (n > 1 ? (n-1) : 0);
    var i0 = n > 1 ? Math.min(Math.floor(ft), n-2) : 0;
    var i1 = Math.min(i0+1, n-1);
    var u = ft - i0; u = (1 - Math.cos(Math.PI*u))/2;
    var tau = ((theta % TWO_PI) + TWO_PI) % TWO_PI / TWO_PI * m;
    var j0 = Math.floor(tau) % m;
    var j1 = (j0+1) % m;
    var v = tau - Math.floor(tau); v = (1 - Math.cos(Math.PI*v))/2;
    function lerp3(p, q, f){
      return [p[0]+(q[0]-p[0])*f, p[1]+(q[1]-p[1])*f, p[2]+(q[2]-p[2])*f];
    }
    var a = lerp3(cage[i0][j0], cage[i0][j1], v);
    var b = lerp3(cage[i1][j0], cage[i1][j1], v);
    return lerp3(a, b, u);
  }

  // Builds a full [row][col][dx,dy,dz] FFD cage from the designer's simple
  // "single mm radial push per cell" grid -- dx,dy derived from the cell's
  // bed angle (col j at theta = 2*pi*j/cols), dz left at 0 (v1 UI scope cut;
  // the backend cage format supports arbitrary dz for future use).
  function buildFFDCageFromGrid(grid){
    var rows = grid.length, cols = grid[0].length;
    var cage = [];
    for(var i = 0; i < rows; i++){
      var row = [];
      for(var j = 0; j < cols; j++){
        var val = grid[i][j] || 0;
        var theta = TWO_PI * j / cols;
        row.push([val * Math.cos(theta), val * Math.sin(theta), 0]);
      }
      cage.push(row);
    }
    return cage;
  }

  // True only when a DEFORMING modifier (FFD / Smooth / Radial Push) is
  // active -- mask/protection alone move nothing, matching apply_point_edits()'s
  // early-return in the Python module exactly.
  function pointEditActive(design){
    return !!(
      (design.point_ffd_enable && design.point_ffd_grid) ||
      (design.point_smooth_enable && Math.round(design.point_smooth_iterations || 0) > 0) ||
      (design.point_radial_push_enable && design.point_radial_push_amp)
    );
  }

  // Mutates `allPos` (Float32Array of world-space [x,y,z, ...] triples,
  // `totalPoints` entries) in place, applying the Point Edit Modifiers stack.
  // Mirrors trident_gcode/point_edit.py's apply_point_edits() point-for-point:
  // gate = mask*protection computed once per point, then FFD, then Radial
  // Push, then Smooth (same order, same clamps, same theta/t derivation).
  function applyPointEditsPreview(design, allPos, totalPoints, pointsPerTurn){
    if(!pointEditActive(design)) return;
    var n = totalPoints, ppt = Math.max(1, pointsPerTurn);
    // Un-pack world-space -> printer-space parallel arrays: worldX=printerX,
    // worldY=printerZ(height) -- see generatePreview()'s wx/wy/wz. worldZ is
    // NEGATED printerY (wz = -py, the handedness-preserving flip), so it is
    // negated back here. This matters beyond cosmetics: the FFD cage's
    // (dx,dy) displacement is built from cos(theta)/sin(theta) at the SAME
    // theta the point itself was swept to (see buildFFDCageFromGrid /
    // ffdSample below), so `py` here must be true printer Y or a cage push
    // would shove the point in the mirror-image direction.
    var px = new Float64Array(n), py = new Float64Array(n), pz = new Float64Array(n);
    var i;
    for(i = 0; i < n; i++){
      px[i] = allPos[i*3];
      py[i] = -allPos[i*3+2];
      pz[i] = allPos[i*3+1];
    }
    var gates = new Float64Array(n);
    for(i = 0; i < n; i++){
      var t0 = i / Math.max(n-1, 1);
      var theta0 = TWO_PI * ((i % ppt) / ppt);
      gates[i] = pointEditGate(design, theta0, t0);
    }

    if(design.point_ffd_enable && design.point_ffd_grid){
      var cage = buildFFDCageFromGrid(design.point_ffd_grid);
      var ffdStrength = design.point_ffd_strength != null ? design.point_ffd_strength : 1.0;
      for(i = 0; i < n; i++){
        var gf = gates[i] * ffdStrength;
        if(gf === 0) continue;
        var t1 = i / Math.max(n-1, 1);
        var theta1 = TWO_PI * ((i % ppt) / ppt);
        var d = ffdSample(cage, theta1, t1);
        px[i] += d[0]*gf; py[i] += d[1]*gf; pz[i] += d[2]*gf;
      }
    }

    if(design.point_radial_push_enable && design.point_radial_push_amp){
      var amt = design.point_radial_push_amp *
        (design.point_radial_push_strength != null ? design.point_radial_push_strength : 1.0);
      for(i = 0; i < n; i++){
        var gp = gates[i];
        if(gp === 0) continue;
        var r = Math.hypot(px[i], py[i]);
        if(r < 1e-9) continue;
        var nx = px[i]/r, ny = py[i]/r;
        var push = amt * gp;
        px[i] += nx*push; py[i] += ny*push;
      }
    }

    if(design.point_smooth_enable && Math.round(design.point_smooth_iterations || 0) > 0){
      var smStrength = design.point_smooth_strength != null ? design.point_smooth_strength : 1.0;
      if(smStrength > 0){
        var iterations = Math.max(1, Math.min(Math.round(design.point_smooth_iterations), 12));
        var thetaAmt = Math.max(0, Math.min(design.point_smooth_theta != null ? design.point_smooth_theta : 0.5, 1)) * smStrength;
        var tAmt = Math.max(0, Math.min(design.point_smooth_t != null ? design.point_smooth_t : 0.5, 1)) * smStrength;
        for(var it = 0; it < iterations; it++){
          var nxs = px.slice(), nys = py.slice(), nzs = pz.slice();
          for(i = 0; i < n; i++){
            var gs = gates[i];
            if(gs === 0) continue;
            var turn0 = Math.floor(i/ppt) * ppt;
            var turnLen = Math.min(ppt, n - turn0);
            var lo = (i - 1 >= turn0) ? i - 1 : turn0 + turnLen - 1;
            var hi = (i + 1 < turn0 + turnLen) ? i + 1 : turn0;
            var bx = (px[lo] + px[hi]) / 2, by = (py[lo] + py[hi]) / 2, bz = (pz[lo] + pz[hi]) / 2;
            var hasT = (i - ppt >= 0 && i + ppt < n);
            var bx2, by2, bz2;
            if(hasT){
              bx2 = (px[i-ppt] + px[i+ppt]) / 2; by2 = (py[i-ppt] + py[i+ppt]) / 2; bz2 = (pz[i-ppt] + pz[i+ppt]) / 2;
            }
            var f = thetaAmt * gs;
            var f2 = hasT ? (tAmt * gs) : 0;
            // Stability: cap the combined blend weight to <=1 (convex
            // combination) -- matches trident_gcode/point_edit.py's
            // _SMOOTH_STABILITY_CAP. Without this, thetaAmt+tAmt can exceed 1
            // (strength goes up to 2x), each iteration overshoots past the
            // neighbor average, and the preview line diverges geometrically.
            var w = f + f2;
            if(w > 1.0){
              var scale = 1.0 / w;
              f *= scale; f2 *= scale;
            }
            nxs[i] = px[i] + (bx - px[i]) * f;
            nys[i] = py[i] + (by - py[i]) * f;
            nzs[i] = pz[i] + (bz - pz[i]) * f;
            if(hasT){
              nxs[i] += (bx2 - px[i]) * f2;
              nys[i] += (by2 - py[i]) * f2;
              nzs[i] += (bz2 - pz[i]) * f2;
            }
          }
          px = nxs; py = nys; pz = nzs;
        }
      }
    }

    for(i = 0; i < n; i++){
      allPos[i*3]   = px[i];
      allPos[i*3+1] = pz[i];
      allPos[i*3+2] = -py[i];   // printer Y -> world Z: re-apply the negation
    }
  }

  // ---- blob site placement (mirrors trident_gcode/blobs.py exactly) ---------
  // Deterministic pseudo-random in [0,1) from a (row, blob) pair -- same
  // formula as Python's _hash01(), used for align="jitter".
  function blobHash01(row, b){
    var x = Math.sin(row * 127.1 + b * 311.7) * 43758.5453;
    return x - Math.floor(x);
  }

  // pathInfo: { positions: Float32Array [x0,y0,z0,...] (one entry per raw
  // sample index i, same world-space mapping generatePreview() used to build
  // the segment list), totalPoints: positions.length/3, pointsPerTurn, radius }
  // Returns a Float32Array of [x,y,z, ...] blob site world positions, or an
  // empty Float32Array when nothing qualifies. Mirrors compute_blob_sites().
  // Resolves the generic site-placement params (per-turn count, spacing,
  // stride, alignment, jitter, fades) for whichever site-based texture is
  // active. Blobs and loops share the exact same placement math -- only the
  // field names and per-turn cap differ -- so both route through here.
  function resolveSitePlacement(design){
    if(!design) return null;
    if(design.pattern === 'blobs'){
      return {
        perTurn: design.blob_per_turn, spacingMm: design.blob_spacing_mm,
        turnStride: design.blob_turn_stride, align: design.blob_align,
        legacyStagger: design.blob_stagger, jitter: design.blob_jitter,
        fadeIn: design.blob_fade_in, fadeOut: design.blob_fade_out,
        maxPerTurn: 72
      };
    }
    if(design.pattern === 'loops'){
      return {
        perTurn: design.loop_per_turn, spacingMm: design.loop_spacing_mm,
        turnStride: design.loop_turn_stride, align: design.loop_align,
        legacyStagger: null, jitter: design.loop_jitter,
        fadeIn: design.loop_fade_in, fadeOut: design.loop_fade_out,
        maxPerTurn: 48
      };
    }
    return null;
  }

  function computeBlobPreview(design, pathInfo){
    var siteParams = resolveSitePlacement(design);
    if(!siteParams || !pathInfo) return null;
    var positions = pathInfo.positions;
    var totalPoints = pathInfo.totalPoints | 0;
    var pointsPerTurn = pathInfo.pointsPerTurn | 0;
    if(!positions || totalPoints <= 0 || pointsPerTurn <= 0) return new Float32Array(0);

    // blobs_per_turn: spacing_mm (when > 0) overrides the explicit per-turn count.
    var blobsPerTurn;
    var spacingMm = siteParams.spacingMm || 0;
    if(spacingMm > 0 && pathInfo.radius){
      var circumference = TWO_PI * pathInfo.radius;
      blobsPerTurn = Math.round(circumference / Math.max(spacingMm, 1.0));
    } else {
      blobsPerTurn = Math.round(siteParams.perTurn || 0);
    }
    if(blobsPerTurn <= 0) return new Float32Array(0);
    blobsPerTurn = Math.max(1, Math.min(blobsPerTurn, siteParams.maxPerTurn));

    var turnStride = Math.max(1, Math.round(siteParams.turnStride || 1));
    var align = siteParams.align;
    if(align !== 'stagger' && align !== 'column' && align !== 'jitter'){
      align = siteParams.legacyStagger ? 'stagger' : 'column';
    }
    var jitter = Math.max(0.0, Math.min(siteParams.jitter != null ? siteParams.jitter : 0.5, 1.0));
    var fadeIn = Math.max(0.0, Math.min(siteParams.fadeIn || 0, 0.5));
    var fadeOut = Math.max(0.0, Math.min(siteParams.fadeOut || 0, 0.5));

    var pitch = 1.0 / blobsPerTurn;
    var totalTurns = Math.floor(totalPoints / pointsPerTurn);
    var seen = {};
    var siteIdx = [];

    for(var t = 0; t < totalTurns; t++){
      if(t % turnStride !== 0) continue;
      var row = Math.floor(t / turnStride);
      for(var b = 0; b < blobsPerTurn; b++){
        var angleFrac = b * pitch;
        if(align === 'stagger' && (row % 2 === 1)){
          angleFrac += 0.5 * pitch;
        } else if(align === 'jitter'){
          var h = blobHash01(row, b);
          angleFrac += (h - 0.5) * jitter * pitch;
          angleFrac = angleFrac - Math.floor(angleFrac);  // Python's %= 1.0
        }
        var idx = t * pointsPerTurn + Math.floor(angleFrac * pointsPerTurn);
        if(idx <= 0 || idx >= totalPoints - 2) continue;
        var heightFrac = idx / totalPoints;
        if(heightFrac < fadeIn || heightFrac > 1.0 - fadeOut) continue;
        if(seen[idx]) continue;
        seen[idx] = true;
        siteIdx.push(idx);
      }
    }

    var out = new Float32Array(siteIdx.length * 3);
    for(var i = 0; i < siteIdx.length; i++){
      var p3 = siteIdx[i] * 3;
      out[i*3]   = positions[p3];
      out[i*3+1] = positions[p3+1];
      out[i*3+2] = positions[p3+2];
    }
    return out;
  }

  // ---- base disk builders (mirror trident_gcode/generators/base_fill.py's
  // base_disk_points/base_disk_concentric EXACTLY) --------------------------
  // The base disk's radius function follows the WALL's OWN t=0 (bottom)
  // cross-section -- shapeFn(theta) scaled by the radius envelope and cage AT
  // t=0 -- not the raw, un-enveloped shape outline. It has to: the base's
  // last printed pass ends exactly where the wall spiral begins, so the two
  // must agree on where "the outline" is. Mirrors
  // continuous_spiral.py's _base_t0_footprint(): before that fix, a
  // radius_profile of [[0,0.5],[1,1.0]] (wall starts at half radius, flares
  // to full) left the base disks printing to the FULL un-enveloped outline
  // (measured max radius 30.00mm on a radius=30 design) while the wall's own
  // first point sat at 15.09mm -- joined by a single extruding move dragging
  // ~19x normal E straight across the gap, over the top of the finished
  // disk. Ovality (an affine squash of the produced x/y, not a radial
  // scale -- see appendSpiralDisk/appendConcentricDisk's `ov` parameter) is
  // applied the same way here as it is on the wall below; the spine offset
  // is not, because it is always (0, 0) at t=0.
  function meanRadius(radiusFn, samples){
    samples = samples || 180;
    var sum = 0;
    for(var k = 0; k < samples; k++) sum += radiusFn(TWO_PI * k / samples);
    return sum / samples;
  }

  // Appends one spiral-style disk's segments (world-space [x,y,z] sextuples)
  // to `segs`. Mirrors base_disk_points(): an outline-warped Archimedean
  // spiral. `nLoops` is the REAL fill density and is never reduced for
  // performance; only the per-loop point count `pptDisk` is budgeted (see the
  // caller). Deliberately does not connect to whatever was appended before or
  // after it in `segs` -- disks sit at different Z, and a straight line
  // between the last point of one disk and the first of the next would draw
  // a bogus diagonal cutting through the part.
  // `ov` is the same clamped [-0.4, 0.4] ovality factor the wall loop below
  // applies: printer x *= (1+ov), printer y *= (1-ov). Since world z = -(printer
  // y), squashing printer y by (1-ov) is exactly squashing world z by (1-ov)
  // too -- no sign correction needed.
  function appendSpiralDisk(segs, radiusFn, nLoops, worldY, pptDisk, ov){
    var total = nLoops * pptDisk;
    var prevX, prevZ;
    for(var i = 0; i <= total; i++){
      var f = i / total;
      var theta = TWO_PI * nLoops * f;
      var r = radiusFn(theta) * f;
      var x = r * Math.cos(theta);
      var z = -(r * Math.sin(theta));   // printer Y -> world Z, negated
      if(ov){ x *= (1 + ov); z *= (1 - ov); }
      if(i > 0) segs.push(prevX, worldY, prevZ, x, worldY, z);
      prevX = x; prevZ = z;
    }
  }

  // Appends one concentric-style disk's segments. Mirrors
  // base_disk_concentric(): closed rings stepping inward from the outline,
  // each ring's point count proportional to its radius (matches the
  // reference's rationale: a fixed count would put ~0.02mm steps on the tiny
  // near-centre rings).
  function appendConcentricDisk(segs, radiusFn, nLoops, step, worldY, pptDisk, ov){
    for(var ring = 0; ring < nLoops; ring++){
      var scale = 1.0 - (nLoops - 1 - ring) * step;
      if(scale <= 1e-6) continue;
      var nPts = Math.max(12, Math.min(pptDisk, Math.round(pptDisk * scale)));
      var prevX, prevZ;
      for(var j = 0; j <= nPts; j++){
        var theta = TWO_PI * (j / nPts);
        var r = radiusFn(theta) * scale;
        var x = r * Math.cos(theta);
        var z = -(r * Math.sin(theta));
        if(ov){ x *= (1 + ov); z *= (1 - ov); }
        if(j > 0) segs.push(prevX, worldY, prevZ, x, worldY, z);
        prevX = x; prevZ = z;
      }
    }
  }

  // ---- loop fabric preview ---------------------------------------------------
  // Mirrors trident_gcode/generators/loop_fabric.py. Loop fabric is NOT the
  // parametric wall with something sprinkled on it -- it REPLACES the wall
  // (serve.py: "loop fabric replaces the wall entirely"), so it cannot be
  // previewed as a spiral plus decoration. It was, though: the draft drew the
  // ordinary vase helix and marked blob-style dots at the stitch sites, which
  // meant every loop style previewed as the same smooth vase, and none of them
  // resembled the knitted rows the G-code actually contains.
  //
  // What the generator emits, and therefore what this draws:
  //   1. a solid anchor cuff -- one flat priming ring at z0, then a vase-mode
  //      helix climbing cuff_turns * cuff_lh;
  //   2. n_rows PLANAR rows (level rings at row_mm pitch, not a helix), each
  //      of `stitches` loop stitches, in dip mode (nozzle dives below the row
  //      line, self-crossing) or spike mode (a leaning bezier arch standing
  //      above it);
  //   3. a 2-turn closing rim.
  //
  // Deliberately NOT applied here, because build_loop_fabric() does not take
  // them: ovality, spine lean, xy/z twist, z-waves and the radial pattern
  // functions. It takes shape, radius_envelope and cage only -- so those are
  // all this consults. (The old shared-wall preview applied ovality and the
  // spine to loop designs, which the print never had.)
  //
  // Segment budget: full fidelity is n_rows * stitches * 12 segments, which on
  // a tall fine-knit design runs past 50k. SEGS_PER_STITCH steps down to keep
  // the draft responsive; it never drops below 6, where the self-crossing that
  // makes a loop read as a loop would start to disappear. Row COUNT and stitch
  // COUNT -- the things that decide what the fabric actually looks like -- are
  // never reduced, the same principle as the base disks' pptDisk budget above.
  var _LOOP_Q = 0.30;          // matches loop_fabric.py's _LOOP_Q
  var _CUFF_HOOK_MM = 0.8;     // matches loop_fabric.py's _CUFF_HOOK_MM
  var _LOOP_SEG_BUDGET = 60000;

  // Resolves design.loop_* through the SAME clamps the server applies, in the
  // same order: _parse_loop_spec() first (per-turn count from spacing, row/up
  // against this machine's ceilings), then build_loop_fabric()'s own
  // interlock and z_amp_max rules. Returns null when loops are off.
  function resolveLoopFabric(design, radius){
    var perTurn = Math.round(design.loop_per_turn || 0);
    var spacingMm = design.loop_spacing_mm || 0;
    if(spacingMm > 0 && radius){
      perTurn = Math.round(TWO_PI * radius / Math.max(spacingMm, 1.0));
    }
    if(perTurn <= 0) return null;
    perTurn = Math.max(1, Math.min(perTurn, 48));

    var align = design.loop_align;
    if(align !== 'stagger' && align !== 'column' && align !== 'jitter') align = 'column';
    var mode = (design.loop_mode === 'spike') ? 'spike' : 'dip';

    // _parse_loop_spec's floors are min(1.0, cap) -- on a machine whose cap is
    // below 1.0 (the Trident: 0.65 row / 0.95 loop) floor and ceiling collapse
    // onto the same number, and every style really does get the same row.
    var capUp = LOOP_UP_MAX, capRow = LOOP_ROW_MAX;
    var rowMm = Math.max(Math.min(1.0, capRow),
                         Math.min(numOr(design.loop_row, 2.5), capRow));
    var upMm = Math.max(Math.min(1.0, capUp),
                        Math.min(numOr(design.loop_up, 3.5), capUp));
    var outMm = Math.max(0.0, Math.min(numOr(design.loop_out, 0.5), 5.0));
    var waveAmp = Math.max(0.0, Math.min(numOr(design.loop_wave_amp, 0.0), 3.0));
    var nWaves = Math.max(1, Math.min(Math.round(numOr(design.loop_waves, 12)), 40));
    var cuffTurns = Math.max(1, Math.min(Math.round(numOr(design.loop_cuff, 3)), 10));
    var leanDeg = Math.max(0.0, Math.min(numOr(design.loop_lean, 20.0), 45.0));

    // ---- build_loop_fabric()'s own resolution, line for line ---------------
    var stitches = Math.max(4, perTurn);
    rowMm = Math.max(0.5, rowMm);
    var loopH = Math.max(rowMm + 0.3, upMm);   // must exceed pitch to interlock
    var zCap = LOOP_UP_MAX;
    var zClamped = false;
    if(mode === 'spike'){
      // Spikes stand ABOVE the line, so only (loopH - rowMm) is exposed to a
      // trailing probe; a probe-less machine rides free here.
      if(LOOP_HAS_PROBE && (loopH - rowMm) > zCap){
        loopH = rowMm + zCap;
        zClamped = true;
      }
      if(waveAmp > 0.0 && LOOP_HAS_PROBE && (loopH - rowMm) + 2.0 * waveAmp > zCap){
        waveAmp = Math.max(0.0, (zCap - (loopH - rowMm)) / 2.0);
        zClamped = true;
      }
    } else {
      // Dip mode descends the full loop height below the line.
      if(loopH > zCap){
        loopH = zCap;
        rowMm = Math.min(rowMm, Math.max(loopH - 0.3, 0.4));
        zClamped = true;
      }
      if(waveAmp > 0.0 && LOOP_HAS_PROBE && loopH + 2.0 * waveAmp > zCap){
        waveAmp = Math.max(0.0, (zCap - loopH) / 2.0);
        zClamped = true;
      }
    }
    return {
      stitches: stitches, rowMm: rowMm, loopH: loopH, outMm: outMm,
      waveAmp: waveAmp, nWaves: nWaves, cuffTurns: cuffTurns,
      mode: mode, leanDeg: leanDeg,
      stagger: align !== 'column',
      cuffHook: Math.min(_CUFF_HOOK_MM, 0.4 * loopH),
      zClamped: zClamped
    };
  }
  function numOr(v, dflt){
    return (typeof v === 'number' && isFinite(v)) ? v : dflt;
  }

  // Builds the whole fabric as world-space line segments. `radiusAt(theta, z)`
  // mirrors loop_fabric.py's _radius(): shape * envelope * cage, sampled at the
  // height fraction of the point being emitted -- NOT at a fixed t.
  function buildLoopFabricPreview(design, lf, radiusAt, height, cuffLh, squish, nozzle){
    var segs = [];
    // strand_h = clamp(nozzle_diameter, 0.3, 0.6) -- the bead is decoupled from
    // the (several-mm) row pitch, exactly as the generator does it.
    var strandH = Math.min(Math.max(numOr(nozzle, 0.4), 0.3), 0.6);
    var stitches = lf.stitches, rowMm = lf.rowMm, loopH = lf.loopH;
    var waveAmp = lf.waveAmp, nWaves = lf.nWaves;

    // ---- vertical layout (loop_fabric.py's own) ---------------------------
    var z0 = squish * cuffLh;
    var cuffTop = z0 + lf.cuffTurns * cuffLh;
    var line0 = (lf.mode === 'spike')
      ? cuffTop + strandH + waveAmp
      : cuffTop + loopH - lf.cuffHook + waveAmp;
    // Math.trunc, not floor: Python's int() truncates toward zero, and this
    // expression goes negative on a design shorter than its own cuff.
    var nRows = Math.max(1, Math.trunc((height - line0) / rowMm) + 1);
    lf.nRows = nRows;   // reported back through __loopFabricPreview

    // Segment budget (see the note above): trade samples per stitch, never
    // rows or stitches.
    var segsPerStitch = 12;
    while(segsPerStitch > 6 && nRows * stitches * segsPerStitch > _LOOP_SEG_BUDGET){
      segsPerStitch -= 2;
    }
    // Cuff and rim are plain rings; 120/turn matches the wall preview's
    // sampling (the generator uses 240, which only smooths the same circle).
    var ppt = 120;

    var prevX = 0, prevY = 0, prevZ = 0, have = false;
    function moveTo(th, rz, r){
      prevX = r * Math.cos(th); prevY = rz; prevZ = -(r * Math.sin(th));
      have = true;
    }
    function lineTo(th, rz, r){
      var x = r * Math.cos(th), y = rz, z = -(r * Math.sin(th));
      if(have) segs.push(prevX, prevY, prevZ, x, y, z);
      prevX = x; prevY = y; prevZ = z; have = true;
    }

    // ---- 1. anchor cuff: one flat ring at z0, then a helix to cuff_top ----
    moveTo(0.0, z0, radiusAt(0.0, z0));
    var cuffPts = (lf.cuffTurns + 1) * ppt;
    for(var i = 1; i <= cuffPts; i++){
      var s = i / ppt;                       // turns completed
      var thC = TWO_PI * (i % ppt) / ppt;
      var zC = z0 + Math.max(0.0, s - 1.0) * cuffLh;
      lineTo(thC, zC, radiusAt(thC, zC));
    }

    // ---- 2. fabric rows ----------------------------------------------------
    var dtheta = TWO_PI / stitches;
    function lineZ(row, th){
      var lz = line0 + row * rowMm;
      if(waveAmp > 0.0){
        // Alternate rows wave in opposite phase so crossing rows meet at the
        // nodes (the zigzag weave).
        lz += waveAmp * Math.sin(nWaves * th + Math.PI * (row % 2));
      }
      return lz;
    }
    for(var row = 0; row < nRows; row++){
      var phase = (lf.stagger && row % 2 === 1) ? 0.5 * dtheta : 0.0;
      for(var k = 0; k < stitches; k++){
        var theta0 = k * dtheta + phase;
        if(lf.mode === 'spike'){
          emitSpikeStitch(lineTo, radiusAt, lf, theta0, dtheta, row, rowMm,
                          loopH, line0, lineZ);
        } else {
          for(var j = 1; j <= segsPerStitch; j++){
            var u = j / segsPerStitch;
            var bell = (1.0 - Math.cos(TWO_PI * u)) / 2.0;      // 0..1..0
            // The sin term drives theta BACKWARD near the stitch ends, so the
            // strand crosses itself at the row line -- a real loop, not a
            // zigzag. This is the whole reason the fabric interlocks.
            var thS = theta0 + dtheta * (u - _LOOP_Q * Math.sin(TWO_PI * u));
            var lz = lineZ(row, thS);
            lineTo(thS, lz - loopH * bell,
                   radiusAt(thS, lz) + lf.outMm * bell);
          }
        }
      }
    }

    // ---- 3. closing rim ("bind-off"): 2 plain rings -----------------------
    var lastLine = line0 + (nRows - 1) * rowMm;
    var rim0 = lastLine + (lf.mode === 'spike' ? rowMm : strandH);
    for(var turn = 0; turn < 2; turn++){
      var rz = rim0 + turn * strandH;
      for(var ri = 1; ri <= ppt; ri++){
        var thR = TWO_PI * ri / ppt;
        var rzI = rz;
        if(waveAmp > 0.0 && lf.mode === 'spike' && turn === 0){
          // The first rim ring follows the last row's wave so it stays in
          // contact with the (wavy) spike tips all the way round.
          rzI = rz + waveAmp * Math.sin(nWaves * thR + Math.PI * ((nRows - 1) % 2));
        }
        lineTo(thR, rzI, radiusAt(thR, rzI));
      }
    }

    var out = new Float32Array(segs.length);
    for(var oi = 0; oi < segs.length; oi++) out[oi] = segs[oi];
    return out;
  }

  // One spike stitch: the rounded bezier arch from loop_fabric.py -- pull up
  // diagonally past the target, curve over the crown, come down vertically to
  // stick the landing, then run along the row line to the next stitch.
  // Sampled at t = j/10 for j = 1..10, apex at j = 5, exactly as emitted.
  function emitSpikeStitch(lineTo, radiusAt, lf, theta0, dtheta, row, rowMm,
                           H, line0, lineZ){
    var rHere = radiusAt(theta0, line0 + row * rowMm);
    var stitchLen = Math.max(dtheta * rHere, 1e-6);
    var uLean = Math.tan(lf.leanDeg * Math.PI / 180) * 1.45 * H / stitchLen;
    uLean = Math.min(Math.max(uLean, 0.08), 0.45);
    var landU = Math.min(Math.max(2.2 * uLean + 0.15, 0.30), 0.70);
    var c1u = uLean, c1z = 1.45 * H;      // pull up diagonally, higher
    var c2u = landU, c2z = 1.05 * H;      // directly above the drop point
    var p3u = landU;
    // The first row keeps a gentler drop; later rows plunge deeper after the
    // crown to lock into the taller loop below.
    var dropDepth = (row === 0 ? 0.18 * H : 0.34 * H);
    for(var j = 1; j <= 10; j++){
      var t = j / 10.0, mt = 1.0 - t;
      var b1 = 3.0*mt*mt*t, b2 = 3.0*mt*t*t, b3 = t*t*t;
      var u = b1*c1u + b2*c2u + b3*p3u;   // p0u = p3u term folded in above
      var z = b1*c1z + b2*c2z;            // p0z = p3z = 0
      if(j > 5) z -= dropDepth * (j - 5) / 5.0;
      var th = theta0 + dtheta * u;
      var lz = lineZ(row, th);
      // Radial lean toward the next row's path so it lands square on the tie.
      var rNext = radiusAt(th, lz + rowMm);
      var rHere2 = radiusAt(th, lz);
      var radialExtra = (rNext - rHere2 + lf.outMm) * Math.min(Math.max(z / H, 0.0), 1.0);
      lineTo(th, lz + z, rHere2 + radialExtra);
    }
    // Run along the row line to the next stitch.
    for(var m = 1; m <= 3; m++){
      var uu = landU + (1.0 - landU) * m / 3.0;
      var th2 = theta0 + dtheta * uu;
      var lz2 = lineZ(row, th2);
      lineTo(th2, lz2, radiusAt(th2, lz2));
    }
  }

  // ---- main preview generator -----------------------------------------------
  // design: the central design state object from designer.js.
  // baseSpec: { base_layers, brim, skirt } -- what will ACTUALLY be sent to
  // /api/generate, per designer.js's effectiveBaseSpec(). Missing/omitted
  // defaults to "no base": effectiveBaseSpec() is the single source of truth
  // for that decision (Bottom=Open and loop-fabric both zero it out
  // independently of the raw design.base_layers/brim fields), so a caller
  // that forgets to pass it must never have the draft promise a base the
  // request will not carry.
  // Returns Float32Array of [x0,y0,z0, x1,y1,z1, ...] for LineSegments2.
  // Coordinates are in world space (Three.js: X = printer X - cx, Y = printer Z,
  // Z = cy - printer Y) to match the viewer's coordinate convention. Printer Y
  // is NEGATED (not just offset) so the world basis stays right-handed --
  // otherwise the swap of two axes (Y-up <- Z-up) is a reflection, and any
  // chiral geometry (e.g. xy_twist) would preview mirrored from what prints.
  // parseGcode() in viewer.js uses the identical transform; see its comment.
  function generatePreview(design, baseSpec){
    var spec = baseSpec || { base_layers: 0, brim: 0 };

    // Bed centre: prefer an explicit per-design override (set by designer.js
    // from the /api/printers response), else fall back to the last printer
    // set via setBedSize(), else the Trident default.
    var bedCx = BED_CX, bedCy = BED_CY;
    if(design.bed_center && design.bed_center.length === 2){
      bedCx = design.bed_center[0];
      bedCy = design.bed_center[1];
    }

    // Build shape function.
    var radius = design.radius;
    var shapeFn;
    if(design.shape === 'star'){
      shapeFn = starShape(radius, Math.round(design.star_points || 5), design.star_depth || 0.35);
    } else if(design.shape === 'square'){
      shapeFn = superellipseShape(radius, 4.0);
    } else {
      shapeFn = circleShape(radius);
    }

    // Build amplitude envelope from amp_profile.
    var ampFn = makeInterp(design.amp_profile, 0, AMP_MAX);

    // Build radius envelope from radius_profile (optionally Catmull-Rom smoothed).
    var radFn = design.radius_profile_smooth
      ? makeSmoothInterp(design.radius_profile, RADIUS_SCALE_MIN, RADIUS_SCALE_MAX)
      : makeInterp(design.radius_profile, RADIUS_SCALE_MIN, RADIUS_SCALE_MAX);

    var height = design.height;
    var layerHeight = design.layer_height;
    var zWaves = Math.round(design.z_waves);
    var xyTwist = design.xy_twist || 0;
    var zTwist = design.z_twist || 0;

    // ---- first-layer adhesion math (mirrors continuous_spiral.py exactly) --
    // Only the disk FILL DENSITY (loop pitch) below depends on the assumed
    // 0.4mm nozzle fallback -- the disk's outer EXTENT is radiusFn's exact
    // outline either way, so a wrong nozzle guess never moves the pancake's
    // edge, only how many loops pave it.
    var lw = (typeof design.line_width === 'number' && design.line_width > 0)
      ? design.line_width
      : Math.round((design.nozzle || 0.4) * 1.125 * 1000) / 1000;  // serve.py:729-730
    var firstLayerSquish = Math.min(Math.max(
      (design.first_layer_height > 0)
        ? design.first_layer_height / Math.max(layerHeight, 1e-6)
        : (design.squish != null ? design.squish : 0.75),
      0.5), 1.0);
    var spacingFactor = Math.max(0.8, Math.min(
      design.spacing_factor != null ? design.spacing_factor : 1.25, 1.5));
    var baseLayers = Math.max(0, Math.round(spec.base_layers || 0));
    var brimLoops = Math.max(0, Math.round(spec.brim || 0));
    // Server's OR also includes skirt_loops > 0 (continuous_spiral.py:201-202).
    // Omitted here on purpose, not by oversight: skirt is a known preview
    // omission (never drawn, see below), AND including/excluding it can never
    // change squish/base_z/wall_off numerically -- squish only ends up below
    // 1.0 when first_layer_squish already is, which is already one of these
    // OR terms.
    var adhesion = (firstLayerSquish < 1.0) || (baseLayers > 0) || (brimLoops > 0);
    var squish = adhesion ? firstLayerSquish : 1.0;
    var baseZ = squish * layerHeight;
    var s0 = lw / Math.max(squish, 1e-6) * Math.max(spacingFactor, 0.5);
    var wallOff = baseLayers > 0 ? (baseZ + (baseLayers - 1) * layerHeight) : baseZ;

    // ---- loop fabric: a different generator, so a different preview --------
    // serve.py routes a parametric loops design to build_loop_fabric(), which
    // REPLACES the wall. Everything below this point (base disks, the helix,
    // z-waves, patterns, point edits, blob dots) belongs to
    // build_continuous_spiral() and describes a print this design will not
    // produce, so return the fabric and stop. `loop_fabric` comes from
    // designer.js's effectiveBaseSpec() -- the same single source of truth
    // that decides the request has no base/brim/skirt -- rather than being
    // re-derived from design.pattern here, because mesh-mode loops are a
    // DIFFERENT feature (hanging loop sites on a normal profile-spiral wall)
    // that this must not hijack.
    if(spec.loop_fabric){
      var lf = resolveLoopFabric(design, radius);
      if(lf){
        // loop_fabric.py's _radius(): shape * envelope * cage at the height
        // fraction of the point being emitted. No ovality, spine or twist --
        // build_loop_fabric() is not given them.
        var radiusAt = function(theta, z){
          var t = Math.min(1.0, Math.max(0.0, z / Math.max(height, 1e-6)));
          var rr = shapeFn(theta) * radFn(t);
          if(design.cage && design.cage.length >= 2){
            rr *= cageScale(design.cage, theta, t);
          }
          return rr;
        };
        window.__blobPreviewSites = null;   // stitches are drawn, not dotted
        var fabric = buildLoopFabricPreview(design, lf, radiusAt, height,
                                            layerHeight, firstLayerSquish,
                                            design.nozzle);
        // Resolved fabric stats for the UI/tests: the numbers the print will
        // ACTUALLY use after every clamp, not the ones sitting in the panel.
        window.__loopFabricPreview = {
          rows: lf.nRows, stitches: lf.stitches, mode: lf.mode,
          row_mm: lf.rowMm, loop_mm: lf.loopH, wave_amp: lf.waveAmp,
          clamped: lf.zClamped
        };
        return fabric;
      }
      // loops selected but no sites resolve (per-turn 0 and no spacing): the
      // server returns None from _parse_loop_spec and builds the ordinary
      // wall, so fall through and preview exactly that.
      window.__loopFabricPreview = null;
    } else {
      window.__loopFabricPreview = null;
    }

    // ---- base disks: built BEFORE the wall loop, into their own plain array
    // of segment endpoints (world-space [x,y,z] sextuples) so they can be
    // dropped into the front of the final buffer without perturbing the wall
    // loop's own indexing below.
    //
    // Brim and skirt loops are a KNOWN OMISSION here, not an oversight: this
    // preview draws the wall + solid base disks only. `brimLoops` above feeds
    // only the adhesion/s0 math (matching the server); spec.skirt is not
    // consulted at all.
    var baseSegs = [];
    // Ovality is computed here (once) because the base disks need it too --
    // the wall loop below reuses this same `ov`, not a second clamp.
    var ov = Math.min(Math.max(design.ovality || 0, -0.4), 0.4);
    // The base's radius function is the wall's own t=0 footprint: shapeFn
    // scaled by the radius envelope and cage AT t=0 (see the comment above
    // appendSpiralDisk/appendConcentricDisk for why ovality is applied to the
    // produced points instead of folded in here, and why the spine offset
    // needs no term -- it is always zero at t=0).
    var baseRadiusFn = function(theta){
      var rb = shapeFn(theta) * radFn(0);
      if(design.cage && design.cage.length >= 2){
        rb *= cageScale(design.cage, theta, 0);
      }
      return rb;
    };
    // Mean radius of the t=0 footprint is the same for every disk (neither
    // shapeFn nor radFn(0)/cage change between them) -- compute it once, not
    // once per disk.
    var rMeanBase = baseLayers > 0 ? Math.max(meanRadius(baseRadiusFn), 1e-6) : 0;
    for(var bk = 0; bk < baseLayers; bk++){
      var spacingK = (bk === 0) ? s0 : lw;
      var worldYk = baseZ + bk * layerHeight;   // printer Z -> world Y (up)
      var nLoopsK = Math.max(1, Math.ceil(rMeanBase / Math.max(spacingK, 1e-6)));
      // Performance budget: cap SAMPLING density per disk, holding total
      // samples roughly constant as loop count grows -- n_loops itself (the
      // real fill density, i.e. how solid the base actually looks) is never
      // cut. The wall samples at 120 pts/turn; disks share that ceiling and a
      // 24-point floor so even a single-loop disk still reads as round.
      var pptDisk = Math.max(24, Math.min(120, Math.round(6000 / nLoopsK)));
      if(design.base_style === 'concentric'){
        appendConcentricDisk(baseSegs, baseRadiusFn, nLoopsK, spacingK / rMeanBase, worldYk, pptDisk, ov);
      } else {
        appendSpiralDisk(baseSegs, baseRadiusFn, nLoopsK, worldYk, pptDisk, ov);
      }
    }

    // Radial pattern.
    var patternName = design.pattern || '';
    var patternFn = patternName ? R_PATTERNS[patternName] : null;
    var patternAmp = design.pattern_amp || 1.0;
    var patternWaves = Math.round(design.pattern_waves || 12);
    var patternBands = design.pattern_bands || 6;
    var patternTwist = design.pattern_twist || 0;
    var patternPhase = design.pattern_phase || 0;
    var patternFadeIn = design.pattern_fade_in || 0.10;
    var patternFadeOut = design.pattern_fade_out || 0;
    var patternAlternate = !!design.pattern_alternate;

    var pointsPerTurn = 120;   // half resolution for speed
    var turns = height / layerHeight;
    var totalSteps = Math.max(2, Math.round(turns * pointsPerTurn));

    // Pre-allocate output: the base disk segments built above go first, then
    // totalSteps wall segments (2 endpoints * 3 coords = 6 floats each).
    var baseFloatCount = baseSegs.length;
    var out = new Float32Array(baseFloatCount + totalSteps * 6);
    for(var bi = 0; bi < baseFloatCount; bi++) out[bi] = baseSegs[bi];
    var amps = new Float32Array(totalSteps + 1);  // rate-limited amplitudes

    // Raw per-sample world positions, kept only when blob placement needs to
    // look up a specific sample index (see computeBlobPreview() below), or a
    // Point Edit Modifier needs neighbor lookups across turns (Point Smooth).
    // Reaching here with pattern 'loops' means loop SITES on a normal wall
    // (mesh mode -- build_profile_spiral + compute_loop_sites), not loop
    // fabric: the fabric branch above already returned. Sites really are dots
    // on a spiral, so the blob-dot rendering is the right one for them.
    var wantBlobs = design.pattern === 'blobs' || design.pattern === 'loops';
    var wantPointEdit = pointEditActive(design);
    var allPos = (wantBlobs || wantPointEdit) ? new Float32Array((totalSteps + 1) * 3) : null;

    var prevX = 0, prevY = 0, prevZ = 0;
    var outIdx = baseFloatCount;   // wall segments start after the base disks'

    for(var i = 0; i <= totalSteps; i++){
      var s = i / totalSteps;          // 0..1 over the whole print
      var t = s;                       // height fraction
      var phi = turns * TWO_PI * s;    // total swept angle
      var theta = phi % TWO_PI;        // angle around this loop

      // Rotate cross-section outline for twisted column.
      var shapeAngle = theta - xyTwist * TWO_PI * t;
      var r = shapeFn(shapeAngle);

      // Radius envelope from silhouette curve.
      r *= radFn(t);

      // Shape cage (asymmetric local deformation grid), if present.
      // Applies to parametric AND loops-fabric designs (both honor it
      // server-side); STL mode is generated from an uploaded mesh.
      if(design.cage && design.cage.length >= 2){
        r *= cageScale(design.cage, theta, t);
      }

      // Radial surface texture.
      if(patternFn){
        var a = patternWaves * theta + TWO_PI * (patternTwist * t + patternPhase);
        if(patternAlternate && Math.floor(phi / TWO_PI) % 2 === 1){
          a += Math.PI;        // odd turns swing the opposite way
        }
        var b = TWO_PI * patternBands * t;
        var env = fadeEnvelope(t, patternFadeIn, patternFadeOut);
        r += patternAmp * env * patternFn(a, b);
      }

      // Printer XY (centred on origin).
      var px = r * Math.cos(theta);
      var py = r * Math.sin(theta);

      // Asymmetry (mirrors Python paths.py): ovality squashes the section
      // (same `ov` computed once above, ahead of the base disks), the spine
      // offset leans the vase (linear ramp over height).
      if(ov !== 0){ px *= (1 + ov); py *= (1 - ov); }
      var spineMm = design.spine_mm || 0;
      if(spineMm > 0){
        var sd = (design.spine_deg || 0) * Math.PI / 180;
        px += t * spineMm * Math.cos(sd);
        py += t * spineMm * Math.sin(sd);
      }

      // Printer Z, lifted by wall_off -- the wall starts AT the top base
      // disk's level (or squish*layer_height with no base) and helixes up
      // from there, exactly like the reference emission. Drawing the wall
      // from z=0 was wrong even with no base: it always sits base_z too low.
      var z = wallOff + t * height;
      if(zWaves > 0){
        var phase = zTwist * TWO_PI * t;
        // Amplitude from the user's amp curve.
        var amp = ampFn(t);

        // Rate limiter: amplitude may only change by _MAX_AMP_STEP * layerHeight
        // per turn (matching Python exactly).
        if(i >= pointsPerTurn){
          var prevAmp = amps[i - pointsPerTurn];
          var d = _MAX_AMP_STEP * layerHeight;
          if(amp > prevAmp + d) amp = prevAmp + d;
          else if(amp < prevAmp - d) amp = prevAmp - d;
        }
        amps[i] = amp;
        z += amp * Math.sin(zWaves * theta + phase);
      } else {
        amps[i] = 0;
      }

      // Convert to Three.js world space:
      // world X = printer X (centred), world Y = printer Z (up),
      // world Z = -printer Y (centred, NEGATED to keep the basis right-handed).
      // Offset to bed centre so the preview sits at the same position as the real print.
      var wx = px;   // already centred on origin; viewer bed is centred at world origin
      var wy = z;    // printer Z -> world Y (up)
      var wz = -py;  // printer Y -> world Z (negated -- see the comment on generatePreview above)

      if(allPos){
        allPos[i*3] = wx; allPos[i*3+1] = wy; allPos[i*3+2] = wz;
      }

      if(i > 0){
        out[outIdx++] = prevX;
        out[outIdx++] = prevY;
        out[outIdx++] = prevZ;
        out[outIdx++] = wx;
        out[outIdx++] = wy;
        out[outIdx++] = wz;
      }

      prevX = wx;
      prevY = wy;
      prevZ = wz;
    }

    // Trim in case of rounding (outIdx should == baseFloatCount + totalSteps * 6).
    if(outIdx < out.length){
      out = out.subarray(0, outIdx);
    }

    // Point Edit Modifiers: mutate the buffered position array, then rebuild
    // the segment list from the edited positions (can't build `out` inline
    // above -- Point Smooth needs neighbor lookups across turns that aren't
    // known yet mid-loop).
    if(wantPointEdit){
      applyPointEditsPreview(design, allPos, totalSteps + 1, pointsPerTurn);
      // Point Edit only ever touches the WALL polyline (`allPos` holds wall
      // samples alone) -- overwrite just the wall region of `out`, leaving
      // the base disk segments already written at the front untouched.
      var oi = baseFloatCount;
      for(var si = 1; si <= totalSteps; si++){
        var a3 = (si-1)*3, b3 = si*3;
        out[oi++] = allPos[a3];   out[oi++] = allPos[a3+1]; out[oi++] = allPos[a3+2];
        out[oi++] = allPos[b3];   out[oi++] = allPos[b3+1]; out[oi++] = allPos[b3+2];
      }
    }

    // Blob dot sites, in the same world-space mapping as `out` above, for
    // viewer.js to render as points on the draft preview.
    if(wantBlobs){
      window.__blobPreviewSites = computeBlobPreview(design, {
        positions: allPos,
        totalPoints: totalSteps + 1,
        pointsPerTurn: pointsPerTurn,
        radius: radius
      });
    } else {
      window.__blobPreviewSites = null;
    }

    return out;
  }

  // Expose to global scope for designer.js and viewer.js.
  window.generatePreview = generatePreview;
  window.computeBlobPreview = computeBlobPreview;
  window.R_PATTERNS = R_PATTERNS;
  window.makeInterp = makeInterp;
  window.makeSmoothInterp = makeSmoothInterp;
  window.setPreviewBedSize = setBedSize;
  window.setPreviewAmpMax = setAmpMax;
  window.setPreviewLoopCaps = setLoopCaps;
  // Test-only, same purpose as __previewAmpMax: lets dev_smoke.html assert the
  // fabric draft clamps to the SELECTED printer rather than to a constant.
  window.__previewLoopCaps = function(){
    return { up: LOOP_UP_MAX, row: LOOP_ROW_MAX, hasProbe: LOOP_HAS_PROBE };
  };
  window.__resolveLoopFabric = resolveLoopFabric;
  // Test-only: lets viewer/dev_smoke.html assert the draft's clamp actually
  // tracks the selected printer instead of inferring it from sampled geometry.
  window.__previewAmpMax = function(){ return AMP_MAX; };
  window.cageScale = cageScale;
  // Point Edit Modifiers -- designer.js reuses buildFFDCageFromGrid() to
  // build the same cage shape it sends to /api/generate.
  window.pointEditActive = pointEditActive;
  window.applyPointEditsPreview = applyPointEditsPreview;
  window.buildFFDCageFromGrid = buildFFDCageFromGrid;
  window.ffdSample = ffdSample;
})();
