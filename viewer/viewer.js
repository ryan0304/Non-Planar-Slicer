import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';

const BED_X = 235, BED_Y = 235, BED_Z = 160;

// Acceleration constant used for print time estimation (mm/s^2)
const ACCEL = 8000;
// Max Z-axis speed (mm/s) for time estimation cap
const MAX_Z_SPEED = 25;
// First-layer height threshold for risky-extrusion detection (mm)
const RISK_Z_MIN = 1.0;
// XY support search radius for risky detection (mm)
const RISK_XY = 1.0;
// Vertical window below segment to look for support (mm, exclusive lower, inclusive upper)
const RISK_DZ_LO = 0.2;
const RISK_DZ_HI = 2.0;
// Grid cell size for the XY bucket hash used in O(n) risk detection
const BUCKET_CELL = 2.0;

// ---- overhang colouring constants -----------------------------------------
// Vertical window below a segment to look for its nearest support point (mm).
const OVH_DZ_LO = 0.05;   // exclusive lower bound (must be at least this far below)
const OVH_DZ_HI = 2.0;    // inclusive upper bound (no deeper than this)
// Max horizontal search radius for a support point (mm). Beyond this a segment
// is treated as unsupported (bridge/air, 90 deg).
const OVH_SEARCH_R = 6.0;
// First-layer threshold: segments at/below this printer-Z are always 0 deg.
const OVH_Z_FIRST = 0.6;
// Overhang angle break points (deg): safe -> caution -> full red.
const OVH_YELLOW_DEG = 30;
const OVH_RED_DEG = 55;

// ---- scene setup ----------------------------------------------------------
const wrap = document.getElementById('canvas-wrap');
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
wrap.appendChild(renderer.domElement);

const scene = new THREE.Scene();
// Raised from 0x1c1f22 to 0x2b3036 -- the old value was close enough to
// --bg (#17191b)/--surface (#1e2124) that the viewport stopped reading as
// its own region and blended into the surrounding chrome. The reference is
// Bambu Studio's mid-grey viewport against a dark bed: that pairing separates
// MORE than a uniformly dark scene does, because the dark bed now has a
// lighter surround to sit inside instead of matching it. Still unambiguously
// a dark theme -- --bg and --surface are untouched, so the panel stays
// darker than the canvas, which is more separation again, not less.
scene.background = new THREE.Color(0x2b3036);

const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 4000);
camera.position.set(BED_X*0.9, BED_Z*1.1, BED_Y*1.3);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = false;   // on-demand rendering instead of a perpetual loop
controls.target.set(0, BED_Z*0.25, 0);

scene.add(new THREE.AmbientLight(0xffffff, 0.9));
const dir = new THREE.DirectionalLight(0xffffff, 0.6);
dir.position.set(1,2,1); scene.add(dir);

// ---- bed ------------------------------------------------------------------
// Printer X -> three X, printer Y -> three -Z (NEGATED), printer Z -> three Y
// (up). The Y negation is required, not cosmetic: swapping two axes of a
// right-handed system (Z-up -> Y-up) without also negating one of them is a
// reflection (determinant -1), which mirrors any chiral geometry (e.g. an
// xy_twist) in the preview relative to what actually prints. Origin shifted
// so bed centre sits at world origin. Every site that maps printer Y <-> world
// Z (parseGcode, fitView, computeTurns, the measure tool, the shape cage, the
// nav cube) must apply this same negation -- see parseGcode() below for the
// primary conversion.
const bedGroup = new THREE.Group(); scene.add(bedGroup);
{
  // Lifted again for the 0x2b3036 canvas background (see scene.background
  // above): 0x2a2e33 was the previous minor-line colour and is now almost
  // exactly the background colour itself -- the grid would have nearly
  // vanished. Both colours keep the same ~1.6x major/minor ratio as before.
  const grid = new THREE.GridHelper(BED_X, 23, 0x5b6572, 0x393f47);
  bedGroup.add(grid);
  // Safe-print-area outline (30-208 x 30-185 from printer.cfg).
  const ax=[30,208], ay=[30,185], cx=BED_X/2, cy=BED_Y/2;
  const c=[[ax[0],ay[0]],[ax[1],ay[0]],[ax[1],ay[1]],[ax[0],ay[1]],[ax[0],ay[0]]];
  const pts=c.map(([x,y])=>new THREE.Vector3(x-cx,0.1,cy-y));
  const ln=new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineDashedMaterial({color:0x2f6bff,dashSize:4,gapSize:3,transparent:true,opacity:.7}));
  ln.computeLineDistances(); bedGroup.add(ln);
}

// ---- Voron Trident frame / bed / gantry -----------------------------------
// A simplified but recognisable machine: 2020 aluminium frame, a moving bed
// plate, and a fixed gantry beam at the top -- sized from the build volume.
const printerGroup = new THREE.Group(); scene.add(printerGroup);
{
  const HX = BED_X/2 + 30, HZ = BED_Y/2 + 30;   // frame half-extents
  const Y_BOT = -25, Y_TOP = BED_Z + 45;         // frame bottom / top (world Y up)
  const T = 12;                                   // extrusion thickness
  // Lightened from 0x3b424c alongside the 0x2b3036 canvas background -- at
  // the old value the frame's contrast against the background nearly halved
  // (a smaller lift than the grid needed since the frame is a lit mesh, not
  // a flat colour, but still enough to start disappearing into the canvas).
  const alu = new THREE.MeshStandardMaterial({ color:0x4a5360, metalness:0.6, roughness:0.5 });
  const accent = new THREE.MeshStandardMaterial({ color:0xc0392b, metalness:0.3, roughness:0.6 });

  const beam = (x,y,z, sx,sy,sz, mat=alu) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(sx,sy,sz), mat);
    m.position.set(x,y,z); printerGroup.add(m); return m;
  };
  const H = Y_TOP - Y_BOT, MY = (Y_TOP + Y_BOT)/2;
  // 4 vertical extrusions
  for (const sx of [-HX,HX]) for (const sz of [-HZ,HZ]) beam(sx,MY,sz, T,H,T);
  // top + bottom rails along X and along Z
  for (const y of [Y_BOT,Y_TOP]) {
    for (const sz of [-HZ,HZ]) beam(0,y,sz, 2*HX,T,T);
    for (const sx of [-HX,HX]) beam(sx,y,0, T,T,2*HZ);
  }
  // moving bed plate. IMPORTANT: the PEI's TOP surface must sit exactly at
  // world Y=0 (the print's Z=0) or first layers get swallowed inside the
  // plate mesh by the depth buffer. Plate stack is built strictly below Y=0.
  // Plate materials are kept transparent so they can fade out when the camera
  // dips below the bed - otherwise the opaque boxes black out the whole model
  // when viewing from underneath.
  const plateMat = new THREE.MeshStandardMaterial({ color:0x14181f, metalness:0.2,
    roughness:0.85, transparent:true, opacity:0.95 });
  const peiMat = new THREE.MeshStandardMaterial({ color:0x2a2f1c, metalness:0.1,
    roughness:0.95, transparent:true, opacity:0.95 });
  window.__bedMats = [plateMat, peiMat];
  const plate = beam(0,-4.6,0, BED_X,8,BED_Y, plateMat);
  const pei = beam(0,-0.32,0, BED_X-6,0.6,BED_Y-6, peiMat);
  // fixed gantry beam across the top + a small toolhead block
  beam(0, Y_TOP-18, 0, 2*HX-T, T, T);                  // X gantry rail
  beam(0, Y_TOP-18, 0, T, T*1.6, T*2.4, accent);       // toolhead carriage
}

let pathObj=null, travelObj=null;

// ---- nozzle marker + print-process animation state ------------------------
const nozzle = (() => {
  const g = new THREE.Group();
  const tip = new THREE.Mesh(
    new THREE.ConeGeometry(2.4, 7, 16),
    new THREE.MeshStandardMaterial({ color:0xbfc6cf, metalness:0.7, roughness:0.35 }));
  tip.rotation.x = Math.PI;     // point the cone tip downward (to the bed)
  tip.position.y = 3.5;         // apex sits at the group origin = the print point
  g.add(tip);
  const hot = new THREE.Mesh(
    new THREE.CylinderGeometry(2.6, 2.6, 5, 16),
    new THREE.MeshStandardMaterial({ color:0xe0402f, metalness:0.3, roughness:0.6 }));
  hot.position.y = 9.5;
  g.add(hot);
  g.visible = false;
  scene.add(g);
  return g;
})();

let animSeg = 0;        // number of extrude segments in the current path
let extArr = null;      // raw world-space segment endpoints (for nozzle lookup)
let lineMat = null;     // fat-line material (for width + resolution updates)
let lineWidthPx = 2;    // on-screen path thickness (thin enough to see layers)
let progress = 1;       // 0..1 fraction of the print drawn (segment-based; master variable)
let xrayOn = false;     // X-ray view: transparent path + dimmed bed (see-through)
let playing = false;
let speedMult = 8;      // playback speed multiplier (1x = real print time), from the speed selector
let playT = 0;          // play clock in seconds, along the extrude-only segT[] timeline

// viridis-ish ramp
function ramp(t){
  const stops=[[0.17,0.23,0.56],[0.12,0.62,0.54],[0.81,0.88,0.12],[0.99,0.65,0.04],[0.88,0.25,0.18]];
  t=Math.max(0,Math.min(1,t)); const f=t*(stops.length-1); const i=Math.floor(f); const a=f-i;
  const c0=stops[i], c1=stops[Math.min(i+1,stops.length-1)];
  return [c0[0]+(c1[0]-c0[0])*a, c0[1]+(c1[1]-c0[1])*a, c0[2]+(c1[2]-c0[2])*a];
}

// ---- gcode parsing --------------------------------------------------------
const FIL_AREA = Math.PI * (1.75/2) * (1.75/2);   // mm^2 of 1.75mm filament

// Build a string key for a 2D bucket in the XY support grid.
// bx/bz are printer-space X/Y coordinates; cell is the bucket size in mm.
function bucketKey(bx, bz, cell){
  return (Math.floor(bx/cell) | 0) + ',' + (Math.floor(bz/cell) | 0);
}

// Compute per-segment risk flags using an O(n) XY grid hash.
// A segment is RISKY if its midpoint is above RISK_Z_MIN and no prior
// extruded segment midpoint lies within RISK_XY horizontally and
// within (RISK_DZ_LO, RISK_DZ_HI] mm below it in printer-Z.
//
// This count MUST agree with trident_gcode/analyze.py's unsupported-move
// counter: the stats panel here calls it "Unsupported moves" and the
// generated safety report calls it "unsupported" for the very same file, so
// two different numbers would read as a contradiction rather than two views
// of one thing. analyze.py is the authority -- its number is what the
// printed report shows -- and it defends against unbounded memory growth on
// tall prints by quantising and de-duplicating the support points it STORES
// (0.5mm XY, 0.1mm Z) into 2mm grid cells keyed by round(coord / cell), while
// still distance-testing each new QUERY midpoint unquantised. Mirror that
// exactly: quantise/dedup what we store below, never what we query, and use
// round() (not floor()) for the cell index so a point lands in the same
// bucket on both sides of the language boundary.
//
// Measured after this change, viewer vs analyze.py: 02_twisted_star
// 9679/9678, 05_aggressive_twist_star 13649/13649, surf_ripple 7020/7018,
// surf_saddle 13871/13869, surf_stl 979/978 -- was ~5% apart before (7968 vs
// 7564 on one design), now at most 2 moves in ~14000. The residual is not a
// logic difference: Python's round() is half-to-EVEN while JS Math.round() is
// half-up, so the two split differently on coordinates landing exactly on a
// .5 boundary, and bit-level float parity across the two languages is not
// worth buying. What matters is the SIGN: every measured difference has the
// viewer counting the same or MORE, never fewer, so this readout can only
// over-warn relative to the authoritative report -- if that ever inverts,
// something here has drifted and needs looking at.
// Returns a Uint8Array of length nSeg (1=risky, 0=safe).
function computeRiskFlags(ext, nSeg){
  // Grid maps "bucket_x,bucket_z" -> Map of "qx,qy,qz" -> {wx,wy,wz}, the
  // de-duplicated, quantised prior midpoints in that cell (Map dedups on the
  // quantised-string key the way analyze.py's per-cell set() does).
  const grid = new Map();
  const risk = new Uint8Array(nSeg);

  const XY_STEP = 0.5, Z_STEP = 0.1;                 // match analyze.py's storage quantisation
  const quant = (v, step) => Math.round(v / step) * step;

  for(let s = 0; s < nSeg; s++){
    const base = s * 6;
    // Midpoint in printer coordinates (not world coords).
    // ext[] stores world-space: ax=printerX-cx, ay=printerZ(=worldY), az=cy-printerY
    // (negated -- see the coordinate-mapping note near the top of this file).
    // We stored ax/ay/az for start, bx/by/bz for end.
    const mx = (ext[base+0] + ext[base+3]) * 0.5;   // world X = printerX - cx
    const mz = (ext[base+2] + ext[base+5]) * 0.5;   // world Z = cy - printerY
    // NOTE: this function only ever uses mz through hypot()/distance math below,
    // so the sign of the Y<->Z mapping does not affect its result -- confirmed,
    // no logic change needed here beyond the comment.
    const my = (ext[base+1] + ext[base+4]) * 0.5;   // world Y = printerZ

    // Only test segments above the first-layer threshold.
    if(my > RISK_Z_MIN){
      // Check neighbouring buckets in XY. Bucket index uses round(), matching
      // analyze.py's `round(mx / CELL)` exactly (NOT Math.floor: a floor-based
      // index and a round-based index can disagree on which cell a point near
      // a boundary falls into, which is the whole reason the two sides used
      // to diverge).
      const bxi = Math.round(mx / BUCKET_CELL);
      const bzi = Math.round(mz / BUCKET_CELL);
      let supported = false;

      // 3x3 neighbour-cell scan (radius 1), same as analyze.py's
      // cx in (gx-1, gx, gx+1) x cy in (gy-1, gy, gy+1).
      outer:
      for(let di = -1; di <= 1 && !supported; di++){
        for(let dj = -1; dj <= 1 && !supported; dj++){
          const key = (bxi+di) + ',' + (bzi+dj);
          const bucket = grid.get(key);
          if(!bucket) continue;
          for(const pr of bucket.values()){
            // Distance test against the RAW (unquantised) query midpoint --
            // only what we store below is quantised, exactly as analyze.py
            // quantises what it stores, not what it queries.
            const dxy = Math.hypot(pr.wx - mx, pr.wz - mz);
            if(dxy > RISK_XY) continue;
            const dz = my - pr.wy;  // positive = current is above prior
            if(dz > RISK_DZ_LO && dz <= RISK_DZ_HI){ supported = true; break; }
          }
        }
      }
      if(!supported) risk[s] = 1;
    }

    // Insert this segment's midpoint into the grid for future segments,
    // quantised (0.5mm XY, 0.1mm Z) and de-duplicated per cell -- mirrors
    // analyze.py's `grid.setdefault(key, set()).add((round(mx*2)/2, ...))`.
    const key = Math.round(mx / BUCKET_CELL) + ',' + Math.round(mz / BUCKET_CELL);
    let bucket = grid.get(key);
    if(!bucket){ bucket = new Map(); grid.set(key, bucket); }
    const qx = quant(mx, XY_STEP), qy = quant(my, Z_STEP), qz = quant(mz, XY_STEP);
    const qkey = qx + ',' + qy + ',' + qz;
    if(!bucket.has(qkey)) bucket.set(qkey, {wx: qx, wy: qy, wz: qz});
  }
  return risk;
}

// Compute a per-segment LOCAL OVERHANG ANGLE (degrees) using the same O(n) XY
// grid hash as computeRiskFlags. For each extruded segment midpoint we look
// among PRIOR segment midpoints for the nearest one (in printer-XY) whose
// printer-Z lies in (mz - OVH_DZ_HI, mz - OVH_DZ_LO] -- i.e. a support point
// just below. The overhang angle = atan2(horizontalDistance, dz), so a wall
// stacked straight up -> ~0 deg, a 45 deg lean -> 45 deg, and a strand printed
// into thin air (no support within OVH_SEARCH_R) -> 90 deg. First-layer
// segments (printer-Z <= OVH_Z_FIRST) are pinned to 0 deg. Returns a
// Float32Array of length nSeg.
function computeOverhang(ext, nSeg){
  const grid = new Map();
  const ovh = new Float32Array(nSeg);
  const NR = Math.ceil(OVH_SEARCH_R / BUCKET_CELL) + 1;

  for(let s = 0; s < nSeg; s++){
    const base = s * 6;
    // Printer coordinates: world X = printerX, world Z = cy - printerY (both
    // the horizontal plane, Y negated per the top-of-file mapping note);
    // world Y = printerZ (height). This function only ever uses mx/my/mz
    // through hypot()/atan2(h, dz) with h itself a hypot() -- purely metric,
    // no chirality -- so the sign of the Y<->Z mapping does not change its
    // output; confirmed, no logic change needed here beyond this comment.
    const mx = (ext[base+0] + ext[base+3]) * 0.5;   // printer X (horizontal)
    const mz = (ext[base+1] + ext[base+4]) * 0.5;   // printer Z (height)
    const my = (ext[base+2] + ext[base+5]) * 0.5;   // printer Y (horizontal)

    let deg;
    if(mz <= OVH_Z_FIRST){
      deg = 0;                                       // first layer: sits on plate
    } else {
      const bxi = Math.floor(mx / BUCKET_CELL);
      const byi = Math.floor(my / BUCKET_CELL);
      let bestH = Infinity, bestDz = 0;
      for(let di = -NR; di <= NR; di++){
        for(let dj = -NR; dj <= NR; dj++){
          const bucket = grid.get((bxi+di) + ',' + (byi+dj));
          if(!bucket) continue;
          for(let k = 0; k < bucket.length; k++){
            const pr = bucket[k];
            const dz = mz - pr.mz;                    // height above the prior point
            if(dz > OVH_DZ_LO && dz <= OVH_DZ_HI){
              const h = Math.hypot(pr.mx - mx, pr.my - my);
              if(h > OVH_SEARCH_R) continue;
              if(h < bestH){ bestH = h; bestDz = dz; }
            }
          }
        }
      }
      deg = (bestH === Infinity)
        ? 90                                          // unsupported -> bridge/air
        : Math.atan2(bestH, bestDz) * 180 / Math.PI;
    }
    ovh[s] = deg;

    // Insert this midpoint for later segments (keyed by printer-XY).
    const key = bucketKey(mx, my, BUCKET_CELL);
    let bucket = grid.get(key);
    if(!bucket){ bucket = []; grid.set(key, bucket); }
    bucket.push({mx, my, mz});
  }
  return ovh;
}

// Map an overhang angle (deg) to a green -> yellow -> red RGB triple.
function overhangColor(deg){
  const green = [0.15, 0.75, 0.20], yellow = [0.95, 0.85, 0.10], red = [0.88, 0.16, 0.12];
  if(deg <= 0) return green.slice();
  if(deg >= OVH_RED_DEG) return red.slice();
  if(deg <= OVH_YELLOW_DEG){
    const t = deg / OVH_YELLOW_DEG;
    return [green[0]+(yellow[0]-green[0])*t, green[1]+(yellow[1]-green[1])*t, green[2]+(yellow[2]-green[2])*t];
  }
  const t = (deg - OVH_YELLOW_DEG) / (OVH_RED_DEG - OVH_YELLOW_DEG);
  return [yellow[0]+(red[0]-yellow[0])*t, yellow[1]+(red[1]-yellow[1])*t, yellow[2]+(red[2]-yellow[2])*t];
}

// Effective duration (seconds) of extrude segment s. Shared physics for both
// the print-time estimate and the time-based playback clock, so they always
// agree: effective_speed = min(commandedSpeed, sqrt(dist * ACCEL)) approximates
// segments that never reach commanded speed (short moves stay slow), and the
// Z-component speed is additionally capped at MAX_Z_SPEED.
function segDuration(ext, segSpeed, s){
  const base = s * 6;
  const dx = ext[base+3] - ext[base+0];
  const dy = ext[base+4] - ext[base+1];  // world Y = printer Z
  const dz = ext[base+5] - ext[base+2];
  const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
  if(dist < 1e-9) return 0;
  let spd = segSpeed[s] || 1;
  // Acceleration-limited effective speed for short moves.
  const accelSpd = Math.sqrt(dist * ACCEL);
  spd = Math.min(spd, accelSpd);
  // Z-axis speed cap: if the Z fraction of speed exceeds MAX_Z_SPEED, scale down.
  const zFrac = Math.abs(dy) / dist;
  const maxSpd = zFrac > 1e-6 ? MAX_Z_SPEED / zFrac : Infinity;
  spd = Math.min(spd, maxSpd);
  if(spd < 1e-9) spd = 1;
  return dist / spd;
}

// Binary search a cumulative, ascending time array `segT` (length nSeg+1,
// segT[0]=0) for the largest index k with segT[k] <= t. Used to map the
// play clock (seconds) to a segment count for setProgress. Clamps t outside
// [segT[0], segT[last]] to the nearest end.
function timeToSegIndex(segT, t){
  const n = segT.length;
  if(n === 0) return 0;
  if(t <= segT[0]) return 0;
  if(t >= segT[n-1]) return n-1;
  let lo = 0, hi = n-1;
  while(hi - lo > 1){
    const mid = (lo+hi) >> 1;
    if(segT[mid] <= t) lo = mid; else hi = mid;
  }
  return lo;
}

// Estimate total print time (seconds) summing dist/effective_speed for every move.
function computeEstTime(ext, trv, segSpeed, nExtSeg){
  let total = 0;

  // Extrude segments -- segSpeed[] has one entry per extrude seg.
  for(let s = 0; s < nExtSeg; s++) total += segDuration(ext, segSpeed, s);

  // Travel segments -- trv[] has no per-segment speed; use a nominal travel speed.
  // We cannot recover per-travel speed after parsing, so skip or use a default.
  // The travel array only has positions; we do best-effort with no speed data.
  // (Travel time is typically small compared to extrude time.)
  const nTrv = trv.length / 6;
  for(let s = 0; s < nTrv; s++){
    const base = s * 6;
    const dx = trv[base+3] - trv[base+0];
    const dy = trv[base+4] - trv[base+1];
    const dz = trv[base+5] - trv[base+2];
    const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
    if(dist < 1e-9) continue;
    // Travel speed is not stored -- use 200 mm/s as a typical Voron travel speed.
    const spd = Math.min(200, Math.sqrt(dist * ACCEL));
    total += dist / spd;
  }
  return total;
}

// Format seconds into "Xh Ym" or "Xm Ys" string.
function fmtTime(secs){
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if(h > 0) return h + 'h ' + m + 'm';
  if(m > 0) return m + 'm ' + s + 's';
  return s + 's';
}

// Format seconds as a clock readout: "M:SS" (e.g. "4:12"), or "H:MM:SS" once
// past an hour (e.g. "1:04:12"). Used for the elapsed/total playback readout.
function fmtClock(secs){
  secs = Math.max(0, Math.round(secs));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const ss = String(s).padStart(2, '0');
  if(h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + ss;
  return m + ':' + ss;
}

function parseGcode(text){
  const cx=BED_X/2, cy=BED_Y/2;
  let x=0,y=0,z=0, has=false, curF=0;
  let minz=Infinity,maxz=-Infinity, minx=Infinity,maxx=-Infinity,miny=Infinity,maxy=-Infinity;
  let fil=0, extrudeCount=0, travelCount=0, maxZrate=0, relE=false;
  let curFan=0, minFan=Infinity, maxFan=-Infinity, fanEverOn=false;   // sticky M106/M107 state (0..1)
  const ext=[], extCol=[], trv=[];          // world-space vertex arrays
  const segSpeed=[], segFlow=[];            // per-extrude-segment telemetry
  const meta={lineWidth:null, layerHeight:null, nozzle:null, nozzleTemp:null};

  for (let raw of text.split('\n')){
    const line=raw.trim(); if(!line) continue;
    if(line[0]===';'){                        // header metadata comments
      let m;
      if((m=line.match(/line_width=([\d.]+)/))) meta.lineWidth=parseFloat(m[1]);
      if((m=line.match(/layer_height=([\d.]+)/))) meta.layerHeight=parseFloat(m[1]);
      if((m=line.match(/nozzle=([\d.]+)/))) meta.nozzle=parseFloat(m[1]);
      continue;
    }
    const up=line.toUpperCase();
    if(up.startsWith('M106')){
      const m=up.match(/S([\d.]+)/);
      if(m) curFan=Math.min(1,Math.max(0,parseFloat(m[1])/255));
      fanEverOn=true;                     // fan commanded on at least once
      continue;
    }
    if(up.startsWith('M107')){ curFan=0; continue; }
    if(up.startsWith('M83')) { relE=true; continue; }
    if(up.startsWith('M82')) { relE=false; continue; }
    // Nozzle temp: Bambu-style profiles emit M104/M109 S<temp>; the Trident
    // (default) profile emits PRINT_START EXTRUDER=<temp> ... instead. S0 is
    // the heater-off line in the end G-code, not a real target -- ignored so
    // it doesn't clobber the value read from the start of the file.
    if(up.startsWith('M104')||up.startsWith('M109')){
      const m=up.match(/S([\d.]+)/);
      if(m){ const t=parseFloat(m[1]); if(t>0) meta.nozzleTemp=t; }
      continue;
    }
    if(up.startsWith('PRINT_START')){
      const m=up.match(/EXTRUDER=([\d.]+)/);
      if(m) meta.nozzleTemp=parseFloat(m[1]);
      continue;
    }
    if(!(up.startsWith('G0')||up.startsWith('G1'))) continue;
    let nx=x,ny=y,nz=z,e=null,f=null;
    for(const tok of line.split(/\s+/).slice(1)){
      const c=tok[0].toUpperCase(), v=parseFloat(tok.slice(1));
      if(Number.isNaN(v))continue;
      if(c==='X')nx=v; else if(c==='Y')ny=v; else if(c==='Z')nz=v;
      else if(c==='E')e=v; else if(c==='F')f=v;
    }
    if(f!==null) curF=f;                       // feedrate is sticky across moves
    const extruding = e!==null && (relE ? e>1e-6 : e>0);
    if(has){
      // Remap to world (Y up). Y is NEGATED (cy-y, not y-cy) so the swap of
      // two axes (printer Z-up -> Three.js Y-up) stays a rotation rather than
      // a reflection -- an un-negated swap has determinant -1 and mirrors any
      // chiral geometry (e.g. xy_twist) in the preview relative to what the
      // machine actually prints. Every other printer-Y <-> world-Z site in
      // this file (fitView, computeTurns, the measure tool, the shape cage,
      // the nav cube) must use this same negated form.
      const ax=x-cx, ay=z, az=cy-y;
      const bx=nx-cx, by=nz, bz=cy-ny;
      const len=Math.hypot(nx-x,ny-y,nz-z);
      const speed=curF/60;                       // mm/s
      // Peak Z-rate deliberately mirrors trident_gcode/analyze.py's max_z_rate
      // (see analyze.py around line 216: `if dist > 0 and speed > 0: zr = ...`).
      // That is computed over EVERY move -- travel included -- not just
      // extruding ones, because a fast Z-lift on a travel move stresses the Z
      // axis exactly as much as one made while extruding. analyze.py's number
      // is the authoritative safety report the server prints; scoping this to
      // extruding-only (as it used to be) made the viewer under-report the
      // same file's peak Z-rate by 4x. Keep this outside the extruding branch
      // below so the two panels cannot drift apart again.
      if(len>0 && speed>0){ const zr=speed*Math.abs(nz-z)/len; if(zr>maxZrate)maxZrate=zr; }
      if(extruding){
        ext.push(ax,ay,az, bx,by,bz);
        extCol.push(nz, nz);            // store z; convert to colour after
        extrudeCount++;
        // volumetric flow = filament volume extruded / time = e*area*speed/len
        const flow=(len>0 && relE)? e*FIL_AREA*speed/len : 0;
        segSpeed.push(speed); segFlow.push(flow);
        // skip pre-M106 extrudes (fan-off adhesion window) so they don't pin minFan to 0
        if(fanEverOn){ if(curFan<minFan) minFan=curFan; if(curFan>maxFan) maxFan=curFan; }
        if(relE) fil+=e;
        minz=Math.min(minz,z,nz); maxz=Math.max(maxz,z,nz);
        minx=Math.min(minx,nx);maxx=Math.max(maxx,nx);miny=Math.min(miny,ny);maxy=Math.max(maxy,ny);
      } else {
        trv.push(ax,ay,az, bx,by,bz); travelCount++;
      }
    }
    x=nx;y=ny;z=nz;has=true;
  }

  const nExtSeg = extrudeCount;
  // Build the flat ext/trv arrays from the pushed values.
  const extFlat = new Float32Array(ext);
  const trvFlat = new Float32Array(trv);

  // NOTE: risk flags (computeRiskFlags) and overhang angles (computeOverhang)
  // are deliberately NOT computed here. Both are O(extrude segments) grid-hash
  // passes as heavy as this parse loop, and load() below stages them as
  // separate "Analyzing supports..." / "Computing overhangs..." steps with a
  // yield to the browser in between so a big file's tab doesn't freeze with
  // zero feedback. Callers must run those two passes themselves and merge the
  // results into this object -- see load().

  // Compute estimated print time.
  const estTimeSec = computeEstTime(extFlat, trvFlat, segSpeed, nExtSeg);
  const estTime = fmtTime(estTimeSec);

  // Cumulative per-segment time (seconds), extrude-only -- the timeline that
  // time-based playback walks. segT[0]=0, segT[nExtSeg]=total extrude-only
  // path time (differs from estTimeSec by the excluded travel time).
  const segT = new Float64Array(nExtSeg + 1);
  for(let s = 0; s < nExtSeg; s++) segT[s+1] = segT[s] + segDuration(extFlat, segSpeed, s);

  // No M106 seen at all (e.g. a printer profile with no part-cooling fan) --
  // minFan/maxFan stay at their unset Infinity/-Infinity sentinels; null reads
  // better than a nonsensical range in the telemetry card.
  const fanSeen = extrudeCount > 0 && isFinite(minFan);

  return {ext:extFlat,extCol,trv:trvFlat,segSpeed,segFlow,meta,minz,maxz,minx,maxx,miny,maxy,
          fil,extrudeCount,travelCount,maxZrate,
          riskFlags:null,riskyCount:null,overhang:null,   // filled in by load() -- see NOTE above
          estTime,estTimeSec,segT,
          minFan: fanSeen ? minFan : null, maxFan: fanSeen ? maxFan : null};
}

// Swap the Display-panel legend to match the active colour mode: height shows
// the viridis gradient + z-range labels, overhang shows a green->yellow->red
// gradient + angle labels, plain hides the legend entirely.
function updateLegend(colorMode, d){
  const legend = document.getElementById('legend');
  const labels = document.getElementById('legend-labels');
  const lo = document.getElementById('z-lo');
  const mid = document.getElementById('z-mid');
  const hi = document.getElementById('z-hi');
  if(!legend || !labels) return;
  if(colorMode === 'overhang'){
    legend.style.display = ''; labels.style.display = '';
    legend.style.background = 'linear-gradient(90deg,#26bf33,#f2d919,#e0402f)';
    if(lo) lo.textContent = '0°';
    if(mid) mid.textContent = 'overhang';
    if(hi) hi.textContent = '≥55°';
  } else if(colorMode === 'plain'){
    legend.style.display = 'none'; labels.style.display = 'none';
  } else {  // height
    legend.style.display = ''; labels.style.display = '';
    legend.style.background = '';   // fall back to the CSS viridis gradient
    if(lo) lo.textContent = d ? d.minz.toFixed(0) : '0';
    if(mid) mid.textContent = 'height (mm)';
    if(hi) hi.textContent = d ? d.maxz.toFixed(0) : '–';
  }
}

function buildGeometry(d){
  if(pathObj){scene.remove(pathObj);pathObj.geometry.dispose();}
  if(travelObj){scene.remove(travelObj);travelObj.geometry.dispose();}

  const colorMode = document.getElementById('t-colormode').value;   // 'height' | 'overhang' | 'plain'
  updateLegend(colorMode, d);
  const banding = document.getElementById('t-bands').checked;
  const showRisk = document.getElementById('t-risk').checked;
  const nSeg = d.ext.length/6;

  // Map each segment to its spiral-turn index so we can shade alternating turns.
  const segTurn = new Int32Array(nSeg);
  if(banding && cuts && cuts.length>1){
    let ti=0;
    for(let s=0;s<nSeg;s++){ while(ti+1<cuts.length && s>=cuts[ti+1]) ti++; segTurn[s]=ti; }
  }

  // Per-vertex colours (one per segment endpoint), then fat-line geometry.
  const cols=new Float32Array(d.ext.length);
  const span=Math.max(1e-6, d.maxz-d.minz);
  // Plain mode carries no data, so it is free to just look like filament:
  // a natural / beige PLA (0xe6d5b0). LineMaterial is UNLIT -- there is no
  // light term applied to vertex colours -- so this value is exactly what
  // renders, and it is chosen to sit clear of both the 0x1c1f22 canvas and
  // the saturated amber used for interactive handles. Banding multiplies
  // alternate turns by 0.55, which lands on a muted 0x7e7560 -- still clearly
  // the same material in shadow rather than a different colour.
  const PLAIN_RGB = [0xe6/255, 0xd5/255, 0xb0/255];   // beige filament (0xe6d5b0)
  for(let i=0;i<d.extCol.length;i++){
    const segIdx = i >> 1;  // two vertices per segment
    const zc=d.extCol[i];
    let rgb;
    // Risky segments get bright red override when highlight-risky is enabled.
    if(showRisk && d.riskFlags && d.riskFlags[segIdx]){
      rgb = [1.0, 0.15, 0.1];
    } else {
      if(colorMode==='overhang'){
        rgb = overhangColor(d.overhang ? d.overhang[segIdx] : 0);
      } else if(colorMode==='plain'){
        rgb = PLAIN_RGB.slice();
      } else {
        rgb = ramp((zc-d.minz)/span);       // height (viridis)
      }
      if(banding){                          // darken every other turn -> visible ribs
        const b = (segTurn[segIdx] % 2 === 0) ? 1.0 : 0.55;
        rgb=[rgb[0]*b, rgb[1]*b, rgb[2]*b];
      }
    }
    cols[i*3]=rgb[0];cols[i*3+1]=rgb[1];cols[i*3+2]=rgb[2];
  }
  const g=new LineSegmentsGeometry();
  g.setPositions(d.ext);
  g.setColors(cols);
  // "True bead width" renders lines at their physical width in mm (world
  // units), so zooming in shows individual filament lines exactly as they'll
  // be laid down. Off = classic constant screen-pixel thickness.
  const trueWidth = document.getElementById('t-truewidth').checked;
  const beadW = (d.meta && d.meta.lineWidth) ? d.meta.lineWidth : 0.45;
  lineMat = trueWidth
    ? new LineMaterial({ vertexColors:true, worldUnits:true, linewidth:beadW })
    : new LineMaterial({ vertexColors:true, worldUnits:false, linewidth:lineWidthPx });
  lineMat.resolution.set(wrap.clientWidth||1, wrap.clientHeight||1);
  pathObj=new LineSegments2(g, lineMat);
  pathObj.computeLineDistances();
  scene.add(pathObj);

  const tg=new THREE.BufferGeometry();
  tg.setAttribute('position', new THREE.Float32BufferAttribute(d.trv,3));
  // 0x5a5a5a was tuned against the 0x1c1f22 canvas; against the lighter
  // 0x2b3036 background its contrast dropped by roughly half, so it is
  // lifted again here to keep travel moves legible without turning them into
  // a distraction from the extrude path they are meant to sit behind.
  travelObj=new THREE.LineSegments(tg, new THREE.LineBasicMaterial({color:0x74747a,transparent:true,opacity:0.45}));
  travelObj.visible=document.getElementById('t-travel').checked;
  scene.add(travelObj);

  // Init print-process animation: reveal is by instance count (1 per segment).
  animSeg = d.ext.length / 6;
  extArr = d.ext;

  applyXray();   // (re)apply the current X-ray state to the freshly built material
}

// Apply the X-ray toggle to the path's fat-line material. On: semi-transparent
// and depth-test off so the whole toolpath is see-through; the bed is dimmed by
// render(). Off: fully opaque with normal depth testing. Reads the checkbox so
// it stays correct after buildGeometry rebuilds lineMat from scratch.
function applyXray(){
  xrayOn = !!document.getElementById('t-xray').checked;
  if(lineMat){
    lineMat.transparent = xrayOn;
    lineMat.opacity = xrayOn ? 0.28 : 1.0;
    lineMat.depthTest = !xrayOn;
    lineMat.needsUpdate = true;
  }
}

// Frame the camera on the loaded model (not the whole bed). Flat prints (e.g.
// a single-layer calibration disk) get a steep, near-top-down view so the
// spiral fill is actually visible; tall prints get the classic 3/4 view.
function fitView(){
  if(!lastData) return;
  const d=lastData;
  // cz uses BED_Y/2 - (miny+maxy)/2 (not the reverse) to match parseGcode's
  // negated printer-Y -> world-Z mapping (az = cy - y).
  const cx=(d.minx+d.maxx)/2-BED_X/2, cz=BED_Y/2-(d.miny+d.maxy)/2;
  const h=d.maxz-d.minz, cy=(d.minz+d.maxz)/2;
  const span=Math.max(d.maxx-d.minx, d.maxy-d.miny, h*1.6, 12);
  const flat = h < span*0.15;
  const dist = span*1.55;
  const elev = flat ? 1.15 : 0.55;          // steeper for flat prints
  // Positive Z offset from the model centre = the FRONT side of the bed
  // (world +Z is printer Y=0 after the negated mapping above), so this is a
  // deliberate front-right-above 3/4 view, matching the initial
  // camera.position.set() near the top of this file -- not an accident of
  // the sign of `dist`.
  camera.position.set(cx + dist*Math.cos(elev)*0.75,
                      cy + dist*Math.sin(elev),
                      cz + dist*Math.cos(elev)*0.75);
  controls.target.set(cx, cy, cz);
  controls.update();
  render();
}

let gcodeTitleEl = null;
function showGcodeTitle(name){
  if(!gcodeTitleEl){
    gcodeTitleEl = document.createElement('div');
    gcodeTitleEl.id = 'gcode-title';
    // Surface/ink stand-ins tracking the palette (--surface rgb(30,33,36) and
    // --ink #e8eaed) -- this label is a plain DOM element positioned over the
    // canvas, not a CSS-var-aware node, so the values are inlined here.
    gcodeTitleEl.style.cssText =
      'position:absolute;top:8px;left:50%;transform:translateX(-50%);' +
      'padding:3px 12px;border-radius:6px;background:rgba(30,33,36,0.86);' +
      'color:#e8eaed;font-size:12px;font-weight:600;pointer-events:none;' +
      'z-index:5;font-family:sans-serif;max-width:60%;overflow:hidden;' +
      'text-overflow:ellipsis;white-space:nowrap;border:1px solid rgba(255,255,255,0.08)';
    wrap.appendChild(gcodeTitleEl);
  }
  gcodeTitleEl.textContent = name;
  gcodeTitleEl.style.display = 'block';
}

// The Z-rate ceiling comes from the SELECTED PRINTER's own declared limit,
// never a literal in this file -- CLAUDE.md's "No machine limit may be a
// module constant" invariant names this exact spot: a bare 25.1 (the Voron
// Trident's max_z_velocity) here would call a file "ok" on some other
// printer's Z axis and "! " on the Trident's, regardless of what that other
// printer can actually take. The agent wiring up the printer-select UI
// publishes the active profile's limit on window.__printerLimits with the
// shape { key, name, max_z_velocity }, refreshed on load and on every printer
// change; that global is read here defensively (it depends on an async
// fetch, so it may be absent or incomplete at the instant this runs). Returns
// a finite number, or null if no grounded limit is available -- callers must
// withhold the ok/danger verdict in the null case rather than guess.
function currentMaxZVelocity(){
  const lim = window.__printerLimits;
  const v = lim && lim.max_z_velocity;
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

function showStats(name,d){
  document.getElementById('stats-group').style.display='block';
  document.getElementById('overlay').style.display='none';
  showGcodeTitle(name);
  const set=(id,v)=>document.getElementById(id).textContent=v;
  set('s-name',name.length>20?name.slice(0,18)+'...':name);
  set('s-moves',d.extrudeCount.toLocaleString());
  set('s-travel',d.travelCount.toLocaleString());
  set('s-height',(d.maxz-d.minz).toFixed(1)+' mm');
  set('s-foot',`${(d.maxx-d.minx).toFixed(0)}x${(d.maxy-d.miny).toFixed(0)} mm`);
  set('s-fil',d.fil>0?(d.fil/1000).toFixed(2)+' m':'n/a');
  const zr=d.maxZrate;
  const zLimit=currentMaxZVelocity();
  const zrateEl=document.getElementById('s-zrate');
  if(zLimit!=null){
    set('s-zrate',zr.toFixed(1)+' mm/s'+(zr>zLimit?' !':' ok'));
    zrateEl.classList.toggle('state-danger', zr>zLimit);
  } else {
    // No grounded limit available -- show the measured rate but withhold the
    // verdict rather than inventing or falling back to a ceiling. A verdict
    // that isn't backed by the selected printer's own declared limit is
    // worse than none (CLAUDE.md).
    set('s-zrate',zr.toFixed(1)+' mm/s (limit unknown)');
    zrateEl.classList.remove('state-danger');
  }
  set('s-time', d.estTime || '--');
  set('s-risk', d.riskyCount != null ? d.riskyCount.toLocaleString() : '--');
  document.getElementById('s-risk').classList.toggle('state-warn', !!d.riskyCount);
  // Legend labels are owned by updateLegend so they stay correct in every
  // colour mode (height z-range vs overhang angle scale vs hidden for plain).
  updateLegend(document.getElementById('t-colormode').value, d);
}

// ---- sparkline ------------------------------------------------------------
// Offscreen canvas holds the static chart bitmap; cursor is drawn on the
// visible canvas on top of a blitted copy each setProgress call.
let sparkOffscreen = null;

const SPARK_FLOW_MAX_REF = 17;    // melt-ceiling reference line at 17 mm^3/s
const SPARK_BUCKETS = 600;
const SPARK_BG = 'rgba(30,33,36,0.85)';  // tracks --surface (#1e2124)
const SPARK_LINE_COL = '#5a8aff';
const SPARK_FILL_COL = 'rgba(47,107,255,0.18)';
const SPARK_REF_COL = '#ffb454';  // mirrors --warn in style.css (safety-state color, not decoration)
const SPARK_CURSOR_COL = 'rgba(255,255,255,0.75)';

function buildSparkline(d){
  const canvas = document.getElementById('spark');
  if(!canvas) return;
  const W = canvas.offsetWidth || canvas.parentElement.clientWidth || 400;
  const H = 26;
  canvas.width = W;
  canvas.height = H;

  const nSeg = d.segFlow.length;
  if(nSeg === 0){ sparkOffscreen = null; return; }

  // Downsample to SPARK_BUCKETS: take max flow in each bucket.
  const nb = Math.min(SPARK_BUCKETS, nSeg);
  const buckets = new Float32Array(nb);
  for(let s = 0; s < nSeg; s++){
    const bi = Math.floor(s / nSeg * nb);
    const v = d.segFlow[s] || 0;
    if(v > buckets[bi]) buckets[bi] = v;
  }

  // Find max flow for vertical scale (at least 2x the ref line so ref is visible).
  let maxFlow = 0;
  for(let i = 0; i < nb; i++) if(buckets[i] > maxFlow) maxFlow = buckets[i];
  maxFlow = Math.max(maxFlow, SPARK_FLOW_MAX_REF * 1.2, 1);

  // Draw to an offscreen canvas so we can blit + cursor cheaply.
  const off = document.createElement('canvas');
  off.width = W; off.height = H;
  const ctx = off.getContext('2d');

  // Background.
  ctx.fillStyle = SPARK_BG;
  ctx.fillRect(0, 0, W, H);

  // Reference line at SPARK_FLOW_MAX_REF mm^3/s.
  const refY = H - (SPARK_FLOW_MAX_REF / maxFlow) * H;
  ctx.strokeStyle = SPARK_REF_COL;
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(0, refY);
  ctx.lineTo(W, refY);
  ctx.stroke();
  ctx.setLineDash([]);

  // Filled area.
  ctx.beginPath();
  ctx.moveTo(0, H);
  for(let i = 0; i < nb; i++){
    const px = (i / nb) * W;
    const py = H - (buckets[i] / maxFlow) * H;
    if(i === 0) ctx.lineTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.lineTo(W, H);
  ctx.closePath();
  ctx.fillStyle = SPARK_FILL_COL;
  ctx.fill();

  // Line on top.
  ctx.beginPath();
  ctx.strokeStyle = SPARK_LINE_COL;
  ctx.lineWidth = 1.5;
  for(let i = 0; i < nb; i++){
    const px = (i / nb) * W;
    const py = H - (buckets[i] / maxFlow) * H;
    if(i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.stroke();

  sparkOffscreen = off;
  // Draw the initial state (progress = 1 = fully done).
  drawSparkCursor(progress);
}

// Blit the offscreen sparkline and overlay a cursor line at the given progress.
function drawSparkCursor(p){
  const canvas = document.getElementById('spark');
  if(!canvas || !sparkOffscreen) return;
  const W = canvas.width, H = canvas.height;
  if(W === 0 || H === 0) return;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(sparkOffscreen, 0, 0);
  // Cursor line.
  const cx = Math.round(p * W);
  ctx.strokeStyle = SPARK_CURSOR_COL;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx, 0);
  ctx.lineTo(cx, H);
  ctx.stroke();
}

// Seek on sparkline click/drag -- same fraction math as the scrub slider.
function sparkSeek(e){
  const canvas = document.getElementById('spark');
  if(!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  stopPlay();
  setProgress(frac);
}

{
  const spark = document.getElementById('spark');
  let draggingSpark = false;
  spark.addEventListener('mousedown', e=>{ draggingSpark=true; sparkSeek(e); });
  window.addEventListener('mousemove', e=>{ if(draggingSpark) sparkSeek(e); });
  window.addEventListener('mouseup', ()=>{ draggingSpark=false; });
}

// ---- telemetry card collapse (persisted across reloads) -------------------
{
  const TM_KEY = 'telemetry-collapsed';
  const card = document.getElementById('telemetry-card');
  const toggleBtn = document.getElementById('telemetry-toggle');
  function setTelemetryCollapsed(collapsed){
    card.classList.toggle('collapsed', collapsed);
    toggleBtn.setAttribute('aria-expanded', String(!collapsed));
    localStorage.setItem(TM_KEY, collapsed ? '1' : '0');
  }
  setTelemetryCollapsed(localStorage.getItem(TM_KEY) === '1');
  toggleBtn.addEventListener('click', ()=> setTelemetryCollapsed(!card.classList.contains('collapsed')));
}

let lastData=null;
let cuts=[0];   // extrude-segment indices at each spiral-turn boundary (+ the end)

// ---- staged load overlay ---------------------------------------------------
// load() below runs several O(extrude segments) passes back to back
// (parseGcode -> computeRiskFlags -> computeOverhang -> buildGeometry). On a
// large file each one can take seconds, and with nothing on screen to say so
// a frozen tab looks exactly like a crash. This overlay names the current
// stage; loadYield() below hands control back to the browser between stages
// so that label actually paints before the next heavy pass blocks the thread.
// Built as a plain DOM node with inline styles (same pattern as
// showGcodeTitle's #gcode-title) rather than a stylesheet class -- viewer.js
// does not own style.css. Colours are hand-picked to track --surface/--ink
// without using the CSS custom properties themselves (and deliberately avoid
// --ok/--warn/--danger/--accent-purple, which are reserved to other subsystems).
let loadOverlayEl = null, loadOverlayStageEl = null, loadOverlayFileEl = null;
function showLoadOverlay(name){
  if(!loadOverlayEl){
    loadOverlayEl = document.createElement('div');
    loadOverlayEl.id = 'load-overlay';
    loadOverlayEl.style.cssText =
      'position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;' +
      'justify-content:center;gap:6px;background:rgba(23,25,27,0.82);' +
      'color:#e8eaed;font-family:sans-serif;z-index:8;pointer-events:none;text-align:center;';
    loadOverlayStageEl = document.createElement('div');
    loadOverlayStageEl.id = 'load-overlay-stage';
    loadOverlayStageEl.style.cssText = 'font-size:14px;font-weight:600;';
    loadOverlayFileEl = document.createElement('div');
    loadOverlayFileEl.id = 'load-overlay-file';
    loadOverlayFileEl.style.cssText =
      'font-size:12px;font-weight:400;opacity:0.72;max-width:70%;overflow:hidden;' +
      'text-overflow:ellipsis;white-space:nowrap;';
    loadOverlayEl.appendChild(loadOverlayStageEl);
    loadOverlayEl.appendChild(loadOverlayFileEl);
    wrap.appendChild(loadOverlayEl);
  }
  loadOverlayFileEl.textContent = name;
  loadOverlayEl.style.display = 'flex';
}
function setLoadStage(text){
  if(loadOverlayStageEl) loadOverlayStageEl.textContent = text;
}
function hideLoadOverlay(){
  if(loadOverlayEl) loadOverlayEl.style.display = 'none';
}
// Yield to the browser and guarantee at least one paint has happened before
// resuming. A single requestAnimationFrame callback runs BEFORE that frame's
// paint, so it alone does not guarantee the label update is on screen yet --
// the standard double-rAF idiom does: the first rAF fires at the start of the
// next frame (paint follows), and the second rAF (scheduled from inside the
// first) cannot fire until the frame after that, i.e. after the paint has
// happened.
//
// Measured while testing this: Chrome can suspend rAF ENTIRELY for a hidden
// or backgrounded tab (a user alt-tabbing away mid-load, or an occluded
// window), and a pure double-rAF wait then never resolves -- the load just
// hangs forever instead of freezing for seconds, which is worse than the bug
// this was meant to fix. A setTimeout fallback races it: on a visible tab it
// never fires (the rAFs win in well under 200ms), so the paint guarantee
// above still holds; on a hidden tab it guarantees forward progress anyway,
// just without that guarantee -- which is fine, because there is nothing to
// paint for the user to see until the tab is visible again regardless.
function loadYield(){
  return new Promise(resolve => {
    let done = false;
    const finish = () => { if(!done){ done = true; resolve(); } };
    requestAnimationFrame(() => requestAnimationFrame(finish));
    setTimeout(finish, 200);
  });
}

async function load(name,text){
  showLoadOverlay(name);
  try {
    // Cheap upfront count for the parse-stage label; a single split() is
    // negligible next to the per-line regex work parseGcode does below.
    const totalLines = text.split('\n').length;
    setLoadStage(`Parsing G-code... (${totalLines.toLocaleString()} lines)`);
    await loadYield();
    const parsed = parseGcode(text);      // heavy: O(lines)

    setLoadStage(`Analyzing supports... (${parsed.extrudeCount.toLocaleString()} segments)`);
    await loadYield();
    const riskFlags = computeRiskFlags(parsed.ext, parsed.extrudeCount);   // heavy: O(segments)
    let riskyCount = 0;
    for(let i=0;i<riskFlags.length;i++) riskyCount += riskFlags[i];

    setLoadStage('Computing overhangs...');
    await loadYield();
    const overhang = computeOverhang(parsed.ext, parsed.extrudeCount);    // heavy: O(segments)

    setLoadStage('Building view...');
    await loadYield();
    parsed.riskFlags = riskFlags;
    parsed.riskyCount = riskyCount;
    parsed.overhang = overhang;
    lastData = parsed;
    cuts=computeTurns(lastData);           // turn boundaries (used for banding + stepping)
    buildGeometry(lastData); showStats(name,lastData);   // heavy: O(segments)
    measureReload();                       // new model: any old measurement is meaningless
    // A file was loaded (dropped, picked, or generated) - show the viewer mode.
    // This happens BEFORE the canvas chrome appears and before fitView(),
    // because switching mode is what reveals that chrome now: visibility is
    // syncCanvasChromeForMode()'s decision alone. load() used to show the
    // strip, the telemetry card and the has-timeline class by hand, which is
    // how they survived into Design mode -- two places setting them, only one
    // ever clearing them.
    if(window.setAppMode) window.setAppMode('viewer');
    syncCanvasChromeForMode();             // also covers designer.js being absent
    stopPlay(); setProgress(1);            // start fully drawn
    fitView();                             // frame the camera on the model
    // Build the sparkline after layout settles (offsetWidth needs a rendered frame).
    requestAnimationFrame(()=>{ buildSparkline(lastData); drawSparkCursor(1); });
  } finally {
    // Always clears, success or failure -- a stuck overlay would be worse
    // than the freeze it replaces (looks like a hang forever, not just once).
    hideLoadOverlay();
  }
}

// Detect spiral-turn boundaries by unwrapping the path's angle about its centre.
// Each full 2*pi revolution = one "layer/turn" for the step controls.
function computeTurns(d){
  // cz mirrors parseGcode's negated printer-Y -> world-Z mapping (az = cy - y),
  // as fitView() does. ext[] holds world coords, so deriving the centre with
  // the un-negated form put it at the reflected position -- harmless for a
  // bed-centred print (both land near 0) but wrong for an off-centre one,
  // where the angle unwrap below would be taken about a point outside the
  // model and hand back garbage turn boundaries.
  const cx=((d.minx+d.maxx)/2)-BED_X/2, cz=BED_Y/2-((d.miny+d.maxy)/2);
  const ext=d.ext, nSeg=ext.length/6;
  const out=[0]; let cum=0, prev=null, last=0;
  for(let s=0;s<nSeg;s++){
    const a=Math.atan2(ext[s*6+5]-cz, ext[s*6+3]-cx);
    if(prev!==null){ let dd=a-prev; if(dd>Math.PI)dd-=2*Math.PI; if(dd<-Math.PI)dd+=2*Math.PI; cum+=dd; }
    prev=a;
    const t=Math.floor(Math.abs(cum)/(2*Math.PI));
    if(t>last){ last=t; out.push(s); }
  }
  out.push(nSeg);            // final boundary = end of print
  return out;
}
// Test/automation hooks so the path + playback can be driven programmatically.
window.loadGcode = load;
window.__camera = camera;   // camera access for automation/tests
window.__controls = controls;   // OrbitControls access for automation/tests (freeze regression checks)
window.__segColor = (s) => {           // brightness (sum of rgb) at segment s start
  if(!pathObj) return null;
  const a = pathObj.geometry.getAttribute('instanceColorStart');
  if(!a) return null;
  return +(a.getX(s)+a.getY(s)+a.getZ(s)).toFixed(3);
};
window.__previewState = () => ({
  animSeg,
  progress: +progress.toFixed(4),
  drawnSegs: pathObj ? pathObj.geometry.instanceCount : null,
  nozzleVisible: nozzle.visible,
  nozzlePos: nozzle.visible ? nozzle.position.toArray().map(v=>+v.toFixed(1)) : null,
  riskyCount: lastData ? lastData.riskyCount : null,
  estTime: lastData ? lastData.estTime : null,
  telemetry: {speed:document.getElementById('tm-speed')?.textContent,
              flow:document.getElementById('tm-flow')?.textContent,
              layerHeight:document.getElementById('tm-lh')?.textContent,
              lineWidth:document.getElementById('tm-lw')?.textContent},
});

// ---- print-process playback ------------------------------------------------
// `progress` (0..1 over extrude segments) stays the master variable that every
// downstream consumer (instance reveal, nozzle marker, sparkline cursor,
// telemetry lookup) reads -- unchanged from before. `playT` is a parallel
// clock in seconds along lastData.segT, kept in sync here.
//
// `fromClock` is true only when playLoop calls this: the play loop already
// advanced playT itself (a continuous clock) and computed the matching
// segment index k via binary search, so resyncing playT = segT[k] here would
// snap the clock backward to the start of segment k every single frame --
// for any segment whose duration exceeds one animation frame (typical at low
// speedMult, or on coarse/slow moves) that snap-back would out-race the next
// frame's dt*speedMult increment and playback would stall. Every OTHER
// progress-setter (scrub, sparkline seek, per-turn stepping, Home/End, load)
// only knows a target segment fraction, not a time, so those DO resync playT
// from segT[k] -- that direction is safe and idempotent (segT[k] is exactly
// the time timeToSegIndex would map back to k).
function setProgress(p, fromClock){
  progress = Math.min(1, Math.max(0, p));
  let k=0;
  if(animSeg>0) k = Math.round(progress*animSeg);
  if(pathObj && animSeg>0){
    pathObj.geometry.instanceCount = k;             // reveal only printed segments
    if(k>0 && progress<1){
      const i=(k-1)*6+3;                            // end of last drawn segment
      nozzle.position.set(extArr[i], extArr[i+1], extArr[i+2]);
      nozzle.visible=true;
    } else {
      nozzle.visible=false;                         // hide at 0% and when finished
    }
  }
  const segT = lastData && lastData.segT;
  const total = (segT && segT.length) ? segT[segT.length-1] : 0;
  if(!fromClock) playT = (segT && segT.length) ? segT[Math.min(k, segT.length-1)] : 0;
  document.getElementById('play').disabled = !(total > 0);

  const z = (lastData? lastData.minz:0) + progress*((lastData? (lastData.maxz-lastData.minz):0));
  let layerHtml='';
  if(cuts && cuts.length>1){
    const tot=cuts.length-1;
    const cur=Math.min(tot, cuts.filter(c=>c<=k).length);
    // Turn/revolution counter gets visual primacy -- per-spiral-turn stepping
    // (arrow keys) is this tool's signature playback mode.
    layerHtml=` &middot; <span class="turn-count">L${cur}/${tot}</span>`;
  }
  // Elapsed/total print time is strictly more informative than a bare percent
  // (e.g. "4:12 / 17:30" tells you how long the real print has left); fall
  // back to percent only when there's no usable timeline (zero extrude
  // segments) to avoid a div-by-zero readout.
  const timeHtml = total > 0
    ? ` &middot; ${fmtClock(playT)} / ${fmtClock(total)}`
    : ` &middot; ${Math.round(progress*100)}%`;
  document.getElementById('tl-read').innerHTML =
    `Z ${z.toFixed(1)}mm${layerHtml}${timeHtml}`;
  // Scrub bar now maps to TIME fraction, not segment fraction, so dragging
  // feels uniform in time regardless of how segment density varies.
  const timeFrac = total > 0 ? Math.min(1, playT/total) : progress;
  document.getElementById('scrub').value = Math.round(timeFrac*1000);
  updateTelemetry(k, z);
  drawSparkCursor(progress);
  render();
}

// Live telemetry HUD -- reflects the move under the playhead.
function updateTelemetry(k, z){
  if(!lastData) return;
  const seg = Math.min(Math.max(k-1,0), (lastData.segSpeed.length-1));
  const spd = lastData.segSpeed[seg] ?? 0;
  const flow = lastData.segFlow[seg] ?? 0;
  const set=(id,v)=>{const el=document.getElementById(id); if(el) el.textContent=v;};
  set('tm-speed', spd ? spd.toFixed(0)+' mm/s' : '--');
  const flowWarn = flow>17 ? ' !' : '';   // 17 mm^3/s ~ typical melt ceiling
  set('tm-flow', flow ? flow.toFixed(1)+' mm3/s'+flowWarn : '--');
  const tmFlowEl = document.getElementById('tm-flow');
  if(tmFlowEl) tmFlowEl.classList.toggle('state-danger', flow>17);
  // Fan min/max are whole-print constants (actual M106 range in the loaded
  // file, not a per-segment value) -- set once here rather than looked up
  // per playhead position, same way meta.lineWidth/layerHeight/nozzle are.
  set('tm-fan-min', lastData.minFan!=null ? Math.round(lastData.minFan*100)+'%' : '--');
  set('tm-fan-max', lastData.maxFan!=null ? Math.round(lastData.maxFan*100)+'%' : '--');
  set('tm-z', z.toFixed(2)+' mm');
  set('tm-lh', lastData.meta.layerHeight!=null ? lastData.meta.layerHeight.toFixed(2)+' mm' : '--');
  set('tm-lw', lastData.meta.lineWidth!=null ? lastData.meta.lineWidth.toFixed(2)+' mm' : '--');
  set('tm-nz', lastData.meta.nozzle!=null ? lastData.meta.nozzle.toFixed(2)+' mm' : '--');
  set('tm-temp', lastData.meta.nozzleTemp!=null ? Math.round(lastData.meta.nozzleTemp)+' C' : '--');
}
function updatePlayBtn(){ document.getElementById('play').textContent = playing?'||':'>'; }
function stopPlay(){ playing=false; updatePlayBtn(); }
let _lastT=0;
function playLoop(t){
  if(!playing) return;
  if(!_lastT)_lastT=t; const dt=(t-_lastT)/1000; _lastT=t;
  const segT = lastData && lastData.segT;
  const total = (segT && segT.length) ? segT[segT.length-1] : 0;
  if(total<=0 || animSeg<=0){ stopPlay(); return; }   // guard: zero-extrude-segment file
  playT = Math.min(total, playT + dt*speedMult);
  const k = timeToSegIndex(segT, playT);
  setProgress(k/animSeg, true);          // fromClock: don't snap playT back to segT[k]
  if(playT>=total){ stopPlay(); return; }
  requestAnimationFrame(playLoop);
}
document.getElementById('play').addEventListener('click',()=>{
  if(playing){ stopPlay(); return; }
  const segT = lastData && lastData.segT;
  const total = (segT && segT.length) ? segT[segT.length-1] : 0;
  if(total<=0) return;                    // guard: nothing to play
  if(progress>=1){ progress=0; playT=0; } // replay from the start
  playing=true; _lastT=0; updatePlayBtn(); requestAnimationFrame(playLoop);
});
document.getElementById('scrub').addEventListener('input',e=>{
  stopPlay();
  const segT = lastData && lastData.segT;
  const total = (segT && segT.length) ? segT[segT.length-1] : 0;
  if(total>0){
    // Scrub bar is a time fraction now: derive playT directly from it (more
    // precise than round-tripping through a segment index) and pass
    // fromClock so setProgress doesn't re-snap it to segT[k].
    playT = (e.target.value/1000) * total;
    const k = timeToSegIndex(segT, playT);
    setProgress(animSeg>0 ? k/animSeg : 0, true);
  } else {
    setProgress(e.target.value/1000);
  }
});
document.getElementById('speed').addEventListener('change',e=>{ speedMult=parseFloat(e.target.value); });

// ---- per-layer (per-turn) stepping ----------------------------------------
function nearestCut(){
  const k=Math.round(progress*animSeg); let best=0, bd=Infinity;
  for(let i=0;i<cuts.length;i++){ const dd=Math.abs(cuts[i]-k); if(dd<bd){bd=dd;best=i;} }
  return best;
}
function stepLayer(dir){
  if(cuts.length<2 || !animSeg) return;
  stopPlay();
  let p=Math.max(0, Math.min(cuts.length-1, nearestCut()+dir));
  setProgress(cuts[p]/animSeg);
}
window.stepLayer = stepLayer;   // automation hook

// ---- global-shortcut guards -------------------------------------------------
// Every window-level shortcut in this file has to answer the same two
// questions before it fires, and each one used to answer them differently.
// That is not cosmetic: these handlers call preventDefault(), so a missed
// case does not merely fire the shortcut, it SWALLOWS the keystroke.
//
// TEXTAREA was the missing case. The printer-import modal edits start/end
// G-code in #pm-start-gcode / #pm-end-gcode, which are textareas, not inputs
// -- so hand-typing "G1 F3000" lost the space to play/pause and the F to
// fit-view, and what got saved was "G1F3000". Start G-code is the one blob of
// text in this app that goes to the machine unedited, which makes a silently
// dropped character a hardware problem, not a typo.
function typingInField(e){
  const el = e.target;
  if(!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' ||
         el.isContentEditable === true;
}

// Playback shortcuts belong to the G-code viewer, not to the design form.
// The old guard tested #tl-wrap's inline display, which reads as "a file is
// loaded" but never as "the viewer is on screen": #tl-wrap lives in
// #canvas-wrap, outside BOTH mode panels, so load() reveals it once and
// nothing hides it again. After one generate, space/arrows/Home/End stayed
// armed over the whole design form for the rest of the session.
function viewerModeActive(){
  const p = document.getElementById('mode-viewer');
  return !!(p && p.classList.contains('active'));
}
window.addEventListener('keydown',e=>{
  if(typingInField(e)) return;
  if(!viewerModeActive()) return;
  if(document.getElementById('tl-wrap').style.display==='none') return;
  if(e.key==='ArrowRight'||e.key==='ArrowUp'){ stepLayer(1); e.preventDefault(); }
  else if(e.key==='ArrowLeft'||e.key==='ArrowDown'){ stepLayer(-1); e.preventDefault(); }
  else if(e.key==='Home'){ setProgress(0); e.preventDefault(); }
  else if(e.key==='End'){ setProgress(1); e.preventDefault(); }
  else if(e.key===' '){ document.getElementById('play').click(); e.preventDefault(); }
});

// ---- file input -----------------------------------------------------------
const fileInput=document.getElementById('file');
document.getElementById('drop').addEventListener('click',()=>fileInput.click());
fileInput.addEventListener('change',e=>{const f=e.target.files[0]; if(f) f.text().then(t=>load(f.name,t));});
['dragenter','dragover'].forEach(ev=>window.addEventListener(ev,e=>{e.preventDefault();document.getElementById('drop').classList.add('hot');}));
['dragleave','drop'].forEach(ev=>window.addEventListener(ev,e=>{e.preventDefault();document.getElementById('drop').classList.remove('hot');}));
window.addEventListener('drop',e=>{const f=e.dataTransfer.files[0]; if(f) f.text().then(t=>load(f.name,t));});

// ---- toggles --------------------------------------------------------------
document.getElementById('t-colormode').addEventListener('change',()=>{if(lastData){buildGeometry(lastData);setProgress(progress);} else {updateLegend(document.getElementById('t-colormode').value, null);}});
document.getElementById('t-xray').addEventListener('change',()=>{applyXray();render();});
document.getElementById('t-bands').addEventListener('change',()=>{if(lastData){buildGeometry(lastData);setProgress(progress);}});
document.getElementById('t-risk').addEventListener('change',()=>{if(lastData){buildGeometry(lastData);setProgress(progress);}});
document.getElementById('t-travel').addEventListener('change',e=>{if(travelObj){travelObj.visible=e.target.checked;render();}});
document.getElementById('t-bed').addEventListener('change',e=>{bedGroup.visible=e.target.checked;render();});
document.getElementById('t-printer').addEventListener('change',e=>{printerGroup.visible=e.target.checked;render();});
document.getElementById('t-width').addEventListener('input',e=>{
  lineWidthPx=parseFloat(e.target.value);
  // px slider only applies in screen-pixel mode
  if(lineMat && !document.getElementById('t-truewidth').checked){ lineMat.linewidth=lineWidthPx; render(); }
});
document.getElementById('t-truewidth').addEventListener('change',e=>{
  document.getElementById('row-width').style.opacity = e.target.checked ? .45 : 1;
  if(lastData){ buildGeometry(lastData); setProgress(progress); }
});
document.getElementById('fit-view').addEventListener('click',fitView);
window.addEventListener('keydown',e=>{
  if(typingInField(e)) return;
  if((e.key==='f'||e.key==='F') && !e.ctrlKey && !e.metaKey){ fitView(); e.preventDefault(); }
});
controls.autoRotateSpeed = 1.2;
document.getElementById('t-spin').addEventListener('change',e=>{controls.autoRotate=e.target.checked; if(e.target.checked) spinLoop();});

// ---- on-demand rendering --------------------------------------------------
// Render only when something changes (orbit, resize, load, toggle). Keeps the
// page idle when nothing moves -- lighter on the GPU and lets screenshots settle.
function render(){
  // Fade the bed plate when the camera is below the print surface so the
  // model stays visible when orbiting to a floor-up view. X-ray view also
  // dims the bed so the see-through toolpath reads clearly against it.
  if(window.__bedMats){
    const below = camera.position.y < 2;
    const op = xrayOn ? 0.12 : (below ? 0.12 : 0.95);
    for(const m of window.__bedMats) m.opacity = op;
  }
  renderer.render(scene,camera);
  // Measure tool's floating value tag is a DOM node, so it has to be
  // re-projected after every camera change. Read off `window` (same idiom as
  // __bedMats above) because render() runs once during module init, before the
  // measure section further down has initialised its bindings.
  if(window.__measureSync) window.__measureSync();
  // Orientation cube tracks the main camera every frame this renders --
  // see the comment on navSyncCamera() further down for why this is a
  // window.__* hook rather than a direct call (TDZ: that section is defined
  // after this function, and render() also runs once during module init).
  if(window.__navCubeSync) window.__navCubeSync();
}
function resize(){
  const w=wrap.clientWidth, h=wrap.clientHeight;
  renderer.setSize(w,h); camera.aspect=w/h; camera.updateProjectionMatrix();
  if(lineMat) lineMat.resolution.set(w,h);     // fat lines need the viewport size
  // Rebuild sparkline if data is loaded (canvas width may have changed).
  if(lastData && sparkOffscreen) requestAnimationFrame(()=>{ buildSparkline(lastData); drawSparkCursor(progress); });
  render();
}
controls.addEventListener('change', render);
function spinLoop(){
  if(!controls.autoRotate) return;
  controls.update(); render();
  requestAnimationFrame(spinLoop);
}
window.addEventListener('resize',resize); resize();
render();

// Expose resize so the panel splitter can trigger a viewport recalculation
// when the canvas area changes width (e.g. dragging the resizable splitter).
window.__viewerResize = resize;

// Debug/test hook: report which layer is currently shown (generated rainbow
// path vs the live blue draft) so automated checks can confirm the swap.
window.__viewFlags = function(){
  return {
    pathVisible: !!(pathObj && pathObj.visible),
    draftPresent: !!previewObj,
    draftLabel: previewLabel ? previewLabel.style.display : 'none'
  };
};

// Debug/test hook: bucket the loaded path's per-segment colours so automated
// checks can confirm the active colour mode without pixel sampling. Reads the
// fat-line geometry's instance colours (one RGB triple per segment start).
window.__colorStats = function(){
  if(!pathObj || !pathObj.geometry) return null;
  const a = pathObj.geometry.getAttribute('instanceColorStart');
  if(!a) return null;
  const n = a.count;
  let green=0, yellow=0, red=0, plain=0, other=0;
  for(let i=0;i<n;i++){
    const r=a.getX(i), g=a.getY(i), b=a.getZ(i);
    // Plain mode's beige filament. Warm (b below r) and light on all three
    // channels, which no viridis height stop and no overhang colour reaches:
    // viridis peaks yellow (b ~= 0.14) and the overhang ramp is green/yellow/
    // red, all far below b>0.6.
    if(r>0.8 && g>0.75 && b>0.6 && b<r) plain++;
    else if(r<0.4 && g>0.55 && b<0.35) green++;      // safe overhang
    else if(r>0.7 && g>0.6 && b<0.35) yellow++;      // caution
    else if(r>0.7 && g<0.45 && b<0.3) red++;         // steep/air
    else other++;
  }
  return { mode: document.getElementById('t-colormode').value,
           xray: !!(lineMat && lineMat.transparent),
           opacity: lineMat ? +lineMat.opacity.toFixed(2) : null,
           n, green, yellow, red, plain, other };
};

// ---- draft preview layer ----------------------------------------------------
// Shows an instant preview from generatePreview() while the user tweaks sliders.
// Visually distinct from the real G-code path: single accent colour, thinner.
let previewObj = null;
let previewBlobObj = null;
let previewLabel = null;

window.showPreview = function(positions){
  // positions: Float32Array [x0,y0,z0, x1,y1,z1, ...]
  if(!positions || positions.length < 6) return;
  window.clearPreview();

  const nSeg = positions.length / 6;
  // Uniform colour: semi-transparent accent blue.
  const cols = new Float32Array(nSeg * 6);   // 2 vertices * 3 rgb per segment
  for(let i = 0; i < nSeg; i++){
    const base = i * 6;
    cols[base]   = 0.30; cols[base+1] = 0.76; cols[base+2] = 1.00;
    cols[base+3] = 0.30; cols[base+4] = 0.76; cols[base+5] = 1.00;
  }

  const g = new LineSegmentsGeometry();
  g.setPositions(positions);
  g.setColors(cols);
  const mat = new LineMaterial({
    vertexColors: true,
    worldUnits: true,
    linewidth: 0.25,          // thinner than the real bead (~0.45)
    transparent: true,
    opacity: 0.65
  });
  mat.resolution.set(wrap.clientWidth || 1, wrap.clientHeight || 1);
  previewObj = new LineSegments2(g, mat);
  previewObj.computeLineDistances();
  scene.add(previewObj);

  // Blob dots (raised-texture site markers), if the design has them.
  const blobSites = window.__blobPreviewSites;
  if(blobSites && blobSites.length >= 3){
    const bg = new THREE.BufferGeometry();
    bg.setAttribute('position', new THREE.Float32BufferAttribute(blobSites, 3));
    const bmat = new THREE.PointsMaterial({
      size: 1.6,
      sizeAttenuation: true,
      color: 0xffc24c,
      transparent: true,
      opacity: 0.9
    });
    previewBlobObj = new THREE.Points(bg, bmat);
    scene.add(previewBlobObj);
  }

  // "Draft preview" label -- top-right, clear of the telemetry card (which
  // owns the top-left corner) at any telemetry collapsed/expanded state.
  if(!previewLabel){
    previewLabel = document.createElement('div');
    previewLabel.className = 'draft-preview-label';
    previewLabel.textContent = 'Draft preview';
    wrap.appendChild(previewLabel);
  }
  previewLabel.style.display = 'block';

  // Hide the "drop gcode" overlay while the draft preview is visible.
  var ov = document.getElementById('overlay');
  if(ov) ov.style.display = 'none';

  // Hide any previously-generated (rainbow) path so the live blue draft is
  // clearly visible while the user is designing — the generated path is
  // opaque and would otherwise occlude the thin draft line. It is restored
  // by clearPreview() when the user views the generated G-code again.
  if(pathObj) pathObj.visible = false;
  if(travelObj) travelObj.visible = false;
  nozzle.visible = false;

  render();
};

window.clearPreview = function(){
  if(previewObj){
    scene.remove(previewObj);
    previewObj.geometry.dispose();
    previewObj.material.dispose();
    previewObj = null;
  }
  if(previewBlobObj){
    scene.remove(previewBlobObj);
    previewBlobObj.geometry.dispose();
    previewBlobObj.material.dispose();
    previewBlobObj = null;
  }
  if(previewLabel) previewLabel.style.display = 'none';
  if(window.hideShapeCage) window.hideShapeCage();
  // Restore the generated (rainbow) path that showPreview hid, so viewing the
  // G-code again brings it back. Travels follow their own toggle.
  if(pathObj) pathObj.visible = true;
  if(travelObj) travelObj.visible = document.getElementById('t-travel').checked;
  // Restore drop overlay only if no real gcode is loaded.
  if(!lastData){
    var ov = document.getElementById('overlay');
    if(ov) ov.style.display = '';
  }
  render();
};

// ---- shape cage (3D, draggable grid of points on the draft preview) --------
// A full N-row x M-col cage of orange spheres wrapped around the model that
// lets the user drag local radius bumps/dents directly on the 3D preview,
// mirrored two-way with design.cage (see designer.js refreshShapeCage).
//
// Picking is screen-space (cagePickAt), not a raycast against the tiny 1.6mm
// spheres -- a raycast miss used to fall straight through to OrbitControls
// and yank the view while the user was trying to grab a dot. Merely hovering
// a handle now suppresses the left-button orbit BEFORE the click happens
// (cageSetHoverLock), so a grab can never be mistaken for an orbit gesture.
// Wheel zoom and right-button pan deliberately keep working while hovering --
// see cageSetHoverLock for why that distinction matters.
//
// Freeze-safety: pointerdown is the only *drag-starting* listener on the
// renderer canvas -- pointermove/pointerup/pointercancel for an active drag
// live on `window` so a drag that ends with the mouse released outside the
// canvas (or the window losing focus) still terminates the drag and
// re-enables OrbitControls. `controls.enabled = false` is written in exactly
// one place (drag start) and restored on every exit: cageEndDrag(), window
// blur, and hideShapeCage(). The lighter hover-lock is released by those same
// three plus canvas pointerleave.
let cageGroup = null;            // THREE.Group holding spheres + wireframe
let cageSpheres = [];            // flat list of {mesh,i,j}, ordered i*cols+j
let cageRows = 0, cageCols = 0;
let cageBase = null;             // N x M base radii (mm, before cage scale)
let cageScales = null;           // N x M current cage scale values
let cageHeight = 0;
let cageDragCallback = null;     // onDrag(changes) -- changes = [{i,j,scale}, ...]
let cageActive = null;           // {i,j} of the handle being dragged, or null
let cageRowLines = [];           // THREE.Line per row (closed ring)
let cageColLines = [];           // THREE.Line per column (open polyline)
let cageHover = -1;              // index into cageSpheres currently hovered, or -1
let cageSelection = new Set();   // flat indices (i*cageCols+j) currently selected
let cageDragStart = null;        // [{idx,i,j,scale}, ...] snapshot at drag start
let cageDragAnchorScale = 0;     // starting scale of the actively-dragged handle
let cageRings = [];              // one camera-facing ring sprite per handle
let cageRingTex = null;          // shared ring texture, built once and cached
const CAGE_PICK_PX = 14;         // screen-space pick radius, in CSS pixels
// Ring sprite size in mm. Must leave clear dark space between the dot and the
// ring: at 6.5 the stroke landed 0.28mm off a hovered (2.16mm) dot, close
// enough that it blended into the dot's antialiased edge and read as an
// orange halo rather than a white ring. At 9.0 the stroke sits ~3.5mm out,
// about 1mm clear, while still fitting between adjacent handles on a small
// model (8 columns on a 10mm radius puts neighbours 7.9mm apart).
const CAGE_RING_MM = 9.0;
// Captured at module load, before any hover can have nulled it out.
const CAGE_LEFT_BUTTON = controls.mouseButtons.LEFT;
const cageRaycaster = new THREE.Raycaster();
const cagePointerNDC = new THREE.Vector2();
const cageDragPlane = new THREE.Plane();
const cageDragPoint = new THREE.Vector3();
const cageTangent = new THREE.Vector3();
const cageRadialDir = new THREE.Vector3();
let cageListenersBound = false;

function cageClamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }

function cageUpdatePointerNDC(e){
  const rect = renderer.domElement.getBoundingClientRect();
  cagePointerNDC.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  cagePointerNDC.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
}

// Screen-space proximity pick: returns the index into cageSpheres of the
// nearest handle within CAGE_PICK_PX of (clientX, clientY), or -1. Deliberately
// NOT a raycast against the handle geometry -- the spheres are 1.6mm, so a
// raycast miss by a pixel used to fall straight through to OrbitControls.
// Projection matches window.__cageDebug()'s screen-position math exactly.
// Ties (equal screen distance) break toward the handle nearer the camera.
function cagePickAt(clientX, clientY){
  if(!cageSpheres.length) return -1;
  const rect = renderer.domElement.getBoundingClientRect();
  let best = -1, bestDist = Infinity, bestZ = Infinity;
  for(let k = 0; k < cageSpheres.length; k++){
    const v = cageSpheres[k].mesh.position.clone().project(camera);
    if(v.z < -1 || v.z > 1) continue;   // behind the camera / clipped
    const sx = rect.left + (v.x+1)/2*rect.width;
    const sy = rect.top + (1-v.y)/2*rect.height;
    const dx = sx - clientX, dy = sy - clientY;
    const dist = Math.sqrt(dx*dx + dy*dy);
    if(dist > CAGE_PICK_PX) continue;
    if(dist < bestDist || (dist === bestDist && v.z < bestZ)){
      best = k; bestDist = dist; bestZ = v.z;
    }
  }
  return best;
}

// White ring drawn around a selected handle, as a Sprite so it always faces
// the camera without any per-frame billboarding work.
//
// Selection needs its own channel. It was originally folded into the size
// bump, which made a selected dot and a hovered dot both 1.35x -- visually
// identical, so there was no way to tell "this is picked" from "the cursor
// happens to be here". The three states are now fully orthogonal:
//   colour = edited (red) / hovered (orange) / idle (amber)
//   size   = hovered
//   ring   = selected
// White is chosen deliberately: it is outside the amber/orange/red ramp, so
// it reads on all three dot colours and adds no new meaning to that ramp.
function cageRingTexture(){
  if(cageRingTex) return cageRingTex;
  const S = 128;
  const c = document.createElement('canvas');
  c.width = S; c.height = S;
  const g = c.getContext('2d');
  // Dark outer casing first, so the white ring keeps its edge even when it
  // passes over a bright amber dot or a pale part of the model behind it.
  g.strokeStyle = 'rgba(0,0,0,0.55)';
  g.lineWidth = 16;
  g.beginPath();
  g.arc(S/2, S/2, S/2 - 14, 0, Math.PI*2);
  g.stroke();
  g.strokeStyle = '#ffffff';
  g.lineWidth = 10;
  g.beginPath();
  g.arc(S/2, S/2, S/2 - 14, 0, Math.PI*2);
  g.stroke();
  // Cached for the page lifetime and shared by every ring; deliberately NOT
  // disposed in hideShapeCage, since the cage is shown/hidden repeatedly.
  cageRingTex = new THREE.CanvasTexture(c);
  // Tag it as sRGB so the white stays white through the renderer's colour
  // pipeline instead of being treated as linear data.
  if(THREE.SRGBColorSpace) cageRingTex.colorSpace = THREE.SRGBColorSpace;
  return cageRingTex;
}

// Walks every handle and sets its material color + mesh scale from its
// edited/hovered/selected state. Colour precedence is deliberately
// edited-over-hover: hovering an edited dot must not hide the fact that it
// is edited, so hover never overwrites the "you changed this" red. Hover is
// instead ALWAYS signalled by size instead, which stays legible on top of
// either colour. Selection is also signalled by size (additively), so a
// selected+hovered dot is visibly larger than either alone.
// These are three.js hex literals private to this subsystem, deliberately
// NOT the reserved CSS --danger/--warn safety tokens (see style.css) -- this
// red means "you edited this point", not "unsafe machine state".
function cageRestyle(){
  for(let k = 0; k < cageSpheres.length; k++){
    const s = cageSpheres[k];
    const idx = s.i*cageCols + s.j;
    const scale = (cageScales && cageScales[s.i]) ? cageScales[s.i][s.j] : 1.0;
    const edited = Math.abs(scale - 1.0) > 1e-6;
    const hovered = (idx === cageHover);
    const selected = cageSelection.has(idx);
    const color = edited ? 0xe8443a : (hovered ? 0xff8c1a : 0xffc24c);
    s.mesh.material.color.setHex(color);
    // Size carries hover ONLY -- see cageRingTexture() for why selection is
    // not allowed to share this channel.
    s.mesh.scale.setScalar(hovered ? 1.35 : 1.0);
    const ring = cageRings[k];
    if(ring){
      ring.visible = selected;
      // Handles move while dragging, so keep the ring pinned to its dot here
      // (cageRestyle runs on every drag move).
      const p = s.mesh.position;
      ring.position.set(p.x, p.y, p.z);
    }
  }
  if(typeof window.onCageSelectionChange === 'function') window.onCageSelectionChange(cageSelection.size);
}

// Hover-lock. Suppresses ONLY the left-button orbit, by nulling OrbitControls'
// LEFT binding -- deliberately not `controls.enabled = false`, which would also
// kill wheel zoom and right-button pan. With 40 handles at a 14px pick radius
// the pointer rests on a dot constantly while just looking at the model, and
// having the scroll wheel go dead whenever that happened would be worse than
// the mis-grab this whole feature exists to fix. Left-drag is the grab gesture,
// so that is the only binding that has to yield.
// An actual in-progress drag still takes the full `controls.enabled = false`
// lock below -- that path is unchanged and proven.
function cageSetHoverLock(on){
  controls.mouseButtons.LEFT = on ? null : CAGE_LEFT_BUTTON;
}

// Ends the current drag exactly the same way regardless of what triggered it
// (pointerup on window, pointercancel, or the window losing focus mid-drag).
// Controls always come back on; if the pointer is still resting on a handle the
// lighter hover-lock takes over from the full drag lock.
function cageEndDrag(){
  cageActive = null;
  cageDragStart = null;
  window.__silDragActive = false;
  controls.enabled = true;
  cageSetHoverLock(cageHover >= 0);
  renderer.domElement.style.cursor =
    measureOn ? 'crosshair' : ((cageHover >= 0) ? 'pointer' : '');
}

function cageRebuildLines(){
  if(!cageGroup) return;
  for(let i = 0; i < cageRows; i++){
    const line = cageRowLines[i];
    if(!line) continue;
    const pos = line.geometry.attributes.position;
    for(let j = 0; j < cageCols; j++){
      const p = cageSpheres[i*cageCols+j].mesh.position;
      pos.setXYZ(j, p.x, p.y, p.z);
    }
    const first = cageSpheres[i*cageCols+0].mesh.position;
    pos.setXYZ(cageCols, first.x, first.y, first.z);   // close the ring
    pos.needsUpdate = true;
  }
  for(let j = 0; j < cageCols; j++){
    const line = cageColLines[j];
    if(!line) continue;
    const pos = line.geometry.attributes.position;
    for(let i = 0; i < cageRows; i++){
      const p = cageSpheres[i*cageCols+j].mesh.position;
      pos.setXYZ(i, p.x, p.y, p.z);
    }
    pos.needsUpdate = true;
  }
}

function cageBindListeners(){
  if(cageListenersBound) return;
  cageListenersBound = true;
  const canvas = renderer.domElement;

  // pointerdown stays on the canvas. Picking is screen-space (cagePickAt),
  // not a raycast -- see the comment on that function.
  canvas.addEventListener('pointerdown', function(e){
    if(!cageSpheres.length) return;
    // The measure tool owns the left button while it is active. Without this
    // a click aimed at a measurement point would also land as "clicked empty
    // space" here and silently wipe the cage selection underneath.
    if(measureOn) return;
    // Left button only. Right-drag is OrbitControls' pan and middle is its
    // dolly; hijacking either to move a handle would make navigation
    // unpredictable exactly where the dots are densest.
    if(e.button !== 0) return;
    const hit = cagePickAt(e.clientX, e.clientY);

    // Multi-select modifier. Shift is accepted alongside ctrl/meta because
    // ctrl+click is unreliable in practice -- some platforms and input
    // devices re-map or swallow it (on macOS it is right-click emulation),
    // and when it is swallowed the click falls through to the plain-click
    // path, which REPLACES the selection and makes previously picked dots
    // silently vanish. Shift is never intercepted, so it is the dependable
    // path; both are documented in the panel hint.
    const addToSel = e.ctrlKey || e.metaKey || e.shiftKey;

    if(hit < 0){
      // Missed every handle. Plain click clears the selection; a modified
      // click on empty space is a no-op (so a stray miss while multi-
      // selecting doesn't wipe the picks already made). Either way, do NOT
      // preventDefault -- let OrbitControls orbit normally.
      if(!addToSel){
        cageSelection.clear();
        cageRestyle();
        render();
      }
      return;
    }

    const s = cageSpheres[hit];
    const idx = s.i*cageCols + s.j;

    if(addToSel){
      // Toggle membership only -- never starts a drag.
      if(cageSelection.has(idx)) cageSelection.delete(idx); else cageSelection.add(idx);
      cageRestyle();
      render();
      e.preventDefault();
      return;
    }

    if(!cageSelection.has(idx)){
      cageSelection.clear();
      cageSelection.add(idx);
    }

    // Snapshot every selected handle's starting scale so the whole group can
    // be dragged rigidly together (see the delta-clamp in the move handler).
    cageDragStart = [];
    cageSelection.forEach(function(fi){
      const si = Math.floor(fi / cageCols), sj = fi % cageCols;
      const sc = (cageScales && cageScales[si]) ? cageScales[si][sj] : 1.0;
      cageDragStart.push({ idx: fi, i: si, j: sj, scale: sc });
    });
    cageDragAnchorScale = (cageScales && cageScales[s.i]) ? cageScales[s.i][s.j] : 1.0;
    cageActive = { i: s.i, j: s.j };
    window.__silDragActive = true;
    controls.enabled = false;
    canvas.style.cursor = 'grabbing';
    e.preventDefault();
  });

  // Hover tracking lives on the canvas (not window) -- only cursor-over-
  // canvas should highlight/lock. Guarded so it only does work when the
  // hovered handle actually changes; pointermove fires constantly.
  canvas.addEventListener('pointermove', function(e){
    if(cageActive) return;   // mid-drag: the window listener below owns this
    if(measureOn) return;    // measure tool owns hover + cursor while active
    const hit = cagePickAt(e.clientX, e.clientY);
    if(hit === cageHover) return;
    cageHover = hit;
    cageRestyle();
    canvas.style.cursor = (cageHover >= 0) ? 'pointer' : '';
    cageSetHoverLock(cageHover >= 0);   // orbit is dead before the click lands
    render();
  });

  // Leaving the canvas clears hover and releases the hover-lock -- see the
  // freeze-safety note at the top of this section. Skipped mid-drag: dragging
  // a handle out past the canvas edge is normal, and the drag's own full lock
  // must stay in force until pointerup.
  canvas.addEventListener('pointerleave', function(){
    if(cageActive) return;
    if(cageHover < 0) return;   // nothing to clear; don't burn a render
    cageHover = -1;
    cageRestyle();
    cageSetHoverLock(false);
    canvas.style.cursor = '';
    render();
  });

  // move/up/cancel + blur all live on window so releasing (or losing focus)
  // outside the canvas can never leave controls.enabled stuck at false.
  window.addEventListener('pointermove', function(e){
    if(!cageActive) return;
    const i = cageActive.i, j = cageActive.j;
    const mesh = cageSpheres[i*cageCols+j].mesh;
    const theta = mesh.userData.theta;

    // Tangent = d/dtheta of the handle's world position (cos t, ., -sin t),
    // i.e. (-sin t, ., -cos t) -- the world Z component carries the same
    // negation as the placement above. It is the drag plane's normal, so the
    // plane spans the radial direction and the vertical axis.
    cageTangent.set(-Math.sin(theta), 0, -Math.cos(theta));
    cageDragPlane.setFromNormalAndCoplanarPoint(cageTangent, mesh.position);

    cageUpdatePointerNDC(e);
    cageRaycaster.setFromCamera(cagePointerNDC, camera);
    const hit = cageRaycaster.ray.intersectPlane(cageDragPlane, cageDragPoint);
    if(!hit) return;   // ray near-parallel to the plane -- ignore this move

    cageRadialDir.set(Math.cos(theta), 0, -Math.sin(theta));
    const dist = cageDragPoint.dot(cageRadialDir);
    const base = cageBase[i][j];
    const newScale = cageClamp(dist / base, 0.5, 1.5);

    // Move the whole selection together, but clamp the shared delta so no
    // selected handle would leave [0.5, 1.5] -- this keeps the group rigid
    // instead of letting handles pile up on the rail and silently distort
    // the group's relative shape.
    let delta = newScale - cageDragAnchorScale;
    let maxDelta = Infinity, minDelta = -Infinity;
    for(let k = 0; k < cageDragStart.length; k++){
      maxDelta = Math.min(maxDelta, 1.5 - cageDragStart[k].scale);
      minDelta = Math.max(minDelta, 0.5 - cageDragStart[k].scale);
    }
    delta = Math.max(minDelta, Math.min(maxDelta, delta));

    const changes = [];
    for(let k = 0; k < cageDragStart.length; k++){
      const st = cageDragStart[k];
      const s = st.scale + delta;
      if(cageScales && cageScales[st.i]) cageScales[st.i][st.j] = s;
      const m = cageSpheres[st.i*cageCols+st.j].mesh;
      const th = m.userData.theta;
      const r = cageBase[st.i][st.j] * s;
      m.position.set(r * Math.cos(th), m.position.y, -r * Math.sin(th));
      changes.push({ i: st.i, j: st.j, scale: s });
    }

    cageRebuildLines();
    cageRestyle();
    render();
    if(cageDragCallback) cageDragCallback(changes);
  });

  window.addEventListener('pointerup', cageEndDrag);
  window.addEventListener('pointercancel', cageEndDrag);
  // blur is a stronger reset than cageEndDrag alone -- hover state has no
  // meaning once the window isn't focused, so force controls back on
  // unconditionally rather than leaving them hostage to a hover that will
  // never get a pointerleave to clear it (e.g. alt-tab while hovering).
  window.addEventListener('blur', function(){
    cageHover = -1;            // cleared BEFORE cageEndDrag so it can't re-lock
    cageEndDrag();
    controls.enabled = true;
    cageSetHoverLock(false);
    canvas.style.cursor = '';
    if(cageSpheres.length){ cageRestyle(); render(); }
  });

  // Escape: mid-drag, revert to the pre-drag scales (and tell designer.js so
  // design.cage matches); otherwise, if there's a selection, just clear it.
  window.addEventListener('keydown', function(e){
    if(e.key !== 'Escape') return;
    if(measureOn) return;    // Esc belongs to the measure tool while it is open
    if(cageActive && cageDragStart){
      const changes = [];
      for(let k = 0; k < cageDragStart.length; k++){
        const st = cageDragStart[k];
        if(cageScales && cageScales[st.i]) cageScales[st.i][st.j] = st.scale;
        const m = cageSpheres[st.i*cageCols+st.j].mesh;
        const th = m.userData.theta;
        const r = cageBase[st.i][st.j] * st.scale;
        m.position.set(r * Math.cos(th), m.position.y, -r * Math.sin(th));
        changes.push({ i: st.i, j: st.j, scale: st.scale });
      }
      cageRebuildLines();
      if(cageDragCallback) cageDragCallback(changes);
      cageEndDrag();
      cageRestyle();
      render();
    } else if(cageSelection.size){
      cageSelection.clear();
      cageRestyle();
      render();
    }
  });
}

window.showShapeCage = function(data, onDrag){
  if(!data || !data.base || !data.base.length || !data.cols) return;
  // A preview refresh (e.g. right after a drag) calls back in here and must
  // not lose the user's selection -- snapshot it before the internal
  // hideShapeCage() call (which clears it) and restore below, but only if
  // the grid shape didn't change: the flat indices in the selection mean a
  // different handle once rows/cols change, so a stale selection would
  // silently select the wrong points.
  const prevRows = cageRows, prevCols = cageCols;
  const prevSelection = new Set(cageSelection);
  window.hideShapeCage();

  const N = data.rows, M = data.cols;
  cageRows = N; cageCols = M;
  cageHeight = data.height;
  cageBase = data.base;
  cageScales = data.scales;
  cageDragCallback = onDrag;

  const group = new THREE.Group();
  const sphereGeo = new THREE.SphereGeometry(1.6, 12, 10);
  cageSpheres = [];
  cageRings = [];

  for(let i = 0; i < N; i++){
    const t = N > 1 ? i/(N-1) : 0;
    for(let j = 0; j < M; j++){
      const theta = 2*Math.PI*j/M;
      const scale = (cageScales && cageScales[i]) ? cageScales[i][j] : 1.0;
      const r = cageBase[i][j] * scale;
      // transparent + high renderOrder so the dots draw in the transparent
      // pass AFTER the semi-transparent blue draft (opaque objects always
      // render before transparent ones, so an opaque dot would be painted
      // over by the draft and vanish at some angles). depthTest off keeps
      // them on top of the model geometry.
      const mat = new THREE.MeshBasicMaterial({ color: 0xffc24c,
        depthTest: false, depthWrite: false, transparent: true, opacity: 1.0 });
      const mesh = new THREE.Mesh(sphereGeo, mat);
      mesh.renderOrder = 20;
      // World Z is NEGATED printer Y (see the coordinate-mapping note at the
      // top of this file). Handle (i,j) steers the model at printer bed angle
      // theta = 2*pi*j/M, and preview_math.js sweeps that angle to world
      // (r*cos theta, ., -r*sin theta) -- so the handle must be drawn there
      // too. Without the minus sign the cage is the mirror image of the model
      // it deforms, and dragging the front-left handle bulges the front-right.
      mesh.position.set(r * Math.cos(theta), t * data.height, -r * Math.sin(theta));
      mesh.userData = { i: i, j: j, theta: theta, t: t };
      group.add(mesh);
      cageSpheres.push({ mesh: mesh, i: i, j: j });

      // Selection ring, hidden until the handle is selected. renderOrder 19
      // puts it just under the dot (20) so the dot stays crisp on top; the
      // ring radius clears even a hovered 1.35x dot, so they never overlap.
      // color is set explicitly rather than left to default so the ring can
      // never pick up a tint from anywhere else.
      const ringMat = new THREE.SpriteMaterial({ map: cageRingTexture(),
        color: 0xffffff, depthTest: false, depthWrite: false, transparent: true });
      const ring = new THREE.Sprite(ringMat);
      ring.scale.set(CAGE_RING_MM, CAGE_RING_MM, 1);
      ring.position.set(mesh.position.x, mesh.position.y, mesh.position.z);
      // Above the dot (20): the ring clears the dot geometrically, so drawing
      // it on top costs nothing and guarantees it is never occluded.
      ring.renderOrder = 21;
      ring.visible = false;
      group.add(ring);
      cageRings.push(ring);
    }
  }

  // Row rings (closed loops around the model at each height).
  cageRowLines = [];
  for(let i = 0; i < N; i++){
    const positions = [];
    for(let j = 0; j <= M; j++){
      const p = cageSpheres[i*M + (j % M)].mesh.position;
      positions.push(p.x, p.y, p.z);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    const mat = new THREE.LineBasicMaterial({ color: 0xffc24c, transparent: true, opacity: 0.35, depthTest: false });
    const line = new THREE.Line(geo, mat);
    line.renderOrder = 10;
    group.add(line);
    cageRowLines.push(line);
  }
  // Column polylines (vertical ribs connecting each angular position).
  cageColLines = [];
  for(let j = 0; j < M; j++){
    const positions = [];
    for(let i = 0; i < N; i++){
      const p = cageSpheres[i*M+j].mesh.position;
      positions.push(p.x, p.y, p.z);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    const mat = new THREE.LineBasicMaterial({ color: 0xffc24c, transparent: true, opacity: 0.35, depthTest: false });
    const line = new THREE.Line(geo, mat);
    line.renderOrder = 10;
    group.add(line);
    cageColLines.push(line);
  }

  group.userData.sphereGeo = sphereGeo;
  scene.add(group);
  cageGroup = group;

  cageBindListeners();

  if(prevRows === N && prevCols === M){
    cageSelection = prevSelection;
  } else if(prevRows){
    // Grid shape changed -- the stored flat indices now name other handles,
    // so this is the one place a selection is dropped without the user
    // asking. (prevRows 0 means there was no previous cage at all.)
    cageSelection = new Set();
  }
  cageRestyle();   // so a reloaded design shows its edited points red immediately
  render();
};

// Returns the current selection as [{i,j}, ...] for designer.js's "reset
// selected points" button.
// Clears the selection from the sidebar, so it never depends on the Esc key
// reaching this listener.
window.clearCageSelection = function(){
  if(!cageSelection.size) return;
  cageSelection.clear();
  cageRestyle();
  render();
};

window.getCageSelection = function(){
  return Array.from(cageSelection).sort(function(a,b){ return a-b; }).map(function(idx){
    return { i: Math.floor(idx / cageCols), j: idx % cageCols };
  });
};

// Debug/automation hook: handle count + world/screen positions so tests can
// drive drags without knowing three.js internals.
window.__cageDebug = function(){
  const rect = renderer.domElement.getBoundingClientRect();
  return {
    rows: cageRows, cols: cageCols,
    count: cageSpheres.length,
    hasGroup: !!cageGroup,
    inScene: cageGroup ? scene.children.indexOf(cageGroup) >= 0 : null,
    dragActive: !!cageActive,
    hover: cageHover,
    selection: Array.from(cageSelection).sort(function(a,b){ return a-b; }),
    controlsEnabled: controls.enabled,
    hoverLocked: controls.mouseButtons.LEFT === null,
    colors: cageSpheres.map(function(s){ return s.mesh.material.color.getHex(); }),
    scaleValues: cageSpheres.map(function(s){
      return (cageScales && cageScales[s.i]) ? cageScales[s.i][s.j] : 1.0;
    }),
    dotScales: cageSpheres.map(function(s){ return s.mesh.scale.x; }),
    ringsVisible: cageRings.map(function(r){ return !!r.visible; }),
    dotMat: cageSpheres.length ? {
      transparent: cageSpheres[0].mesh.material.transparent,
      depthTest: cageSpheres[0].mesh.material.depthTest,
      depthWrite: cageSpheres[0].mesh.material.depthWrite,
      renderOrder: cageSpheres[0].mesh.renderOrder
    } : null,
    handles: cageSpheres.map(function(s){
      const v = s.mesh.position.clone().project(camera);
      return {
        i: s.i, j: s.j,
        pos: s.mesh.position.toArray(),
        screen: [rect.left + (v.x+1)/2*rect.width, rect.top + (1-v.y)/2*rect.height]
      };
    })
  };
};

window.hideShapeCage = function(){
  if(cageGroup){
    cageSpheres.forEach(function(s){ s.mesh.material.dispose(); });
    cageRings.forEach(function(r){ r.material.dispose(); });   // shared map kept
    if(cageGroup.userData.sphereGeo) cageGroup.userData.sphereGeo.dispose();
    cageRowLines.forEach(function(l){ l.geometry.dispose(); l.material.dispose(); });
    cageColLines.forEach(function(l){ l.geometry.dispose(); l.material.dispose(); });
    scene.remove(cageGroup);
    cageGroup = null;
  }
  cageSpheres = [];
  cageRings = [];
  cageRowLines = [];
  cageColLines = [];
  // Failsafe: always leave drag/hover/selection state clean, even if hide is
  // called mid-drag or mid-hover (e.g. the checkbox is toggled off while
  // dragging, or a preview refresh rebuilds the cage under the mouse).
  cageActive = null;
  cageDragStart = null;
  cageHover = -1;
  // The SELECTION deliberately survives a hide. clearPreview() calls this
  // every time the draft preview is rebuilt -- which happens as soon as you
  // start dragging a handle -- so clearing here made a multi-point selection
  // evaporate the instant the user began adjusting it. The selection is only
  // dropped by an explicit user action (Esc / Clear selection, both via
  // window.clearCageSelection) or by a grid-shape change in showShapeCage,
  // where the stored flat indices would otherwise point at other handles.
  window.__silDragActive = false;
  controls.enabled = true;
  cageSetHoverLock(false);
  renderer.domElement.style.cursor = measureOn ? 'crosshair' : '';
  // Re-assert the real count: the sidebar buttons stay correct even with no
  // handles on screen, and getCageSelection() still resolves (cageCols is
  // left intact), so "Reset selected points" keeps working.
  if(typeof window.onCageSelectionChange === 'function'){
    window.onCageSelectionChange(cageSelection.size);
  }
  render();
};

// ---- measure tool -----------------------------------------------------------
// A read-only instrument. It reports distances read off the toolpath that is
// already on screen; it never writes G-code and never feeds a value back into
// the design.
//
// What the numbers mean, precisely: every figure is measured on the toolpath
// CENTRELINE, which is not what calipers read off the finished part. A bead
// straddles its centreline, so the printed wall stands half a line width
// further out on each side -- an outer diameter is centreline + one full line
// width, a bore is centreline - one full line width. The card prints those two
// derived figures beside the measured one and labels them derived, rather than
// folding the correction in silently. A dimension that quietly means something
// other than what you would measure is how a lid gets printed to the wrong
// size.
//
// Picks snap to the toolpath and never to empty space, so the same spot clicked
// twice gives the same number.

const MEASURE_CELL = 2.0;       // pick-grid cell size (mm)
const MEASURE_STEP = 1.0;       // max spacing between pick samples along a segment (mm)
const MEASURE_PICK_PX = 14;     // screen-space pick radius (CSS px), as CAGE_PICK_PX
const MEASURE_CLICK_PX = 5;     // pointer travel under which a press counts as a click
const MEASURE_COL = 0x22d3ee;   // mirrors --measure in style.css -- keep the two in step
const MEASURE_MIN_RING = 12;    // fewest band samples that can define a cross-section
// Largest angular gap to the far side of a cross-section that still counts as
// "closed". Past this there is no opposite wall within reach and the diameter
// is withheld rather than measured to whatever happened to be nearest.
const MEASURE_OPP_RAD = 0.25;   // radians (~14 deg)
const MEASURE_BAND_MAX = 2.0;   // widest half-band the ring search will grow to (mm)
const MEASURE_BINS = 72;        // angular sectors (5 deg) used to trace the outer profile

let measureOn = false;
let measureMode = 'span';       // 'span' = point to point, 'ring' = radius/diameter
let measurePts = [];            // picked world-space points, in click order
let measureHoverPt = null;      // ghost point under the cursor, or null
let measureRingInfo = null;     // measureRing() result for the current ring pick
let measureGroup = null;        // scene overlay (markers + spans)
let measureLineMat = null;      // shared fat-line material for the overlay
let measureTagEl = null;        // floating value tag over the canvas
let measureTagPos = null;       // world anchor for that tag, or null when hidden
let measureDownX = 0, measureDownY = 0, measureDownOK = false;
let measureHoverX = 0, measureHoverY = 0, measureHoverRAF = 0;
let measureListenersBound = false;

// Pick cloud: one sample every MEASURE_STEP mm (or less) along the extrude
// path, plus the segment each sample came from -- so a pick can be refined onto
// the exact segment, and so segments the scrubber has not revealed yet can be
// excluded.
let msX = null, msY = null, msZ = null, msSeg = null;
let msGrid = null;              // Map "ix,iy,iz" -> array of sample indices

const msRaycaster = new THREE.Raycaster();
const msNDC = new THREE.Vector2();
const msPoint = new THREE.Vector3();
const msProj = new THREE.Vector3();

function measureKey(x, y, z){
  return Math.floor(x/MEASURE_CELL) + ',' + Math.floor(y/MEASURE_CELL) + ',' +
         Math.floor(z/MEASURE_CELL);
}

function measureBuildGrid(){
  msX = msY = msZ = msSeg = null;
  msGrid = null;
  if(!extArr || extArr.length < 6) return;
  const nSeg = extArr.length / 6;
  const xs = [], ys = [], zs = [], sg = [];
  for(let s = 0; s < nSeg; s++){
    const b = s*6;
    const ax = extArr[b], ay = extArr[b+1], az = extArr[b+2];
    const dx = extArr[b+3]-ax, dy = extArr[b+4]-ay, dz = extArr[b+5]-az;
    // Long moves (a calibration disk's straight fills) would otherwise only
    // register at their endpoints, and clicking the middle of one would snap
    // the marker to an end. Subdivide anything longer than MEASURE_STEP.
    const n = Math.max(1, Math.ceil(Math.sqrt(dx*dx+dy*dy+dz*dz) / MEASURE_STEP));
    for(let k = 0; k < n; k++){
      const t = k/n;
      xs.push(ax+dx*t); ys.push(ay+dy*t); zs.push(az+dz*t); sg.push(s);
    }
  }
  const lb = (nSeg-1)*6;                        // close with the final vertex
  xs.push(extArr[lb+3]); ys.push(extArr[lb+4]); zs.push(extArr[lb+5]); sg.push(nSeg-1);

  msX = new Float32Array(xs); msY = new Float32Array(ys); msZ = new Float32Array(zs);
  msSeg = new Uint32Array(sg);
  msGrid = new Map();
  for(let i = 0; i < msX.length; i++){
    const key = measureKey(msX[i], msY[i], msZ[i]);
    const cell = msGrid.get(key);
    if(cell) cell.push(i); else msGrid.set(key, [i]);
  }
}

// How much of the print is currently on screen. Measuring a wall the scrubber
// has not drawn yet would report a number for something the user cannot see.
// A fresh InstancedBufferGeometry starts at Infinity (= draw everything).
function measureDrawnSegs(){
  if(!pathObj || !pathObj.geometry || !extArr) return 0;
  const n = pathObj.geometry.instanceCount;
  const all = extArr.length/6;
  return (n == null || !isFinite(n)) ? all : Math.min(n, all);
}

// Millimetres per on-screen pixel at `dist` from the camera. The pick
// tolerance is defined in CSS pixels and converted through this, so it stays a
// constant on-screen size at every zoom level instead of getting unusably tight
// when you zoom out.
function measureMmPerPx(dist){
  const h = wrap.clientHeight || 1;
  return 2 * dist * Math.tan(camera.fov*Math.PI/360) / h;
}

// Slab test: [entry, exit] parameters of `ray` through the loaded model's
// bounds, padded so a pick just off the surface still starts marching inside.
// null when the ray misses the model entirely.
function measureRaySpan(ray, pad){
  if(!lastData) return null;
  const d = lastData;
  // World Z runs OPPOSITE printer Y (wz = cy - printerY), so the printer-Y
  // span [miny, maxy] maps to the world-Z span [cy-maxy, cy-miny] -- max and
  // min swap sides. Subtracting cy from each without the flip gave the
  // mirror-image slab, which for an off-centre model does not contain the
  // model at all and made every measure pick miss.
  const lo = [d.minx-BED_X/2-pad, d.minz-pad, BED_Y/2-d.maxy-pad];
  const hi = [d.maxx-BED_X/2+pad, d.maxz+pad, BED_Y/2-d.miny+pad];
  const o = [ray.origin.x, ray.origin.y, ray.origin.z];
  const v = [ray.direction.x, ray.direction.y, ray.direction.z];
  let t0 = 0, t1 = Infinity;
  for(let a = 0; a < 3; a++){
    if(Math.abs(v[a]) < 1e-9){
      if(o[a] < lo[a] || o[a] > hi[a]) return null;   // parallel and outside the slab
      continue;
    }
    let ta = (lo[a]-o[a])/v[a], tb = (hi[a]-o[a])/v[a];
    if(ta > tb){ const s = ta; ta = tb; tb = s; }
    if(ta > t0) t0 = ta;
    if(tb < t1) t1 = tb;
    if(t0 > t1) return null;
  }
  return [t0, t1];
}

// Closest point on extrude segment `s` to `ray`, clamped to the segment ends.
// Standard closest-approach between two lines; the ray direction is unit
// length, so its own squared length drops out of the denominator.
function measureClosestOnSeg(s, ray, out){
  const b = s*6;
  const ax = extArr[b], ay = extArr[b+1], az = extArr[b+2];
  const ux = extArr[b+3]-ax, uy = extArr[b+4]-ay, uz = extArr[b+5]-az;
  const o = ray.origin, d = ray.direction;
  const wx = ax-o.x, wy = ay-o.y, wz = az-o.z;
  const uu = ux*ux + uy*uy + uz*uz;
  const ud = ux*d.x + uy*d.y + uz*d.z;
  const uw = ux*wx + uy*wy + uz*wz;
  const dw = d.x*wx + d.y*wy + d.z*wz;
  const den = uu - ud*ud;
  let f = (den > 1e-9) ? (ud*dw - uw)/den : 0;
  if(f < 0) f = 0; else if(f > 1) f = 1;
  return out.set(ax+ux*f, ay+uy*f, az+uz*f);
}

// Nearest toolpath point under (clientX, clientY), or null. Marches the camera
// ray through the sample grid rather than projecting every vertex: a 3 MB
// G-code file is well over 100k segments, and a full projection sweep on every
// pointermove would make the hover ghost stutter. Front-most wins, which is
// what "the point I am looking at" means on a surface.
function measurePick(clientX, clientY){
  if(!msGrid) measureBuildGrid();
  if(!msGrid) return null;
  const drawn = measureDrawnSegs();
  if(drawn <= 0) return null;             // scrubbed to 0% -- nothing is on screen

  const rect = renderer.domElement.getBoundingClientRect();
  msNDC.x = ((clientX-rect.left)/rect.width)*2 - 1;
  msNDC.y = -((clientY-rect.top)/rect.height)*2 + 1;
  msRaycaster.setFromCamera(msNDC, camera);
  const ray = msRaycaster.ray;

  // Pad the bounds by the pick tolerance as well as a couple of cells: zoomed
  // right out, 14 px is several millimetres of world space, and a pick aimed at
  // the silhouette edge would otherwise never enter the box to start marching.
  const mmPerPx = measureMmPerPx(camera.position.distanceTo(controls.target));
  const span = measureRaySpan(ray, MEASURE_CELL*2 + MEASURE_PICK_PX*mmPerPx);
  if(!span) return null;
  const ox = ray.origin.x, oy = ray.origin.y, oz = ray.origin.z;
  const dx = ray.direction.x, dy = ray.direction.y, dz = ray.direction.z;

  const seen = new Set();                 // each cell is scanned once per pick
  let best = -1, bestT = Infinity;
  for(let t = Math.max(span[0], 0); t <= span[1]; t += MEASURE_CELL){
    const ix = Math.floor((ox+dx*t)/MEASURE_CELL);
    const iy = Math.floor((oy+dy*t)/MEASURE_CELL);
    const iz = Math.floor((oz+dz*t)/MEASURE_CELL);
    for(let a = -1; a <= 1; a++) for(let b = -1; b <= 1; b++) for(let c = -1; c <= 1; c++){
      const key = (ix+a) + ',' + (iy+b) + ',' + (iz+c);
      if(seen.has(key)) continue;
      seen.add(key);
      const cell = msGrid.get(key);
      if(!cell) continue;
      for(let m = 0; m < cell.length; m++){
        const i = cell[m];
        if(msSeg[i] >= drawn) continue;             // not drawn at this scrub position
        const wx = msX[i]-ox, wy = msY[i]-oy, wz = msZ[i]-oz;
        const along = wx*dx + wy*dy + wz*dz;
        if(along <= 0 || along >= bestT) continue;  // behind the camera, or already beaten
        const px = wx-dx*along, py = wy-dy*along, pz = wz-dz*along;
        if(Math.sqrt(px*px+py*py+pz*pz) > MEASURE_PICK_PX*measureMmPerPx(along)) continue;
        bestT = along; best = i;
      }
    }
  }
  if(best < 0) return null;

  // Refine onto the exact segment the winning sample came from, so the reading
  // is the toolpath itself and not the sampling lattice.
  measureClosestOnSeg(msSeg[best], ray, msPoint);
  return { point: msPoint.clone(), seg: msSeg[best] };
}

// One horizontal cross-section through `p`, using samples within +/-`band` of
// its height. Returns the section's centroid (the local axis), the radius out
// to `p`, and the section's OUTER wall on both the pick's side and the far side.
//
// The outer wall is found by splitting the section into angular sectors and
// keeping the FARTHEST point in each. Taking the nearest point across from the
// pick instead is what the first cut of this did, and it read 36.0 mm across a
// vase whose base is 64.0 mm: the base is solid, so there is toolpath at every
// radius from the centre outwards, and the search happily returned an infill
// line near the middle and called it the far wall. Calipers close on the
// outside of a part, so the outside is what gets traced.
function measureRingAt(p, band, drawn){
  const n = msX.length;
  let sx = 0, sz = 0, count = 0;
  for(let i = 0; i < n; i++){
    if(msSeg[i] >= drawn) continue;
    if(Math.abs(msY[i]-p.y) > band) continue;
    sx += msX[i]; sz += msZ[i]; count++;
  }
  if(count < 3) return null;
  const cx = sx/count, cz = sz/count;

  const outR = new Float64Array(MEASURE_BINS);
  const outX = new Float64Array(MEASURE_BINS);
  const outY = new Float64Array(MEASURE_BINS);
  const outZ = new Float64Array(MEASURE_BINS);
  const used = new Uint8Array(MEASURE_BINS);
  for(let i = 0; i < n; i++){
    if(msSeg[i] >= drawn) continue;
    if(Math.abs(msY[i]-p.y) > band) continue;
    const dx = msX[i]-cx, dz = msZ[i]-cz;
    const r = Math.hypot(dx, dz);
    let b = Math.floor((Math.atan2(dz, dx) + Math.PI) / (2*Math.PI) * MEASURE_BINS);
    if(b < 0) b = 0; else if(b >= MEASURE_BINS) b = MEASURE_BINS-1;
    if(!used[b] || r > outR[b]){
      used[b] = 1; outR[b] = r; outX[b] = msX[i]; outY[b] = msY[i]; outZ[b] = msZ[i];
    }
  }

  // Nearest occupied sector to `ang`, plus how far off it is in radians. An
  // empty sector means the section genuinely has no wall in that direction at
  // this height, which the caller reports rather than papering over.
  function nearestBin(ang){
    const binW = 2*Math.PI/MEASURE_BINS;
    // Wrap into [0, MEASURE_BINS) first. Callers pass theta+PI for the far
    // side, which runs past 2*PI, and an unwrapped target made the distance
    // below come out NEGATIVE -- which then slid under every "is the section
    // closed" threshold unchallenged.
    let target = ((ang + Math.PI) / (2*Math.PI) * MEASURE_BINS) % MEASURE_BINS;
    if(target < 0) target += MEASURE_BINS;
    let best = -1, bestOff = Infinity;
    for(let b = 0; b < MEASURE_BINS; b++){
      if(!used[b]) continue;
      let off = Math.abs((b + 0.5) - target);
      if(off > MEASURE_BINS/2) off = MEASURE_BINS - off;
      if(off < bestOff){ bestOff = off; best = b; }
    }
    // Discount half a sector: a fully covered section still lands up to half a
    // bin off the exact direction purely from binning, and reporting that as a
    // gap would overstate how open the section is.
    return { bin:best, off: Math.max(0, bestOff*binW - binW/2) };
  }

  const radius = Math.hypot(p.x-cx, p.z-cz);
  const theta = Math.atan2(p.z-cz, p.x-cx);
  const near = nearestBin(theta);
  const far = nearestBin(theta + Math.PI);

  let coverage = 0, outerMin = Infinity, outerMax = -Infinity;
  for(let b = 0; b < MEASURE_BINS; b++){
    if(!used[b]) continue;
    coverage++;
    if(outR[b] < outerMin) outerMin = outR[b];
    if(outR[b] > outerMax) outerMax = outR[b];
  }

  return {
    cx:cx, cz:cz, radius:radius, band:band, count:count,
    nearR: near.bin >= 0 ? outR[near.bin] : radius,
    farR:  far.bin  >= 0 ? outR[far.bin]  : 0,
    gap:   far.bin  >= 0 ? far.off : Math.PI,
    nearPt: near.bin >= 0
      ? new THREE.Vector3(outX[near.bin], outY[near.bin], outZ[near.bin])
      : p.clone(),
    opp: far.bin >= 0
      ? new THREE.Vector3(outX[far.bin], outY[far.bin], outZ[far.bin])
      : new THREE.Vector3(cx, p.y, cz),
    coverage:coverage, bins:MEASURE_BINS,
    outerMin: coverage ? outerMin : 0, outerMax: coverage ? outerMax : 0
  };
}

// Cross-section through the picked point, widening the band until the section
// actually closes.
//
// It has to widen. This generator modulates Z by up to z_amp_max, so a
// horizontal slice cuts several spiral turns at whatever phase each is in, and
// a band sized for flat layers can come back as a few disconnected arcs. A
// centroid taken from arcs is not on the axis at all, and the radius read off
// it is wrong by a lot rather than a little. So the band grows until the far
// side is genuinely covered -- and the band that was used is printed on the
// card, because a reading smeared over 2 mm of height is a different claim
// from one taken over 0.2 mm.
function measureRing(p){
  if(!msX || !lastData) return null;
  const drawn = measureDrawnSegs();
  if(drawn <= 0) return null;
  const lh = (lastData.meta && lastData.meta.layerHeight) || null;
  // Half a layer is the floor: a spiral vase climbs one layer height per
  // revolution, so a thinner band cannot contain a whole turn even on a
  // perfectly flat-layered print.
  let band = lh ? Math.max(lh*0.55, 0.15) : 0.5;
  let out = null;
  for(;;){
    const got = measureRingAt(p, band, drawn);
    if(got) out = got;
    if(got && got.count >= MEASURE_MIN_RING && got.gap <= MEASURE_OPP_RAD) return got;
    if(band >= MEASURE_BAND_MAX) break;
    band = Math.min(band*2, MEASURE_BAND_MAX);
  }
  return out;   // best effort; the caller reports the gap instead of a diameter
}

// ---- overlay ---------------------------------------------------------------
// Markers and spans deliberately ignore the depth buffer: a measurement you can
// only see when it happens to fall on the near side of the model is not much of
// a measurement. renderOrder keeps them above the toolpath.

function measureLineMaterial(){
  if(measureLineMat) return measureLineMat;
  measureLineMat = new LineMaterial({ color:MEASURE_COL, linewidth:2,
    worldUnits:false, depthTest:false, transparent:true });
  measureLineMat.resolution.set(wrap.clientWidth||1, wrap.clientHeight||1);
  return measureLineMat;
}

function measureDot(p, ghost){
  const m = new THREE.Mesh(
    new THREE.SphereGeometry(ghost ? 0.55 : 0.8, 12, 8),
    new THREE.MeshBasicMaterial({ color:MEASURE_COL, depthTest:false,
      transparent:true, opacity: ghost ? 0.45 : 1.0 }));
  m.position.copy(p);
  m.renderOrder = 1000;
  return m;
}

// `pts` is a polyline; LineSegmentsGeometry wants explicit endpoint pairs.
function measureLine(pts){
  const segs = [];
  for(let i = 0; i+1 < pts.length; i++){
    segs.push(pts[i].x, pts[i].y, pts[i].z, pts[i+1].x, pts[i+1].y, pts[i+1].z);
  }
  const g = new LineSegmentsGeometry();
  g.setPositions(segs);
  const l = new LineSegments2(g, measureLineMaterial());
  l.renderOrder = 1000;
  return l;
}

function measureEnsureGroup(){
  if(!measureGroup){
    measureGroup = new THREE.Group();
    scene.add(measureGroup);
  }
  return measureGroup;
}

function measureClearOverlay(){
  if(!measureGroup) return;
  while(measureGroup.children.length){
    const c = measureGroup.children[0];
    measureGroup.remove(c);
    if(c.geometry) c.geometry.dispose();
    // The line material is shared and cached for the page lifetime; marker
    // materials are one per marker and must go with them.
    if(c.material && c.material !== measureLineMat) c.material.dispose();
  }
}

function measureSetTag(pos, text){
  measureTagPos = pos;
  if(!measureTagEl){
    measureTagEl = document.createElement('div');
    measureTagEl.className = 'measure-tag';
    wrap.appendChild(measureTagEl);
  }
  if(text != null) measureTagEl.textContent = text;
}

// Re-projects the floating tag. Called from render() through window.__measureSync
// so it tracks orbit, zoom and resize without a loop of its own.
function measureSync(){
  if(measureLineMat) measureLineMat.resolution.set(wrap.clientWidth||1, wrap.clientHeight||1);
  if(!measureTagEl) return;
  if(!measureOn || !measureTagPos){ measureTagEl.style.display = 'none'; return; }
  msProj.copy(measureTagPos).project(camera);
  if(msProj.z < -1 || msProj.z > 1){ measureTagEl.style.display = 'none'; return; }
  measureTagEl.style.display = '';
  measureTagEl.style.left = ((msProj.x+1)/2*wrap.clientWidth) + 'px';
  measureTagEl.style.top  = ((1-msProj.y)/2*wrap.clientHeight) + 'px';
}
window.__measureSync = measureSync;

function measureMid(a, b){
  return new THREE.Vector3((a.x+b.x)/2, (a.y+b.y)/2, (a.z+b.z)/2);
}

function measureRedraw(){
  measureClearOverlay();
  measureTagPos = null;
  if(measureOn){
    const g = measureEnsureGroup();
    if(measureMode === 'span'){
      for(let i = 0; i < measurePts.length; i++) g.add(measureDot(measurePts[i], false));
      if(measurePts.length >= 2){
        g.add(measureLine([measurePts[0], measurePts[1]]));
        measureSetTag(measureMid(measurePts[0], measurePts[1]),
                      measurePts[0].distanceTo(measurePts[1]).toFixed(2) + ' mm');
      } else if(measurePts.length === 1 && measureHoverPt){
        // Rubber band: the span updates live as the cursor moves, so you can
        // see the number before committing the second point.
        g.add(measureDot(measureHoverPt, true));
        g.add(measureLine([measurePts[0], measureHoverPt]));
        measureSetTag(measureMid(measurePts[0], measureHoverPt),
                      measurePts[0].distanceTo(measureHoverPt).toFixed(2) + ' mm');
      } else if(measureHoverPt){
        g.add(measureDot(measureHoverPt, true));
      }
    } else {
      if(measureHoverPt && !measurePts.length) g.add(measureDot(measureHoverPt, true));
      if(measurePts.length){
        const p = measurePts[0], r = measureRingInfo;
        g.add(measureDot(p, false));
        if(r){
          const axis = new THREE.Vector3(r.cx, p.y, r.cz);
          const closed = r.gap <= MEASURE_OPP_RAD && r.radius > 0.05;
          // Axis tick: a short cross on the section centre, so it is obvious
          // which point the radius is being measured from.
          g.add(measureLine([new THREE.Vector3(r.cx-1.5, p.y, r.cz),
                             new THREE.Vector3(r.cx+1.5, p.y, r.cz)]));
          g.add(measureLine([new THREE.Vector3(r.cx, p.y, r.cz-1.5),
                             new THREE.Vector3(r.cx, p.y, r.cz+1.5)]));
          // The diameter is drawn across the outer wall, which is where it is
          // measured -- when the pick is on that wall the two coincide, and
          // when it is not (a pick on a solid base's infill) the line shows
          // plainly that the span is not the one through the picked point.
          g.add(measureLine(closed ? [r.nearPt, axis, r.opp] : [p, axis]));
          if(closed){
            g.add(measureDot(r.opp, true));
            measureSetTag(axis, 'd ' + (r.nearR + r.farR).toFixed(2) + ' mm');
          } else {
            measureSetTag(measureMid(p, axis), 'r ' + r.radius.toFixed(2) + ' mm');
          }
        }
      }
    }
  }
  measureSync();
  render();
}

// ---- readout ---------------------------------------------------------------

function measureFmt(v){ return v.toFixed(2) + ' mm'; }

// Printer coordinates. The scene is bed-centred with Y up; the machine is
// corner-origin with Z up. Always report what the machine would call the point,
// so a number read here can be typed straight into G-code.
//
// Printer Y is the INVERSE of parseGcode's wz = cy - printerY, i.e.
// printerY = cy - worldZ -- NOT worldZ + cy. The un-negated form reported the
// mirror-image Y for every point, and this readout is documented as safe to
// type straight into G-code, so it was handing back a coordinate on the wrong
// side of the bed.
function measureFmtPt(p){
  return (p.x+BED_X/2).toFixed(1) + ', ' + (BED_Y/2-p.z).toFixed(1) + ', ' + p.y.toFixed(1);
}

function measureRow(label, value, primary){
  return '<div class="mrow' + (primary ? ' is-primary' : '') + '"><span>' + label +
         '</span><b>' + value + '</b></div>';
}

function measureSetHint(text){
  const el = document.getElementById('measure-hint');
  if(el) el.textContent = text;
}

function measureRenderCard(){
  const body = document.getElementById('measure-read');
  if(!body) return;
  const lw = (lastData && lastData.meta && lastData.meta.lineWidth) || null;
  let html = '', hint = '';

  if(measureMode === 'span'){
    if(measurePts.length < 2){
      html = '<div class="measure-empty">Click two points on the model to span ' +
             'between them. Picks snap to the nearest toolpath point.</div>';
      hint = measurePts.length ? 'Click the second point.' : 'Click the first point.';
    } else {
      const a = measurePts[0], b = measurePts[1];
      // World X/Z are the machine's X/Y; world Y is the machine's Z.
      const dx = b.x-a.x, dz = b.z-a.z, dh = b.y-a.y;
      html = measureRow('From X,Y,Z', measureFmtPt(a)) +
             measureRow('To X,Y,Z', measureFmtPt(b)) +
             measureRow('Distance', measureFmt(Math.sqrt(dx*dx+dz*dz+dh*dh)), true) +
             measureRow('Delta X', measureFmt(dx)) +
             measureRow('Delta Y', measureFmt(dz)) +
             measureRow('Delta Z (height)', measureFmt(dh)) +
             measureRow('In the XY plane', measureFmt(Math.hypot(dx, dz))) +
             '<div class="measure-note">Toolpath centreline, not the outside of the ' +
             'wall. ' + (lw
               ? 'The printed bead stands ' + (lw/2).toFixed(2) + ' mm beyond it on each side.'
               : 'This file declares no line width, so the bead offset is unknown.') +
             '</div>';
      hint = 'Click again to start a new span.';
    }
  } else {
    if(!measurePts.length){
      html = '<div class="measure-empty">Click a point on the wall. Reports the ' +
             'radius out to it and the diameter across the model at that height.</div>';
      hint = 'Click a point on the wall.';
    } else if(!measureRingInfo){
      html = '<div class="measure-empty">Not enough of the model is drawn at this ' +
             'height to form a cross-section. Scrub the timeline further along, or ' +
             'pick a point lower down.</div>';
      hint = 'No cross-section here.';
    } else {
      const p = measurePts[0], r = measureRingInfo;
      const closed = r.gap <= MEASURE_OPP_RAD && r.radius > 0.05;
      const dia = r.nearR + r.farR;
      // A pick that is not itself on the outer wall (the solid base of a vase,
      // or any infill) still has an honest radius, but the diameter beside it
      // is measured wall-to-wall and is NOT twice that radius. Say so rather
      // than letting the two numbers sit together implying they match.
      const inside = closed && r.radius < r.nearR - 0.05;
      html = measureRow('Point X,Y,Z', measureFmtPt(p)) +
             // printerY = cy - worldZ, as measureFmtPt above -- r.cz is a
             // world-space centre and needs the same inverse, not an offset.
             measureRow('Section centre', (r.cx+BED_X/2).toFixed(1) + ', ' +
                                          (BED_Y/2-r.cz).toFixed(1)) +
             measureRow('Radius to pick', measureFmt(r.radius), !closed);
      if(closed){
        html += measureRow('Diameter', measureFmt(dia), true);
        if(lw){
          html += measureRow('Outer / inner',
            (dia+lw).toFixed(2) + ' / ' + (dia-lw).toFixed(2) + ' mm');
        }
      }
      html += '<div class="measure-note">Cross-section at Z ' + p.y.toFixed(2) +
        ', from ' + r.count + ' toolpath points within +/-' + r.band.toFixed(2) +
        ' mm of that height; the centre above is their centroid, not an assumed ' +
        'axis. Outer wall traced in ' + r.coverage + ' of ' + r.bins +
        ' sectors, ranging ' + r.outerMin.toFixed(2) + ' to ' + r.outerMax.toFixed(2) +
        ' mm from that centre, so the section is ' +
        ((r.outerMax-r.outerMin) <= 0.05
          ? 'round to within the reading.'
          : 'not round -- the diameter depends on which way you measure.');
      if(!closed){
        html += r.radius <= 0.05
          ? ' No diameter: that pick is on the section centre, so there is no ' +
            'direction to measure across. Pick a point on the wall.'
          : ' No diameter: the section has no wall opposite the pick at this ' +
            'height (nearest is ' + (r.gap*180/Math.PI).toFixed(0) +
            ' deg off), so there is nothing to measure across to.';
      } else {
        if(inside){
          html += ' The diameter is measured across the outer wall, which your ' +
                  'pick is ' + (r.nearR-r.radius).toFixed(2) + ' mm inside of, so it ' +
                  'is not twice the radius above.';
        }
        html += lw
          ? ' Outer and inner add and remove one full ' + lw.toFixed(2) +
            ' mm line width -- derived from the file header, not measured.'
          : ' This file declares no line width, so the outer and inner wall ' +
            'diameters cannot be derived.';
      }
      html += '</div>';
      hint = 'Click elsewhere to move the measurement.';
    }
  }
  body.innerHTML = html;
  measureSetHint(hint);
}

// ---- interaction -----------------------------------------------------------

function measureAddPoint(clientX, clientY){
  const hit = measurePick(clientX, clientY);
  if(!hit){ measureSetHint('Nothing there -- click on the toolpath itself.'); return; }
  if(measureMode === 'ring'){
    measurePts = [hit.point];
    measureRingInfo = measureRing(hit.point);
  } else {
    if(measurePts.length >= 2) measurePts = [];
    measurePts.push(hit.point);
  }
  measureRedraw();
  measureRenderCard();
}

function measureClear(){
  measurePts = [];
  measureRingInfo = null;
  measureRedraw();
  measureRenderCard();
}

// Hover is throttled to one pick per frame: pointermove fires far faster than
// the overlay needs rebuilding, and each rebuild allocates geometry.
function measureQueueHover(x, y){
  if(!measureOn) return;
  measureHoverX = x; measureHoverY = y;
  if(measureHoverRAF) return;
  measureHoverRAF = requestAnimationFrame(function(){
    measureHoverRAF = 0;
    if(!measureOn) return;
    const hit = measurePick(measureHoverX, measureHoverY);
    const p = hit ? hit.point : null;
    if(!p && !measureHoverPt) return;                                    // still nothing
    if(p && measureHoverPt && p.distanceToSquared(measureHoverPt) < 1e-6) return;
    measureHoverPt = p;
    measureRedraw();
  });
}

function measureBindListeners(){
  if(measureListenersBound) return;
  measureListenersBound = true;
  const canvas = renderer.domElement;

  // A press that turns into an orbit must not drop a point, so the point lands
  // on pointerup and only when the pointer barely moved. Orbit, pan and zoom
  // all keep working untouched while the tool is active -- nothing here ever
  // takes controls.enabled away, which is what makes this tool safe to leave on.
  canvas.addEventListener('pointerdown', function(e){
    if(!measureOn || e.button !== 0) return;
    measureDownOK = true; measureDownX = e.clientX; measureDownY = e.clientY;
  });
  canvas.addEventListener('pointerup', function(e){
    if(!measureOn || !measureDownOK || e.button !== 0) return;
    measureDownOK = false;
    if(Math.abs(e.clientX-measureDownX) > MEASURE_CLICK_PX ||
       Math.abs(e.clientY-measureDownY) > MEASURE_CLICK_PX) return;   // that was an orbit
    measureAddPoint(e.clientX, e.clientY);
  });
  canvas.addEventListener('pointermove', function(e){ measureQueueHover(e.clientX, e.clientY); });
  canvas.addEventListener('pointerleave', function(){
    measureDownOK = false;
    if(!measureOn || !measureHoverPt) return;
    measureHoverPt = null;
    measureRedraw();
  });
}

function measureSetOn(on){
  if(measureOn === on) return;
  measureOn = on;
  document.getElementById('tool-measure').setAttribute('aria-pressed', on ? 'true' : 'false');
  document.getElementById('measure-card').style.display = on ? '' : 'none';
  renderer.domElement.style.cursor = on ? 'crosshair' : '';
  if(on){
    measureBindListeners();
    if(!msGrid) measureBuildGrid();
  } else {
    measurePts = [];
    measureHoverPt = null;
    measureRingInfo = null;
  }
  measureRedraw();
  measureRenderCard();
}

// A new file invalidates everything the tool knows: the pick grid was built
// from the old path, and a measurement taken on it means nothing here.
function measureReload(){
  msGrid = null; msX = msY = msZ = msSeg = null;
  measurePts = [];
  measureHoverPt = null;
  measureRingInfo = null;
  syncCanvasChromeForMode();
  if(measureOn) measureBuildGrid();
  measureRedraw();
  measureRenderCard();
}

// The rail is an instrument of the G-code viewer, so it follows the mode as
// well as the file. Called on load and from designer.js's mode switch.
// Without the mode half, the rail stayed on the canvas in Design mode -- one
// click away from measuring the loaded G-code while the canvas is showing the
// blue draft preview of a shape that has since been edited.
// Everything that floats ON the canvas has to follow the app mode by hand.
// The canvas is visible in BOTH modes, so none of this chrome is covered by
// the panel's own .active class toggling -- it has to be told.
//
// The telemetry card is here for exactly that reason: load() shows it when a
// file loads (see the has-timeline block) and nothing hid it again, so
// switching Design -> G-code Viewer -> Design left a telemetry readout
// floating over the design view, describing G-code the user is no longer
// looking at. Its display is restored (not forced) when returning to the
// viewer so the card's own collapsed/expanded state survives.
function syncCanvasChromeForMode(){
  const show = viewerModeActive() && !!extArr;

  const rail = document.getElementById('tool-rail');
  if(rail) rail.style.display = show ? '' : 'none';
  if(!show && measureOn) measureSetOn(false);

  const tele = document.getElementById('telemetry-card');
  if(tele) tele.style.display = show ? '' : 'none';

  // The machine-readout strip and the filename label are the same kind of
  // thing and were missed: load() revealed them and nothing put them back, so
  // Design -> G-code Viewer -> Design left a playback timeline and a .gcode
  // filename sitting over the design view. Worse than untidy -- the scrub bar
  // is live, so dragging it there drove playback of a print the canvas was no
  // longer showing, and the layer readout kept counting against it.
  const tl = document.getElementById('tl-wrap');
  if(tl) tl.style.display = show ? '' : 'none';
  // .has-timeline widens --overlay-bottom to clear that strip; with the strip
  // hidden the clearance has to go too, or the nav cube floats 92px above
  // nothing (the exact fault the class was added to fix, in mirror image).
  if(wrap) wrap.classList.toggle('has-timeline', show);
  if(gcodeTitleEl) gcodeTitleEl.style.display = show ? 'block' : 'none';

  // The toolpath IN the scene has to follow the mode as well -- it is not
  // chrome, but it is on the same shared canvas and the rule is the same.
  //
  // Hiding it used to be a side effect of drawing the blue draft
  // (showPreview() hides the generated path; clearPreview() restores it),
  // which works only if a draft actually gets drawn. The draft stays off
  // until the user touches a design control, so anyone who generated without
  // editing -- or who scrubbed, then switched straight back -- was left
  // looking at the generated G-code in DESIGN mode, still clipped to wherever
  // the scrub was left, with the nozzle marker parked mid-print. Ownership
  // belongs to the mode switch, not to whether some other feature ran.
  if(show){
    if(pathObj) pathObj.visible = true;
    if(travelObj) travelObj.visible = document.getElementById('t-travel').checked;
    // Re-derive the marker and the readouts from the progress we left at, so
    // returning to the viewer resumes where the scrub was rather than showing
    // a marker-less path until the next scrub.
    if(lastData) setProgress(progress);
  } else {
    // A running playback would undo every line above on its next frame, and
    // it has no business animating a canvas that is showing the design.
    stopPlay();
    if(pathObj) pathObj.visible = false;
    if(travelObj) travelObj.visible = false;
    nozzle.visible = false;
  }
  render();
}
// Kept under the old name too: designer.js's activateMode() calls it, and a
// silently-renamed hook is a chrome-desync bug that only shows up at runtime.
window.__syncCanvasChrome = syncCanvasChromeForMode;
window.__measureAppMode = syncCanvasChromeForMode;

document.getElementById('tool-measure').addEventListener('click', function(){
  measureSetOn(!measureOn);
});
document.getElementById('measure-close').addEventListener('click', function(){
  measureSetOn(false);
});
document.getElementById('measure-clear').addEventListener('click', measureClear);
document.querySelectorAll('.measure-mode').forEach(function(btn){
  btn.addEventListener('click', function(){
    const m = btn.getAttribute('data-mmode');
    if(m === measureMode) return;
    measureMode = m;
    document.querySelectorAll('.measure-mode').forEach(function(b){
      const on = (b === btn);
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    measureClear();   // the two modes mean different things by "the points"
  });
});

window.addEventListener('keydown', function(e){
  if(e.ctrlKey || e.metaKey || e.altKey) return;
  if(typingInField(e)) return;
  if(e.key === 'm' || e.key === 'M'){
    const rail = document.getElementById('tool-rail');
    if(!rail || rail.style.display === 'none') return;   // nothing loaded to measure
    // Same trap the playback shortcuts fell into: the rail is revealed once on
    // load and never hidden, so it says "a file exists", not "the viewer is on
    // screen". Measuring from Design mode would report distances off the
    // loaded G-code while the canvas is showing the blue draft preview of a
    // shape that has since been edited -- a number describing something other
    // than what the user is looking at.
    if(!viewerModeActive()) return;
    measureSetOn(!measureOn);
    e.preventDefault();
  } else if(e.key === 'Escape' && measureOn){
    // First Esc drops the measurement, second closes the tool, so an accidental
    // pick is cheap to undo without losing the tool you are in the middle of.
    if(measurePts.length) measureClear(); else measureSetOn(false);
  }
});

// Test/automation hook: measure at a known machine coordinate instead of a
// screen click, so the arithmetic can be checked against a model whose real
// dimensions are known without having to land a pixel-perfect click. Snaps to
// the nearest toolpath point exactly as a click does.
window.__measureAt = function(px, py, pz){
  if(!msGrid) measureBuildGrid();
  if(!msX) return null;
  // Same printer -> world transform parseGcode uses, negation included
  // (wz = cy - printerY). This hook exists to check measurement arithmetic
  // against known dimensions, so it has to land on the same point a click
  // would; the un-negated form snapped to the mirror-image point instead.
  const wx = px - BED_X/2, wy = pz, wz = BED_Y/2 - py;
  const drawn = measureDrawnSegs();
  let best = -1, bestD = Infinity;
  for(let i = 0; i < msX.length; i++){
    if(msSeg[i] >= drawn) continue;
    const dx = msX[i]-wx, dy = msY[i]-wy, dz = msZ[i]-wz;
    const d = dx*dx + dy*dy + dz*dz;
    if(d < bestD){ bestD = d; best = i; }
  }
  if(best < 0) return null;
  const p = new THREE.Vector3(msX[best], msY[best], msZ[best]);
  if(measureMode === 'ring'){
    measurePts = [p];
    measureRingInfo = measureRing(p);
  } else {
    if(measurePts.length >= 2) measurePts = [];
    measurePts.push(p);
  }
  measureRedraw();
  measureRenderCard();
  return window.__measureState();
};

// Test/automation hook, same shape as __previewState / __viewFlags.
window.__measureState = function(){
  return {
    on: measureOn,
    mode: measureMode,
    points: measurePts.map(function(p){
      // Printer coords, same inverse as measureFmtPt: printerY = cy - worldZ.
      return [+(p.x+BED_X/2).toFixed(3), +(BED_Y/2-p.z).toFixed(3), +p.y.toFixed(3)];
    }),
    samples: msX ? msX.length : 0,
    cells: msGrid ? msGrid.size : 0,
    ring: measureRingInfo ? {
      radius: +measureRingInfo.radius.toFixed(3),
      diameter: +(measureRingInfo.nearR + measureRingInfo.farR).toFixed(3),
      band: +measureRingInfo.band.toFixed(3),
      count: measureRingInfo.count,
      coverage: measureRingInfo.coverage,
      outerMin: +measureRingInfo.outerMin.toFixed(3),
      outerMax: +measureRingInfo.outerMax.toFixed(3),
      gapDeg: +(measureRingInfo.gap*180/Math.PI).toFixed(2)
    } : null
  };
};

// ---- measure card: drag / lock / reset -------------------------------------
// The card ships CSS-anchored bottom-right of the canvas (.measure-card in
// style.css: right:76px, bottom:var(--overlay-bottom)) so by design it can sit
// right on top of the geometry someone is trying to measure. Dragging swaps it
// to an explicit inline left/top; Reset just deletes those inline styles so
// the CSS anchor takes back over -- no separate "remembered original spot" to
// keep in sync with a stylesheet this task does not own.
//
// This file owns every DOM/CSS change for this feature (index.html and
// style.css are being edited elsewhere right now), so the lock/reset buttons
// and their styling are injected here rather than hand-added to the markup.
// Colour stays on --measure/MEASURE_COL, same token the rest of this tool
// uses -- see the reserved-token comment at the top of style.css.
const MEASURE_POS_KEY = 'measure-card-pos';
const MEASURE_LOCK_KEY = 'measure-card-locked';
const MEASURE_CARD_W = 246;      // mirrors .measure-card's width in style.css
const MEASURE_REACH_PAD = 28;    // px of the card that must stay reachable, however far it is dragged

(function(){
  const card = document.getElementById('measure-card');
  const head = card ? card.querySelector('.measure-head') : null;
  const closeBtn = document.getElementById('measure-close');
  if(!card || !head || !closeBtn) return;   // defensive: markup shape changed elsewhere

  const css = document.createElement('style');
  css.textContent =
    '.measure-head{cursor:grab;touch-action:none;}' +
    '.measure-head.measure-dragging{cursor:grabbing;}' +
    '.measure-head.measure-locked{cursor:default;}' +
    '.measure-head-actions{display:flex;align-items:center;gap:2px;}' +
    '.measure-drag-btn{background:none;border:none;color:var(--ink);cursor:pointer;' +
      'font-size:12px;min-width:22px;min-height:22px;padding:2px 5px;line-height:1;' +
      'border-radius:4px;font-family:var(--font-ui);opacity:.75;' +
      'transition:color .15s,background-color .15s,opacity .15s;}' +
    '.measure-drag-btn:hover{opacity:1;color:var(--ink);background:var(--surface-raised);}' +
    '.measure-drag-btn[aria-pressed="true"]{opacity:1;color:var(--measure);' +
      'background:var(--measure-dim);}';
  document.head.appendChild(css);

  // Grouping reset+lock+close in their own flex box (instead of dropping them
  // straight into .measure-head) keeps the existing title-left/controls-right
  // layout: .measure-head is `justify-content:space-between` over exactly two
  // children today, and adding buttons directly would spread all of them out
  // evenly instead of clustering them against the close button.
  const actions = document.createElement('div');
  actions.className = 'measure-head-actions';

  const resetBtn = document.createElement('button');
  resetBtn.id = 'measure-reset-pos';
  resetBtn.type = 'button';
  resetBtn.className = 'measure-drag-btn';
  resetBtn.title = 'Reset card position';
  resetBtn.setAttribute('aria-label', 'Reset card position');
  resetBtn.innerHTML = '&#8634;';    // anticlockwise open circle arrow

  const lockBtn = document.createElement('button');
  lockBtn.id = 'measure-lock-pos';
  lockBtn.type = 'button';
  lockBtn.className = 'measure-drag-btn';
  lockBtn.setAttribute('aria-pressed', 'false');

  actions.appendChild(resetBtn);
  actions.appendChild(lockBtn);
  actions.appendChild(closeBtn);   // re-parented, not cloned -- its own listener (below) is untouched
  head.appendChild(actions);

  let locked = false;
  let dragging = false;
  let dragStartX = 0, dragStartY = 0, dragOrigLeft = 0, dragOrigTop = 0;

  function updateLockUI(){
    lockBtn.setAttribute('aria-pressed', locked ? 'true' : 'false');
    lockBtn.title = locked ? 'Unlock card position' : 'Lock card position (prevents dragging)';
    lockBtn.setAttribute('aria-label', lockBtn.title);
    lockBtn.innerHTML = locked ? '&#128274;' : '&#128275;';   // closed / open padlock
    head.classList.toggle('measure-locked', locked);
  }

  // Keeps at least MEASURE_REACH_PAD px of the card within the wrap on every
  // side: horizontally by bounding how far left/right of it can go relative
  // to its own (fixed) width, vertically by never letting the header strip
  // above the wrap's top edge and never letting the whole card slide past the
  // bottom. This is what stands between a bad drag and a card the user can
  // never click again -- see the CLAUDE.md-adjacent brief for why that matters.
  function clamp(left, top){
    const minLeft = MEASURE_REACH_PAD - MEASURE_CARD_W;
    const maxLeft = Math.max(minLeft, wrap.clientWidth - MEASURE_REACH_PAD);
    const maxTop = Math.max(0, wrap.clientHeight - MEASURE_REACH_PAD);
    return {
      left: Math.min(maxLeft, Math.max(minLeft, left)),
      top: Math.min(maxTop, Math.max(0, top))
    };
  }

  function applyPos(left, top){
    const c = clamp(left, top);
    card.style.right = 'auto';
    card.style.bottom = 'auto';
    card.style.left = c.left + 'px';
    card.style.top = c.top + 'px';
  }

  function savePos(){
    try {
      localStorage.setItem(MEASURE_POS_KEY, JSON.stringify({
        left: parseFloat(card.style.left), top: parseFloat(card.style.top)
      }));
    } catch(e){}
  }

  function resetPos(){
    card.style.left = '';
    card.style.top = '';
    card.style.right = '';
    card.style.bottom = '';
    // Reset only clears the remembered position; a reload without this would
    // put the card right back where it was, which is not what "reset" means.
    try { localStorage.removeItem(MEASURE_POS_KEY); } catch(e){}
  }

  function endDrag(){
    if(!dragging) return;
    dragging = false;
    head.classList.remove('measure-dragging');
    savePos();
  }

  head.addEventListener('pointerdown', function(e){
    if(locked || e.button !== 0) return;
    if(e.target.closest('button')) return;   // reset/lock/close live in this same strip
    const cardRect = card.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    dragOrigLeft = cardRect.left - wrapRect.left;
    dragOrigTop = cardRect.top - wrapRect.top;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragging = true;
    applyPos(dragOrigLeft, dragOrigTop);
    head.classList.add('measure-dragging');
    // The card is a sibling of the canvas, not a descendant, so this can
    // never reach OrbitControls' own pointerdown listener regardless --
    // preventDefault here just stops the browser reading the drag as a text
    // or image selection gesture.
    e.preventDefault();
  });

  // move/up/cancel + blur all live on window, the same freeze-safety pattern
  // the shape-cage and nav-cube drags in this file use: if the pointer is
  // released (or the window loses focus) anywhere other than exactly over the
  // card, the drag still has to end, or the card is stuck following the
  // pointer with no way to let go of it.
  window.addEventListener('pointermove', function(e){
    if(!dragging) return;
    applyPos(dragOrigLeft + (e.clientX - dragStartX), dragOrigTop + (e.clientY - dragStartY));
  });
  window.addEventListener('pointerup', endDrag);
  window.addEventListener('pointercancel', endDrag);
  window.addEventListener('blur', endDrag);

  // A stored (or mid-session) position can be stranded off-screen by a window
  // resize -- reclamp live, not just on the next load.
  window.addEventListener('resize', function(){
    if(!card.style.left) return;   // still at the CSS default; nothing to reclamp
    applyPos(parseFloat(card.style.left) || 0, parseFloat(card.style.top) || 0);
    savePos();
  });

  resetBtn.addEventListener('click', resetPos);
  lockBtn.addEventListener('click', function(){
    locked = !locked;
    updateLockUI();
    try { localStorage.setItem(MEASURE_LOCK_KEY, locked ? '1' : '0'); } catch(e){}
  });

  // Restore persisted position/lock at load. The card is display:none at this
  // point (nothing is loaded yet), but clamp() only needs the CSS-fixed card
  // width (MEASURE_CARD_W) and the wrap's own size, neither of which depend
  // on the card's own layout, so restoring while hidden is safe.
  let storedPos = null;
  try { storedPos = JSON.parse(localStorage.getItem(MEASURE_POS_KEY) || 'null'); } catch(e){}
  if(storedPos && isFinite(storedPos.left) && isFinite(storedPos.top)){
    applyPos(storedPos.left, storedPos.top);
  }
  let storedLock = null;
  try { storedLock = localStorage.getItem(MEASURE_LOCK_KEY); } catch(e){}
  locked = storedLock === '1';
  updateLockUI();
})();

// ---- orientation / view cube -----------------------------------------------
// Bambu-Studio-style gizmo: a small cube in the corner that mirrors the main
// camera's orientation and whose faces snap the camera to a standard view
// when clicked.
//
// It gets its OWN canvas, renderer, scene and camera rather than sharing the
// main renderer through a scissored viewport. A scissor pass would still
// leave the gizmo's pointer handling entangled with the main canvas's -- any
// hit-test would have to happen in the same event stream OrbitControls reads,
// which is exactly the kind of "is this click for the model or for the cube"
// ambiguity this widget cannot afford. A separate DOM element sidesteps that
// completely: it sits on top in z-order, so every pointer event inside its
// 96x96 box belongs to it and never reaches OrbitControls underneath.
const navCanvas = document.getElementById('navcube');
const navRenderer = new THREE.WebGLRenderer({ canvas: navCanvas, alpha:true, antialias:true });
navRenderer.setPixelRatio(Math.min(devicePixelRatio,2));
navRenderer.setSize(132, 132, false);   // false: leave the CSS size (style.css #navcube) alone

const navScene = new THREE.Scene();
const navCamera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
// Distance is what decides how much of the 132px frame the cube fills, and so
// how big the baked face labels end up. At fov 30 the visible height here is
// 2*d*tan(15deg) = 0.536*d. The cube's own corner-to-corner diagonal is
// 1.6*sqrt(3) = 2.77 (d must stay above ~5.17 for that alone), but the axis
// triad added below sticks out further: TRIAD_ORIGIN ~= (-0.95,-0.95,-0.95)
// is 0.95*sqrt(3) = 1.645 from the origin, and its label sprites poke out to
// roughly 1.68 including their own footprint -- both past the cube's own
// 0.8*sqrt(3) = 1.386. Using 2*1.68 = 3.36 as the worst-case extent in place
// of 2.77, d must stay above 3.36/0.536 = ~6.27 or the triad clips. 6.9 keeps
// ~10% margin on that while the frame grew to 132px (from 112, in step) so
// the baked face labels stay at ~13.4 CSS px -- just above the ~13px
// legibility floor this was tuned to (see the face-texture comment below).
const NAV_CAM_DIST = 6.9;

// Face order MUST match THREE.BoxGeometry's material-group order:
// [+X, -X, +Y, -Y, +Z, -Z]. Directions describe the face's outward normal in
// world space. Labels describe the PRINTER's axes (world Y = printer Z,
// world Z = NEGATED printer Y -- see the coordinate-mapping note near the top
// of this file), because every number this app reports is in printer
// coordinates.
//
// Front/back: parseGcode() maps printer Y to world Z via `az = cy - y`
// (cy = BED_Y/2), so printer Y=0 -- the origin corner, the edge the operator
// stands at -- sits at POSITIVE world Z, and printer Y=BED_Y sits at negative
// world Z. Front is therefore world +Z and Back world -Z. Both were the other
// way round while the mapping was un-negated; the negation swapped them, so
// clicking Front used to walk the camera round to the back of the machine.
//
// Top/bottom carry a tiny epsilon off the exact +/-Y axis. Camera.up is
// never touched anywhere in this app (OrbitControls bakes object.up into an
// internal quaternion the FIRST time update() runs and never recomputes it),
// so a dead-on top-down view would put the view direction exactly parallel
// to that fixed up vector -- a degenerate camera.lookAt with an undefined
// roll. The epsilon keeps Top/Bottom visually indistinguishable from a true
// top-down view while staying just off the singularity.
const NAV_EPS = 0.02;
const NAV_FACES = [
  { key:'right',  label:'Right',  dir:new THREE.Vector3( 1, 0, 0) },
  { key:'left',   label:'Left',   dir:new THREE.Vector3(-1, 0, 0) },
  // The epsilon's sign picks which way is up on screen in a top-down view.
  // A camera offset toward world -Z (i.e. +NAV_EPS on the dir's Z, since the
  // camera sits along +dir) makes screen-up land on world -Z, which under the
  // negated mapping is printer +Y -- the BACK of the bed at the top of the
  // view, how every slicer (and anyone standing at the machine) reads it.
  // It was -NAV_EPS while world +Z meant printer +Y; the negation flipped
  // which sign gets that, so leaving it would have shown the bed upside down.
  //
  // `spin` rotates the baked label, NOT the camera, and is now unnecessary:
  // BoxGeometry's +Y/-Y face UVs put the texture's "up" toward world -Z,
  // which is exactly where screen-up now lands, so the labels read the right
  // way up unrotated. (They were spun by PI when screen-up was world +Z,
  // without which "Top" read as "doL".)
  { key:'top',    label:'Top',    dir:new THREE.Vector3( 0, 1, NAV_EPS).normalize() },
  { key:'bottom', label:'Bottom', dir:new THREE.Vector3( 0,-1, NAV_EPS).normalize() },
  // Front/Back swapped POSITION in this array, not just their `dir` vectors:
  // slot 4 is BoxGeometry's +Z material group and slot 5 its -Z, so the label
  // baked into each slot has to be the one whose outward normal that slot
  // carries. Swapping only the vectors would have painted "Back" on the front
  // face while still flying the camera to the right place.
  { key:'front',  label:'Front',  dir:new THREE.Vector3( 0, 0, 1) },
  { key:'back',   label:'Back',   dir:new THREE.Vector3( 0, 0,-1) },
];

// Face textures are drawn with the 2D canvas API -- no external font/image
// files, per the no-build-step / no-new-dependency rule. Two variants per
// face (idle + hover) are baked up front and swapped wholesale on hover
// rather than tinted at render time, so the hover colour is exactly --accent
// with no blending artifacts against the label text.
function navFaceTexture(label, hovered, spin){
  const S = 256;
  const c = document.createElement('canvas');
  c.width = S; c.height = S;
  const g = c.getContext('2d');
  // Idle faces sit in the cool-slate palette; hover uses --accent (#2f6bff)
  // and NOTHING else may use that highlight -- --accent-purple/--ok/--warn/
  // --danger/--measure are reserved elsewhere.
  //
  // The idle fill was #343b42, chosen against the old #1c1f22 canvas. The
  // canvas is now #2b3036, which left the cube barely a shade off its own
  // background -- a widget for reading orientation that you first had to find.
  // Lifted well clear of it instead.
  g.fillStyle = hovered ? '#2f6bff' : '#4a535e';
  g.fillRect(0, 0, S, S);
  g.strokeStyle = hovered ? 'rgba(255,255,255,0.45)' : 'rgba(255,255,255,0.22)';
  g.lineWidth = 8;
  g.strokeRect(4, 4, S-8, S-8);
  g.fillStyle = '#ffffff';
  // 60px of a 256px texture on a face that projects to ~57px (at the 132px
  // frame / NAV_CAM_DIST=6.9 pairing -- widened for the axis triad, see the
  // comment on NAV_CAM_DIST) lands the label at ~13.4 CSS px, matching the
  // icon-legibility floor set in style.css.
  g.font = '700 60px Inter, sans-serif';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  // Applied to the glyph only -- the fill and border are rotationally
  // symmetric, so spinning them would be a no-op. See `spin` in NAV_FACES.
  if(spin){
    g.translate(S/2, S/2);
    g.rotate(spin);
    g.translate(-S/2, -S/2);
  }
  g.fillText(label, S/2, S/2);
  const tex = new THREE.CanvasTexture(c);
  if(THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const navTexIdle = NAV_FACES.map(f => navFaceTexture(f.label, false, f.spin));
const navTexHover = NAV_FACES.map(f => navFaceTexture(f.label, true, f.spin));
// MeshBasicMaterial (unlit): a gizmo's faces must show their true assigned
// colour regardless of viewing angle, not get shaded darker/lighter by a
// light direction the user has no control over.
const navMats = NAV_FACES.map((f,i) => new THREE.MeshBasicMaterial({ map:navTexIdle[i] }));
const navGeo = new THREE.BoxGeometry(1.6, 1.6, 1.6);
const navCube = new THREE.Mesh(navGeo, navMats);
navCube.add(new THREE.LineSegments(new THREE.EdgesGeometry(navGeo),
  new THREE.LineBasicMaterial({ color:0x14171a })));   // dark seam between faces, decorative only
navScene.add(navCube);

// ---- axis triad -------------------------------------------------------
// Small XYZ indicator anchored just outside the cube corner that stands for
// the printer's origin (smallest X, Y and Z), RGB = XYZ per the universal
// convention. All three arrows therefore point inward across the cube, the
// way a machine's axes leave its origin. Added to navScene directly (NOT as
// a child of navCube) so navPickIndex's intersectObject(navCube, false)
// never sees it -- face picking below is unaffected by its presence.
//
// The labels are PRINTER axes, matching every other number this app
// reports, NOT Three.js world axes. Per the coordinate-mapping note near
// the top of this file: printer X -> world X, printer Y -> world -Z
// (NEGATED), printer Z (up) -> world Y. So the red line (world +X) reads
// "X", the green line (world -Z) reads "Y", and the blue line (world +Y)
// reads "Z". The green line pointed at world +Z while the mapping was
// un-negated; leaving it there would make the widget lie about which way
// the machine actually moves. Printer Y smallest is world Z largest, which
// is why the anchor sits at +Z.
const TRIAD_ORIGIN = new THREE.Vector3(-0.95, -0.95, 0.95);   // just outside the 0.8 half-extent cube
const TRIAD_LEN = 0.9;
const TRIAD_LABEL_GAP = 0.18;   // label sprite sits this far past the line tip, clear of it
const TRIAD_AXES = [
  { dir:new THREE.Vector3(1,0,0), color:'#e8483f', label:'X' },   // printer X -> world +X
  { dir:new THREE.Vector3(0,0,-1), color:'#4cc264', label:'Y' },  // printer Y -> world -Z (negated)
  { dir:new THREE.Vector3(0,1,0), color:'#4a90e8', label:'Z' },   // printer Z (up) -> world +Y
];

// Canvas-drawn sprite, same no-external-font/image reasoning as
// navFaceTexture above. Drawn at 128px and scaled down via sprite.scale
// (0.5) rather than drawn small, so the GPU supersamples instead of
// rasterising an already-jagged glyph.
function navAxisLabelSprite(text, color){
  const S = 128;
  const c = document.createElement('canvas');
  c.width = S; c.height = S;
  const g = c.getContext('2d');
  g.fillStyle = color;
  g.font = '700 88px Inter, sans-serif';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(text, S/2, S/2 + 4);
  const tex = new THREE.CanvasTexture(c);
  if(THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map:tex }));
  sprite.scale.set(0.5, 0.5, 1);
  return sprite;
}

for(const axis of TRIAD_AXES){
  const end = TRIAD_ORIGIN.clone().addScaledVector(axis.dir, TRIAD_LEN);
  const line = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([TRIAD_ORIGIN, end]),
    new THREE.LineBasicMaterial({ color:axis.color }));
  navScene.add(line);
  const label = navAxisLabelSprite(axis.label, axis.color);
  label.position.copy(TRIAD_ORIGIN).addScaledVector(axis.dir, TRIAD_LEN + TRIAD_LABEL_GAP);
  navScene.add(label);
}

let navHoverIdx = -1;
let navLastView = null;
let navAnimHandle = 0;

const navRaycaster = new THREE.Raycaster();
const navNDC = new THREE.Vector2();

function navPickIndex(clientX, clientY){
  const rect = navCanvas.getBoundingClientRect();
  navNDC.x = ((clientX - rect.left) / rect.width) * 2 - 1;
  navNDC.y = -((clientY - rect.top) / rect.height) * 2 + 1;
  navRaycaster.setFromCamera(navNDC, navCamera);
  // recursive MUST be false: intersectObject's default is recursive=true, which
  // also tests the decorative edge LineSegments child added below. Line
  // raycasting uses a fixed world-space pick threshold (Raycaster.params.Line
  // .threshold, default 1) meant for full-size scene geometry -- on this 1.6
  // unit cube that threshold is bigger than the cube itself, so the edges
  // reported a bogus near-camera "hit" (no .face) on almost every ray and
  // permanently shadowed the real box hit, making every click silently miss.
  const hit = navRaycaster.intersectObject(navCube, false)[0];
  return (hit && hit.face) ? hit.face.materialIndex : -1;
}

function navApplyHover(){
  for(let i = 0; i < navMats.length; i++){
    navMats[i].map = (i === navHoverIdx) ? navTexHover[i] : navTexIdle[i];
    navMats[i].needsUpdate = true;
  }
}

function navRenderCube(){
  navRenderer.render(navScene, navCamera);
}

// Mirrors the main camera's orientation into the gizmo: keep the cube fixed
// and unrotated at the origin, and instead move the gizmo's OWN camera to
// the same direction (from the target) that the main camera sits at. Called
// from the main render() via the window.__* guard pattern (see the comment
// on window.__measureSync above) so it tracks orbit/zoom/resize for free,
// with no extra render loop of its own.
function navSyncCamera(){
  const dir = camera.position.clone().sub(controls.target);
  if(dir.lengthSq() < 1e-8) dir.set(0, 0, 1);   // degenerate camera-at-target guard
  dir.normalize().multiplyScalar(NAV_CAM_DIST);
  navCamera.position.copy(dir);
  navCamera.up.copy(camera.up);
  navCamera.lookAt(0, 0, 0);
  navRenderCube();
}
window.__navCubeSync = navSyncCamera;

// Animates the MAIN camera to an orthogonal view along `dir`, preserving the
// current distance from controls.target (so zoom level survives a view
// snap). `instant` skips the animation -- used by the automation hook below
// so tests don't have to wait out a tween -- and reduced-motion users get
// the same instant jump for the real click path.
function navAnimateTo(toPos, instant){
  cancelAnimationFrame(navAnimHandle);
  const reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  if(instant || reduced){
    camera.position.copy(toPos);
    controls.update();
    render();
    return;
  }
  const fromPos = camera.position.clone();
  const DUR = 350;
  const t0 = performance.now();
  function step(now){
    const p = Math.min(1, (now - t0) / DUR);
    const e = p < 0.5 ? 2*p*p : 1 - Math.pow(-2*p+2, 2)/2;   // ease-in-out quad
    camera.position.lerpVectors(fromPos, toPos, e);
    controls.update();
    render();
    if(p < 1) navAnimHandle = requestAnimationFrame(step);
  }
  navAnimHandle = requestAnimationFrame(step);
}

function navGoToFace(idx, instant){
  const f = NAV_FACES[idx];
  if(!f) return;
  navLastView = f.key;
  // Asking for a named view is asking the camera to STOP there, so auto-spin
  // has to yield. Without this the click looked like it did nothing at all:
  // OrbitControls applies autoRotate inside update(), which the tween below
  // calls every frame, so the rotation cancelled each lerp step -- and
  // spinLoop's own rAF loop kept turning the camera long after the tween
  // finished. The checkbox is cleared too, so the control still reports the
  // truth about what the camera is doing.
  if(controls.autoRotate){
    controls.autoRotate = false;
    const spin = document.getElementById('t-spin');
    if(spin) spin.checked = false;
  }
  const dist = camera.position.distanceTo(controls.target);
  const toPos = controls.target.clone().add(f.dir.clone().multiplyScalar(dist));
  navAnimateTo(toPos, instant);
}

// ---- drag-to-orbit ------------------------------------------------------
// Press-and-drag on the cube orbits the MAIN camera, Fusion/Bambu-style.
// A click is just a drag that never crossed the movement threshold, so
// pointerdown starts tracking, pointermove rotates once past the threshold,
// and pointerup either snapped a face (no movement) or ends the drag
// (movement happened) -- never both for the same gesture.
let navDragButtonDown = false;   // button 0 is currently held on this canvas
let navDragMoved = false;        // this press has crossed the click/drag threshold
let navDragging = false;         // exposed on __navCubeState: true for the duration of an actual drag
let navDragCount = 0;            // exposed on __navCubeState: gestures that became a drag
let navDragLastX = 0, navDragLastY = 0;
let navDragStartX = 0, navDragStartY = 0;
const NAV_DRAG_THRESHOLD = 3;    // px of movement before a press counts as a drag, not a click
const NAV_DRAG_K = 0.008;        // rad of camera rotation per px of pointer movement

// Rotates the MAIN camera about controls.target by a pixel delta. Shared by
// the real pointermove handler below and the __navCubeDrag test hook so
// both exercise identical math. THREE.Spherical's phi is measured from the
// +Y axis, which is "up" in this app's world space, so clamping it to
// [0.02, PI-0.02] keeps the orbit just off the poles -- the same singularity
// NAV_EPS sidesteps for the Top/Bottom face snap above.
function navOrbitBy(dx, dy){
  const sph = new THREE.Spherical().setFromVector3(camera.position.clone().sub(controls.target));
  sph.theta -= dx * NAV_DRAG_K;
  sph.phi = Math.max(0.02, Math.min(Math.PI - 0.02, sph.phi - dy * NAV_DRAG_K));
  camera.position.setFromSpherical(sph).add(controls.target);
  controls.update();
  render();   // re-syncs the cube for free, same as navAnimateTo's step()
}

navCanvas.addEventListener('pointerdown', function(e){
  if(e.button !== 0) return;
  // Best-effort: capture keeps the drag alive if the pointer leaves the
  // canvas mid-gesture, but its absence must not skip the state setup below
  // -- an environment where capture throws (seen from synthetic/automated
  // pointer events, which have no browser-tracked "active pointer" to
  // capture) would otherwise silently break dragging entirely.
  try { navCanvas.setPointerCapture(e.pointerId); } catch(err){ /* no active pointer to capture */ }
  navDragButtonDown = true;
  navDragMoved = false;
  navDragStartX = navDragLastX = e.clientX;
  navDragStartY = navDragLastY = e.clientY;
  cancelAnimationFrame(navAnimHandle);
  // A press is a request for a specific orientation, same as a face-snap
  // click -- auto-spin has to yield for the same reason given in the
  // comment inside navGoToFace above, so it does not fight the drag.
  if(controls.autoRotate){
    controls.autoRotate = false;
    const spin = document.getElementById('t-spin');
    if(spin) spin.checked = false;
  }
});
navCanvas.addEventListener('pointermove', function(e){
  if(navDragButtonDown){
    const dx = e.clientX - navDragLastX, dy = e.clientY - navDragLastY;
    navDragLastX = e.clientX; navDragLastY = e.clientY;
    if(!navDragMoved){
      const totalX = e.clientX - navDragStartX, totalY = e.clientY - navDragStartY;
      if(Math.hypot(totalX, totalY) > NAV_DRAG_THRESHOLD){
        navDragMoved = true;
        navDragging = true;
        navDragCount++;
        navCanvas.style.cursor = 'grabbing';
      }
    }
    if(navDragMoved) navOrbitBy(dx, dy);
    return;
  }
  const idx = navPickIndex(e.clientX, e.clientY);
  if(idx !== navHoverIdx){
    navHoverIdx = idx;
    navApplyHover();
    // 'grab', not 'default': the whole canvas is drag-enabled now, not just
    // the pickable faces, so even a non-face hover gets the grab affordance.
    navCanvas.style.cursor = idx >= 0 ? 'pointer' : 'grab';
    navRenderCube();
  }
});
function navEndDrag(e){
  if(!navDragButtonDown) return;
  navDragButtonDown = false;
  navDragging = false;
  navCanvas.style.cursor = 'grab';
  if(e && e.pointerId !== undefined){
    try { navCanvas.releasePointerCapture(e.pointerId); } catch(err){ /* already released */ }
  }
}
navCanvas.addEventListener('pointerup', function(e){
  if(e.button !== 0) return;
  const wasDrag = navDragMoved;
  navDragMoved = false;
  navEndDrag(e);
  // Only a press that never crossed the drag threshold snaps a face -- a
  // real drag already did its job by orbiting the camera, and actioning a
  // face on top of that would fight the orientation the drag just set.
  if(!wasDrag){
    const idx = navPickIndex(e.clientX, e.clientY);
    if(idx >= 0) navGoToFace(idx);
  }
});
navCanvas.addEventListener('pointercancel', function(e){ navDragMoved = false; navEndDrag(e); });
window.addEventListener('blur', function(){ navDragMoved = false; navEndDrag(); });
navCanvas.addEventListener('pointerleave', function(){
  if(navHoverIdx !== -1){
    navHoverIdx = -1;
    navApplyHover();
    if(!navDragButtonDown) navCanvas.style.cursor = 'grab';
    navRenderCube();
  }
});

navSyncCamera();   // first frame -- render() already ran once, before this section existed

// Test/automation hooks, same shape as __measureState / __measureAt.
window.__navCubeState = function(){
  return {
    faces: NAV_FACES.map(f => f.key),
    hovered: (navHoverIdx >= 0 && NAV_FACES[navHoverIdx]) ? NAV_FACES[navHoverIdx].key : null,
    lastView: navLastView,
    dragging: navDragging,
    dragCount: navDragCount
  };
};
window.__navCubeClick = function(key){
  const idx = NAV_FACES.findIndex(f => f.key === key);
  if(idx < 0) return null;
  navGoToFace(idx, true);   // instant: automation shouldn't have to wait out the tween
  return window.__navCubeState();
};
// Synthetic drag for automation: applies navOrbitBy's math directly rather
// than dispatching real pointer events, since pointer capture + a movement
// threshold are awkward to drive from a test harness. Counts as one drag
// gesture, matching what a real press-move-release does.
window.__navCubeDrag = function(dx, dy){
  navDragging = true;
  navDragCount++;
  navOrbitBy(dx, dy);
  navDragging = false;
  return window.__navCubeState();
};
