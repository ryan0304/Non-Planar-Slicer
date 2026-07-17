#!/usr/bin/env python3
"""Local design server for the Trident non-planar G-code generator.

Serves the project directory statically AND exposes a tiny JSON API so the
in-browser viewer can DESIGN a vase (shape/size + two editable curves over
height) and get back machine-safe G-code rendered immediately.

Pure standard library only. ASCII-only console/server strings.

Safety is not optional: every design is generated through GcodeWriter (which
clamps Z feedrate, volumetric flow, footprint, top Z) and then re-checked with
analyze_gcode before it is returned. The wave amplitude is additionally clamped
to the empirical probe keep-out ceiling (1.9 mm) on the server side, so a
malicious or buggy client can never ask for an over-limit print.

Run:

    python serve.py

then open http://localhost:8777/viewer/index.html
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler

from trident_gcode import PRINTER_PROFILES, TRIDENT, GcodeWriter, PrinterProfile
from trident_gcode.blobs import BlobSpec, LoopSpec
from trident_gcode.paths import SpiralSpec, circle, star, superellipse
from trident_gcode.generators import build_continuous_spiral, build_profile_spiral
from trident_gcode.generators.loop_fabric import build_loop_fabric
from trident_gcode.analyze import analyze_gcode, format_report
from trident_gcode.mesh import load_stl, mesh_bounds
from trident_gcode.profile_stack import stack_from_mesh

DEFAULT_PRINTER_KEY = "trident"


def _get_profile(body):
    from dataclasses import replace
    key = str(body.get("printer") or DEFAULT_PRINTER_KEY)
    profile = PRINTER_PROFILES.get(key, PRINTER_PROFILES[DEFAULT_PRINTER_KEY])
    nozzle = body.get("nozzle")
    if nozzle:
        nd = float(nozzle)
        if 0.1 <= nd <= 1.2:
            profile = replace(profile, nozzle_diameter=nd)
    return profile

PORT = 8777
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Server-side safety ceilings (mirror the UI clamps).
AMP_MIN, AMP_MAX = 0.0, 0.95         # user-revised ceiling: z-amp < 1mm
RADIUS_SCALE_MIN, RADIUS_SCALE_MAX = 0.2, 1.5
# Empirical print-quality slope ceiling (amp*waves/radius): waves steeper than
# this collapsed above half height on the 2026-07-05 test print (R3D PETG).
QUALITY_SLOPE_LIMIT = 0.25
DEFAULT_FILAMENT = "R3D PETG"

# Mesh upload caps and cache.
MESH_MAX_BYTES = 50 * 1024 * 1024     # 50 MB
MESH_MAX_TRIANGLES = 500_000
MESH_CACHE_MAX = 4
_mesh_cache: "OrderedDict[str, dict]" = OrderedDict()


# --------------------------------------------------------------------------
# Piecewise-linear interpolator from a list of [t, value] control points.
# --------------------------------------------------------------------------
def _make_interp(points, clamp_lo, clamp_hi):
    """Return f(t) -> clamped, piecewise-linear value.

    points: list of [t, value]. Sorted by t; values clamped into range; the
    curve is held flat (clamped) beyond the first/last control point.
    """
    pts = []
    for pair in points:
        try:
            t = float(pair[0])
            v = float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        v = min(max(v, clamp_lo), clamp_hi)
        pts.append((min(max(t, 0.0), 1.0), v))
    if not pts:
        pts = [(0.0, clamp_lo)]
    pts.sort(key=lambda p: p[0])

    def f(t):
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for i in range(1, len(pts)):
            t0, v0 = pts[i - 1]
            t1, v1 = pts[i]
            if t <= t1:
                if t1 - t0 < 1e-9:
                    return v1
                frac = (t - t0) / (t1 - t0)
                return v0 + frac * (v1 - v0)
        return pts[-1][1]

    return f


def _catmull_rom(p0, p1, p2, p3, frac):
    f2 = frac * frac
    f3 = f2 * frac
    return 0.5 * (
        (2 * p1)
        + (-p0 + p2) * frac
        + (2 * p0 - 5 * p1 + 4 * p2 - p3) * f2
        + (-p0 + 3 * p1 - 3 * p2 + p3) * f3
    )


def _make_smooth_interp(points, clamp_lo, clamp_hi):
    """Like _make_interp, but uses a Catmull-Rom spline through the control
    points instead of straight line segments (mirrors preview_math.js
    makeSmoothInterp so the live preview matches the generated g-code)."""
    pts = []
    for pair in points:
        try:
            t = float(pair[0])
            v = float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        v = min(max(v, clamp_lo), clamp_hi)
        pts.append((min(max(t, 0.0), 1.0), v))
    if not pts:
        pts = [(0.0, clamp_lo)]
    pts.sort(key=lambda p: p[0])
    n = len(pts)

    def f(t):
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for i in range(1, n):
            t0, v0 = pts[i - 1]
            t1, v1 = pts[i]
            if t <= t1:
                frac = 1.0 if (t1 - t0) < 1e-9 else (t - t0) / (t1 - t0)
                vm1 = pts[i - 2][1] if i - 2 >= 0 else v0
                v2 = pts[i + 1][1] if i + 1 < n else v1
                v = _catmull_rom(vm1, v0, v1, v2, frac)
                return min(max(v, clamp_lo), clamp_hi)
        return pts[-1][1]

    return f


def _make_shape(name, radius, star_points=5, star_depth=0.35):
    if name == "circle":
        return circle(radius)
    if name == "star":
        return star(radius, points=int(star_points), depth=float(star_depth))
    if name == "square":
        return superellipse(radius, n=4.0)
    raise ValueError("unknown shape: %s (use circle, star, or square)" % name)


def _parse_cage(raw):
    """Validate + clean an asymmetric control-cage grid from the request body.

    Expects a list of lists of numbers: N rows (height) x M cols (azimuth).
    Rules: 2 <= rows <= 9, 3 <= cols <= 16, all rows the same length; every
    value is clamped to [0.5, 1.5]; non-numeric values become 1.0. Returns None
    (symmetric, byte-identical default) when the field is missing, not a
    list-of-lists of the required shape, or every value is within 1e-6 of 1.0.
    """
    if not isinstance(raw, list):
        return None
    rows = len(raw)
    if rows < 2 or rows > 9:
        return None
    if not isinstance(raw[0], list):
        return None
    cols = len(raw[0])
    if cols < 3 or cols > 16:
        return None
    cleaned = []
    for row in raw:
        if not isinstance(row, list) or len(row) != cols:
            return None
        out = []
        for val in row:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                v = 1.0
            else:
                v = float(val)
                if v != v:  # NaN
                    v = 1.0
            out.append(min(1.5, max(0.5, v)))
        cleaned.append(out)
    if all(abs(v - 1.0) < 1e-6 for row in cleaned for v in row):
        return None
    return cleaned


def _parse_blob_spec(body, radius: float | None = None) -> BlobSpec | None:
    """Extract blob parameters from the request body.

    Returns None when blobs are disabled (no per-turn count or spacing given).
    ``blob_spacing_mm`` (with a known radius) overrides ``blob_per_turn``:
    the count is derived from the circumference so bead pitch stays constant
    across sizes.  Server-side clamps keep values in safe ranges.
    """
    blobs_per_turn = int(body.get("blob_per_turn", 0))
    spacing_mm = float(body.get("blob_spacing_mm", 0) or 0)
    if spacing_mm > 0 and radius:
        circumference = 2.0 * math.pi * radius
        blobs_per_turn = int(round(circumference / max(spacing_mm, 1.0)))
    if blobs_per_turn <= 0:
        return None
    blobs_per_turn = max(1, min(blobs_per_turn, 72))
    turn_stride = max(1, int(body.get("blob_turn_stride", 3)))
    stagger = bool(body.get("blob_stagger", True))
    align = str(body.get("blob_align", "") or ("stagger" if stagger else "column"))
    if align not in ("stagger", "column", "jitter"):
        align = "stagger"
    jitter = max(0.0, min(float(body.get("blob_jitter", 0.5)), 1.0))
    volume = max(0.1, min(float(body.get("blob_volume", 1.5)), 15.0))
    vol_start = max(0.1, min(float(body.get("blob_vol_start", 1.0)), 3.0))
    vol_end = max(0.1, min(float(body.get("blob_vol_end", 1.0)), 3.0))
    dwell = max(0, min(int(body.get("blob_dwell", 200)), 2000))
    fade_in = max(0.0, min(float(body.get("blob_fade_in", 0.15)), 0.5))
    fade_out = max(0.0, min(float(body.get("blob_fade_out", 0.05)), 0.5))
    return BlobSpec(
        blobs_per_turn=blobs_per_turn,
        turn_stride=turn_stride,
        stagger=stagger,
        align=align,
        jitter=jitter,
        volume_mm3=volume,
        volume_scale_start=vol_start,
        volume_scale_end=vol_end,
        dwell_after_ms=dwell,
        fade_in=fade_in,
        fade_out=fade_out,
    )


def _parse_loop_spec(body, radius: float | None = None) -> LoopSpec | None:
    """Extract hanging-loop parameters from the request body.

    Returns None when loops are disabled.  ``loop_spacing_mm`` (with a known
    radius) overrides ``loop_per_turn`` via the circumference, like blobs.
    """
    per_turn = int(body.get("loop_per_turn", 0))
    spacing_mm = float(body.get("loop_spacing_mm", 0) or 0)
    if spacing_mm > 0 and radius:
        per_turn = int(round(2.0 * math.pi * radius / max(spacing_mm, 1.0)))
    if per_turn <= 0:
        return None
    per_turn = max(1, min(per_turn, 48))
    align = str(body.get("loop_align", "column"))
    if align not in ("stagger", "column", "jitter"):
        align = "column"
    stitch_mode = str(body.get("loop_mode", "dip"))
    if stitch_mode not in ("dip", "spike"):
        stitch_mode = "dip"
    return LoopSpec(
        stitch_mode=stitch_mode,
        lean_deg=max(0.0, min(float(body.get("loop_lean", 20.0)), 45.0)),
        coast_mm=max(0.0, min(float(body.get("loop_coast", 0.8)), 6.0)),
        tip_retract_mm=max(0.0, min(float(body.get("loop_retract", 0.0)), 2.0)),
        loops_per_turn=per_turn,
        turn_stride=max(1, int(body.get("loop_turn_stride", 1))),
        align=align,
        jitter=max(0.0, min(float(body.get("loop_jitter", 0.5)), 1.0)),
        row_mm=max(1.0, min(float(body.get("loop_row", 2.5)), 6.0)),
        up_mm=max(1.0, min(float(body.get("loop_up", 3.5)), 8.0)),
        out_mm=max(0.0, min(float(body.get("loop_out", 0.5)), 5.0)),
        rejoin_mm=max(0.5, min(float(body.get("loop_rejoin", 2.0)), 6.0)),
        dwell_ms=max(0, min(int(body.get("loop_dwell", 0)), 2000)),
        flow=max(0.5, min(float(body.get("loop_flow", 1.2)), 2.5)),
        speed_mm_s=max(5.0, min(float(body.get("loop_speed", 10.0)), 30.0)),
        cuff_turns=max(1, min(int(body.get("loop_cuff", 3)), 10)),
        wave_amp=max(0.0, min(float(body.get("loop_wave_amp", 0.0)), 3.0)),
        waves=max(1, min(int(body.get("loop_waves", 12)), 40)),
        fade_in=max(0.0, min(float(body.get("loop_fade_in", 0.10)), 0.5)),
        fade_out=max(0.0, min(float(body.get("loop_fade_out", 0.0)), 0.5)),
    )


# --------------------------------------------------------------------------
# Core: turn a design request dict into (gcode_text, report_text, issues, stats)
# --------------------------------------------------------------------------
def generate_design(body):
    shape_name = str(body.get("shape", "circle"))
    radius = float(body.get("radius", 32.0))
    height = float(body.get("height", 60.0))
    layer_height = float(body.get("layer_height", 0.30))
    line_width = body.get("line_width", None)
    z_waves = int(body.get("z_waves", 5))
    xy_twist = float(body.get("xy_twist", 0.0))
    z_twist = float(body.get("z_twist", 0.0))
    base_layers = int(body.get("base_layers", 2))
    if str(body.get("bottom", "solid")) == "open":
        base_layers = 0
    brim = int(body.get("brim", 0))
    # Base fill style + shielding skirt.
    base_style = str(body.get("base_style", "spiral"))
    if base_style not in ("spiral", "concentric"):
        base_style = "spiral"
    skirt_loops = max(0, min(int(body.get("skirt", 0)), 3))
    # First-layer height (mm) takes precedence over the raw squish factor:
    # lower first layer = bead pressed harder into the plate = firmer grip.
    _flh = float(body.get("first_layer_height", 0) or 0)
    if _flh > 0:
        squish = min(max(_flh / max(layer_height, 1e-6), 0.5), 1.0)
    else:
        squish = min(max(float(body.get("squish", 0.75)), 0.5), 1.0)
    spacing_factor = max(0.8, min(float(body.get("first_layer_spacing_factor", 1.25)), 1.5))
    filament = body.get("filament", None)
    print_speed = float(body.get("print_speed", 40.0))
    star_points = int(body.get("star_points", 5))
    star_depth = float(body.get("star_depth", 0.35))

    # Symmetry-breaking: leaning spine + elliptical cross-section.
    spine_mm = max(0.0, min(float(body.get("spine_mm", 0.0)), 20.0))
    spine_deg = float(body.get("spine_deg", 0.0))
    ovality = max(-0.4, min(float(body.get("ovality", 0.0)), 0.4))
    spine_offset = None
    if spine_mm > 0.0:
        _sc = math.cos(math.radians(spine_deg))
        _ss = math.sin(math.radians(spine_deg))
        spine_offset = lambda t: (t * spine_mm * _sc, t * spine_mm * _ss)

    # Radial surface texture (probe-safe: displaces radius, never Z).
    pattern = body.get("pattern") or None
    pattern_amp = max(0.0, min(float(body.get("pattern_amp", 1.0)), 4.0))
    pattern_waves = int(body.get("pattern_waves", 12))
    pattern_bands = float(body.get("pattern_bands", 6.0))
    pattern_twist = float(body.get("pattern_twist", 0.0))
    pattern_phase = float(body.get("pattern_phase", 0.0))
    pattern_fade_in = max(0.0, min(float(body.get("pattern_fade_in", 0.10)), 0.5))
    pattern_fade_out = max(0.0, min(float(body.get("pattern_fade_out", 0.0)), 0.5))
    pattern_alternate = bool(body.get("pattern_alternate", False))

    amp_profile = body.get("amp_profile") or [[0, 0.0], [1, 0.0]]
    radius_profile = body.get("radius_profile") or [[0, 1.0], [1, 1.0]]
    radius_profile_smooth = bool(body.get("radius_profile_smooth", False))

    # Asymmetric control cage: N rows (height) x M cols (azimuth) of radius scale
    # factors. Validate shape, clamp values to [0.5, 1.5], coerce non-numeric to
    # 1.0. An all-1.0 (or missing/malformed) cage resolves to None so default
    # output stays byte-identical.
    cage = _parse_cage(body.get("cage"))

    # Server-side clamps (never trust the client).
    amp_fn = _make_interp(amp_profile, AMP_MIN, AMP_MAX)
    radius_interp = _make_smooth_interp if radius_profile_smooth else _make_interp
    radius_fn = radius_interp(radius_profile, RADIUS_SCALE_MIN, RADIUS_SCALE_MAX)

    profile = _get_profile(body)

    # Line width: default like generate.py (~1.125x a 0.4 nozzle, rounded).
    nozzle = profile.nozzle_diameter
    if line_width is None:
        lw = round(nozzle * 1.125, 3)
    else:
        lw = float(line_width)

    # Writer kwargs: selected-printer defaults; merge Orca filament settings if given.
    writer_kwargs = dict(
        profile=profile,
        line_width=lw,
        layer_height=layer_height,
        print_speed=print_speed,
        first_layer_speed=18.0,
    )
    if filament:
        from trident_gcode.orca import FilamentSettings
        try:
            fs = FilamentSettings.from_orca(str(filament))
        except KeyError as e:
            raise KeyError(str(e))
        writer_kwargs.update(fs.writer_kwargs())

    writer = GcodeWriter(**writer_kwargs)

    # z_amp=1.0 so the envelope's return value IS the absolute mm of amplitude
    # (exactly how calibrate.py's z-amp ladder drives it). z_amp_ramp=0 so the
    # user's own amp curve (which defaults to 0 at the base) controls ramp-in.
    spec = SpiralSpec(
        base_radius=radius,
        height=height,
        layer_height=layer_height,
        points_per_turn=240,
        xy_twist_turns=xy_twist,
        z_waves=z_waves,
        z_twist_turns=z_twist,
        z_amp=1.0,
        z_amp_ramp=0.0,
        z_amp_envelope=amp_fn,
        radius_envelope=radius_fn,
        r_pattern=pattern,
        r_amp=pattern_amp,
        r_waves=pattern_waves,
        r_bands=pattern_bands,
        r_twist_turns=pattern_twist,
        r_phase=pattern_phase,
        r_fade_in=pattern_fade_in,
        r_fade_out=pattern_fade_out,
        r_alternate=pattern_alternate,
        spine_offset=spine_offset,
        ovality=ovality,
        cage=cage,
    )
    shape = _make_shape(shape_name, radius, star_points, star_depth)

    blob_spec = _parse_blob_spec(body, radius=radius)
    loop_spec = _parse_loop_spec(body, radius=radius)
    overhang_flow_k = max(0.0, min(float(body.get("overhang_flow_k", 0.0)), 1.0))

    if loop_spec is not None:
        # Loop fabric replaces the wall entirely (knitted rows of vertical
        # loop stitches) — z-waves/patterns don't apply; the silhouette does.
        report = build_loop_fabric(
            writer, shape=shape, height=height, spec=loop_spec,
            radius_envelope=radius_fn,
            cage=cage,
            first_layer_squish=squish,
            cuff_lh=layer_height,
        )
    else:
        report = build_continuous_spiral(
            writer, spec, shape=shape,
            first_layer_squish=squish,
            first_layer_spacing_factor=spacing_factor,
            base_layers=base_layers,
            brim_loops=brim,
            base_style=base_style,
            skirt_loops=skirt_loops,
            blob_spec=blob_spec,
            overhang_flow_k=overhang_flow_k,
        )

    gcode_text = writer.text()

    # Re-check the generated G-code against the machine (independent verification).
    fd, tmp_path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(gcode_text)
        analysis = analyze_gcode(tmp_path, profile)
        report_text = format_report(analysis, profile)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Empirical print-quality check: peak wall slope = amp*waves/(radius*sil).
    # 2026-07-05 test print on this machine: wave slopes beyond ~0.25 (about
    # 14 deg) collapsed above half height. Distinct from the probe amp ceiling.
    peak_slope = 0.0
    for k in range(51):
        t = k / 50.0
        s = amp_fn(t) * z_waves / max(radius * radius_fn(t), 1e-6)
        peak_slope = max(peak_slope, s)
    issues_extra = []
    if peak_slope > QUALITY_SLOPE_LIMIT + 1e-9:
        issues_extra.append(
            "Peak wave slope %.2f exceeds the empirically printable ~%.2f "
            "(amp*waves/radius) - upper waves may collapse; reduce amplitude "
            "or wave count." % (peak_slope, QUALITY_SLOPE_LIMIT))
    stats = {
        "wave_slope": round(peak_slope, 3),
        "moves": analysis.moves,
        "extrude_moves": analysis.extrude_moves,
        "height_mm": round(analysis.height, 1),
        "footprint_mm": [round(analysis.footprint[0], 1), round(analysis.footprint[1], 1)],
        "filament_m": round(analysis.filament_mm / 1000.0, 2),
        "est_time_s": round(analysis.est_time_s, 1),
        "max_z_rate": round(analysis.max_z_rate, 1),
        "max_flow": round(analysis.max_flow, 1),
        "probe_hits": analysis.probe_hits,
        "unsupported_moves": analysis.unsupported_moves,
        "top_z_mm": report.get("top_z_mm"),
        "blob_count": report.get("blob_count", 0),
    }

    filename = "design_%s_%dmm.gcode" % (_clean_shape(shape_name), int(round(height)))

    return {
        "gcode": gcode_text,
        "report": report_text,
        "issues": list(analysis.issues) + issues_extra,
        "stats": stats,
        "filename": filename,
    }


# --------------------------------------------------------------------------
# Mesh cache helpers (in-memory, single-user local server).
# --------------------------------------------------------------------------
def _mesh_cache_put(mesh_id, tris):
    _mesh_cache[mesh_id] = {"tris": tris}
    _mesh_cache.move_to_end(mesh_id)
    while len(_mesh_cache) > MESH_CACHE_MAX:
        _mesh_cache.popitem(last=False)


def _clean_shape(s):
    return "".join(c if (c.isalnum()) else "_" for c in s)


# --------------------------------------------------------------------------
# Core: mesh_texture mode -- contour stack from an uploaded STL + profile spiral.
# --------------------------------------------------------------------------
def generate_mesh_texture_design(body):
    mesh_id = body.get("mesh_id")
    if not mesh_id:
        raise ValueError("mesh_id is required for mode=mesh_texture")
    entry = _mesh_cache.get(mesh_id)
    if entry is None:
        raise KeyError("mesh_id not found (upload may have expired) - re-upload the STL")

    scale = float(body.get("scale", 1.0))
    layer_height = float(body.get("layer_height", 0.30))
    points_per_turn = int(body.get("points_per_turn", 240))
    line_width = body.get("line_width", None)
    base_layers = int(body.get("base_layers", 2))
    if str(body.get("bottom", "solid")) == "open":
        base_layers = 0
    brim = int(body.get("brim", 0))
    # First-layer height (mm) takes precedence over the raw squish factor:
    # lower first layer = bead pressed harder into the plate = firmer grip.
    _flh = float(body.get("first_layer_height", 0) or 0)
    if _flh > 0:
        squish = min(max(_flh / max(layer_height, 1e-6), 0.5), 1.0)
    else:
        squish = min(max(float(body.get("squish", 0.75)), 0.5), 1.0)
    spacing_factor = max(0.8, min(float(body.get("first_layer_spacing_factor", 1.25)), 1.5))
    filament = body.get("filament", None)
    print_speed = float(body.get("print_speed", 40.0))

    # Radial surface texture (probe-safe: displaces radius, never Z).
    pattern = body.get("pattern") or None
    pattern_amp = max(0.0, min(float(body.get("pattern_amp", 1.0)), 4.0))
    pattern_waves = int(body.get("pattern_waves", 12))
    pattern_bands = float(body.get("pattern_bands", 6.0))
    pattern_twist = float(body.get("pattern_twist", 0.0))
    pattern_phase = float(body.get("pattern_phase", 0.0))
    pattern_fade_in = max(0.0, min(float(body.get("pattern_fade_in", 0.10)), 0.5))
    pattern_fade_out = max(0.0, min(float(body.get("pattern_fade_out", 0.0)), 0.5))
    pattern_alternate = bool(body.get("pattern_alternate", False))

    z_waves = int(body.get("z_waves", 5))
    z_twist = float(body.get("z_twist", 0.0))

    amp_profile = body.get("amp_profile") or [[0, 0.0], [1, 0.0]]
    amp_fn = _make_interp(amp_profile, AMP_MIN, AMP_MAX)

    tris = entry["tris"]
    if scale != 1.0:
        tris = [tuple((vx * scale, vy * scale, vz * scale) for (vx, vy, vz) in t)
                for t in tris]

    contours = stack_from_mesh(tris, layer_height, points_per_turn)
    heights = [layer_height * (i + 0.5) for i in range(len(contours))]

    profile = _get_profile(body)

    # Line width: default like generate.py (~1.125x a 0.4 nozzle, rounded).
    nozzle = profile.nozzle_diameter
    if line_width is None:
        lw = round(nozzle * 1.125, 3)
    else:
        lw = float(line_width)

    writer_kwargs = dict(
        profile=profile,
        line_width=lw,
        layer_height=layer_height,
        print_speed=print_speed,
        first_layer_speed=18.0,
    )
    if filament:
        from trident_gcode.orca import FilamentSettings
        try:
            fs = FilamentSettings.from_orca(str(filament))
        except KeyError as e:
            raise KeyError(str(e))
        writer_kwargs.update(fs.writer_kwargs())

    writer = GcodeWriter(**writer_kwargs)

    mesh_radius = max(math.hypot(x, y) for c in contours for (x, y) in c) if contours else None
    blob_spec = _parse_blob_spec(body, radius=mesh_radius)
    loop_spec = _parse_loop_spec(body, radius=mesh_radius)
    overhang_flow_k = max(0.0, min(float(body.get("overhang_flow_k", 0.0)), 1.0))

    report = build_profile_spiral(
        writer, contours, heights,
        points_per_turn=points_per_turn,
        z_amp=1.0,
        z_waves=z_waves,
        z_twist_turns=z_twist,
        z_amp_ramp=0.0,
        z_amp_envelope=amp_fn,
        r_pattern=pattern,
        r_amp=pattern_amp,
        r_waves=pattern_waves,
        r_bands=pattern_bands,
        r_twist_turns=pattern_twist,
        r_phase=pattern_phase,
        r_fade_in=pattern_fade_in,
        r_fade_out=pattern_fade_out,
        r_alternate=pattern_alternate,
        first_layer_squish=squish,
        first_layer_spacing_factor=spacing_factor,
        base_layers=base_layers,
        brim_loops=brim,
        blob_spec=blob_spec,
        loop_spec=loop_spec,
        overhang_flow_k=overhang_flow_k,
    )

    gcode_text = writer.text()

    fd, tmp_path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(gcode_text)
        analysis = analyze_gcode(tmp_path, profile)
        report_text = format_report(analysis, profile)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Empirical print-quality check: peak wall slope = amp*waves/(radius*sil).
    bottom_radius = max(math.hypot(x, y) for (x, y) in contours[0]) if contours else 1.0
    peak_slope = 0.0
    for k in range(51):
        t = k / 50.0
        s = amp_fn(t) * z_waves / max(bottom_radius, 1e-6)
        peak_slope = max(peak_slope, s)
    issues_extra = []
    if peak_slope > QUALITY_SLOPE_LIMIT + 1e-9:
        issues_extra.append(
            "Peak wave slope %.2f exceeds the empirically printable ~%.2f "
            "(amp*waves/radius) - upper waves may collapse; reduce amplitude "
            "or wave count." % (peak_slope, QUALITY_SLOPE_LIMIT))
    # STL contour-stack mode uses build_profile_spiral, which has no cage input,
    # so a cage sent from the live preview is silently ignored here. Tell the user.
    if _parse_cage(body.get("cage")) is not None:
        issues_extra.append(
            "cage deformation only applies to parametric designs "
            "(not loops fabric / STL mode) - the STL texture wall was "
            "generated without the cage.")

    stats = {
        "wave_slope": round(peak_slope, 3),
        "moves": analysis.moves,
        "extrude_moves": analysis.extrude_moves,
        "height_mm": round(analysis.height, 1),
        "footprint_mm": [round(analysis.footprint[0], 1), round(analysis.footprint[1], 1)],
        "filament_m": round(analysis.filament_mm / 1000.0, 2),
        "est_time_s": round(analysis.est_time_s, 1),
        "max_z_rate": round(analysis.max_z_rate, 1),
        "max_flow": round(analysis.max_flow, 1),
        "probe_hits": analysis.probe_hits,
        "unsupported_moves": analysis.unsupported_moves,
        "top_z_mm": report.get("top_z_mm"),
        "blob_count": report.get("blob_count", 0),
    }

    filename = "design_mesh_%s_%dmm.gcode" % (_clean_shape(mesh_id), int(round(max(heights) if heights else 0)))

    return {
        "gcode": gcode_text,
        "report": report_text,
        "issues": list(analysis.issues) + issues_extra,
        "stats": stats,
        "filename": filename,
    }


# --------------------------------------------------------------------------
# HTTP handler: static files + JSON API.
# --------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=REPO_ROOT, **kwargs)

    def log_message(self, fmt, *args):
        # Keep console quiet-ish and ASCII-safe.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        # Static assets change during development: force the browser to
        # revalidate every request instead of heuristically caching JS/CSS.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/printers":
            printers = []
            for key, prof in PRINTER_PROFILES.items():
                printers.append({
                    "key": key,
                    "name": prof.name,
                    "bed": [prof.bed_size_x, prof.bed_size_y],
                    "z_max": prof.z_max,
                    "z_amp_max": prof.z_amp_max,
                })
            self._send_json({"printers": printers, "default": DEFAULT_PRINTER_KEY})
            return
        if path == "/api/filaments":
            try:
                from trident_gcode.orca import list_filaments
                names = list_filaments()
                default = DEFAULT_FILAMENT if DEFAULT_FILAMENT in names else (
                    names[0] if names else None)
                self._send_json({"filaments": names, "default": default})
            except Exception:
                self._send_json({"filaments": [], "default": None})
            return
        if path == "/api/mesh_profile":
            self._handle_mesh_profile()
            return
        return super().do_GET()

    def _handle_mesh_profile(self):
        query = parse_qs(urlparse(self.path).query)
        mesh_id = (query.get("mesh_id") or [None])[0]
        try:
            layer_height = float((query.get("layer_height") or ["0.30"])[0])
        except (TypeError, ValueError):
            layer_height = 0.30

        entry = _mesh_cache.get(mesh_id) if mesh_id else None
        if entry is None:
            self._send_json({"error": "mesh_id not found"}, status=404)
            return

        try:
            contours = stack_from_mesh(entry["tris"], layer_height, 96)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:
            self._send_json({"error": "Slicing failed: %s" % e}, status=500)
            return

        downsampled = [
            [[round(x, 3), round(y, 3)] for (x, y) in c]
            for c in contours[::4]
        ]
        self._send_json({
            "mesh_id": mesh_id,
            "layers": len(contours),
            "contours": downsampled,
        })

    def _handle_upload_mesh(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0

        if length <= 0:
            self._send_json({"error": "Empty request body."}, status=400)
            return
        if length > MESH_MAX_BYTES:
            self._send_json(
                {"error": "File too large (max %d MB)." % (MESH_MAX_BYTES // (1024 * 1024))},
                status=400)
            return

        data = self.rfile.read(length)
        filename = self.headers.get("X-Filename", "upload.stl")

        fd, tmp_path = tempfile.mkstemp(suffix=".stl")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            tris = load_stl(tmp_path)
        except Exception as e:
            self._send_json({"error": "Could not parse STL: %s" % e}, status=400)
            return
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if len(tris) > MESH_MAX_TRIANGLES:
            self._send_json(
                {"error": "Mesh has too many triangles (max %d)." % MESH_MAX_TRIANGLES},
                status=400)
            return
        if not tris:
            self._send_json({"error": "STL contained no triangles."}, status=400)
            return

        try:
            (minx, miny, minz), (maxx, maxy, maxz) = mesh_bounds(tris)
        except Exception as e:
            self._send_json({"error": "Could not compute mesh bounds: %s" % e}, status=400)
            return

        mesh_id = hashlib.md5(data).hexdigest()[:12]
        _mesh_cache_put(mesh_id, tris)

        self._send_json({
            "mesh_id": mesh_id,
            "bounds": {
                "min": [round(minx, 3), round(miny, 3), round(minz, 3)],
                "max": [round(maxx, 3), round(maxy, 3), round(maxz, 3)],
            },
            "triangles": len(tris),
            "height": round(maxz - minz, 3),
        })
        # filename is accepted for client bookkeeping; not otherwise used server-side.
        _ = filename

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/upload_mesh":
            self._handle_upload_mesh()
            return
        if path != "/api/generate":
            self.send_error(404, "Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "Request body is not valid JSON."}, status=400)
            return

        mode = str(body.get("mode", "parametric"))
        try:
            if mode == "mesh_texture":
                result = generate_mesh_texture_design(body)
            else:
                result = generate_design(body)
        except KeyError as e:
            # Unknown filament profile / mesh_id.
            msg = str(e).strip('"').strip("'")
            self._send_json({"error": msg}, status=400)
            return
        except ValueError as e:
            # Footprint / height too big, unknown shape, bad numbers.
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:
            self._send_json({"error": "Generation failed: %s" % e}, status=500)
            return

        self._send_json(result)


def main():
    try:
        httpd = ThreadingHTTPServer(("", PORT), Handler)
    except OSError as e:
        sys.stderr.write(
            "ERROR: could not bind port %d - is another server already running? (%s)\n"
            % (PORT, e))
        return 1
    sys.stdout.write(
        "Trident design server on http://localhost:%d/viewer/index.html (Ctrl-C to stop)\n"
        % PORT)
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nstopped.\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
