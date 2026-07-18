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
scene.background = new THREE.Color(0x0e1116);

const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 4000);
camera.position.set(BED_X*0.9, BED_Z*1.1, BED_Y*1.3);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = false;   // on-demand rendering instead of a perpetual loop
controls.target.set(0, BED_Z*0.25, 0);

scene.add(new THREE.AmbientLight(0xffffff, 0.9));
const dir = new THREE.DirectionalLight(0xffffff, 0.6);
dir.position.set(1,2,1); scene.add(dir);

// ---- bed ------------------------------------------------------------------
// Printer X -> three X, printer Y -> three Z, printer Z -> three Y (up).
// Origin shifted so bed centre sits at world origin.
const bedGroup = new THREE.Group(); scene.add(bedGroup);
{
  const grid = new THREE.GridHelper(BED_X, 23, 0x3a4350, 0x222831);
  bedGroup.add(grid);
  // Safe-print-area outline (30-208 x 30-185 from printer.cfg).
  const ax=[30,208], ay=[30,185], cx=BED_X/2, cy=BED_Y/2;
  const c=[[ax[0],ay[0]],[ax[1],ay[0]],[ax[1],ay[1]],[ax[0],ay[1]],[ax[0],ay[0]]];
  const pts=c.map(([x,y])=>new THREE.Vector3(x-cx,0.1,y-cy));
  const ln=new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineDashedMaterial({color:0x4cc2ff,dashSize:4,gapSize:3,transparent:true,opacity:.55}));
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
  const alu = new THREE.MeshStandardMaterial({ color:0x3b424c, metalness:0.6, roughness:0.5 });
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
let progress = 1;       // 0..1 fraction of the print drawn
let xrayOn = false;     // X-ray view: transparent path + dimmed bed (see-through)
let playing = false;
let perSec = 0.0667;    // progress per second (from the speed selector)

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
// Returns a Uint8Array of length nSeg (1=risky, 0=safe).
function computeRiskFlags(ext, nSeg){
  // Grid maps bucket key -> array of {px, pz, midZ} for prior midpoints.
  const grid = new Map();
  const risk = new Uint8Array(nSeg);

  // How many surrounding buckets to check (ceil of RISK_XY / BUCKET_CELL + 1).
  // With RISK_XY=1 and BUCKET_CELL=2, the neighbour radius is 1 extra cell.
  const NR = Math.ceil(RISK_XY / BUCKET_CELL) + 1;

  for(let s = 0; s < nSeg; s++){
    const base = s * 6;
    // Midpoint in printer coordinates (not world coords).
    // ext[] stores world-space: ax=printerX-cx, ay=printerZ(=worldY), az=printerY-cy.
    // We stored ax/ay/az for start, bx/by/bz for end.
    const mx = (ext[base+0] + ext[base+3]) * 0.5;   // world X = printerX - cx
    const mz = (ext[base+2] + ext[base+5]) * 0.5;   // world Z = printerY - cy
    const my = (ext[base+1] + ext[base+4]) * 0.5;   // world Y = printerZ

    // Only test segments above the first-layer threshold.
    if(my > RISK_Z_MIN){
      // Check neighbouring buckets in XY.
      const bxi = Math.floor(mx / BUCKET_CELL);
      const bzi = Math.floor(mz / BUCKET_CELL);
      let supported = false;

      outer:
      for(let di = -NR; di <= NR && !supported; di++){
        for(let dj = -NR; dj <= NR && !supported; dj++){
          const key = (bxi+di) + ',' + (bzi+dj);
          const bucket = grid.get(key);
          if(!bucket) continue;
          for(let k = 0; k < bucket.length; k++){
            const pr = bucket[k];
            const dxy = Math.hypot(pr.wx - mx, pr.wz - mz);
            if(dxy > RISK_XY) continue;
            const dz = my - pr.wy;  // positive = current is above prior
            if(dz > RISK_DZ_LO && dz <= RISK_DZ_HI){ supported = true; break; }
          }
        }
      }
      if(!supported) risk[s] = 1;
    }

    // Insert this segment's midpoint into the grid for future segments.
    const key = bucketKey(mx, mz, BUCKET_CELL);
    let bucket = grid.get(key);
    if(!bucket){ bucket = []; grid.set(key, bucket); }
    bucket.push({wx: mx, wy: my, wz: mz});
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
    // Printer coordinates: world X = printerX, world Z = printerY (both the
    // horizontal plane); world Y = printerZ (height).
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

// Estimate total print time (seconds) summing dist/effective_speed for every move.
// effective_speed = min(commandedSpeed, sqrt(dist * ACCEL)) approximates
// segments that never reach commanded speed (short moves stay slow).
// Z-component speed is additionally capped at MAX_Z_SPEED.
function computeEstTime(ext, trv, segSpeed, nExtSeg){
  let total = 0;

  // Extrude segments -- segSpeed[] has one entry per extrude seg.
  const nExt = nExtSeg;
  for(let s = 0; s < nExt; s++){
    const base = s * 6;
    const dx = ext[base+3] - ext[base+0];
    const dy = ext[base+4] - ext[base+1];  // world Y = printer Z
    const dz = ext[base+5] - ext[base+2];
    const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
    if(dist < 1e-9) continue;
    let spd = segSpeed[s] || 1;
    // Acceleration-limited effective speed for short moves.
    const accelSpd = Math.sqrt(dist * ACCEL);
    spd = Math.min(spd, accelSpd);
    // Z-axis speed cap: if the Z fraction of speed exceeds MAX_Z_SPEED, scale down.
    const zFrac = Math.abs(dy) / dist;
    const maxSpd = zFrac > 1e-6 ? MAX_Z_SPEED / zFrac : Infinity;
    spd = Math.min(spd, maxSpd);
    if(spd < 1e-9) spd = 1;
    total += dist / spd;
  }

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

function parseGcode(text){
  const cx=BED_X/2, cy=BED_Y/2;
  let x=0,y=0,z=0, has=false, curF=0;
  let minz=Infinity,maxz=-Infinity, minx=Infinity,maxx=-Infinity,miny=Infinity,maxy=-Infinity;
  let fil=0, extrudeCount=0, travelCount=0, maxZrate=0, relE=false;
  const ext=[], extCol=[], trv=[];          // world-space vertex arrays
  const segSpeed=[], segFlow=[];            // per-extrude-segment telemetry
  const meta={lineWidth:null, layerHeight:null, nozzle:null};

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
    if(up.startsWith('M83')) { relE=true; continue; }
    if(up.startsWith('M82')) { relE=false; continue; }
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
      const ax=x-cx, ay=z, az=y-cy;     // remap to world (Y up)
      const bx=nx-cx, by=nz, bz=ny-cy;
      if(extruding){
        ext.push(ax,ay,az, bx,by,bz);
        extCol.push(nz, nz);            // store z; convert to colour after
        extrudeCount++;
        const len=Math.hypot(nx-x,ny-y,nz-z);
        const speed=curF/60;                       // mm/s
        // volumetric flow = filament volume extruded / time = e*area*speed/len
        const flow=(len>0 && relE)? e*FIL_AREA*speed/len : 0;
        segSpeed.push(speed); segFlow.push(flow);
        if(relE) fil+=e;
        if(len>0){ const zr=speed*Math.abs(nz-z)/len; if(zr>maxZrate)maxZrate=zr; }
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

  // Compute risk flags (O(n) grid hash).
  const riskFlags = computeRiskFlags(extFlat, nExtSeg);
  const riskyCount = riskFlags.reduce((a,v)=>a+v, 0);

  // Compute per-segment overhang angles once (cached on the loaded data).
  const overhang = computeOverhang(extFlat, nExtSeg);

  // Compute estimated print time.
  const estTimeSec = computeEstTime(extFlat, trvFlat, segSpeed, nExtSeg);
  const estTime = fmtTime(estTimeSec);

  return {ext:extFlat,extCol,trv:trvFlat,segSpeed,segFlow,meta,minz,maxz,minx,maxx,miny,maxy,
          fil,extrudeCount,travelCount,maxZrate,riskFlags,riskyCount,overhang,estTime,estTimeSec};
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
  const PLAIN_RGB = [0x9f/255, 0xd8/255, 0xff/255];   // uniform light blue (0x9fd8ff)
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
        rgb = ramp((zc-d.minz)/span);       // height (viridis) -- default
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
  travelObj=new THREE.LineSegments(tg, new THREE.LineBasicMaterial({color:0x556070,transparent:true,opacity:0.45}));
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
  const cx=(d.minx+d.maxx)/2-BED_X/2, cz=(d.miny+d.maxy)/2-BED_Y/2;
  const h=d.maxz-d.minz, cy=(d.minz+d.maxz)/2;
  const span=Math.max(d.maxx-d.minx, d.maxy-d.miny, h*1.6, 12);
  const flat = h < span*0.15;
  const dist = span*1.55;
  const elev = flat ? 1.15 : 0.55;          // steeper for flat prints
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
    gcodeTitleEl.style.cssText =
      'position:absolute;top:8px;left:50%;transform:translateX(-50%);' +
      'padding:3px 12px;border-radius:6px;background:rgba(14,17,22,0.82);' +
      'color:#dfe7f3;font-size:12px;font-weight:600;pointer-events:none;' +
      'z-index:5;font-family:sans-serif;max-width:60%;overflow:hidden;' +
      'text-overflow:ellipsis;white-space:nowrap;border:1px solid rgba(255,255,255,0.08)';
    wrap.appendChild(gcodeTitleEl);
  }
  gcodeTitleEl.textContent = name;
  gcodeTitleEl.style.display = 'block';
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
  set('s-zrate',zr.toFixed(1)+' mm/s'+(zr>25.1?' !':' ok'));
  document.getElementById('s-zrate').classList.toggle('state-danger', zr>25.1);
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
const SPARK_BG = 'rgba(21,26,34,0.85)';
const SPARK_LINE_COL = '#4cc2ff';
const SPARK_FILL_COL = 'rgba(76,194,255,0.18)';
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

let lastData=null;
let cuts=[0];   // extrude-segment indices at each spiral-turn boundary (+ the end)
function load(name,text){
  lastData=parseGcode(text);
  cuts=computeTurns(lastData);           // turn boundaries (used for banding + stepping)
  buildGeometry(lastData); showStats(name,lastData);
  document.getElementById('tl-wrap').style.display='block';
  document.getElementById('telemetry-group').style.display='block';
  stopPlay(); setProgress(1);            // start fully drawn
  fitView();                             // frame the camera on the model
  // Build the sparkline after layout settles (offsetWidth needs a rendered frame).
  requestAnimationFrame(()=>{ buildSparkline(lastData); drawSparkCursor(1); });
  // A file was loaded (dropped, picked, or generated) - show the viewer mode.
  if(window.setAppMode) window.setAppMode('viewer');
}

// Detect spiral-turn boundaries by unwrapping the path's angle about its centre.
// Each full 2*pi revolution = one "layer/turn" for the step controls.
function computeTurns(d){
  const cx=((d.minx+d.maxx)/2)-BED_X/2, cz=((d.miny+d.maxy)/2)-BED_Y/2;
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
function setProgress(p){
  progress = Math.min(1, Math.max(0, p));
  let k=0;
  if(pathObj && animSeg>0){
    k = Math.round(progress*animSeg);
    pathObj.geometry.instanceCount = k;             // reveal only printed segments
    if(k>0 && progress<1){
      const i=(k-1)*6+3;                            // end of last drawn segment
      nozzle.position.set(extArr[i], extArr[i+1], extArr[i+2]);
      nozzle.visible=true;
    } else {
      nozzle.visible=false;                         // hide at 0% and when finished
    }
  }
  const z = (lastData? lastData.minz:0) + progress*((lastData? (lastData.maxz-lastData.minz):0));
  let layerHtml='';
  if(cuts && cuts.length>1){
    const tot=cuts.length-1;
    const cur=Math.min(tot, cuts.filter(c=>c<=k).length);
    // Turn/revolution counter gets visual primacy -- per-spiral-turn stepping
    // (arrow keys) is this tool's signature playback mode.
    layerHtml=` &middot; <span class="turn-count">L${cur}/${tot}</span>`;
  }
  document.getElementById('tl-read').innerHTML =
    `Z ${z.toFixed(1)}mm${layerHtml} &middot; ${Math.round(progress*100)}%`;
  document.getElementById('scrub').value = Math.round(progress*1000);
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
  set('tm-z', z.toFixed(2)+' mm');
  set('tm-lh', lastData.meta.layerHeight!=null ? lastData.meta.layerHeight.toFixed(2)+' mm' : '--');
  set('tm-lw', lastData.meta.lineWidth!=null ? lastData.meta.lineWidth.toFixed(2)+' mm' : '--');
  set('tm-nz', lastData.meta.nozzle!=null ? lastData.meta.nozzle.toFixed(2)+' mm' : '--');
}
function updatePlayBtn(){ document.getElementById('play').textContent = playing?'||':'>'; }
function stopPlay(){ playing=false; updatePlayBtn(); }
let _lastT=0;
function playLoop(t){
  if(!playing) return;
  if(!_lastT)_lastT=t; const dt=(t-_lastT)/1000; _lastT=t;
  setProgress(progress + perSec*dt);
  if(progress>=1){ stopPlay(); return; }
  requestAnimationFrame(playLoop);
}
document.getElementById('play').addEventListener('click',()=>{
  if(playing){ stopPlay(); return; }
  if(progress>=1) progress=0;             // replay from the start
  playing=true; _lastT=0; updatePlayBtn(); requestAnimationFrame(playLoop);
});
document.getElementById('scrub').addEventListener('input',e=>{ stopPlay(); setProgress(e.target.value/1000); });
document.getElementById('speed').addEventListener('change',e=>{ perSec=parseFloat(e.target.value); });

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
window.addEventListener('keydown',e=>{
  if(e.target.tagName==='SELECT' || document.getElementById('tl-wrap').style.display==='none') return;
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
  if((e.key==='f'||e.key==='F') && !e.ctrlKey && !e.metaKey &&
     e.target.tagName!=='INPUT' && e.target.tagName!=='SELECT'){ fitView(); e.preventDefault(); }
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
  let green=0, yellow=0, red=0, blue=0, other=0;
  for(let i=0;i<n;i++){
    const r=a.getX(i), g=a.getY(i), b=a.getZ(i);
    if(b>0.6 && r>0.5 && g>0.7) blue++;              // plain light blue
    else if(r<0.4 && g>0.55 && b<0.35) green++;      // safe overhang
    else if(r>0.7 && g>0.6 && b<0.35) yellow++;      // caution
    else if(r>0.7 && g<0.45 && b<0.3) red++;         // steep/air
    else other++;
  }
  return { mode: document.getElementById('t-colormode').value,
           xray: !!(lineMat && lineMat.transparent),
           opacity: lineMat ? +lineMat.opacity.toFixed(2) : null,
           n, green, yellow, red, blue, other };
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

  // "Draft preview" label.
  if(!previewLabel){
    previewLabel = document.createElement('div');
    previewLabel.style.cssText =
      'position:absolute;top:8px;left:8px;padding:2px 8px;border-radius:4px;' +
      'background:rgba(76,194,255,0.18);color:#4cc2ff;font-size:11px;' +
      'pointer-events:none;z-index:5;font-family:sans-serif';
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
// Freeze-safety: pointerdown is the only listener on the renderer canvas --
// pointermove/pointerup/pointercancel live on `window` so a drag that ends
// with the mouse released outside the canvas (or the window losing focus)
// still terminates the drag and re-enables OrbitControls. See cageEndDrag().
let cageGroup = null;            // THREE.Group holding spheres + wireframe
let cageSpheres = [];            // flat list of {mesh,i,j}, ordered i*cols+j
let cageRows = 0, cageCols = 0;
let cageBase = null;             // N x M base radii (mm, before cage scale)
let cageScales = null;           // N x M current cage scale values
let cageHeight = 0;
let cageDragCallback = null;     // onDrag(i, j, newScale)
let cageActive = null;           // {i,j} of the handle being dragged, or null
let cageRowLines = [];           // THREE.Line per row (closed ring)
let cageColLines = [];           // THREE.Line per column (open polyline)
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

// Ends the current drag exactly the same way regardless of what triggered it
// (pointerup on window, pointercancel, or the window losing focus mid-drag).
function cageEndDrag(){
  cageActive = null;
  window.__silDragActive = false;
  controls.enabled = true;
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

  // pointerdown stays on the canvas -- only a hit on a handle sphere starts
  // a drag; otherwise the pointer falls through to OrbitControls normally.
  canvas.addEventListener('pointerdown', function(e){
    if(!cageSpheres.length) return;
    cageUpdatePointerNDC(e);
    cageRaycaster.setFromCamera(cagePointerNDC, camera);
    const hits = cageRaycaster.intersectObjects(cageSpheres.map(function(s){ return s.mesh; }), false);
    if(!hits.length) return;
    const ud = hits[0].object.userData;
    cageActive = { i: ud.i, j: ud.j };
    window.__silDragActive = true;
    controls.enabled = false;
    e.preventDefault();
  });

  // move/up/cancel + blur all live on window so releasing (or losing focus)
  // outside the canvas can never leave controls.enabled stuck at false.
  window.addEventListener('pointermove', function(e){
    if(!cageActive) return;
    const i = cageActive.i, j = cageActive.j;
    const mesh = cageSpheres[i*cageCols+j].mesh;
    const theta = mesh.userData.theta;

    cageTangent.set(-Math.sin(theta), 0, Math.cos(theta));
    cageDragPlane.setFromNormalAndCoplanarPoint(cageTangent, mesh.position);

    cageUpdatePointerNDC(e);
    cageRaycaster.setFromCamera(cagePointerNDC, camera);
    const hit = cageRaycaster.ray.intersectPlane(cageDragPlane, cageDragPoint);
    if(!hit) return;   // ray near-parallel to the plane -- ignore this move

    cageRadialDir.set(Math.cos(theta), 0, Math.sin(theta));
    const dist = cageDragPoint.dot(cageRadialDir);
    const base = cageBase[i][j];
    const newScale = cageClamp(dist / base, 0.5, 1.5);
    const r = base * newScale;
    mesh.position.set(r * Math.cos(theta), mesh.position.y, r * Math.sin(theta));
    if(cageScales && cageScales[i]) cageScales[i][j] = newScale;

    cageRebuildLines();
    render();
    if(cageDragCallback) cageDragCallback(i, j, newScale);
  });

  window.addEventListener('pointerup', cageEndDrag);
  window.addEventListener('pointercancel', cageEndDrag);
  window.addEventListener('blur', cageEndDrag);
}

window.showShapeCage = function(data, onDrag){
  if(!data || !data.base || !data.base.length || !data.cols) return;
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
      mesh.position.set(r * Math.cos(theta), t * data.height, r * Math.sin(theta));
      mesh.userData = { i: i, j: j, theta: theta, t: t };
      group.add(mesh);
      cageSpheres.push({ mesh: mesh, i: i, j: j });
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
  render();
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
    if(cageGroup.userData.sphereGeo) cageGroup.userData.sphereGeo.dispose();
    cageRowLines.forEach(function(l){ l.geometry.dispose(); l.material.dispose(); });
    cageColLines.forEach(function(l){ l.geometry.dispose(); l.material.dispose(); });
    scene.remove(cageGroup);
    cageGroup = null;
  }
  cageSpheres = [];
  cageRowLines = [];
  cageColLines = [];
  // Failsafe: always leave the drag state clean, even if hide is called
  // mid-drag (e.g. the checkbox is toggled off while dragging).
  cageActive = null;
  window.__silDragActive = false;
  controls.enabled = true;
  render();
};
