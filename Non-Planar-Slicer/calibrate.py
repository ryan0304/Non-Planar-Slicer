#!/usr/bin/env python3
"""Calibration print suite for the Voron Trident.

Generates small, machine-safe G-code files into examples/cal/ (the count has
grown over time -- main() is the authority on what actually gets written):

  cal_first_layer.gcode
      Single-layer spiral disk, 40 mm diameter, squish 0.75.
      Print at 205 C / 60 C bed; scrub the live-Z while it runs.

  cal_spacing_ladder.gcode
      Three single-layer disks at first-layer spacing factors 1.15/1.25/1.35.
      Pick the leftmost disk whose fill lines lie side by side without ridging.

  cal_flow_ladder.gcode
      Single-wall cylinder r=25 mm, 30 mm tall.  Flow multiplier steps
      0.90 / 0.95 / 1.00 / 1.05 / 1.10 in 6 mm Z bands.
      Find the band whose wall looks most uniform and use that as --flow.

  cal_zamp_ladder.gcode
      Cylinder r=25 mm, 36 mm tall. Non-planar z_amp steps 0/0.3/0.6/0.95 mm
      in 9 mm Z bands (z_waves 5).  Spot where the surface quality degrades.

  cal_base_adhesion.gcode
      Circle, base radius 25 mm, height 15 mm, flaring from 0.5x to 1.0x
      radius (top-heavy on purpose).  2-layer solid base + 3-loop brim sized
      to the WALL's t=0 footprint (r=12.5, not the raw r=25 outline) --
      commit 6332a6a made that correct but cut bed contact 75% (491 vs
      1963 mm^2) on exactly this kind of narrowing silhouette.  Watch
      whether the smaller base still holds the part down.

IMPORTANT: calibrate with the filament you will actually print with::

    python calibrate.py --filament "R3D PETG"   # temps/flow/limits from OrcaSlicer
    python calibrate.py                          # generic PLA fallback (205C/60C)
    python analyze.py examples/cal/cal_first_layer.gcode
    python analyze.py examples/cal/cal_flow_ladder.gcode
    python analyze.py examples/cal/cal_zamp_ladder.gcode
    python analyze.py examples/cal/cal_base_adhesion.gcode
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys

from trident_gcode import TRIDENT, GcodeWriter
from trident_gcode.paths import SpiralSpec, circle
from trident_gcode.generators import build_continuous_spiral


# ---------------------------------------------------------------------------
_CAL_DIR = os.path.join("examples", "cal")

# Generic PLA fallback, used only when no --filament is given.
_BASE_WRITER_KWARGS = dict(
    bed_temp=60.0,
    nozzle_temp=205.0,
    material="PLA",
    print_speed=40.0,
    first_layer_speed=18.0,
)

# Filled in by main() from --filament (OrcaSlicer profile import).
_FILAMENT_KWARGS: dict = {}


def _new_writer(**extra) -> GcodeWriter:
    kw = dict(_BASE_WRITER_KWARGS)
    kw.update(_FILAMENT_KWARGS)      # Orca temps/flow/limits override the fallback
    kw.update(extra)
    return GcodeWriter(profile=TRIDENT, **kw)


# ---------------------------------------------------------------------------
# (a) First-layer disk
# ---------------------------------------------------------------------------

def cal_first_layer() -> str:
    """Single-layer spiral disk, 40 mm diameter, squish 0.75."""
    layer_height = 0.20
    line_width = 0.45
    diameter = 40.0
    radius = diameter / 2.0

    writer = _new_writer(layer_height=layer_height, line_width=line_width)

    # Build with base_layers=1, height=1 layer — the entire print is one base
    # disk.  We use a tiny wall height so the wall barely runs after the base.
    # The cleanest way: spec height = 1 layer, base_layers = 1 gives us the
    # full base disk fill + a single priming loop wall turn.
    spec = SpiralSpec(
        base_radius=radius,
        height=layer_height,          # only 1 wall turn
        layer_height=layer_height,
        points_per_turn=240,
        z_amp=0.0,
    )
    shape = circle(radius)
    build_continuous_spiral(
        writer, spec, shape=shape,
        first_layer_squish=0.75,
        first_layer_spacing_factor=1.25,
        first_layer_flow=1.0,
        base_layers=1,
        brim_loops=0,
    )
    out = os.path.join(_CAL_DIR, "cal_first_layer.gcode")
    writer.save(out)
    return out


# ---------------------------------------------------------------------------
# (a2) First-layer spacing ladder
# ---------------------------------------------------------------------------

_SPACING_FACTORS = [1.15, 1.25, 1.35]
_SPACING_DISK_R = 12.0          # mm radius per mini disk
_SPACING_PITCH = 30.0           # mm between disk centres


def cal_spacing_ladder() -> str:
    """Three small single-layer disks at first-layer spacing factors 1.15/1.25/1.35.

    Print and compare: pick the leftmost disk whose fill lines lie side by side
    without ridging up between lines. Use that factor as
    --first-layer-spacing-factor (CLI) / the designer's spacing setting.
    Left disk = 1.15 (tightest), right disk = 1.35 (widest); 1.25 (default) sits
    in the middle to bracket it.
    """
    from trident_gcode.paths import circle
    from trident_gcode.generators.base_fill import base_disk_points

    layer_height = 0.20
    line_width = 0.45
    squish = 0.75
    z0 = squish * layer_height

    writer = _new_writer(layer_height=layer_height, line_width=line_width)
    writer.header()
    cx0, cy0 = TRIDENT.bed_center

    for idx, factor in enumerate(_SPACING_FACTORS):
        cx = cx0 + (idx - 1) * _SPACING_PITCH
        spacing = line_width / squish * factor
        pts = base_disk_points(circle(_SPACING_DISK_R), spacing,
                               points_per_turn=200, outward=True)
        writer.comment(f"CAL DISK spacing_factor={factor:.2f} (x={cx:.0f})")
        sx, sy = cx + pts[0][0], cy0 + pts[0][1]
        writer.safe_lift(z0 + 5.0)
        writer.travel(sx, sy, z0 + 5.0)
        writer.travel(sx, sy, z0)
        writer.unretract()
        for (bx, by) in pts:
            writer.extrude_to(cx + bx, cy0 + by, z0,
                              speed=writer.first_layer_speed,
                              layer_height_override=layer_height)
        writer.retract()
    writer.footer()

    out = os.path.join(_CAL_DIR, "cal_spacing_ladder.gcode")
    writer.save(out)
    return out


# ---------------------------------------------------------------------------
# (b) Flow ladder
# ---------------------------------------------------------------------------

# Flow steps and band height (mm).
_FLOW_BANDS = [0.90, 0.95, 1.00, 1.05, 1.10]
_BAND_HEIGHT_MM = 6.0    # height per flow band


def _flow_callback_for_bands(
    spec_height: float,
    bands: list[float],
    band_height: float,
    writer: GcodeWriter,
) -> callable:
    """Return a flow_callback(t) that also emits band-start comments.

    ``t`` is the height fraction (0..1).  The comment is emitted the first time
    t crosses each band boundary, using the writer's comment() method.

    Because flow_callback is called point-by-point in build_continuous_spiral,
    we track which band was last active via a mutable cell.
    """
    last_band: list[int] = [-1]

    def cb(t: float) -> float:
        z = t * spec_height
        band_idx = min(int(z / band_height), len(bands) - 1)
        if band_idx != last_band[0]:
            last_band[0] = band_idx
            flow = bands[band_idx]
            writer.comment(f"CAL BAND flow={flow:.2f}")
        return bands[band_idx]

    return cb


def cal_flow_ladder() -> str:
    """Single-wall cylinder, flow steps 0.90-1.10 in 6 mm bands."""
    layer_height = 0.20
    line_width = 0.45
    radius = 25.0
    total_height = _BAND_HEIGHT_MM * len(_FLOW_BANDS)  # 30 mm

    writer = _new_writer(layer_height=layer_height, line_width=line_width)
    spec = SpiralSpec(
        base_radius=radius,
        height=total_height,
        layer_height=layer_height,
        points_per_turn=240,
        z_amp=0.0,
    )
    shape = circle(radius)
    cb = _flow_callback_for_bands(total_height, _FLOW_BANDS, _BAND_HEIGHT_MM, writer)
    build_continuous_spiral(
        writer, spec, shape=shape,
        first_layer_squish=0.75,
        first_layer_spacing_factor=1.25,
        first_layer_flow=1.0,
        base_layers=1,
        brim_loops=0,
        flow_callback=cb,
    )
    out = os.path.join(_CAL_DIR, "cal_flow_ladder.gcode")
    writer.save(out)
    return out


# ---------------------------------------------------------------------------
# (c) Z-amp ladder
# ---------------------------------------------------------------------------

# User-revised 2026-07-06: z-amp < 1mm max.
_ZAMP_BANDS = [0.0, 0.3, 0.6, 0.95]  # z_amp per band (mm)
_ZAMP_BAND_HEIGHT_MM = 9.0             # height per z-amp band
_ZAMP_WAVES = 5


def cal_zamp_ladder() -> str:
    """Cylinder, z_amp steps 0/1/2/3 mm in 9 mm Z bands."""
    layer_height = 0.20
    line_width = 0.45
    radius = 25.0
    total_height = _ZAMP_BAND_HEIGHT_MM * len(_ZAMP_BANDS)  # 36 mm

    # z_amp_envelope(t): returns the amplitude scale (0..3 mm) for the given
    # height fraction.  The envelope replaces the built-in ramp.  For the first
    # band (z_amp=0) the nozzle rides flat on the bed, so no ramp needed; for
    # higher bands we want full amplitude immediately (we're well above the bed).
    def z_amp_envelope(t: float) -> float:
        z = t * total_height
        band_idx = min(int(z / _ZAMP_BAND_HEIGHT_MM), len(_ZAMP_BANDS) - 1)
        return _ZAMP_BANDS[band_idx]   # absolute mm, divided by z_amp=1 below

    # We set z_amp=1.0 in the spec so the envelope directly gives mm of amplitude.
    writer = _new_writer(layer_height=layer_height, line_width=line_width)

    # Track last-emitted band for the comment, using a side-channel via the writer.
    _last_band: list[int] = [-1]
    _orig_extrude = writer.extrude_to.__func__ if hasattr(writer.extrude_to, '__func__') else None

    # We'll emit band comments via a wrapper around build_continuous_spiral:
    # patch the writer to emit the comment on band change before each extrude.
    spec = SpiralSpec(
        base_radius=radius,
        height=total_height,
        layer_height=layer_height,
        points_per_turn=240,
        z_amp=1.0,              # envelope provides the actual mm
        z_waves=_ZAMP_WAVES,
        z_twist_turns=0.0,
        z_amp_ramp=0.0,         # envelope overrides ramp
        z_amp_envelope=z_amp_envelope,
    )
    shape = circle(radius)

    # Intercept extrude_to to inject band-start comments.  We track by z_amp
    # band (derived from current Z, which we can infer from p.z baked in).
    # Simpler: use the flow_callback hook (which receives t) just for comments.
    comment_last: list[int] = [-1]

    def comment_callback(t: float) -> float:
        z = t * total_height
        band_idx = min(int(z / _ZAMP_BAND_HEIGHT_MM), len(_ZAMP_BANDS) - 1)
        if band_idx != comment_last[0]:
            comment_last[0] = band_idx
            amp = _ZAMP_BANDS[band_idx]
            writer.comment(f"CAL BAND z_amp={amp:.1f}mm")
        return 1.0  # no flow change

    build_continuous_spiral(
        writer, spec, shape=shape,
        first_layer_squish=0.75,
        first_layer_spacing_factor=1.25,
        first_layer_flow=1.0,
        base_layers=1,
        brim_loops=0,
        flow_callback=comment_callback,
    )

    out = os.path.join(_CAL_DIR, "cal_zamp_ladder.gcode")
    writer.save(out)
    return out


# ---------------------------------------------------------------------------
# (d) Base adhesion (narrowing-silhouette base + brim, print-test)
# ---------------------------------------------------------------------------

_ADHESION_BASE_RADIUS = 25.0    # mm, the WALL's outline radius (top, t=1)
_ADHESION_HEIGHT = 15.0         # mm


def _adhesion_radius_envelope(t: float) -> float:
    """0.5x at the base rising linearly to 1.0x at the top.

    The wall starts at half radius and flares to full -- 2x wider at the top
    than at its base -- so the part is deliberately top-heavy: the stress
    case for whether a smaller base can hold it down.
    """
    return 0.5 + 0.5 * t


def cal_base_adhesion() -> str:
    """Flaring circle, 2-layer base + 3-loop brim sized to the wall's own
    t=0 footprint (r=12.5), not the raw r=25 outline.

    Commit 6332a6a fixed the base/brim/skirt to follow the WALL's own t=0
    cross-section instead of the raw ``shape`` outline -- correct, because
    the old behaviour joined a full-radius base to a half-radius wall start
    with a single EXTRUDING move dragging ~19x a normal move's plastic 15 mm
    across the finished disk. But on a narrowing silhouette like this one,
    the fix also shrinks bed contact a lot: measured 491 mm^2 (r=12.5 base)
    versus 1963 mm^2 (old r=25 base) -- 75% less plastic on the plate. The
    3-loop brim claws some of it back (first-layer reach 12.50 -> 14.75 mm,
    contact 491 -> 683 mm^2), but nobody has print-tested whether that is
    enough to keep a top-heavy, flaring part stuck down. z_amp is 0 -- this
    is an adhesion test, not a non-planar one.
    """
    layer_height = 0.30
    line_width = 0.45

    writer = _new_writer(layer_height=layer_height, line_width=line_width)
    spec = SpiralSpec(
        base_radius=_ADHESION_BASE_RADIUS,
        height=_ADHESION_HEIGHT,
        layer_height=layer_height,
        points_per_turn=240,
        z_amp=0.0,
        z_waves=0,
        radius_envelope=_adhesion_radius_envelope,
    )
    shape = circle(_ADHESION_BASE_RADIUS)

    # One-shot banner, emitted via the flow_callback hook (same technique the
    # ladder cases use for their per-band comments) so it lands right where
    # the wall spiral starts -- after the base/brim, before the first
    # extrude. Returns 1.0 (no flow change): this case has no flow ladder.
    banner_done = [False]

    def banner_callback(t: float) -> float:
        if not banner_done[0]:
            banner_done[0] = True
            writer.comment(
                "CAL BASE ADHESION: base r=12.5mm vs old r=25.0mm "
                "(pre-6332a6a) -- 75% less bed contact, 491mm^2 vs 1963mm^2 "
                "(683mm^2 with the 3-loop brim). Watch the first 2-3mm for "
                "lift, and the point where the wall starts leaning outward."
            )
        return 1.0

    build_continuous_spiral(
        writer, spec, shape=shape,
        first_layer_squish=0.75,
        first_layer_spacing_factor=1.25,
        first_layer_flow=1.0,
        base_layers=2,
        brim_loops=3,
        flow_callback=banner_callback,
    )
    out = os.path.join(_CAL_DIR, "cal_base_adhesion.gcode")
    writer.save(out)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Generate calibration prints")
    ap.add_argument("--filament", default=None,
                    help="OrcaSlicer filament profile to calibrate with "
                         "(temps, flow ratio, volumetric limit, fan, retraction). "
                         "STRONGLY recommended - calibrate with what you'll print.")
    args = ap.parse_args()

    if args.filament:
        from trident_gcode.orca import FilamentSettings
        try:
            fs = FilamentSettings.from_orca(args.filament)
        except KeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        _FILAMENT_KWARGS.update(fs.writer_kwargs())
        print(f"Calibrating for: {fs.name}  "
              f"({_FILAMENT_KWARGS.get('nozzle_temp', '?'):g}C nozzle / "
              f"{_FILAMENT_KWARGS.get('bed_temp', '?'):g}C bed, "
              f"material {_FILAMENT_KWARGS.get('material', '?')})")
    else:
        print("NOTE: no --filament given; using generic PLA fallback (205C/60C).")
        print("      Calibrate with your real filament: "
              "python calibrate.py --filament \"R3D PETG\"")
    print("")

    os.makedirs(_CAL_DIR, exist_ok=True)

    jobs = [
        ("cal_first_layer", cal_first_layer),
        ("cal_spacing_ladder", cal_spacing_ladder),
        ("cal_flow_ladder", cal_flow_ladder),
        ("cal_zamp_ladder", cal_zamp_ladder),
        ("cal_base_adhesion", cal_base_adhesion),
    ]

    results = []
    any_fail = False

    for name, fn in jobs:
        try:
            out = fn()
        except Exception as exc:
            print(f"  FAILED {name}: {exc}")
            any_fail = True
            results.append((name, None, str(exc)))
            continue
        results.append((name, out, None))
        print(f"  wrote {out}")

    # Run analyze.py on each file (exit 0 = clean).
    analyze = os.path.join(os.path.dirname(__file__), "analyze.py")
    print("")
    print(f"{'file':<30}  {'est time':>10}  {'top Z':>8}  status")
    print("-" * 62)
    for name, out, err in results:
        if out is None:
            print(f"{name:<30}  {'---':>10}  {'---':>8}  FAILED: {err}")
            continue
        try:
            proc = subprocess.run(
                [sys.executable, analyze, out],
                capture_output=True, text=True,
            )
            status = "OK" if proc.returncode == 0 else f"WARN (exit {proc.returncode})"
            # Extract est. print time and top Z from analyze output.
            time_str = "?"
            topz_str = "?"
            for line in proc.stdout.splitlines():
                if "est. print time" in line:
                    time_str = line.split(":", 1)[-1].strip()
                if "height (Z)" in line:
                    # e.g. "  height (Z)      : 5.0 mm  (Z 0.1-5.2)"
                    rhs = line.split(":", 1)[-1].strip()
                    topz_str = rhs.split()[0]  # "5.0"
        except Exception as exc:
            status = f"analyze error: {exc}"
            time_str = "?"
            topz_str = "?"
        print(f"{name:<30}  {time_str:>10}  {topz_str:>8}  {status}")
    print("-" * 62)
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
