#!/usr/bin/env python3
"""Functional (non-byte-exact) tests for build_profile_spiral()'s Zone
Overrides support (trident_gcode/generators/profile_spiral.py).

Plain ``python tools/test_profile_spiral_zones.py``: prints PASS/FAIL per
case, exits non-zero on any failure. Mirrors tools/test_hybrid.py's style.

This is the CONTOUR-STACK counterpart to paths.py's spiral_path() zone
support (regression-tested byte-exact in ref_zone_overrides.gcode /
ref_zone_overlap.gcode) -- build_profile_spiral is the generator hybrid and
mesh-hybrid walls use, which had no Zone Override support at all until now.
check_regression.py already proves zones=None is byte-identical to before
this parameter existed; this file checks the ACTIVE-zone behavior, which
check_regression.py cannot (no existing reference passes zones=...).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.gcode import GcodeWriter
from trident_gcode.generators.profile_spiral import build_profile_spiral
from trident_gcode.paths import ZoneOverride, circle
from trident_gcode.profile import PrinterProfile
from trident_gcode.profile_stack import stack_from_shape


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


_MOVE_RE = re.compile(r"^G1\b.*\bX([-\d.]+)\s+Y([-\d.]+)")


def _new_writer(profile, layer_height=0.5):
    return GcodeWriter(
        profile=profile, line_width=0.45, layer_height=layer_height,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def _run(zones=None, **kwargs):
    """Build a small circular wall (radius 5mm, height 10mm, 0.5mm layers,
    60 points/turn -- 21 rings, ~1260 wall points) and return the parsed
    (x, y) of every G1 move that carries both X and Y, in emission order.
    """
    profile = PrinterProfile()
    writer = _new_writer(profile)
    shape = circle(5.0)
    contours = stack_from_shape(shape, 5.0, 10.0, 0.5, 60)
    heights = [i * 0.5 for i in range(len(contours))]
    args = dict(points_per_turn=60, center=(profile.bed_center))
    args.update(kwargs)
    build_profile_spiral(writer, contours, heights, zones=zones, **args)
    text = writer.text()
    # Only the wall spiral's own G1 moves -- the two travel moves + unretract
    # emitted just before it also carry X/Y and would otherwise misalign
    # every turn-to-turn (index i vs i-points_per_turn) comparison below by
    # a constant few-point offset.
    wall_text = text.split("; wall spiral", 1)[1]
    pts = []
    for line in wall_text.splitlines():
        m = _MOVE_RE.match(line)
        if m:
            pts.append((float(m.group(1)), float(m.group(2))))
    return pts


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# ---------------------------------------------------------------------------
# 1. zones=None and zones=[] are both byte-identical to omitting the param.
# ---------------------------------------------------------------------------
def test_no_zones_is_unchanged():
    baseline = _run(zones=None)
    omitted = _run()
    check(baseline == omitted,
          "zones=None matches the parameter being omitted entirely",
          f"{len(baseline)} vs {len(omitted)} points")
    empty = _run(zones=[])
    check(baseline == empty,
          "zones=[] (empty list) is byte-identical to zones=None",
          f"{len(baseline)} vs {len(empty)} points")


# ---------------------------------------------------------------------------
# 2. A texture zone displaces points INSIDE [t_lo, t_hi] and leaves every
#    point strictly outside the band (past its blend ramp) untouched.
# ---------------------------------------------------------------------------
def test_texture_zone_contained_to_its_band():
    baseline = _run(zones=None)
    zone = ZoneOverride(t_lo=0.3, t_hi=0.5, blend=0.02,
                         r_pattern="vwave", r_amp=2.0)
    zoned = _run(zones=[zone])
    check(len(baseline) == len(zoned),
          "texture zone: point count unchanged", f"{len(baseline)} vs {len(zoned)}")

    n = len(baseline)
    any_displaced_inside = False
    max_outside_diff = 0.0
    for i, (b, z) in enumerate(zip(baseline, zoned)):
        t = i / max(n - 1, 1)
        d = _dist(b, z)
        # Well outside the band + its blend ramp: must be an EXACT match.
        if t < 0.3 - 0.02 - 1e-9 or t > 0.5 + 0.02 + 1e-9:
            max_outside_diff = max(max_outside_diff, d)
        # Solidly inside (past the ramp on both sides): displacement expected.
        elif 0.3 + 0.02 < t < 0.5 - 0.02:
            if d > 1e-6:
                any_displaced_inside = True

    check(max_outside_diff < 1e-9,
          "texture zone: every point outside [t_lo-blend, t_hi+blend] is "
          "an EXACT match to the no-zone baseline", f"max diff {max_outside_diff}")
    check(any_displaced_inside,
          "texture zone: at least one point solidly inside the band is "
          "measurably displaced from the no-zone baseline")


# ---------------------------------------------------------------------------
# 3. A zone's own r_pattern works even when the GLOBAL r_pattern is None --
#    proves normals are computed whenever a zone needs them, not gated on
#    the global texture alone (the bug this task's own port must avoid).
# ---------------------------------------------------------------------------
def test_zone_texture_active_with_no_global_pattern():
    baseline = _run(zones=None)  # r_pattern defaults to None
    zone = ZoneOverride(t_lo=0.4, t_hi=0.6, blend=0.02,
                         r_pattern="hwave", r_amp=1.5)
    zoned = _run(zones=[zone])
    n = len(baseline)
    displaced = any(_dist(b, z) > 1e-6
                     for i, (b, z) in enumerate(zip(baseline, zoned))
                     if 0.4 + 0.02 < i / max(n - 1, 1) < 0.6 - 0.02)
    check(displaced,
          "a zone's own r_pattern displaces points even when the global "
          "r_pattern is None")


# ---------------------------------------------------------------------------
# 4. Overlapping zones: combined displacement never exceeds the deepest
#    single zone's own r_amp (weight-normalization proof).
# ---------------------------------------------------------------------------
def test_overlapping_zones_never_exceed_max_r_amp():
    z1 = ZoneOverride(t_lo=0.30, t_hi=0.55, blend=0.02, r_pattern="vwave", r_amp=1.0)
    z2 = ZoneOverride(t_lo=0.40, t_hi=0.65, blend=0.02, r_pattern="hwave", r_amp=3.0)
    baseline = _run(zones=None)
    zoned = _run(zones=[z1, z2])
    max_r_amp = max(z1.r_amp, z2.r_amp)
    n = len(baseline)
    worst = 0.0
    for i, (b, z) in enumerate(zip(baseline, zoned)):
        t = i / max(n - 1, 1)
        if 0.42 < t < 0.53:  # solidly inside BOTH bands' ramps
            worst = max(worst, _dist(b, z))
    check(worst <= max_r_amp + 1e-6,
          "overlapping zones: combined displacement never exceeds the "
          f"deepest zone's own r_amp ({max_r_amp})", f"worst observed: {worst}")


# ---------------------------------------------------------------------------
# 5. xy_twist_turns zone override: the extra rotation applied at height
#    fraction t must equal -zone_twist_integral(zone, t) * 2*pi (matching
#    spiral_path()'s own sign/scale convention exactly), checked directly
#    against the actual output geometry at several t values spanning
#    before/inside/after the zone -- proves the WIRING (sign, scale, which
#    function gets called) is correct. zone_twist_integral() itself is
#    already continuous by construction (closed-form integral of a
#    trapezoid) and is untouched, shared code -- not re-proven here.
# ---------------------------------------------------------------------------
def test_xy_twist_zone_matches_predicted_rotation():
    import math
    from trident_gcode.paths import zone_twist_integral

    # Points come back in bed-absolute coordinates (cx+px, cy+py) -- subtract
    # the same center _run() uses so atan2 reads the wall's own LOCAL angle;
    # otherwise a local rotation is swamped by the bed-center offset and
    # reads as near-zero (the print area does not allow centering at the
    # origin directly, so this subtraction is done here instead).
    cx, cy = PrinterProfile().bed_center
    zone = ZoneOverride(t_lo=0.3, t_hi=0.6, blend=0.05, xy_twist_turns=2.0)
    baseline = _run(zones=None, xy_twist_turns=0.0)
    zoned = _run(zones=[zone], xy_twist_turns=0.0)
    n = len(baseline)
    ppt = 60
    n_rings = n // ppt

    worst_err = 0.0
    for ring in (2, 6, 9, 11, 15, 18):
        if ring >= n_rings:
            continue
        j = 0  # angle index within the ring; frac = j/ppt = 0.0
        idx = ring * ppt + j
        t = ring / max(n_rings - 1, 1)  # matches the generator's own t at frac=0
        bx, by = baseline[idx]
        zx, zy = zoned[idx]
        base_theta = math.atan2(by - cy, bx - cx)
        zoned_theta = math.atan2(zy - cy, zx - cx)
        actual_delta = math.atan2(math.sin(zoned_theta - base_theta),
                                   math.cos(zoned_theta - base_theta))
        predicted_extra = ((zone.xy_twist_turns - 0.0)
                           * zone_twist_integral(zone, t))
        predicted_delta = math.atan2(
            math.sin(-predicted_extra * 2.0 * math.pi),
            math.cos(-predicted_extra * 2.0 * math.pi))
        err = abs(math.atan2(math.sin(actual_delta - predicted_delta),
                              math.cos(actual_delta - predicted_delta)))
        worst_err = max(worst_err, err)
    # Tolerance, not exact equality: G-code coordinates round-trip through
    # fixed-decimal text (a few 1e-4 mm), which shows up as ~1e-5 rad of
    # atan2 noise at this radius -- real, not a bug in the rotation math.
    check(worst_err < 1e-4,
          "xy_twist zone: actual point rotation at each sampled ring matches "
          "-zone_twist_integral(zone, t) * 2*pi exactly (same convention "
          "spiral_path() uses)", f"worst angular error {worst_err} rad")


# ---------------------------------------------------------------------------
# 6. An unknown r_pattern name on a ZONE fails the same way a bad global one
#    does -- fast, before any point is emitted.
# ---------------------------------------------------------------------------
def test_unknown_zone_pattern_raises():
    profile = PrinterProfile()
    writer = _new_writer(profile)
    shape = circle(5.0)
    contours = stack_from_shape(shape, 5.0, 10.0, 0.5, 60)
    heights = [i * 0.5 for i in range(len(contours))]
    bad_zone = ZoneOverride(t_lo=0.2, t_hi=0.4, r_pattern="not_a_real_pattern")
    try:
        build_profile_spiral(writer, contours, heights, points_per_turn=60,
                              center=profile.bed_center, zones=[bad_zone])
        check(False, "unknown zone r_pattern: raises ValueError", "no exception raised")
    except ValueError as e:
        check("not_a_real_pattern" in str(e),
              "unknown zone r_pattern: raises ValueError naming the bad pattern",
              str(e))


def main() -> int:
    test_no_zones_is_unchanged()
    test_texture_zone_contained_to_its_band()
    test_zone_texture_active_with_no_global_pattern()
    test_overlapping_zones_never_exceed_max_r_amp()
    test_xy_twist_zone_matches_predicted_rotation()
    test_unknown_zone_pattern_raises()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
