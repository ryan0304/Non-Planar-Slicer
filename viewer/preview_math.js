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
  // layout (worldX=printerX, worldY=printerZ/height, worldZ=printerY) around
  // the call into applyPointEditsPreview() below.
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
    // worldZ=printerY, worldY=printerZ(height) -- see generatePreview()'s wx/wy/wz.
    var px = new Float64Array(n), py = new Float64Array(n), pz = new Float64Array(n);
    var i;
    for(i = 0; i < n; i++){
      px[i] = allPos[i*3];
      py[i] = allPos[i*3+2];
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
      allPos[i*3+2] = py[i];
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

  // ---- main preview generator -----------------------------------------------
  // design: the central design state object from designer.js.
  // Returns Float32Array of [x0,y0,z0, x1,y1,z1, ...] for LineSegments2.
  // Coordinates are in world space (Three.js: X = printer X - cx, Y = printer Z,
  // Z = printer Y - cy) to match the viewer's coordinate convention.
  function generatePreview(design){
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

    // Pre-allocate output: each step after the first produces a line segment
    // (2 endpoints * 3 coords = 6 floats per segment).
    var out = new Float32Array(totalSteps * 6);
    var amps = new Float32Array(totalSteps + 1);  // rate-limited amplitudes

    // Raw per-sample world positions, kept only when blob placement needs to
    // look up a specific sample index (see computeBlobPreview() below), or a
    // Point Edit Modifier needs neighbor lookups across turns (Point Smooth).
    var wantBlobs = design.pattern === 'blobs' || design.pattern === 'loops';
    var wantPointEdit = pointEditActive(design);
    var allPos = (wantBlobs || wantPointEdit) ? new Float32Array((totalSteps + 1) * 3) : null;

    var prevX = 0, prevY = 0, prevZ = 0;
    var outIdx = 0;

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

      // Asymmetry (mirrors Python paths.py): ovality squashes the section,
      // the spine offset leans the vase (linear ramp over height).
      var ov = Math.min(Math.max(design.ovality || 0, -0.4), 0.4);
      if(ov !== 0){ px *= (1 + ov); py *= (1 - ov); }
      var spineMm = design.spine_mm || 0;
      if(spineMm > 0){
        var sd = (design.spine_deg || 0) * Math.PI / 180;
        px += t * spineMm * Math.cos(sd);
        py += t * spineMm * Math.sin(sd);
      }

      // Printer Z.
      var z = t * height;
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
      // world X = printer X (centred), world Y = printer Z (up), world Z = printer Y (centred).
      // Offset to bed centre so the preview sits at the same position as the real print.
      var wx = px;   // already centred on origin; viewer bed is centred at world origin
      var wy = z;    // printer Z -> world Y (up)
      var wz = py;   // printer Y -> world Z

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

    // Trim in case of rounding (outIdx should == totalSteps * 6).
    if(outIdx < out.length){
      out = out.subarray(0, outIdx);
    }

    // Point Edit Modifiers: mutate the buffered position array, then rebuild
    // the segment list from the edited positions (can't build `out` inline
    // above -- Point Smooth needs neighbor lookups across turns that aren't
    // known yet mid-loop).
    if(wantPointEdit){
      applyPointEditsPreview(design, allPos, totalSteps + 1, pointsPerTurn);
      out = new Float32Array(totalSteps * 6);
      var oi = 0;
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
  window.cageScale = cageScale;
  // Point Edit Modifiers -- designer.js reuses buildFFDCageFromGrid() to
  // build the same cage shape it sends to /api/generate.
  window.pointEditActive = pointEditActive;
  window.applyPointEditsPreview = applyPointEditsPreview;
  window.buildFFDCageFromGrid = buildFFDCageFromGrid;
  window.ffdSample = ffdSample;
})();
