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
from trident_gcode.mesh import load_stl, mesh_bounds, analyze_vase_compatibility
from trident_gcode.profile_stack import stack_from_mesh, stack_from_shape, contour_normals
from trident_gcode.point_edit import (MaskSpec, ProtectionSpec, FFDSpec, SmoothSpec,
                                       RadialPushSpec, MASK_CHANNEL_NAMES, FFD_MAX_MM,
                                       apply_point_edits)
from trident_gcode.paths import PathPoint
from trident_gcode.paths import _R_PATTERNS, _fade_envelope, R_PATTERN_NAMES, cage_scale
from trident_gcode.stl_export import contours_to_mesh, write_binary_stl

DEFAULT_PRINTER_KEY = "trident"

# Texture modes that place discrete SITES along the bead (blobs) or replace the
# wall emission entirely (loop fabric), rather than displacing the radius. They
# are not members of _R_PATTERNS, and they have no surface representation, so an
# STL export falls back to the smooth wall instead of rejecting the design.
# The UI already omits `pattern` for these; this keeps direct API callers (and
# saved designs that carry the name through) from hitting a spurious 400.
_SITE_TEXTURE_PATTERNS = frozenset({"blobs", "loops"})

# A single export request must not be able to exhaust memory. The G-code path is
# bounded by the printer (profile.z_max rejects an over-tall design), but an STL
# export has no machine to bound it -- height=1e6 at a 0.3 mm layer is 3.3 M
# rings, and every ring costs points_per_turn vertices, so the build balloons
# long before anything is returned. These ceilings sit far above any real design
# (4 M vertices is a 16k-layer part at 240 points/turn).
_EXPORT_MAX_VERTICES = 4_000_000
_EXPORT_MAX_PPT = 2000


def _check_export_budget(n_layers, points_per_turn):
    """Reject an export whose vertex count would blow up, with a clear reason."""
    if points_per_turn > _EXPORT_MAX_PPT:
        raise ValueError(
            "points_per_turn %d exceeds the export limit of %d"
            % (points_per_turn, _EXPORT_MAX_PPT))
    total = n_layers * points_per_turn
    if total > _EXPORT_MAX_VERTICES:
        raise ValueError(
            "design is too large to export: %d layers x %d points = %d vertices "
            "(limit %d). Reduce height, or raise layer height."
            % (n_layers, points_per_turn, total, _EXPORT_MAX_VERTICES))


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


# --------------------------------------------------------------------------
# Point Edit Modifiers: parse the 5 optional purple-modifier request blocks.
# Each returns None (no-op, byte-identical output) when disabled/malformed.
# --------------------------------------------------------------------------
POINT_FFD_ROWS_MIN, POINT_FFD_ROWS_MAX = 2, 12
POINT_FFD_COLS_MIN, POINT_FFD_COLS_MAX = 3, 24


def _parse_point_mask(raw) -> MaskSpec | None:
    if not isinstance(raw, dict):
        return None
    channel = str(raw.get("channel", "none"))
    image = raw.get("image")
    cleaned_image = None
    if isinstance(image, list) and image and isinstance(image[0], list):
        rows = len(image)
        cols = len(image[0])
        if 2 <= rows <= 64 and 2 <= cols <= 64:
            cleaned_image = []
            for row in image:
                if not isinstance(row, list) or len(row) != cols:
                    cleaned_image = None
                    break
                cleaned_image.append([
                    min(1.0, max(0.0, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
                    for v in row
                ])
    if channel not in MASK_CHANNEL_NAMES and not cleaned_image:
        return None
    return MaskSpec(
        channel=channel if channel in MASK_CHANNEL_NAMES else "none",
        scale_u=max(0.5, min(float(raw.get("scale_u", 8.0)), 40.0)),
        scale_v=max(0.5, min(float(raw.get("scale_v", 6.0)), 40.0)),
        invert=bool(raw.get("invert", False)),
        image=cleaned_image,
    )


def _parse_point_protection(raw) -> ProtectionSpec | None:
    if not isinstance(raw, dict):
        return None
    pb = max(0.0, min(float(raw.get("protect_bottom", 0.0)), 1.0))
    pt = max(0.0, min(float(raw.get("protect_top", 0.0)), 1.0))
    if pb <= 0.0 and pt <= 0.0:
        return None
    fo = max(0.005, min(float(raw.get("falloff", 0.08)), 0.5))
    return ProtectionSpec(protect_bottom=pb, protect_top=pt, falloff=fo)


def _parse_point_ffd(raw) -> FFDSpec | None:
    if not isinstance(raw, dict):
        return None
    cage = raw.get("cage")
    if not isinstance(cage, list):
        return None
    rows = len(cage)
    if rows < POINT_FFD_ROWS_MIN or rows > POINT_FFD_ROWS_MAX:
        return None
    if not isinstance(cage[0], list):
        return None
    cols = len(cage[0])
    if cols < POINT_FFD_COLS_MIN or cols > POINT_FFD_COLS_MAX:
        return None
    cleaned = []
    any_nonzero = False
    for row in cage:
        if not isinstance(row, list) or len(row) != cols:
            return None
        out_row = []
        for cell in row:
            if not isinstance(cell, list) or len(cell) != 3:
                out_row.append([0.0, 0.0, 0.0])
                continue
            v = []
            for comp in cell:
                f = float(comp) if isinstance(comp, (int, float)) and not isinstance(comp, bool) else 0.0
                if f != f:  # NaN
                    f = 0.0
                f = min(FFD_MAX_MM, max(-FFD_MAX_MM, f))
                if abs(f) > 1e-6:
                    any_nonzero = True
                v.append(f)
            out_row.append(v)
        cleaned.append(out_row)
    if not any_nonzero:
        return None
    strength = max(0.0, min(float(raw.get("strength", 1.0)), 2.0))
    return FFDSpec(cage=cleaned, strength=strength)


def _parse_point_smooth(raw) -> SmoothSpec | None:
    if not isinstance(raw, dict):
        return None
    iterations = int(raw.get("iterations", 0) or 0)
    if iterations <= 0:
        return None
    iterations = max(1, min(iterations, 12))
    theta_amt = max(0.0, min(float(raw.get("theta_amount", 0.5)), 1.0))
    t_amt = max(0.0, min(float(raw.get("t_amount", 0.5)), 1.0))
    strength = max(0.0, min(float(raw.get("strength", 1.0)), 2.0))
    return SmoothSpec(iterations=iterations, theta_amount=theta_amt,
                       t_amount=t_amt, strength=strength)


def _parse_point_radial_push(raw) -> RadialPushSpec | None:
    if not isinstance(raw, dict):
        return None
    amp = float(raw.get("amp_mm", 0.0) or 0.0)
    if amp == 0.0:
        return None
    amp = max(-5.0, min(amp, 5.0))
    strength = max(0.0, min(float(raw.get("strength", 1.0)), 2.0))
    return RadialPushSpec(amp_mm=amp, strength=strength)


def _parse_point_edit_specs(body):
    """Parse all 5 point-edit blocks from a request body at once."""
    return (
        _parse_point_mask(body.get("point_mask")),
        _parse_point_protection(body.get("point_protection")),
        _parse_point_ffd(body.get("point_ffd")),
        _parse_point_smooth(body.get("point_smooth")),
        _parse_point_radial_push(body.get("point_radial_push")),
    )


def _point_edit_active(body) -> bool:
    return any(x is not None for x in _parse_point_edit_specs(body))


def _parse_overhang_fan(body):
    """Fan min/max bounds (0..1 fractions) -- always concrete, never None. The
    fan runs between these two speeds, selected by wall lean (see
    _overhang_fan). The client only sends fan_min/fan_max when they differ
    from the "always full fan" default (100/100); absence just means both 1.0.

    Deliberately authoritative over any fan speed a selected filament profile
    suggests (Orca's fan_max_speed etc.): the UI's own promise is "both at
    100% = constant full fan", so the sliders must be what actually reaches
    M106, not silently overridden by writer.fan_speed. A filament that wants
    a specific fan curve requires the user to actually dial the sliders to it.
    """
    fan_min = body.get("fan_min")
    fan_max = body.get("fan_max")
    try:
        fan_min = max(0.0, min(float(fan_min), 1.0)) if fan_min is not None else 1.0
        fan_max = max(0.0, min(float(fan_max), 1.0)) if fan_max is not None else 1.0
    except (TypeError, ValueError):
        fan_min, fan_max = 1.0, 1.0
    # "min" ramps up to "max" as overhang steepens -- if the client ever sends
    # them inverted, treat max as the floor so the ramp direction stays sane.
    fan_max = max(fan_max, fan_min)
    return fan_min, fan_max


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
    width_profile = body.get("width_profile") or [[0, 1.0], [1, 1.0]]

    # Asymmetric control cage: N rows (height) x M cols (azimuth) of radius scale
    # factors. Validate shape, clamp values to [0.5, 1.5], coerce non-numeric to
    # 1.0. An all-1.0 (or missing/malformed) cage resolves to None so default
    # output stays byte-identical.
    cage = _parse_cage(body.get("cage"))

    # Server-side clamps (never trust the client). width_fn's band (0.6-1.8) is
    # tighter than the writer's hard [0.5, 2.0] line_width_override clamp -- the
    # writer's clamp is the real safety net, this just keeps a malicious/buggy
    # client from ever getting close to it.
    amp_fn = _make_interp(amp_profile, AMP_MIN, AMP_MAX)
    radius_interp = _make_smooth_interp if radius_profile_smooth else _make_interp
    radius_fn = radius_interp(radius_profile, RADIUS_SCALE_MIN, RADIUS_SCALE_MAX)
    width_fn = _make_interp(width_profile, 0.6, 1.8)

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
    fan_overhang_min, fan_overhang_max = _parse_overhang_fan(body)
    fan_off_layers = max(0, min(int(body.get("fan_off_layers", 0) or 0), 50))

    point_mask, point_protection, point_ffd, point_smooth, point_radial_push = (
        _parse_point_edit_specs(body))

    point_edit_issue = None
    fan_overhang_issue = None
    if loop_spec is not None:
        # Loop fabric replaces the wall entirely (knitted rows of vertical
        # loop stitches) — z-waves/patterns don't apply; the silhouette does.
        if _point_edit_active(body):
            point_edit_issue = (
                "point edit modifiers only apply to the parametric wall "
                "(not loop fabric) - ignored for this design.")
        if fan_overhang_min != fan_overhang_max:
            fan_overhang_issue = (
                "fan min/max only ramp on the parametric wall (loop fabric has "
                "no wall lean to select a speed from) - a flat "
                f"{round(fan_overhang_min * 100)}% fan (your Fan min) was used.")
        report = build_loop_fabric(
            writer, shape=shape, height=height, spec=loop_spec,
            radius_envelope=radius_fn,
            cage=cage,
            first_layer_squish=squish,
            cuff_lh=layer_height,
            fan_speed=fan_overhang_min,
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
            fan_overhang_min=fan_overhang_min,
            fan_overhang_max=fan_overhang_max,
            fan_off_layers=fan_off_layers,
            width_callback=width_fn,
            point_mask=point_mask,
            point_protection=point_protection,
            point_ffd=point_ffd,
            point_smooth=point_smooth,
            point_radial_push=point_radial_push,
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
    if point_edit_issue:
        issues_extra.append(point_edit_issue)
    if fan_overhang_issue:
        issues_extra.append(fan_overhang_issue)
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
        "fan_min_pct": (round(analysis.min_fan_speed * 100) if analysis.min_fan_speed is not None else None),
        "fan_max_pct": (round(analysis.max_fan_speed * 100) if analysis.max_fan_speed is not None else None),
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
    # Server-side clamp tighter than the writer's hard [0.5, 2.0] line_width
    # override band -- see generate_design()'s width_fn for the rationale.
    width_profile = body.get("width_profile") or [[0, 1.0], [1, 1.0]]
    width_fn = _make_interp(width_profile, 0.6, 1.8)

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
    fan_overhang_min, fan_overhang_max = _parse_overhang_fan(body)
    fan_off_layers = max(0, min(int(body.get("fan_off_layers", 0) or 0), 50))

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
        fan_overhang_min=fan_overhang_min,
        fan_overhang_max=fan_overhang_max,
        fan_off_layers=fan_off_layers,
        width_callback=width_fn,
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
    if _point_edit_active(body):
        issues_extra.append(
            "point edit modifiers only apply to parametric wall designs "
            "(not STL mode) - the STL texture wall was generated without them.")

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
        "fan_min_pct": (round(analysis.min_fan_speed * 100) if analysis.min_fan_speed is not None else None),
        "fan_max_pct": (round(analysis.max_fan_speed * 100) if analysis.max_fan_speed is not None else None),
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
# Core: surface-only export -- same design, no toolpath (no Z-waves, no loop
# fabric), turned into a watertight STL so Orca can slice it as a SOLID part.
# Mirrors generate_design()/generate_mesh_texture_design()'s parsing so the
# exported shape matches what the viewer shows, but stops at the contour
# stack instead of feeding it to a G-code generator.
# --------------------------------------------------------------------------
def _export_contours_mesh_texture(body):
    """Contour stack for mode=mesh_texture: sliced STL + radial texture only.

    Cage deformation does not apply here (mirrors generate_mesh_texture_design,
    which only ever built the wall from stack_from_mesh + radial texture --
    the cage grid is a parametric-only feature).
    """
    mesh_id = body.get("mesh_id")
    if not mesh_id:
        raise ValueError("mesh_id is required for mode=mesh_texture")
    entry = _mesh_cache.get(mesh_id)
    if entry is None:
        raise KeyError("mesh_id not found (upload may have expired) - re-upload the STL")

    scale = float(body.get("scale", 1.0))
    layer_height = float(body.get("layer_height", 0.30))
    points_per_turn = int(body.get("points_per_turn", 240))

    pattern = body.get("pattern") or None
    pattern_amp = max(0.0, min(float(body.get("pattern_amp", 1.0)), 4.0))
    pattern_waves = int(body.get("pattern_waves", 12))
    pattern_bands = float(body.get("pattern_bands", 6.0))
    pattern_twist = float(body.get("pattern_twist", 0.0))
    pattern_phase = float(body.get("pattern_phase", 0.0))
    pattern_fade_in = max(0.0, min(float(body.get("pattern_fade_in", 0.10)), 0.5))
    pattern_fade_out = max(0.0, min(float(body.get("pattern_fade_out", 0.0)), 0.5))
    pattern_alternate = bool(body.get("pattern_alternate", False))
    if pattern in _SITE_TEXTURE_PATTERNS:
        pattern = None
    elif pattern is not None and pattern not in _R_PATTERNS:
        raise ValueError(f"unknown pattern '{pattern}', expected one of {R_PATTERN_NAMES}")

    tris = entry["tris"]
    if scale != 1.0:
        tris = [tuple((vx * scale, vy * scale, vz * scale) for (vx, vy, vz) in t)
                for t in tris]

    # Same ceiling as the parametric path, but the height comes from the mesh's
    # own bounds -- a large `scale` on a tall STL is how this one runs away.
    if layer_height <= 0:
        raise ValueError("layer_height must be positive")
    (_mlo, _mhi) = mesh_bounds(tris)
    _check_export_budget(max(1, int(round((_mhi[2] - _mlo[2]) / layer_height))),
                         points_per_turn)

    contours = stack_from_mesh(tris, layer_height, points_per_turn)
    heights = [layer_height * (i + 0.5) for i in range(len(contours))]

    if pattern is not None:
        n_layers = len(contours)
        n = points_per_turn
        fn = _R_PATTERNS[pattern]
        textured = []
        for i, c in enumerate(contours):
            t = i / max(n_layers - 1, 1)
            normals = contour_normals(c)
            env = _fade_envelope(t, pattern_fade_in, pattern_fade_out)
            b = 2.0 * math.pi * pattern_bands * t
            new_c = []
            for j, (x, y) in enumerate(c):
                theta = 2.0 * math.pi * j / n
                a = pattern_waves * theta + 2.0 * math.pi * (pattern_twist * t + pattern_phase)
                if pattern_alternate and i % 2 == 1:
                    a += math.pi
                disp = pattern_amp * env * fn(a, b)
                nx, ny = normals[j]
                new_c.append((x + disp * nx, y + disp * ny))
            textured.append(new_c)
        contours = textured

    filename = "design_mesh_%s_%dmm.stl" % (_clean_shape(mesh_id), int(round(max(heights) if heights else 0)))
    return contours, heights, filename


def _export_contours_parametric(body):
    """Contour stack for the parametric path: every input that defines the WALL
    SURFACE -- shape, xy_twist, silhouette envelope, cage, radial texture,
    ovality and the leaning spine.

    xy_twist / ovality / spine are sculpting controls (the UI's "Asymmetry"
    group), not toolpath effects, so an export that dropped them would hand back
    a straight, round vase for a design the user had deliberately leaned and
    squashed. Only z_waves and loop-fabric stitching are excluded, because those
    modulate the bead path itself and have no surface to hand a solid slicer.
    """
    shape_name = str(body.get("shape", "circle"))
    radius = float(body.get("radius", 32.0))
    height = float(body.get("height", 60.0))
    layer_height = float(body.get("layer_height", 0.30))
    points_per_turn = int(body.get("points_per_turn", 240))
    star_points = int(body.get("star_points", 5))
    star_depth = float(body.get("star_depth", 0.35))

    radius_profile = body.get("radius_profile") or [[0, 1.0], [1, 1.0]]
    radius_profile_smooth = bool(body.get("radius_profile_smooth", False))
    radius_interp = _make_smooth_interp if radius_profile_smooth else _make_interp
    radius_fn = radius_interp(radius_profile, RADIUS_SCALE_MIN, RADIUS_SCALE_MAX)

    cage = _parse_cage(body.get("cage"))

    pattern = body.get("pattern") or None
    pattern_amp = max(0.0, min(float(body.get("pattern_amp", 1.0)), 4.0))
    pattern_waves = int(body.get("pattern_waves", 12))
    pattern_bands = float(body.get("pattern_bands", 6.0))
    pattern_twist = float(body.get("pattern_twist", 0.0))
    pattern_phase = float(body.get("pattern_phase", 0.0))
    pattern_fade_in = max(0.0, min(float(body.get("pattern_fade_in", 0.10)), 0.5))
    pattern_fade_out = max(0.0, min(float(body.get("pattern_fade_out", 0.0)), 0.5))
    pattern_alternate = bool(body.get("pattern_alternate", False))
    if pattern in _SITE_TEXTURE_PATTERNS:
        pattern = None
    elif pattern is not None and pattern not in _R_PATTERNS:
        raise ValueError(f"unknown pattern '{pattern}', expected one of {R_PATTERN_NAMES}")

    xy_twist = float(body.get("xy_twist", 0.0))
    # Same clamps generate_design() applies, so the exported solid can never
    # differ from the printed wall just because one path validated harder.
    spine_mm = max(0.0, min(float(body.get("spine_mm", 0.0)), 20.0))
    spine_deg = float(body.get("spine_deg", 0.0))
    ovality = max(-0.4, min(float(body.get("ovality", 0.0)), 0.4))
    spine_cos = math.cos(math.radians(spine_deg))
    spine_sin = math.sin(math.radians(spine_deg))

    shape = _make_shape(shape_name, radius, star_points, star_depth)

    # Budget-check BEFORE building anything -- stack_from_shape would otherwise
    # allocate the whole oversized stack before we could reject it.
    if layer_height <= 0:
        raise ValueError("layer_height must be positive")
    _check_export_budget(max(1, int(round(height / layer_height))), points_per_turn)

    # Layer count comes from the shared stack builder so an export always has the
    # same number of rings as the print; the outlines themselves are rebuilt
    # below rather than reused, because a twisted cross-section cannot be
    # recovered from baked XY (hypot loses the shape angle).
    n_layers = len(stack_from_shape(shape, radius, height, layer_height,
                                    points_per_turn, radius_envelope=radius_fn))
    n = points_per_turn
    heights = [height * (i / max(n_layers - 1, 1)) for i in range(n_layers)]

    # Mirrors spiral_path()'s per-point order exactly: twist the sampled shape
    # angle, scale by the silhouette envelope, scale by the cage, add the radial
    # texture, then squash to an ellipse and finally translate the whole
    # cross-section for the leaning spine. Any other order changes the surface.
    fn = _R_PATTERNS[pattern] if pattern is not None else None
    contours = []
    for i in range(n_layers):
        t = i / max(n_layers - 1, 1)
        r_env = radius_fn(t)
        env = _fade_envelope(t, pattern_fade_in, pattern_fade_out) if fn is not None else 0.0
        b = 2.0 * math.pi * pattern_bands * t
        dx = t * spine_mm * spine_cos
        dy = t * spine_mm * spine_sin
        ring = []
        for j in range(n):
            theta = 2.0 * math.pi * j / n
            r = shape(theta - xy_twist * 2.0 * math.pi * t) * r_env
            if cage is not None:
                r *= cage_scale(cage, theta, t)
            if fn is not None:
                a = pattern_waves * theta + 2.0 * math.pi * (pattern_twist * t + pattern_phase)
                if pattern_alternate and i % 2 == 1:
                    a += math.pi
                r += pattern_amp * env * fn(a, b)
            x = r * math.cos(theta)
            y = r * math.sin(theta)
            if ovality != 0.0:
                x *= (1.0 + ovality)
                y *= (1.0 - ovality)
            ring.append((x + dx, y + dy))
        contours.append(ring)

    filename = "design_%s_%dmm.stl" % (_clean_shape(shape_name), int(round(height)))
    return contours, heights, filename


def _point_edits_on_contours(body, contours, heights, points_per_turn):
    """Run the point-edit stack (FFD / radial push / smooth) over a contour stack.

    The modifiers are defined against the printed point stream, but that stream
    is just this contour stack walked in layer-major order, and point_edit
    recovers (theta, t) from the point INDEX via _theta_t_at() -- theta from
    i % points_per_turn, t from i / (total - 1). Flattening the rings in the
    same order therefore reproduces the exact deformation the toolpath gets,
    rather than re-deriving it and risking drift.

    Returns 3-tuple rings when anything deformed, because FFD displaces Z per
    point and a single height per ring can no longer describe the result.
    """
    mask, protection, ffd, smooth, radial_push = _parse_point_edit_specs(body)
    if ffd is None and smooth is None and radial_push is None:
        return contours                     # nothing deforming; leave (x, y) rings
    pts = [PathPoint(x=x, y=y, z=heights[i])
           for i, ring in enumerate(contours) for (x, y) in ring]
    edited = apply_point_edits(
        pts, points_per_turn, mask=mask, protection=protection,
        ffd=ffd, smooth=smooth, radial_push=radial_push)
    n = points_per_turn
    return [[(p.x, p.y, p.z) for p in edited[i * n:(i + 1) * n]]
            for i in range(len(contours))]


def generate_export_stl(body):
    """Turn a design request body into watertight STL bytes.

    Everything that shapes the WALL SURFACE: shape, xy_twist, silhouette
    envelope, cage, radial texture pattern, ovality, leaning spine, and the
    point-edit modifier stack (FFD, radial push, smooth). Deliberately excludes
    non-planar Z-waves and loop-fabric stitching -- both are toolpath artifacts
    of the single-wall vase print with no meaningful surface to hand a
    solid-part slicer. When the request asks for pattern="loops" the underlying
    smooth wall surface is exported (loop fabric replaces the WALL EMISSION,
    not the wall's own outline).
    """
    mode = str(body.get("mode", "parametric"))
    if mode == "mesh_texture":
        contours, heights, filename = _export_contours_mesh_texture(body)
    else:
        contours, heights, filename = _export_contours_parametric(body)

    if len(contours) < 2:
        raise ValueError("design is too short (need at least 2 layers) to export a solid mesh")

    contours = _point_edits_on_contours(
        body, contours, heights, int(body.get("points_per_turn", 240)))

    tris = contours_to_mesh(contours, heights, cap_bottom=True, cap_top=True)
    data = write_binary_stl(tris, name=b"trident_export")
    return data, filename


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

        # Single-outer-contour slicing silently discards holes and islands, so
        # check up front and hand the verdict to the UI rather than letting the
        # user discover it in a failed print. Never fatal: a mesh that fails the
        # check still slices, it just won't reproduce the uploaded shape.
        try:
            vase_check = analyze_vase_compatibility(tris)
        except Exception as e:
            vase_check = {"error": "Could not analyse mesh: %s" % e}

        self._send_json({
            "mesh_id": mesh_id,
            "bounds": {
                "min": [round(minx, 3), round(miny, 3), round(minz, 3)],
                "max": [round(maxx, 3), round(maxy, 3), round(maxz, 3)],
            },
            "triangles": len(tris),
            "height": round(maxz - minz, 3),
            "vase_check": vase_check,
        })
        # filename is accepted for client bookkeeping; not otherwise used server-side.
        _ = filename

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/upload_mesh":
            self._handle_upload_mesh()
            return
        if path == "/api/export_stl":
            self._handle_export_stl()
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

    def _handle_export_stl(self):
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

        try:
            data, filename = generate_export_stl(body)
        except KeyError as e:
            msg = str(e).strip('"').strip("'")
            self._send_json({"error": msg}, status=400)
            return
        except (ValueError, TypeError) as e:
            # TypeError covers a malformed body (e.g. "radius": null reaching
            # float()) -- that is a bad request, not a server fault.
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:
            self._send_json({"error": "STL export failed: %s" % e}, status=500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(ThreadingHTTPServer):
    # SO_REUSEADDR lets a second process silently bind a port that is still
    # LISTENing on Windows (unlike POSIX, where it only helps rebind a
    # socket stuck in TIME_WAIT) -- so a stale `python serve.py` left running
    # from an earlier edit session keeps answering requests with old code
    # while a newer instance starts up, prints the banner, and looks fine.
    # Disabling reuse on Windows makes that collision hit the OSError guard
    # in main() instead of silently double-binding. POSIX keeps reuse (quick
    # restarts there rebind a TIME_WAIT socket, which is the safe case).
    allow_reuse_address = (os.name != "nt")


def main():
    try:
        httpd = _Server(("", PORT), Handler)
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
