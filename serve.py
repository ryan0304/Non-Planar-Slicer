#!/usr/bin/env python3
"""Local design server for the Trident non-planar G-code generator.

Serves the project directory statically AND exposes a tiny JSON API so the
in-browser viewer can DESIGN a vase (shape/size + two editable curves over
height) and get back machine-safe G-code rendered immediately.

Pure standard library only. ASCII-only console/server strings.

Safety is not optional: every design is generated through GcodeWriter (which
clamps Z feedrate, volumetric flow, footprint, top Z) and then re-checked with
analyze_gcode before it is returned. Every Z-excursion ceiling -- wave
amplitude, loop-fabric stitch dip, loop row height -- is additionally clamped
server-side to the selected PrinterProfile's own z_amp_max (see amp_ceiling()
below), so a malicious or buggy client can never ask for more Z dip than the
chosen machine itself declares safe.

Run:

    python serve.py

then open http://localhost:8777/viewer/index.html

The server binds loopback ONLY by default. Set TRIDENT_BIND=0.0.0.0 to expose
it (a hosted deployment must); see _bind_config() for what that costs you.
PORT overrides the listen port, which is what Render and similar platforms
supply.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler

from trident_gcode import PRINTER_PROFILES, TRIDENT, GcodeWriter, PrinterProfile
from trident_gcode import printer_store
from trident_gcode.printer_import import parse as parse_printer_config, PrinterImportError
from trident_gcode.printer_validate import validate_raw, validate_profile_dict
from trident_gcode.blobs import BlobSpec, LoopSpec
from trident_gcode.paths import SpiralSpec, circle, star, superellipse
from trident_gcode.generators import build_continuous_spiral, build_profile_spiral, build_surface_spiral
from trident_gcode.generators.surface_spiral import _archimedean
from trident_gcode.generators.loop_fabric import build_loop_fabric
from trident_gcode import surface as surf
from trident_gcode.analyze import SUPPORT_Z_HI_MM, analyze_gcode, format_report
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
    """Resolve the request's printer KEY against this session's profiles.

    ``body["_session"]`` is injected by the request handler and overwritten
    unconditionally, so a client cannot pick its own session by putting the
    field in the JSON it sends.

    An unknown key is a hard error, never a fallback to DEFAULT_PRINTER_KEY.
    Custom printers now live in a session store that empties when the server
    restarts, so "key not found" is an ordinary, expected condition rather
    than a rare one -- and falling back would mean generating with the
    Trident's limits for someone who selected a 180x180 machine. That is
    exactly the "never inherit another machine's limits" rule: the
    conservative answer to an unresolvable printer is to refuse, because
    there is no such thing as a conservative default bed size.
    """
    from dataclasses import replace
    sid = body.get("_session")
    requested = body.get("printer")
    key = str(requested) if requested else DEFAULT_PRINTER_KEY
    profiles = printer_store.session_all_profiles(sid)
    profile = profiles.get(key)
    if profile is None:
        raise ValueError(
            "unknown printer '%s'. Custom printers are held for the browser "
            "session and are cleared when the server restarts; reload the "
            "page to restore them, or re-import the printer config."
            % key)
    nozzle = body.get("nozzle")
    if nozzle:
        nd = float(nozzle)
        if 0.1 <= nd <= 1.2:
            profile = replace(profile, nozzle_diameter=nd)
    return profile


# The session id is opaque and carries no authority: it only selects which
# bucket of replayed custom printers this request sees. It is read from a
# header so the same helper serves GET and POST alike.
_SESSION_HEADER = "X-Trident-Session"


def _session_id(handler):
    sid = handler.headers.get(_SESSION_HEADER) or ""
    return sid if printer_store.is_valid_session(sid) else None

PORT = 8777
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def amp_ceiling(profile) -> float:
    """Max wave amplitude (mm) for this machine.

    This is the profile's own z_amp_max -- the max Z excursion below
    already-printed material before the toolhead fouls something. It was
    previously a module constant fixed at the Trident's 0.95, which silently
    capped high-clearance machines and, worse, allowed nearly 2.5x over the
    limit on a custom printer that declared a tighter one.
    """
    return max(0.0, float(profile.z_amp_max))


def slope_ceiling(profile) -> float:
    """Max printable wave-wall slope (amp*waves/(radius*silhouette)) for this
    machine.

    This used to be QUALITY_SLOPE_LIMIT, a module constant fixed at the
    Trident's empirically measured 0.25 and applied to every printer -- the
    exact shape of the already-fixed amp_ceiling defect (a hardcoded
    AMP_MAX = 0.95 once capped a Bambu at 0.95 against its own 4.0 and
    allowed 0.95 on a printer declaring 0.40). CLAUDE.md: "No machine limit
    may be a module constant. Ceilings come from the selected
    PrinterProfile." Reads profile.quality_slope_max instead.

    Non-finite (NaN/inf) survives min()/max() clamping silently (every
    comparison against NaN is False), so it is refused outright and mapped
    to the conservative QUALITY_SLOPE_DEFAULT rather than being clamped.
    A finite value is clamped into [0.0, 10.0]; a declared 0.0 stays 0.0,
    mirroring the amp_ceiling/z_amp_max precedent where a printer declaring
    zero tolerance keeps zero -- over-warning is the safe direction here.
    """
    value = getattr(profile, "quality_slope_max", None)
    if value is None:
        return QUALITY_SLOPE_DEFAULT
    try:
        value = float(value)
    except (TypeError, ValueError):
        return QUALITY_SLOPE_DEFAULT
    if not math.isfinite(value):
        return QUALITY_SLOPE_DEFAULT
    return min(max(value, 0.0), 10.0)


def _peak_wave_slope(amp_fn, z_waves, radius_at) -> float:
    """Sample amp_fn(t) * z_waves / radius_at(t) across the design height and
    return the peak -- the empirical print-quality metric slope_ceiling()
    gates. ``radius_at(t)`` is the effective wall radius at height-fraction
    t (radius * silhouette(t) for the parametric wall; a fixed bottom_radius
    for the mesh-texture path, which has no separate silhouette function).

    Factored out of generate_design/generate_mesh_texture_design so both
    call sites (and the regression test that guards against slope_ceiling
    ever being replaced by a constant again) share one sampling loop rather
    than three copies that could quietly drift apart.
    """
    peak = 0.0
    for k in range(51):
        t = k / 50.0
        peak = max(peak, amp_fn(t) * z_waves / max(radius_at(t), 1e-6))
    return peak


def _min_wall_radius(radius_at) -> float:
    """Narrowest effective wall radius across the design height.

    The slope-implied amplitude cap must be derived from THIS, not from the
    design's nominal radius: slope is amp*waves/(radius*silhouette), so a
    waisted silhouette reads the same amplitude as a steeper wall and trips
    the check sooner. Quoting the nominal radius instead overstates the cap
    by 1/min(silhouette) -- on a design waisted to 0.5 it would tell the user
    they have twice the amplitude headroom they actually have.

    Samples the same 51 points as _peak_wave_slope so the number in the
    message is the one the check actually used.
    """
    return min(max(radius_at(k / 50.0), 1e-6) for k in range(51))


def _slope_exceeded_message(peak_slope, limit, min_radius, z_waves, amp_cap_limit) -> str:
    """Explain WHY the quality-slope check fired below the amplitude (z-amp)
    ceiling: the two gate independently (see
    PrinterProfile.quality_slope_max), and a design can be miles under
    z_amp_max while still exceeding the slope this printer can hold.

    Derives the amplitude this slope limit implies at the design's own
    narrowest wall radius / wave count (limit * min_radius / waves) so the
    number is actionable, not just descriptive. Guarded against z_waves == 0
    -- zero waves has no meaningful slope-implied amplitude cap.

    The "under the z-amp ceiling" clause is stated only when it is TRUE. The
    slope cap is not always the binding one: on a waisted silhouette the
    check can fire with a cap that sits ABOVE z_amp_max (the waist, not the
    amplitude, is what got there first), and a message asserting otherwise
    would be a false claim about which limit the user has to move.
    """
    msg = ("Peak wave slope %.2f exceeds this printer's printable ~%.2f "
           "(amp*waves/radius) - upper waves may collapse" % (peak_slope, limit))
    if z_waves > 0:
        amp_cap = limit * min_radius / z_waves
        msg += (". At its narrowest wall radius %.1f mm with %d waves that caps "
                "amplitude at %.2f mm" % (min_radius, z_waves, amp_cap))
        if amp_cap < amp_cap_limit:
            msg += ", under this printer's %.2f mm z-amp ceiling" % amp_cap_limit
    return msg + "; reduce amplitude or wave count."


# Smallest loop-fabric row / stitch height the server will accept, in mm.
# NOT a machine limit -- machine limits come from the PrinterProfile and never
# live here (see CLAUDE.md). This is the GENERATOR's own lower bound:
# build_loop_fabric() starts with ``row_mm = max(0.5, spec.row_mm)``, so
# accepting anything smaller would just be silently raised there, and the panel
# would show a row height the print does not use. Every ceiling that matters is
# still the profile's.
LOOP_MIN_MM = 0.5

# How far apart loop-fabric rows may sit, in mm, REGARDLESS of how much Z
# clearance the machine has. Not a machine limit -- machine limits come from
# the PrinterProfile and never live here -- but a process one, and it binds
# separately from z_amp_max via the min() in loop_up_ceiling().
#
# A fabric row has to land on the row beneath it. SUPPORT_Z_HI_MM is the
# analyzer's statement of how far below already-printed material may sit and
# still be something you can deposit onto; a row pitch wider than that leaves
# each stitch hanging with nothing in reach. On machines with clearance to
# spare that is exactly what happened: the shipped styles ask for 2.5-4.0mm
# rows, and on a 4.0mm-clearance profile they were used as authored, so 5-28%
# of every loop-fabric print was extrusion into air the analyzer could not
# find support for -- against 0.1% on the Trident, whose 0.95mm ceiling had
# been holding the pitch down by accident rather than by intent.
#
# The 0.8 factor is margin, and it IS a guess: the support test measures the
# MIDPOINT of each move, while the pitch is measured line-to-line, so a stitch
# sample sits partway down its own dip and needs the row below closer than the
# raw ceiling. 0.8 * 2.0 = 1.6mm puts all seven shipped styles under 1%
# unsupported where 1.8mm leaves the widest at 1.98% -- right on the 2%
# threshold. Conservative default, NOT print-tested: bridging really does
# depend on part cooling, so a machine with strong cooling could likely carry
# a wider pitch. If that is ever measured, it belongs on the PrinterProfile as
# its own field, not as a bigger number here.
LOOP_SUPPORT_REACH_MM = round(0.8 * SUPPORT_Z_HI_MM, 3)


def loop_up_ceiling(profile) -> float:
    """Max loop-fabric stitch height (mm) for this machine.

    The machine's own Z-excursion ceiling AND the process support reach,
    whichever is tighter. Both bind: z_amp_max is what the toolhead can
    physically clear (see amp_ceiling), LOOP_SUPPORT_REACH_MM is how far a
    stitch can get from the material it has to attach to.

    Deliberately NOT amp_ceiling() itself -- the wave amplitude is a different
    consumer of z_amp_max and must keep the machine's full range; only the
    fabric is bounded by reach. Leaves the Trident (0.95) and every Creality
    (1.0) exactly where they were, and brings the 4.0mm profiles down to 1.6.
    """
    return min(amp_ceiling(profile), LOOP_SUPPORT_REACH_MM)


def loop_row_ceiling(profile) -> float:
    """Max loop-fabric row height (mm) for this machine.

    Derived from loop_up_ceiling the same way the client used to derive it, so
    the two can never drift. The 0.3 subtraction is build_loop_fabric's own
    interlock margin (``loop_h = max(row_mm + 0.3, up_mm)``): keeping the row
    that far under the stitch ceiling is what stops the derived loop height
    from exceeding it and quietly undoing the cap.

    Rounded because the naive subtraction is not exact in binary floating
    point -- 0.95 - 0.3 is 0.6499999999999999, which reached the browser and
    ended up as a number input's literal `min` attribute. A clearance is a
    physical measurement; three decimals is far finer than anyone can set a
    nozzle.
    """
    return round(max(0.4, loop_up_ceiling(profile) - 0.3), 3)


# Server-side safety ceilings (mirror the UI clamps).
RADIUS_SCALE_MIN, RADIUS_SCALE_MAX = 0.2, 1.5
# Mirrors viewer/index.html's #d-starpoints / #d-stardepth min/max attributes.
# The floor matters because cos() is even, so points < 3 doesn't just look
# odd -- it's indistinguishable from a plain circle. The ceiling matters
# because the spiral is sampled at a fixed points_per_turn (240, see
# generate_design() below): above ~120 lobes the cross-section aliases into
# XY jumps of a large fraction of depth*radius at full print speed.
STAR_POINTS_MIN, STAR_POINTS_MAX = 3, 12
STAR_DEPTH_MIN, STAR_DEPTH_MAX = 0.0, 1.0
# Fallback ONLY for a profile object that lacks quality_slope_max entirely
# (e.g. a bare object built outside PrinterProfile's dataclass default) --
# see slope_ceiling() above. Every real PrinterProfile carries its own
# quality_slope_max (0.25 for the Trident's 2026-07-05 measurement, 0.25 as
# an unmeasured conservative placeholder for everything else); this constant
# is deliberately NOT read directly by either quality check below, so it can
# never regress into the module-constant defect slope_ceiling() replaced.
QUALITY_SLOPE_DEFAULT = 0.25
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


def _parse_star(body):
    """Read + clamp star_points/star_depth from a request body.

    Mirrors viewer/index.html's #d-starpoints / #d-stardepth min/max so a
    client that skips the browser (or a loaded design JSON that skips
    bindNumber's clamp) can never hand the generator an out-of-range lobe
    count or depth. Non-finite values (NaN/Infinity) never reach here --
    _read_json_body rejects those for the whole request body before any
    field-level parsing runs, so this only needs to handle range and type.

    One helper shared by both call sites (generate_design and
    _export_contours_parametric) rather than duplicating the clamp inline:
    the two lines were already duplicated verbatim and still drifted into
    being the only unclamped pair in either function.
    """
    try:
        points = int(body.get("star_points", 5))
        depth = float(body.get("star_depth", 0.35))
    except (TypeError, ValueError):
        raise ValueError("star_points/star_depth must be numbers")
    points = max(STAR_POINTS_MIN, min(points, STAR_POINTS_MAX))
    depth = max(STAR_DEPTH_MIN, min(depth, STAR_DEPTH_MAX))
    return points, depth


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


def _parse_nozzle_temp(body, profile):
    """User-requested nozzle temp override, bounded by ``profile``.

    Returns None when absent/null/empty/0 -- meaning "don't touch it", so the
    profile's (and any selected filament's) own default keeps flowing through
    unchanged. Only a genuine, non-zero value becomes an override.

    The ceiling comes from the SELECTED profile, never from a constant here.
    A hardcoded 320 let a request for 320 C through on every built-in profile
    in the app -- they declare 260 or 300, so the module constant was looser
    than every real machine it was supposed to be protecting. That is the same
    shape as the AMP_MAX bug in CLAUDE.md: a limit typed into serve.py belongs
    on the profile. 320 is kept only as an absolute backstop for a profile
    that somehow declares something wilder.

    The 150 floor stays absolute: there is no min_nozzle_temp on the profile,
    and a cold-extrude floor is not something a config should be able to relax.
    """
    raw = body.get("nozzle_temp")
    if raw is None or raw == "":
        return None
    try:
        temp = float(raw)
    except (TypeError, ValueError):
        return None
    # float() accepts the strings "NaN"/"Infinity", and every comparison
    # against NaN is False -- it would survive the min()/max() below rather
    # than being clamped by it (it currently lands on the floor only by luck
    # of Python's argument order). Reject non-finite at the boundary.
    if not math.isfinite(temp):
        return None
    if temp == 0:
        return None
    ceiling = min(320.0, float(profile.max_nozzle_temp))
    return max(150.0, min(temp, ceiling))


def _parse_bed_temp(body, profile):
    """User-requested bed temp override, bounded by ``profile``.

    Mirrors _parse_nozzle_temp with ONE deliberate difference: a nozzle at
    0 C is meaningless (there's no such thing as "no nozzle temp"), so that
    path treats 0 as "no override". A bed is different -- 0 C means "bed off",
    which is a real, requestable setting (see the built-in end_gcode templates,
    which all send "M140 S0" on purpose). So here, 0 is preserved as a genuine
    override rather than being folded into "absent". This is not a new output
    class: trident_gcode/orca.py's FilamentSettings.writer_kwargs() already
    passes a filament's bed_temp straight through, so bed_temp=0 is reachable
    today; this just lets a user ask for it explicitly too.

    Returns None only when the request is absent/null/empty/unparseable/non-
    finite -- meaning "don't touch it", so the profile's (and any selected
    filament's) own default keeps flowing through unchanged.

    The ceiling comes from the SELECTED profile's max_bed_temp, never from a
    constant here -- same reasoning as the nozzle version and the same shape
    CLAUDE.md warns about (a limit typed into serve.py belongs on the profile).
    Unlike the nozzle temp there is no absolute floor other than 0: a bed
    genuinely can be run at 0.
    """
    raw = body.get("bed_temp")
    if raw is None or raw == "":
        return None
    try:
        temp = float(raw)
    except (TypeError, ValueError):
        return None
    # float() accepts the strings "NaN"/"Infinity", and every comparison
    # against NaN is False -- it would survive the min()/max() below rather
    # than being clamped by it, and could reach the writer's own min() at
    # gcode.py:91 to be emitted as "M140 Snan". Reject non-finite at the
    # boundary; never clamp it.
    if not math.isfinite(temp):
        return None
    ceiling = float(profile.max_bed_temp)
    return max(0.0, min(temp, ceiling))


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


def _parse_loop_spec(body, profile, radius: float | None = None) -> LoopSpec | None:
    """Extract hanging-loop parameters from the request body.

    Returns None when loops are disabled.  ``loop_spacing_mm`` (with a known
    radius) overrides ``loop_per_turn`` via the circumference, like blobs.

    row_mm and up_mm are Z excursions (the stitch dip / row height), so their
    ceilings are derived from the selected machine's amp_ceiling() rather than
    a fixed constant -- see amp_ceiling() for why. row_mm's cap mirrors the
    formula the client already uses in applyPrinterCaps() (cap - 0.3, floored
    at 0.4) so both paths agree.

    The FLOOR is LOOP_MIN_MM, not the old min(1.0, cap). That expression was a
    patch for a genuinely empty range (a hardcoded 1.0 floor sat above the
    Trident's 0.95 ceiling) but it overshot: whenever the ceiling is at or
    below 1.0 it makes floor == ceiling, and the whole control collapses to a
    single value. Ten of the fifteen shipped profiles are in that band -- the
    Trident (0.65 row / 0.95 loop) and every Creality (0.7 / 1.0) -- so on all
    of them EVERY loop style resolved to identical row and loop heights, and
    picking a different style changed nothing about the fabric's proportions.
    Only the three Bambus (4.0) had a usable range.

    A floor is not a safety bound in this direction: smaller row/loop values
    are strictly smaller Z excursions, i.e. strictly safer. The safety-relevant
    bound is the CEILING, and it is untouched -- z_amp_max still caps both, as
    it must. LOOP_MIN_MM matches build_loop_fabric's own row floor
    (``row_mm = max(0.5, spec.row_mm)``) so the server never accepts a row the
    generator would silently raise, and is itself capped by the machine so a
    profile declaring less than LOOP_MIN_MM of clearance still gets a valid
    (if degenerate) range rather than an inverted one.
    """
    cap = loop_up_ceiling(profile)
    cap_row = loop_row_ceiling(profile)
    floor_row = min(LOOP_MIN_MM, cap_row)
    floor_up = min(LOOP_MIN_MM, cap)
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
        row_mm=max(floor_row, min(float(body.get("loop_row", 2.5)), cap_row)),
        up_mm=max(floor_up, min(float(body.get("loop_up", 3.5)), cap)),
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
    star_points, star_depth = _parse_star(body)

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

    # Resolved before amp_fn below: the amplitude ceiling is derived from the
    # selected machine's own z_amp_max (amp_ceiling()), so the profile must be
    # in scope first.
    profile = _get_profile(body)

    # Server-side clamps (never trust the client). width_fn's band (0.6-1.8) is
    # tighter than the writer's hard [0.5, 2.0] line_width_override clamp -- the
    # writer's clamp is the real safety net, this just keeps a malicious/buggy
    # client from ever getting close to it.
    amp_fn = _make_interp(amp_profile, 0.0, amp_ceiling(profile))
    radius_interp = _make_smooth_interp if radius_profile_smooth else _make_interp
    radius_fn = radius_interp(radius_profile, RADIUS_SCALE_MIN, RADIUS_SCALE_MAX)
    width_fn = _make_interp(width_profile, 0.6, 1.8)

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

    # Applied AFTER the filament merge: fs.writer_kwargs() carries its own
    # nozzle_temp/bed_temp, which would otherwise silently clobber an explicit
    # user override. The user's choice must win over the filament's default.
    nozzle_temp = _parse_nozzle_temp(body, profile)
    if nozzle_temp is not None:
        writer_kwargs["nozzle_temp"] = nozzle_temp
    bed_temp = _parse_bed_temp(body, profile)
    if bed_temp is not None:
        writer_kwargs["bed_temp"] = bed_temp

    # Fail fast on a height that cannot fit whatever the waves do.
    #
    # The authoritative check is build_continuous_spiral's (continuous_spiral.py,
    # "Print top Z ... exceeds Z max"), and it stays exactly where it is -- but it
    # runs only after the whole spiral path has been generated. A height of 9000mm
    # at 0.3mm layers is 30,000 layers x 240 points, so the user waited 31 seconds
    # (measured) to be told a number that was impossible before any work started.
    #
    # This is deliberately a LOWER bound, not a second opinion. Every wave point
    # sits within amp_ceiling of the nominal height, so the path's top point is at
    # least (height - amp_cap); the base offset and any loop excursion only push it
    # higher. A design that fails this bound therefore cannot possibly pass the real
    # check, and one that passes here still faces the real check unchanged. It can
    # never reject something the generator would have accepted -- which is the only
    # way a fast pre-check is safe to add in front of a safety check.
    _amp_cap = amp_ceiling(profile)
    if height - _amp_cap > profile.z_max:
        raise ValueError(
            "Print height %.1f exceeds Z max %.1f. Reduce height or layer count."
            % (height, profile.z_max))

    writer = GcodeWriter(**writer_kwargs)

    # Progress side-channel (publish-only, for /api/generate_stream). Set only
    # when the streaming endpoint attached a dict via body["_progress"]; when
    # that key is absent (the plain /api/generate path, or any direct caller)
    # this block is a no-op and behaviour is identical to before it existed.
    # writer._lines grows monotonically as generation proceeds, so another
    # thread can read len(writer._lines) / total for a live fraction without
    # this function doing any extra work or changing anything it computes.
    _progress = body.get("_progress")
    if _progress is not None:
        _progress["writer"] = writer
        _progress["total"] = 240 * math.ceil(height / max(layer_height, 1e-6)) + 27623
        _progress["stage"] = "toolpath"

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
    loop_spec = _parse_loop_spec(body, profile, radius=radius)
    overhang_flow_k = max(0.0, min(float(body.get("overhang_flow_k", 0.0)), 1.0))
    fan_overhang_min, fan_overhang_max = _parse_overhang_fan(body)
    fan_off_layers = max(0, min(int(body.get("fan_off_layers", 0) or 0), 50))

    point_mask, point_protection, point_ffd, point_smooth, point_radial_push = (
        _parse_point_edit_specs(body))

    point_edit_issue = None
    fan_overhang_issue = None
    loop_base_issue = None
    # Point Mask and Point Protection are GATES, not deformations: they scale
    # how strongly FFD / Smooth / Radial Push act at each point (see
    # point_edit.py). On their own there is nothing to scale, so
    # apply_point_edits returns the path untouched -- byte-identical output for
    # a modifier the user switched on and configured. The panel lets them be
    # enabled independently, so say so rather than letting it look applied.
    point_gate_issue = None
    if ((point_mask is not None or point_protection is not None)
            and point_ffd is None and point_smooth is None
            and point_radial_push is None):
        _gates = []
        if point_mask is not None:
            _gates.append("Point Mask")
        if point_protection is not None:
            _gates.append("Point Protection")
        point_gate_issue = (
            " and ".join(_gates)
            + (" only scale" if len(_gates) > 1 else " only scales")
            + " how strongly a deforming modifier acts - with no FFD, Smooth "
            "or Radial Push enabled there is nothing to scale, so the wall was "
            "generated unchanged.")
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
        if base_layers > 0 or brim > 0 or skirt_loops > 0:
            requested = []
            if base_layers > 0:
                requested.append("base layers")
            if brim > 0:
                requested.append("brim")
            if skirt_loops > 0:
                requested.append("skirt")
            loop_base_issue = (
                "loop fabric anchors itself with its own solid cuff, so the "
                "requested " + ", ".join(requested) + " were not printed.")
        report = build_loop_fabric(
            writer, shape=shape, height=height, spec=loop_spec,
            radius_envelope=radius_fn,
            xy_twist_turns=xy_twist,
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
    if _progress is not None:
        _progress["stage"] = "verify"
        # gcode_text.count("\n") is exact and cheap relative to writing the
        # temp file and re-parsing it below -- gives analyze_gcode's progress
        # counter a denominator so the bar can advance during this stage too.
        _progress["verify_total"] = gcode_text.count("\n")
    fd, tmp_path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(gcode_text)
        analysis = analyze_gcode(tmp_path, profile, _progress=_progress)
        report_text = format_report(analysis, profile)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Empirical print-quality check: peak wall slope = amp*waves/(radius*sil).
    # The ceiling is this machine's own quality_slope_max (slope_ceiling()) --
    # 0.25 (about 14 deg) is the Trident's 2026-07-05 measurement, not a
    # constant applied to every printer. Distinct from the probe amp ceiling.
    slope_limit = slope_ceiling(profile)
    wall_radius_at = lambda t: radius * radius_fn(t)
    peak_slope = _peak_wave_slope(amp_fn, z_waves, wall_radius_at)
    issues_extra = []
    if peak_slope > slope_limit + 1e-9:
        issues_extra.append(_slope_exceeded_message(
            peak_slope, slope_limit, _min_wall_radius(wall_radius_at),
            z_waves, amp_ceiling(profile)))
    if point_edit_issue:
        issues_extra.append(point_edit_issue)
    if point_gate_issue:
        issues_extra.append(point_gate_issue)
    if fan_overhang_issue:
        issues_extra.append(fan_overhang_issue)
    if loop_base_issue:
        issues_extra.append(loop_base_issue)
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
# Core: mode=surface -- non-planar conformal shell over a height field
# (dome/ripple/saddle/waves, or draped over an uploaded STL).
# --------------------------------------------------------------------------

# A surface spiral's point count scales with (footprint area) / (line_width *
# resolution) -- the Archimedean spiral fills the disk with turns spaced
# line_width apart, each turn stepped ~resolution mm apart, so total path
# length is roughly area / line_width and point count is that length /
# resolution. Verified against examples/surf_dome.gcode (radius ~39.7mm,
# line_width 0.45mm, resolution 0.4mm, 1 shell, 27924 actual extrude moves):
# this formula predicts ~27573, well within a small margin. Every point is
# then walked AGAIN by the probe-slope check below (each with its own ring of
# field samples), so the budget must be checked before either runs, not after
# either one hangs.
_SURFACE_MAX_POINTS = 2_000_000


def _check_surface_budget(radius, line_width, resolution, shells):
    """Reject a surface design whose point count would blow up, with a clear
    reason, before any generation or slope-checking work is done."""
    if resolution <= 0:
        raise ValueError("surface resolution must be positive")
    if line_width <= 0:
        raise ValueError("line width must be positive")
    approx_points = math.pi * radius * radius / (line_width * resolution)
    total = approx_points * max(shells, 1)
    if total > _SURFACE_MAX_POINTS:
        raise ValueError(
            "surface design is too large: an estimated %d points (radius "
            "%.1f mm / line_width %.3f mm x %d shells) exceeds the export "
            "limit of %d. Reduce the radius or shell count."
            % (int(total), radius, line_width, shells, _SURFACE_MAX_POINTS))


def _check_probe_slope(profile, field, base_pts, z_offset, shells, layer_height):
    """Refuse a conformal shell whose local slope the trailing probe cannot clear.

    Unlike the parametric vase's wave amplitude (bounded machine-wide by
    profile.z_amp_max / amp_ceiling() -- a small wiggle riding a big cylinder),
    a surface spiral's Z *is* the height field: it can rise and fall by many mm
    over a short XY run. The inductive probe rides at a fixed offset from the
    nozzle (profile.probe_dx, profile.probe_dy), body bottom profile.probe_clearance
    above the nozzle tip, keep-out radius profile.probe_radius. Descending a
    dome (or any steep field), the probe can pass directly over material well
    above the nozzle's own current height -- a physical collision risk during
    printing, not just during bed leveling.

    Every millimetre figure used here (probe_dx/dy, probe_clearance,
    probe_radius) comes from the selected PrinterProfile, never a literal, so
    a probe-less printer (has_probe=False) is skipped entirely and a printer
    with a different clearance gets a correctly-scaled ceiling of its own --
    never the Trident's.

    DELIBERATELY CONSERVATIVE: this assumes material exists wherever the
    height field is *defined* near the probe's swept centre, not only where
    the spiral has actually already deposited a bead by that point in the
    toolpath. That can only over-reject (flag something a print-order-aware
    model would pass) -- it can never under-reject, because "assume the whole
    neighbourhood is already printed" is a superset of whatever is actually
    there at any real moment in time.

    GUESS, NOT PRINT-TESTED: unlike z_amp_max (revised down after an actual
    probe strike on this machine), this specific slope-rejection threshold has
    not itself been validated by a real print. A pass here means "consistent
    with the same geometry that governs the physical probe", not "proven safe".

    Refuses (raises ValueError) rather than warning-and-continuing -- that is
    the explicit, deliberate choice for this check.
    """
    if not profile.has_probe or profile.probe_radius <= 0:
        return
    p_rad = profile.probe_radius
    p_dx, p_dy = profile.probe_dx, profile.probe_dy
    p_clear = profile.probe_clearance

    # Sample the neighbourhood the probe body sweeps using the SPIRAL'S OWN
    # sample points, not a hand-picked ring: base_pts already tile the whole
    # disk at the print's real resolution (turns spaced line_width apart,
    # stepped ~resolution mm along each turn), so reusing them is both denser
    # and cheaper than evaluating field() again at made-up offsets. This
    # matters for fields with fast ANGULAR variation (saddle flips sign every
    # 90 degrees) -- an earlier version of this check sampled a coarse 2-ring,
    # 8-angle pattern around the probe centre and missed a saddle peak that
    # analyze_gcode's own independent re-check then caught for real (probe
    # collision, not merely a slope warning). Bucketing the spiral's own
    # points into PCELL-sized cells and keeping each cell's max field value
    # mirrors analyze.py's probe keep-out grid (PCELL coarse cells, worst-
    # value-per-cell, a p_cells x p_cells window with a circular distance
    # test -- see analyze.py around line 131-136 and 305-322) -- except keyed
    # on the height FIELD (defined disk-wide, independent of print order)
    # rather than "material already printed by this point in time", which is
    # exactly the deliberate over-conservatism this check requires.
    PCELL = 4.0
    cell_max: dict = {}
    fvals = [0.0] * len(base_pts)
    for i, (x, y) in enumerate(base_pts):
        fv = field(x, y)
        fvals[i] = fv
        key = (round(x / PCELL), round(y / PCELL))
        if fv > cell_max.get(key, -1e30):
            cell_max[key] = fv

    p_cells = int(math.ceil(p_rad / PCELL)) + 1
    reach2 = (p_rad + PCELL * 0.71) ** 2

    # Evaluated at the top shell (the last one printed, i.e. tallest nozzle
    # path) per the spec this check implements.
    top_offset = max(0, shells - 1) * layer_height
    for (x, y), fv in zip(base_pts, fvals):
        nozzle_z = z_offset + fv + top_offset
        limit = nozzle_z + p_clear
        pcx, pcy = x + p_dx, y + p_dy
        gx, gy = round(pcx / PCELL), round(pcy / PCELL)
        worst = -1e30
        for cxx in range(gx - p_cells, gx + p_cells + 1):
            for cyy in range(gy - p_cells, gy + p_cells + 1):
                cval = cell_max.get((cxx, cyy))
                if cval is None or cval <= worst:
                    continue
                ddx = cxx * PCELL - pcx
                ddy = cyy * PCELL - pcy
                if ddx * ddx + ddy * ddy <= reach2:
                    worst = cval
        if worst <= -1e29:
            continue  # no sampled material anywhere near the probe centre
        material_z = z_offset + worst
        if material_z > limit:
            radius_here = math.hypot(x, y)
            overshoot = material_z - limit
            slope = overshoot / p_rad
            raise ValueError(
                "Surface too steep for this printer's probe: at radius "
                "%.1f mm from the surface centre, the trailing probe (offset "
                "%.1f/%.1f mm, %.1f mm keep-out radius, %.2f mm body "
                "clearance) would ride %.2f mm above its clearance limit -- "
                "local slope ~%.2f mm/mm across the probe's reach. Reduce "
                "amplitude, shrink the affected radius, or choose a "
                "shallower surface."
                % (radius_here, p_dx, p_dy, p_rad, p_clear, overshoot, slope))


def generate_surface_design(body):
    """mode=surface -- non-planar conformal shell over a height field.

    Mirrors generate_design's structure (profile resolution via _get_profile,
    writer construction, the progress side-channel, analyze_gcode
    re-verification, and the same gcode/report/issues/stats/filename return
    shape) but drives build_surface_spiral instead of build_continuous_spiral.
    See generate.py's ``--surface`` CLI wiring (around line 378) for the
    reference this mirrors.
    """
    surface_name = str(body.get("surface", "dome"))
    valid_surfaces = surf.PARAMETRIC | {"stl"}
    if surface_name not in valid_surfaces:
        raise ValueError(
            "unknown surface '%s', expected one of %s"
            % (surface_name, sorted(valid_surfaces)))

    layer_height = float(body.get("layer_height", 0.30))
    if layer_height <= 0:
        raise ValueError("layer_height must be positive")
    line_width = body.get("line_width", None)
    print_speed = float(body.get("print_speed", 40.0))
    filament = body.get("filament", None)

    surface_amp = float(body.get("surface_amp", 5.0))
    surface_wavelength = float(body.get("surface_wavelength", 20.0))
    surface_shells = max(1, min(int(body.get("surface_shells", 1)), 20))
    radius = float(body.get("radius", 32.0))
    if radius <= 0:
        raise ValueError("radius must be positive")
    surface_scale = max(0.01, min(float(body.get("surface_scale", 1.0)), 100.0))

    profile = _get_profile(body)

    # Sane-range clamps (never trust the client), resolved against THIS
    # printer -- surface_amp is bounded by the machine's own Z travel
    # (profile.z_max), not by z_amp_max: unlike the vase's wave amplitude,
    # this is the field's total excursion, not a dip below already-printed
    # material, so it is z_max (top-of-print ceiling, also re-checked inside
    # build_surface_spiral) and the probe-slope check below -- not
    # amp_ceiling() -- that bound this generator's safety.
    surface_amp = max(0.0, min(surface_amp, profile.z_max))
    surface_wavelength = max(1.0, min(surface_wavelength, 1000.0))

    nozzle = profile.nozzle_diameter
    lw = round(nozzle * 1.125, 3) if line_width is None else float(line_width)
    if lw <= 0:
        raise ValueError("line width must be positive")

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

    # Applied AFTER the filament merge, same reasoning as generate_design: the
    # user's explicit override must win over the filament's own default.
    nozzle_temp = _parse_nozzle_temp(body, profile)
    if nozzle_temp is not None:
        writer_kwargs["nozzle_temp"] = nozzle_temp
    bed_temp = _parse_bed_temp(body, profile)
    if bed_temp is not None:
        writer_kwargs["bed_temp"] = bed_temp

    # Resolve the height field. STL comes from the shared mesh cache the
    # mesh_texture path also uses; radius then comes from the mesh's own
    # footprint (surface_from_stl) exactly like generate.py's CLI wiring --
    # a user-supplied radius is not meaningful for a draped mesh and is
    # discarded in favour of the mesh's real extent.
    if surface_name == "stl":
        mesh_id = body.get("mesh_id")
        if not mesh_id:
            raise ValueError("mesh_id is required for surface=stl")
        entry = _mesh_cache.get(mesh_id)
        if entry is None:
            raise KeyError(
                "mesh_id not found (upload may have expired) - re-upload the STL")
        field, radius = surf.surface_from_stl(entry["tris"], scale=surface_scale)
    else:
        field = surf.make_parametric(surface_name, surface_amp, radius, surface_wavelength)

    resolution = 0.4  # build_surface_spiral's own default; not client-exposed
    _check_surface_budget(radius, lw, resolution, surface_shells)

    writer = GcodeWriter(**writer_kwargs)

    # Replicate build_surface_spiral's own base-points / z-offset computation
    # (surface_spiral.py) so the probe-slope pre-check below runs over the
    # SAME sample points and Z the real generator will use.
    base_pts = _archimedean(radius, writer.line_width, resolution)
    if len(base_pts) < 2:
        raise ValueError("surface too small for the given line width")
    first_layer_z = writer.layer_height
    fmin = min(field(x, y) for (x, y) in base_pts)
    z_offset = first_layer_z - fmin

    _check_probe_slope(profile, field, base_pts, z_offset, surface_shells, writer.layer_height)

    # Progress side-channel (publish-only, for /api/generate_stream) -- same
    # publish-only pattern as generate_design: a no-op unless the streaming
    # endpoint attached a dict via body["_progress"].
    _progress = body.get("_progress")
    if _progress is not None:
        _progress["writer"] = writer
        _progress["total"] = int(len(base_pts) * surface_shells) + 200
        _progress["stage"] = "toolpath"

    report = build_surface_spiral(
        writer, field, radius,
        shells=surface_shells,
        resolution=resolution,
        first_layer_z=first_layer_z,
    )

    gcode_text = writer.text()

    # Re-check the generated G-code against the machine (independent
    # verification), same as generate_design.
    if _progress is not None:
        _progress["stage"] = "verify"
        _progress["verify_total"] = gcode_text.count("\n")
    fd, tmp_path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(gcode_text)
        analysis = analyze_gcode(tmp_path, profile, _progress=_progress)
        report_text = format_report(analysis, profile)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    issues_extra = []
    if profile.has_probe and profile.probe_radius > 0:
        issues_extra.append(
            "Probe-slope check passed (probe offset %.1f/%.1f mm, clearance "
            "%.2f mm, keep-out radius %.1f mm). GUESS, NOT PRINT-TESTED: this "
            "ceiling is derived from probe geometry, not from an actual probe "
            "strike the way z_amp_max was -- treat a pass as consistent with "
            "the probe's geometry, not as a proven-safe claim."
            % (profile.probe_dx, profile.probe_dy, profile.probe_clearance,
               profile.probe_radius))

    stats = {
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
        "shells": report.get("shells"),
        "footprint_radius_mm": report.get("footprint_radius_mm"),
        "fan_min_pct": (round(analysis.min_fan_speed * 100) if analysis.min_fan_speed is not None else None),
        "fan_max_pct": (round(analysis.max_fan_speed * 100) if analysis.max_fan_speed is not None else None),
    }

    filename = "design_surface_%s_%dmm.gcode" % (
        _clean_shape(surface_name), int(round(report.get("top_z_mm") or 0)))

    return {
        "gcode": gcode_text,
        "report": report_text,
        "issues": list(analysis.issues) + issues_extra,
        "stats": stats,
        "filename": filename,
    }


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
    xy_twist = float(body.get("xy_twist", 0.0))

    # Symmetry-breaking: leaning spine + elliptical cross-section. Same
    # clamps as generate_design()'s copy of this block -- ovality and the
    # spine are affine transforms (scale / translate) applied to the wall
    # point AFTER it is computed, so they need no assumption that the
    # underlying contour is star-convex and apply cleanly to a mesh-derived
    # contour stack (see build_profile_spiral).
    spine_mm = max(0.0, min(float(body.get("spine_mm", 0.0)), 20.0))
    spine_deg = float(body.get("spine_deg", 0.0))
    ovality = max(-0.4, min(float(body.get("ovality", 0.0)), 0.4))
    spine_offset = None
    if spine_mm > 0.0:
        _sc = math.cos(math.radians(spine_deg))
        _ss = math.sin(math.radians(spine_deg))
        spine_offset = lambda t: (t * spine_mm * _sc, t * spine_mm * _ss)

    # Asymmetric control cage: reuses _parse_cage (identical [0.5, 1.5] clamp
    # to the parametric path). build_profile_spiral applies it as a per-point
    # radial scale about the origin, sampled at the ring-index angle -- no
    # star-convexity assumption needed (unlike base_fill's disk paver).
    cage = _parse_cage(body.get("cage"))

    # Resolved before amp_fn below: the amplitude ceiling is derived from the
    # selected machine's own z_amp_max (amp_ceiling()), so the profile must be
    # in scope first.
    profile = _get_profile(body)

    amp_profile = body.get("amp_profile") or [[0, 0.0], [1, 0.0]]
    amp_fn = _make_interp(amp_profile, 0.0, amp_ceiling(profile))
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

    # Point Edit Modifier stack (FFD / Smooth / Radial Push, gated by Mask /
    # Protection): _point_edits_on_contours already applies the whole stack
    # to an arbitrary contour stack for the STL export path -- reused as-is
    # here, with the SAME clamps (_parse_point_* helpers), so the mesh
    # G-code path can never accept a looser edit than the parametric wall or
    # the STL export. Returns the contours unchanged (still 2-tuple (x, y))
    # when no deforming modifier is active; otherwise 3-tuple (x, y, z)
    # rings that build_profile_spiral below reads as a per-point Z override.
    contours = _point_edits_on_contours(body, contours, heights, points_per_turn)

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

    # Applied AFTER the filament merge: fs.writer_kwargs() carries its own
    # nozzle_temp/bed_temp, which would otherwise silently clobber an explicit
    # user override. The user's choice must win over the filament's default.
    nozzle_temp = _parse_nozzle_temp(body, profile)
    if nozzle_temp is not None:
        writer_kwargs["nozzle_temp"] = nozzle_temp
    bed_temp = _parse_bed_temp(body, profile)
    if bed_temp is not None:
        writer_kwargs["bed_temp"] = bed_temp

    writer = GcodeWriter(**writer_kwargs)

    # Progress side-channel (publish-only, for /api/generate_stream) -- same
    # mechanism and same no-op-when-absent guarantee as generate_design()'s
    # copy of this block above. contours is already built at this point, so
    # the wall-line estimate uses the real layer count instead of guessing it.
    _progress = body.get("_progress")
    if _progress is not None:
        _progress["writer"] = writer
        _progress["total"] = points_per_turn * max(len(contours), 1) + 27623
        _progress["stage"] = "toolpath"

    # pt[0]/pt[1] rather than unpacking (x, y) directly: point edits above
    # may have widened each ring's points to 3-tuple (x, y, z).
    mesh_radius = (max(math.hypot(pt[0], pt[1]) for c in contours for pt in c)
                   if contours else None)
    blob_spec = _parse_blob_spec(body, radius=mesh_radius)
    loop_spec = _parse_loop_spec(body, profile, radius=mesh_radius)
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
        xy_twist_turns=xy_twist,
        cage=cage,
        ovality=ovality,
        spine_offset=spine_offset,
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

    if _progress is not None:
        _progress["stage"] = "verify"
        # Same denominator as generate_design()'s copy of this block: exact,
        # cheap, and gives analyze_gcode's progress counter something to
        # divide by so the bar can advance during verification too.
        _progress["verify_total"] = gcode_text.count("\n")
    fd, tmp_path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(gcode_text)
        analysis = analyze_gcode(tmp_path, profile, _progress=_progress)
        report_text = format_report(analysis, profile)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Empirical print-quality check: peak wall slope = amp*waves/(radius*sil).
    # pt[0]/pt[1] rather than unpacking (x, y): the bottom ring may be a
    # 3-tuple (x, y, z) after point edits above. Ceiling is this machine's
    # own quality_slope_max (slope_ceiling()), not a constant -- see the
    # generate_design() copy of this check for the full rationale.
    bottom_radius = (max(math.hypot(pt[0], pt[1]) for pt in contours[0])
                      if contours else 1.0)
    slope_limit = slope_ceiling(profile)
    peak_slope = _peak_wave_slope(amp_fn, z_waves, lambda t: bottom_radius)
    issues_extra = []
    if peak_slope > slope_limit + 1e-9:
        issues_extra.append(_slope_exceeded_message(
            peak_slope, slope_limit, bottom_radius, z_waves, amp_ceiling(profile)))
    # Point Mask and Point Protection are GATES, not deformations (see
    # point_edit.py): they scale how strongly FFD / Smooth / Radial Push act
    # at each point. On their own there is nothing to scale, so
    # apply_point_edits (inside _point_edits_on_contours above) returns the
    # contours untouched -- byte-identical output for a modifier the user
    # switched on and configured. Same message generate_design() gives for
    # the parametric wall, so the STL path doesn't look like it silently did
    # nothing.
    (_pe_mask, _pe_protection, _pe_ffd,
     _pe_smooth, _pe_radial_push) = _parse_point_edit_specs(body)
    if ((_pe_mask is not None or _pe_protection is not None)
            and _pe_ffd is None and _pe_smooth is None
            and _pe_radial_push is None):
        _gates = []
        if _pe_mask is not None:
            _gates.append("Point Mask")
        if _pe_protection is not None:
            _gates.append("Point Protection")
        issues_extra.append(
            " and ".join(_gates)
            + (" only scale" if len(_gates) > 1 else " only scales")
            + " how strongly a deforming modifier acts - with no FFD, Smooth "
            "or Radial Push enabled there is nothing to scale, so the wall "
            "was generated unchanged.")
    # build_profile_spiral takes neither base_style nor skirt_loops: it always
    # paves the disks as the Archimedean spiral and never lays a skirt (both
    # rely on a star-convex theta->radius function, which a mesh-derived
    # contour cannot guarantee -- see base_fill.py's module docstring). Report
    # the ones actually asked for.
    _mesh_dropped = []
    if str(body.get("base_style", "spiral")) == "concentric" and base_layers > 0:
        _mesh_dropped.append("concentric base style (the spiral disk was used)")
    if int(body.get("skirt", 0) or 0) > 0:
        _mesh_dropped.append("the skirt")
    if _mesh_dropped:
        issues_extra.append(
            "STL mode cannot vary the base fill or lay a skirt - "
            + ", ".join(_mesh_dropped) + " was not printed.")

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

    xy_twist / cage / ovality / spine and the Point Edit stack do NOT apply
    here, unlike generate_mesh_texture_design (the G-code path), which now
    wires all of them through build_profile_spiral. This export path stops
    at stack_from_mesh + radial texture, so a solid STL exported from an
    xy_twist/cage/ovality/spine'd mesh design will not match the G-code
    preview -- a real gap, not mirrored intentionally; left alone here
    because solid-export parity for those knobs needs its own contour-stack
    plumbing and is out of scope for this pass.
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
    star_points, star_depth = _parse_star(body)

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
# Custom printer import/validate/save/delete/export API.
#
# The browser is never trusted: /validate and /save both run the FULL
# printer_validate pipeline from scratch on whatever JSON arrives, exactly as
# strict as the server-side parser path (/parse). Only printer_store.save_custom
# ever writes to disk, and only after that re-validation has passed.
# --------------------------------------------------------------------------
PRINTER_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB; mirrors printer_import's own cap


def _profile_to_json(profile):
    from dataclasses import asdict
    return asdict(profile)


def _issues_json(issues):
    return [{"field": i.field, "message": i.message, "severity": i.severity} for i in issues]


def _printer_entry_json(key, profile, custom, meta=None):
    meta = meta or {}
    warnings = meta.get("warnings") or []
    # loop_row_max / loop_up_max mirror _parse_loop_spec's cap/cap_row formula
    # exactly, computed here from the same loop_up_ceiling() so the client
    # never re-derives it and the two can never drift apart. Note this is
    # loop_up_ceiling, NOT amp_ceiling: the fabric is bounded by the support
    # reach as well as by the machine, while z_amp_max below stays the raw
    # machine figure the amp curve and the draft's wave clamp need.
    cap = loop_up_ceiling(profile)
    cap_row = loop_row_ceiling(profile)
    # max_nozzle_temp/max_bed_temp: computed here (mirroring the 320 C
    # absolute backstop _parse_nozzle_temp applies server-side) so the client
    # never re-derives its input "max" attribute from a hardcoded HTML value
    # and the two can never drift apart. See _parse_nozzle_temp/_parse_bed_temp.
    return {
        "key": key,
        "name": profile.name,
        "bed": [profile.bed_size_x, profile.bed_size_y],
        "z_max": profile.z_max,
        "z_amp_max": profile.z_amp_max,
        "loop_row_max": cap_row,
        "loop_up_max": cap,
        # loop_min_mm: the FLOOR of the same two ranges, served for the same
        # reason as the ceilings above -- the client scales its loop-style
        # presets into [loop_min_mm, loop_row_max] and must not carry its own
        # copy of a bound the server enforces. Capped by the machine so a
        # profile with less clearance than LOOP_MIN_MM still reports a floor at
        # or below its own ceiling instead of an inverted range.
        "loop_min_mm": min(LOOP_MIN_MM, cap_row, cap),
        "max_nozzle_temp": min(320.0, float(profile.max_nozzle_temp)),
        "max_bed_temp": float(profile.max_bed_temp),
        # max_z_velocity: the viewer's Print Stats panel flags the peak Z-rate
        # of a loaded file, and it used to compare against a bare 25.1 literal
        # in viewer.js -- the Trident's own limit, applied to every printer.
        # That is the module-constant machine limit CLAUDE.md forbids: it would
        # pass a file at 20 mm/s on a machine declaring 10. Served from the
        # profile so the client never carries a ceiling of its own.
        "max_z_velocity": float(profile.max_z_velocity),
        # quality_slope_max: same reasoning as max_z_velocity above -- this
        # used to be QUALITY_SLOPE_LIMIT, a bare 0.25 baked into serve.py and
        # applied to every printer regardless of what it actually declared.
        # Served from slope_ceiling(profile) so the client never carries a
        # quality ceiling of its own and the two can never drift apart.
        "quality_slope_max": slope_ceiling(profile),
        # has_probe: lets the client adapt copy (e.g. loop-fabric's "probe
        # keep-out" note, see loop_fabric.py) to probe-less machines without
        # hardcoding a machine list of its own.
        "has_probe": bool(profile.has_probe),
        "custom": bool(custom),
        "source_format": meta.get("source_format") if custom else None,
        "warnings": len(warnings) if custom else 0,
    }


def _validation_result_json(vr, detected_format=None, stripped_gcode=None):
    out = {
        "ok": vr.ok,
        "profile": _profile_to_json(vr.profile),
        "fields": [
            {"name": f.name, "label": f.label, "unit": f.unit, "group": f.group,
             "value": f.value, "source": f.source, "raw": f.raw, "note": f.note}
            for f in vr.fields
        ],
        "issues": _issues_json(vr.issues),
    }
    if detected_format is not None:
        out["detected_format"] = detected_format
    if stripped_gcode is not None:
        out["stripped_gcode"] = stripped_gcode
    return out


def _extract_stripped_gcode(source, allow_raw_gcode):
    """Re-run sanitize_gcode just to recover the removed-line list for the API
    response's `stripped_gcode` field. Cheap, and keeps ValidationResult's
    shape exactly as specified (no extra fields bolted on) while still giving
    the client the raw text of every line the safety scan stripped."""
    from trident_gcode.printer_validate import sanitize_gcode
    from trident_gcode.printer_import import RawConfig

    def get(field_name):
        if isinstance(source, RawConfig):
            pf = source.fields.get(field_name)
            return pf.value if pf is not None else None
        if isinstance(source, dict):
            return source.get(field_name)
        return None

    lines = []
    for kind, field_name in (("start", "start_gcode"), ("end", "end_gcode")):
        raw = get(field_name)
        if raw:
            _clean, _issues, stripped = sanitize_gcode(
                str(raw), kind, allow_raw_gcode=allow_raw_gcode)
            lines.extend(line for line, _reason in stripped)
    return lines


def _reject_nonfinite(token):
    """json.loads accepts the bare tokens NaN/Infinity/-Infinity by default.

    A non-finite number must never reach the generator: every comparison
    against NaN is False, so it survives clamping AND defeats the guards meant
    to catch it -- including GcodeWriter._check_bounds, whose out-of-bounds
    tests would all silently pass. Refuse it at the door instead.
    """
    raise ValueError("non-finite number %s is not allowed" % token)


def _reject_nonfinite_tree(value, path="body"):
    """Walk a parsed JSON body and refuse every non-finite number in it.

    ``parse_constant`` above only intercepts the BARE tokens NaN/Infinity, and
    that is not the whole door. Two other ways in, both confirmed to reach the
    emitted G-code before this existed:

      * a quoted STRING -- ``{"z_twist": "nan"}``. Nothing exotic: ``float()``
        accepts "nan"/"inf"/"-inf" happily, and nearly every numeric field is a
        bare ``float(body.get(...))``. One such field produced 16002 lines of
        ``G1 X142.5000 Y117.5000 Znan Enan F2400`` with ``issues == []``.
      * a numeric literal that OVERFLOWS to inf on parse -- ``1e400`` -- which
        is a float by the time parse_constant would have seen it.

    Checked here, once, on the whole tree, rather than at each of the ~60
    numeric reads: this is the boundary CLAUDE.md means by "reject non-finite
    values at the boundary; never clamp them", and a per-field check is one
    forgotten field away from reopening the hole.

    Strings are only refused when ``float()`` itself yields a non-finite value,
    i.e. exactly the tokens "nan"/"inf"/"-infinity" and friends. Ordinary text
    is untouched -- a printer named "Infinity 3D" does not parse as a float at
    all, so it passes through unharmed.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                "%s is not a finite number (got %r)" % (path, value))
        return value
    if isinstance(value, str):
        try:
            as_float = float(value)
        except (TypeError, ValueError):
            return value          # ordinary text, not a number at all
        if not math.isfinite(as_float):
            raise ValueError(
                "%s is not a finite number (got %r)" % (path, value))
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_tree(item, "%s.%s" % (path, key))
        return value
    if isinstance(value, list):
        for i, item in enumerate(value):
            _reject_nonfinite_tree(item, "%s[%d]" % (path, i))
        return value
    return value


def _read_json_body(handler):
    """Read + parse a JSON request body. On malformed JSON, sends the 400
    itself and returns None -- callers must check for that sentinel."""
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        length = 0
    raw = handler.rfile.read(length) if length else b""
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_reject_nonfinite) if raw else {}
        # Second pass: quoted "nan"/"inf" strings and overflowed literals like
        # 1e400 are already plain values by the time parse_constant has run.
        return _reject_nonfinite_tree(body)
    except (ValueError, UnicodeDecodeError) as exc:
        # Name the offending field when we know it -- "body.amp_profile[1][1]
        # is not a finite number" is actionable, "not valid JSON" is not.
        detail = str(exc) if "finite" in str(exc) else "Request body is not valid JSON."
        handler._send_json({"error": detail}, status=400)
        return None


def _progress_fraction(progress, floor):
    """Honest 0..0.99 progress estimate for /api/generate_stream.

    `progress` is the dict generate_design()/generate_mesh_texture_design()
    publish into (see the "Progress side-channel" comments there): "writer",
    "total" and "stage" appear only once generation has actually started, so
    before that this returns `floor` unchanged rather than guessing.

    Generation has two phases and each owns a slice of the bar instead of
    the second one being a dead zone at the end:
      - "toolpath" (building the G-code)  -> maps into [0.00, 0.45]
      - "verify"   (analyze_gcode's independent re-check of what toolpath
        just built) -> maps into [0.45, 0.99], driven by analyze_gcode's own
        "analyzed" line counter (see trident_gcode/analyze.py's _progress
        parameter) against "verify_total" (the exact line count of the
        G-code being re-checked, published just before analyze_gcode is
        called).
    The 0.45 split point is an estimate from a single measurement (one
    design took ~2.4s to build toolpath and ~4.7s to verify it) -- the real
    ratio depends on the design and the printer profile, it is not derived
    from anything exact. Treat it as a reasonable guess, not a promise.

    Two honesty rules enforced here, per CLAUDE.md ("this project cares a lot
    about not lying to the user"): the result never exceeds 0.99 -- an
    in-progress print must never look done -- and it never drops below
    `floor`, so a caller that always passes its own last-reported value gets
    a monotonic sequence even though both "total" and "verify_total" are
    estimates/line-counts a fast-moving stage can overshoot or that a slow
    poll can catch mid-update.
    """
    writer = progress.get("writer")
    total = progress.get("total")
    if writer is None or not total:
        return floor
    TOOLPATH_SPLIT = 0.45
    if progress.get("stage") == "verify":
        verify_total = progress.get("verify_total")
        analyzed = progress.get("analyzed", 0)
        verify_frac = min(analyzed / float(verify_total), 1.0) if verify_total else 0.0
        frac = TOOLPATH_SPLIT + verify_frac * (0.99 - TOOLPATH_SPLIT)
    else:
        toolpath_frac = min(len(writer._lines) / float(total), 1.0)
        frac = toolpath_frac * TOOLPATH_SPLIT
    return max(floor, min(frac, 0.99))


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

    def _write_ndjson_line(self, obj):
        """Write one NDJSON progress/result line and flush immediately.

        Without the explicit flush, a client sees nothing until the whole
        response completes (buffering happens either here or in the socket
        layer) -- for a 100+ second generate that defeats the entire point of
        streaming. Caller is responsible for catching the disconnect errors
        this can raise (BrokenPipeError / ConnectionAbortedError / OSError)
        if the client has gone away.
        """
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.wfile.flush()

    def send_head(self):
        """Static-file hook shared by GET and HEAD. Hidden paths are refused.

        The server's document root is the repo checkout, so without this
        every dot-directory in it is downloadable: /.git/config, /.git/HEAD
        and the whole object store. On loopback that is merely untidy, but
        TRIDENT_BIND turns the same handler into a public one, and a deploy
        that hands out its own git history to anyone who asks is not a
        deploy anyone should make. Blocking the whole class of hidden names
        (rather than /.git specifically) also covers .env and friends if they
        ever land in the tree.
        """
        path = urlparse(self.path).path
        if any(seg.startswith(".") for seg in path.split("/") if seg):
            self.send_error(404, "Not Found")
            return None
        return super().send_head()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # A bare URL should open the app, not a directory listing of the
        # source tree. Matters once this is hosted: "/" is what a visitor
        # gets handed, and a file index is not an application.
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/viewer/index.html")
            self.end_headers()
            return
        if path == "/api/printers":
            custom = printer_store.session_list(_session_id(self))
            printers = []
            for key, prof in PRINTER_PROFILES.items():
                printers.append(_printer_entry_json(key, prof, custom=False))
            for key, entry in custom.items():
                printers.append(_printer_entry_json(key, entry["profile"], custom=True, meta=entry["meta"]))
            self._send_json({"printers": printers, "default": DEFAULT_PRINTER_KEY})
            return
        if path == "/api/printer":
            self._handle_get_printer(parse_qs(urlparse(self.path).query))
            return
        if path == "/api/printer/export":
            self._handle_printer_export(parse_qs(urlparse(self.path).query))
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

    def _handle_get_printer(self, query):
        key = (query.get("key") or [None])[0]
        if not key:
            self._send_json({"ok": False, "error": "key is required"}, status=400)
            return
        sid = _session_id(self)
        custom_entries = printer_store.session_list(sid)
        entry = custom_entries.get(key)
        if entry is not None:
            profile, meta, custom = entry["profile"], entry["meta"], True
        elif key in PRINTER_PROFILES:
            profile, meta, custom = PRINTER_PROFILES[key], {}, False
        else:
            self._send_json({"ok": False, "error": "unknown printer key '%s'" % key}, status=404)
            return
        self._send_json({
            "ok": True, "key": key, "profile": _profile_to_json(profile),
            "meta": meta, "custom": custom,
        })

    def _handle_printer_export(self, query):
        key = (query.get("key") or [None])[0]
        if not key:
            self._send_json({"ok": False, "error": "key is required"}, status=400)
            return
        entry = printer_store.session_list(_session_id(self)).get(key)
        if entry is not None:
            profile, meta = entry["profile"], entry["meta"]
        elif key in PRINTER_PROFILES:
            profile, meta = PRINTER_PROFILES[key], {}
        else:
            self._send_json({"ok": False, "error": "unknown printer '%s'" % key}, status=404)
            return

        data_obj = {
            "schema": "trident-printer/1",
            "key": key,
            "profile": _profile_to_json(profile),
            "meta": meta,
        }
        payload = json.dumps(data_obj, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", 'attachment; filename="%s.json"' % key)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_printer_parse(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._send_json({"ok": False, "error": "empty request body", "detected_format": None},
                             status=400)
            return
        if length > PRINTER_UPLOAD_MAX_BYTES:
            self._send_json(
                {"ok": False, "error": "file too large (max %d MB)" % (PRINTER_UPLOAD_MAX_BYTES // (1024 * 1024)),
                 "detected_format": None}, status=400)
            return

        data = self.rfile.read(length)
        filename = self.headers.get("X-Filename", "printer.cfg")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json({"ok": False, "error": "file is not valid UTF-8 text",
                              "detected_format": None}, status=400)
            return

        from trident_gcode.printer_import import detect_format
        detected = detect_format(text, filename)
        try:
            raw = parse_printer_config(text, filename)
        except PrinterImportError as e:
            self._send_json({"ok": False, "error": str(e), "detected_format": detected}, status=400)
            return
        except Exception as e:
            self._send_json({"ok": False, "error": "parse failed: %s" % e,
                              "detected_format": detected}, status=400)
            return

        try:
            # repair=True: this IS the import boundary -- a freshly uploaded
            # config becoming a profile for the first time. A start block
            # with no heating at all is a guaranteed abort (Klipper refuses
            # to extrude below min_extrude_temp), so it gets fixed here, not
            # just warned about. Re-validation later (edit stage, /api/
            # printer/validate) must NOT repeat this -- see sanitize_gcode.
            vr = validate_raw(raw, repair=True)
            stripped = _extract_stripped_gcode(raw, allow_raw_gcode=False)
            out = _validation_result_json(vr, detected_format=raw.fmt, stripped_gcode=stripped)
            out["suggested_key"] = printer_store.session_make_key(
                _session_id(self), vr.profile.name)
            out["notes"] = list(raw.notes)
        except Exception as e:
            self._send_json({"ok": False, "error": "validation failed: %s" % e,
                              "detected_format": raw.fmt}, status=400)
            return
        self._send_json(out, status=200)

    def _handle_printer_validate(self):
        body = _read_json_body(self)
        if body is None:
            return
        profile_dict = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(profile_dict, dict):
            profile_dict = {}
        allow_raw = bool(body.get("allow_raw_gcode", False)) if isinstance(body, dict) else False
        try:
            vr = validate_profile_dict(profile_dict, allow_raw_gcode=allow_raw)
            stripped = _extract_stripped_gcode(profile_dict, allow_raw_gcode=allow_raw)
            out = _validation_result_json(vr, stripped_gcode=stripped)
        except Exception as e:
            self._send_json({"ok": False, "error": "validation failed: %s" % e}, status=400)
            return
        self._send_json(out, status=200)

    def _handle_printer_save(self):
        body = _read_json_body(self)
        if body is None:
            return
        profile_dict = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(profile_dict, dict):
            self._send_json({"ok": False, "error": "'profile' object is required"}, status=400)
            return
        allow_raw = bool(body.get("allow_raw_gcode", False))

        try:
            vr = validate_profile_dict(profile_dict, allow_raw_gcode=allow_raw)
        except Exception as e:
            self._send_json({"ok": False, "error": "validation failed: %s" % e}, status=400)
            return
        if not vr.ok:
            self._send_json({"ok": False, "issues": _issues_json(vr.issues)}, status=400)
            return

        sid = _session_id(self)
        if sid is None:
            self._send_json(
                {"ok": False, "error": "missing or malformed browser session id; reload the page"},
                status=400)
            return

        requested_key = body.get("key")
        if requested_key and printer_store.session_is_custom(sid, str(requested_key)):
            key = str(requested_key)
        elif requested_key and str(requested_key) in PRINTER_PROFILES:
            self._send_json(
                {"ok": False, "error": "'%s' is a built-in printer and cannot be overwritten" % requested_key},
                status=400)
            return
        else:
            key = printer_store.session_make_key(sid, vr.profile.name)

        meta_in = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        import datetime as _dt
        meta = dict(meta_in)
        meta["source_format"] = meta_in.get("source_format")
        meta["source_file"] = meta_in.get("source_file", "")
        meta["imported_at"] = meta_in.get("imported_at") or _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        # Re-validating an already-complete profile legitimately produces almost
        # no warnings: "not found in config, using a conservative default" and
        # "clamped from 99999" can only be said at IMPORT time, and by now those
        # fields are all present and in range. Those are exactly the caveats
        # worth remembering, so the import-time list is carried in meta and
        # merged here. It is display-only text, but it still arrives from the
        # browser, so it is bounded before being written to disk.
        import_warnings = meta_in.get("warnings")
        merged: list[str] = []
        seen: set = set()

        def _add(msg: str) -> None:
            # De-duplicated: the client sends its import-time list plus whatever
            # the latest re-validation produced, and those overlap heavily.
            if msg and msg not in seen:
                seen.add(msg)
                merged.append(msg)

        if isinstance(import_warnings, list):
            for w in import_warnings[:100]:
                if isinstance(w, str) and w.strip():
                    _add(w.strip()[:300])
        for i in vr.issues:
            if i.severity == "warn":
                _add(i.message)
        meta["warnings"] = merged[:50]

        try:
            printer_store.session_save(sid, key, vr.profile, meta)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        self._send_json({
            "ok": True, "key": key,
            "printer": _printer_entry_json(key, vr.profile, custom=True, meta=meta),
            "profile": _profile_to_json(vr.profile),
            "meta": meta,
            "issues": _issues_json(vr.issues),
        })

    def _handle_printer_session(self):
        """Replay the browser's localStorage copy into this session's store.

        This is the ONLY way a custom printer gets back into the server after
        a restart, and it is fed straight from client storage -- so every
        entry goes through the full validate_profile_dict pipeline here,
        exactly as strictly as a fresh import would. A profile that no longer
        passes (hand-edited localStorage, or a rule that got stricter since it
        was saved) is reported as rejected and simply does not exist, rather
        than being repaired into something the user did not check.
        """
        body = _read_json_body(self)
        if body is None:
            return
        sid = _session_id(self)
        if sid is None:
            self._send_json(
                {"ok": False, "error": "missing or malformed browser session id"}, status=400)
            return

        entries = body.get("printers")
        if not isinstance(entries, list):
            self._send_json({"ok": False, "error": "'printers' array is required"}, status=400)
            return

        restored, rejected = [], []
        for item in entries[:printer_store.MAX_PRINTERS_PER_SESSION]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            profile_dict = item.get("profile")
            if not isinstance(profile_dict, dict):
                rejected.append({"key": key, "reason": "no profile object"})
                continue
            try:
                vr = validate_profile_dict(
                    profile_dict, allow_raw_gcode=bool(item.get("allow_raw_gcode", False)))
            except Exception as e:
                rejected.append({"key": key, "reason": "validation failed: %s" % e})
                continue
            if not vr.ok:
                reason = "; ".join(i.message for i in vr.issues if i.severity == "error")
                rejected.append({"key": key, "reason": reason or "failed validation"})
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            # The stored key is preserved so a saved design that references
            # it still resolves; if it is unusable, mint a fresh one rather
            # than dropping the printer.
            if not (key and key not in PRINTER_PROFILES):
                key = printer_store.session_make_key(sid, vr.profile.name)
            try:
                printer_store.session_save(sid, key, vr.profile, meta)
            except ValueError as e:
                rejected.append({"key": key, "reason": str(e)})
                continue
            restored.append(_printer_entry_json(key, vr.profile, custom=True, meta=meta))

        self._send_json({"ok": True, "restored": restored, "rejected": rejected})

    def _handle_printer_delete(self):
        body = _read_json_body(self)
        if body is None:
            return
        sid = _session_id(self)
        key = str(body.get("key") or "")
        if not key or key in PRINTER_PROFILES or not printer_store.session_is_custom(sid, key):
            self._send_json({"ok": False, "error": "'%s' is not a deletable custom printer" % key},
                             status=400)
            return
        if not printer_store.session_delete(sid, key):
            self._send_json({"ok": False, "error": "could not delete '%s'" % key}, status=400)
            return
        self._send_json({"ok": True})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/upload_mesh":
            self._handle_upload_mesh()
            return
        if path == "/api/export_stl":
            self._handle_export_stl()
            return
        if path == "/api/printer/parse":
            self._handle_printer_parse()
            return
        if path == "/api/printer/validate":
            self._handle_printer_validate()
            return
        if path == "/api/printer/save":
            self._handle_printer_save()
            return
        if path == "/api/printer/session":
            self._handle_printer_session()
            return
        if path == "/api/printer/delete":
            self._handle_printer_delete()
            return
        if path == "/api/generate_stream":
            self._handle_generate_stream()
            return
        if path != "/api/generate":
            self.send_error(404, "Not Found")
            return
        # Via _read_json_body, NOT a private copy of the parse: that helper is
        # where the non-finite boundary check lives, and this endpoint having
        # its own inline json.loads is exactly how a quoted "nan" reached the
        # generator while the shared boundary looked like it was guarding it.
        body = _read_json_body(self)
        if body is None:
            return

        # Injected, never read from the client's JSON: _get_profile resolves
        # the printer key against this session's store, and letting a request
        # name its own session would defeat the isolation entirely.
        if isinstance(body, dict):
            body["_session"] = _session_id(self)

        mode = str(body.get("mode", "parametric"))
        try:
            if mode == "mesh_texture":
                result = generate_mesh_texture_design(body)
            elif mode == "surface":
                result = generate_surface_design(body)
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

    def _handle_generate_stream(self):
        """POST /api/generate_stream -- NDJSON progress, then a final result.

        Same request body, same shared JSON boundary, same _session
        injection, and the exact same dispatch (generate_mesh_texture_design
        / generate_design) as /api/generate above -- this is deliberately
        NOT a parallel copy of that endpoint's validation. The only thing
        added is: generation runs on a worker thread while this (request)
        thread polls the "_progress" side channel those functions publish
        into and streams a line whenever the estimated fraction has moved.

        Wire format, one JSON object per "\\n"-terminated line:
            {"type":"progress","stage":"toolpath","frac":0.42}
            {"type":"done","result":{...same dict /api/generate returns...}}
          or, on failure, in place of "done":
            {"type":"error","error":"...","status":400}
        """
        body = _read_json_body(self)
        if body is None:
            return

        if isinstance(body, dict):
            # Both server-owned, never read from client JSON: _session for
            # the same isolation reason as /api/generate, and _progress is
            # unconditionally overwritten here so a client cannot hand in
            # its own dict and have this code publish a live GcodeWriter
            # reference into something it controls.
            body["_session"] = _session_id(self)
            body["_progress"] = {}

        mode = str(body.get("mode", "parametric"))
        progress = body.get("_progress") if isinstance(body, dict) else None

        outcome = {}

        def _worker():
            try:
                if mode == "mesh_texture":
                    outcome["result"] = generate_mesh_texture_design(body)
                elif mode == "surface":
                    outcome["result"] = generate_surface_design(body)
                else:
                    outcome["result"] = generate_design(body)
            except KeyError as e:
                # Unknown filament profile / mesh_id -- same mapping as
                # /api/generate.
                outcome["error"] = str(e).strip('"').strip("'")
                outcome["status"] = 400
            except ValueError as e:
                outcome["error"] = str(e)
                outcome["status"] = 400
            except Exception as e:
                outcome["error"] = "Generation failed: %s" % e
                outcome["status"] = 500

        worker = threading.Thread(target=_worker, name="generate_stream", daemon=True)
        worker.start()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        last_frac = 0.0
        last_emit = 0.0
        client_gone = False
        # Poll every 50ms so the worker finishing is noticed promptly, but
        # only ever WRITE a line every ~200ms -- polling fast and emitting
        # slow is what keeps this from spamming a line per iteration while
        # still closing the response quickly once generation is done.
        while worker.is_alive():
            worker.join(timeout=0.05)
            if client_gone or progress is None:
                continue
            now = time.monotonic()
            if now - last_emit < 0.2:
                continue
            frac = _progress_fraction(progress, last_frac)
            if frac <= last_frac:
                continue
            last_frac = frac
            last_emit = now
            stage = progress.get("stage") or "toolpath"
            try:
                self._write_ndjson_line(
                    {"type": "progress", "stage": stage, "frac": round(frac, 4)})
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                # Client went away. The worker cannot be safely interrupted
                # mid-write, so let it run to completion in the background;
                # just stop trying to talk to a socket that no longer exists.
                client_gone = True

        worker.join()
        if client_gone:
            return

        if "error" in outcome:
            try:
                self._write_ndjson_line(
                    {"type": "error", "error": outcome["error"], "status": outcome["status"]})
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                pass
            return

        try:
            self._write_ndjson_line({"type": "done", "result": outcome["result"]})
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def _handle_export_stl(self):
        # Shared boundary, same reason as /api/generate above.
        body = _read_json_body(self)
        if body is None:
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


# Loopback, deliberately. An empty host binds every interface, which put the
# whole API (printer import, G-code generation, mesh upload) on the LAN for
# anyone who could reach the port. This tool has no authentication.
_DEFAULT_BIND = "127.0.0.1"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Wildcard binds accept on every interface, so they are not themselves an
# address you can open -- printing "http://0.0.0.0:10000" hands the reader a
# URL that does not work anywhere.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


def _startup_line(host, port):
    """The one line telling the operator where the server actually is.

    It used to say "localhost" regardless of the bind, which is right for the
    default and wrong the moment TRIDENT_BIND is set: on a hosted instance it
    named an address that resolves to the container itself. Pure function of
    (host, port) so the wording is testable without a socket.
    """
    path = "/viewer/index.html"
    if host in _LOOPBACK_HOSTS:
        return ("Trident design server on http://localhost:%d%s (Ctrl-C to stop)\n"
                % (port, path))
    if host in _WILDCARD_HOSTS:
        return ("Trident design server listening on %s:%d (all interfaces).\n"
                "Open it at this host's own address, or the URL your platform\n"
                "assigns, path %s. (Ctrl-C to stop)\n" % (host, port, path))
    return ("Trident design server on http://%s:%d%s (Ctrl-C to stop)\n"
            % (host, port, path))


def _bind_config(env=None):
    """Resolve (host, port) from the environment.

    Exposing the server is opt-in and explicit: TRIDENT_BIND must be set to
    widen it, because the safe value is the one you get by forgetting. A
    hosted deployment needs 0.0.0.0, and that is a real change in what the
    process is -- there is no login, so every visitor can import printer
    configs and generate G-code. The session store keeps their printers
    apart, but it is not an access control.

    PORT is honoured because Render (and most PaaS) assign it at runtime; an
    unusable value falls back to the default rather than crashing on boot,
    since a design tool that will not start is worse than one on a surprising
    port. Pure function of ``env`` so tests need no live socket.
    """
    env = os.environ if env is None else env
    host = (env.get("TRIDENT_BIND") or "").strip() or _DEFAULT_BIND
    raw_port = (env.get("PORT") or "").strip()
    port = PORT
    if raw_port:
        try:
            candidate = int(raw_port)
        except ValueError:
            candidate = -1
        if 1 <= candidate <= 65535:
            port = candidate
        else:
            sys.stderr.write(
                "WARNING: PORT=%r is not a usable port; falling back to %d.\n"
                % (raw_port, PORT))
    return host, port


def main():
    host, port = _bind_config()
    try:
        httpd = _Server((host, port), Handler)
    except OSError as e:
        sys.stderr.write(
            "ERROR: could not bind %s:%d - is another server already running? (%s)\n"
            % (host, port, e))
        return 1
    if host not in _LOOPBACK_HOSTS:
        # Loud, because this is the moment the tool stops being private. Said
        # on every start, not once: an operator who scrolls past it is exactly
        # the person who needs to see it again.
        sys.stdout.write(
            "WARNING: bound to %s - this server is reachable beyond this machine.\n"
            "         It has NO authentication: anyone who can reach it can import\n"
            "         printer configs and generate G-code. Do not point it at a\n"
            "         printer you care about on an untrusted network.\n" % host)
    sys.stdout.write(_startup_line(host, port))
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
