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
      ? makeSmoothInterp(design.radius_profile, 0.5, 1.3)
      : makeInterp(design.radius_profile, 0.5, 1.3);

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
    // look up a specific sample index (see computeBlobPreview() below).
    var wantBlobs = design.pattern === 'blobs' || design.pattern === 'loops';
    var allPos = wantBlobs ? new Float32Array((totalSteps + 1) * 3) : null;

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
})();
