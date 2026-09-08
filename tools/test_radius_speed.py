#!/usr/bin/env python3
"""Functional tests for radius-based speed compensation
(SpiralSpec.radius_speed_comp / paths.radius_speed_scale, wired through
continuous_spiral.py and profile_spiral.py).

Plain ``python tools/test_radius_speed.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors tools/test_profile_spiral_zones.py's
style.

WHY THIS FILE AND NOT ANOTHER regression_ref/ REFERENCE
-------------------------------------------------------
check_regression.py proves the DEFAULT (feature off) stays byte-identical --
every one of its 14 references is generated with radius_speed_comp unset, so
they collectively lock the no-op. What they cannot show is that the feature,
when ON, does the right thing in the right DIRECTION: a byte comparison
against a file this change itself produced would only prove the output is
stable, not that a narrow section is actually slower than a wide one. That is
a property of the emitted F values, so it is asserted here directly.

This file also re-proves the default no-op in memory (case 1) rather than
trusting that check_regression.py covers it, since the two run
independently.

NOT PRINT-VALIDATED. These cases lock the formula's shape, sign and bounds.
Whether 0.5 is the right floor, or a linear radius ratio the right model of
cooling time, needs a real print on real hardware -- nobody has run one.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.gcode import GcodeWriter
from trident_gcode.generators.continuous_spiral import build_continuous_spiral
from trident_gcode.generators.profile_spiral import build_profile_spiral
from trident_gcode.paths import (_MIN_SPEED_SCALE, SpiralSpec, ZoneOverride,
                                 circle, radius_speed_scale, spiral_path, star)
from trident_gcode.profile import PrinterProfile
from trident_gcode.profile_stack import stack_from_shape

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


# G1 moves that carry X, Y and F. The wall spiral emits X/Y/Z/E/F on every
# move (gcode.py's _move), so this matches every wall bead and nothing else.
_MOVE_RE = re.compile(
    r"^G1 X(-?[\d.]+) Y(-?[\d.]+) Z(-?[\d.]+) E(-?[\d.]+) F([\d.]+)\s*$")

# The star used throughout: radius 30, 5 points, depth 0.6 -> radius swings
# between 30 (tips) and 30 * (1 - 0.6) = 18 (valleys). A 0.6 ratio is inside
# the compensation band (above _MIN_SPEED_SCALE = 0.5), so the valleys land on
# the sloped part of the curve rather than pinned to the floor -- which is
# what makes "smaller radius => strictly lower F" checkable.
_R = 30.0
_STAR = star(_R, 5, 0.6)


def _new_writer(profile, layer_height=0.5):
    return GcodeWriter(
        profile=profile, line_width=0.45, layer_height=layer_height,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def _spec(comp: bool, zones=None) -> SpiralSpec:
    return SpiralSpec(
        base_radius=_R, height=10.0, layer_height=0.5, points_per_turn=60,
        radius_speed_comp=comp, zones=zones,
    )


def _wall_moves(text: str) -> list[tuple[float, float, float]]:
    """(radius from the bed centre, F, z) for every wall bead, in order.

    Only the moves after the "wall spiral"/first turn: the plain (no-adhesion)
    generator has no such marker, so the caller slices instead -- see _run.
    """
    profile = PrinterProfile()
    cx, cy = profile.bed_center
    out = []
    for line in text.splitlines():
        m = _MOVE_RE.match(line)
        if m:
            x, y, z, _e, f = (float(g) for g in m.groups())
            out.append((math.hypot(x - cx, y - cy), f, z))
    return out


def _run_continuous(comp: bool, zones=None) -> str:
    profile = PrinterProfile()
    writer = _new_writer(profile)
    build_continuous_spiral(writer, _spec(comp, zones), shape=_STAR)
    return writer.text()


def _run_profile(ref: float | None) -> str:
    profile = PrinterProfile()
    writer = _new_writer(profile)
    contours = stack_from_shape(_STAR, _R, 10.0, 0.5, 60)
    heights = [i * 0.5 for i in range(len(contours))]
    build_profile_spiral(writer, contours, heights, points_per_turn=60,
                         radius_speed_ref=ref)
    return writer.text()


# ---------------------------------------------------------------------------
# 1. Default OFF is a bit-exact no-op
# ---------------------------------------------------------------------------

def test_default_is_noop() -> None:
    check(SpiralSpec().radius_speed_comp is False,
          "SpiralSpec.radius_speed_comp defaults to False")

    off = _run_continuous(False)
    check(off == _run_continuous(False),
          "continuous_spiral output is deterministic")

    # radius_speed_comp=False must produce EXACTLY what the field's absence
    # produced -- the same guarantee check_regression.py's 14 references make
    # from disk, re-proved here in memory and independently of them.
    profile = PrinterProfile()
    writer = _new_writer(profile)
    build_continuous_spiral(
        writer,
        SpiralSpec(base_radius=_R, height=10.0, layer_height=0.5,
                   points_per_turn=60),
        shape=_STAR)
    check(writer.text() == off,
          "radius_speed_comp=False is identical to not passing the field")

    pts = spiral_path(_spec(False))
    check(all(p.speed_scale is None for p in pts),
          "every PathPoint.speed_scale is None when the feature is off")

    check(_run_profile(None) == _run_profile(None),
          "profile_spiral output is deterministic")


# ---------------------------------------------------------------------------
# 2. The scale function itself: bounds, direction, non-finite handling
# ---------------------------------------------------------------------------

def test_scale_function() -> None:
    check(radius_speed_scale(30.0, 30.0) == 1.0,
          "scale at the reference radius is exactly 1.0")
    check(radius_speed_scale(45.0, 30.0) == 1.0,
          "scale ABOVE the reference radius is capped at 1.0 (never speeds up)")
    check(abs(radius_speed_scale(21.0, 30.0) - 0.7) < 1e-12,
          "scale is the plain radius ratio inside the band (21/30 -> 0.70)")
    check(radius_speed_scale(0.0, 30.0) == _MIN_SPEED_SCALE,
          "scale at radius 0 is pinned to the floor, never 0")
    check(radius_speed_scale(1.0, 30.0) == _MIN_SPEED_SCALE,
          "scale below the floor is pinned to the floor")

    # A zero / near-zero reference radius must not divide by zero.
    for ref in (0.0, 1e-12, -0.0):
        s = radius_speed_scale(5.0, ref)
        check(math.isfinite(s) and 0.0 < s <= 1.0,
              f"zero-ish reference radius {ref!r} yields a finite scale in (0,1]",
              f"got {s!r}")

    # Non-finite inputs: fall back to 1.0 (the pre-feature speed), never
    # propagate a NaN/inf that min()/max() would silently pass through
    # (CLAUDE.md: non-finite floats defeat guards rather than tripping them).
    for bad in (float("nan"), float("inf"), float("-inf")):
        check(radius_speed_scale(bad, 30.0) == 1.0,
              f"non-finite local radius {bad!r} falls back to 1.0")
        check(radius_speed_scale(30.0, bad) == 1.0,
              f"non-finite reference radius {bad!r} falls back to 1.0")

    # Exhaustive bound check over the whole plausible input range.
    worst = None
    for i in range(0, 2001):
        s = radius_speed_scale(i * 0.05, _R)
        if not (math.isfinite(s) and _MIN_SPEED_SCALE <= s <= 1.0):
            worst = (i * 0.05, s)
            break
    check(worst is None,
          "scale stays finite and within [floor, 1.0] across 0..100 mm",
          f"failed at radius {worst[0]} -> {worst[1]}" if worst else "")


# ---------------------------------------------------------------------------
# 3. The effect reaches the G-code, in the right direction (continuous_spiral)
# ---------------------------------------------------------------------------

def _fmt(rows, n=3):
    return "; ".join(f"r={r:.2f} F={f:.0f}" for r, f, _z in rows[:n])


def test_continuous_spiral_feedrates() -> None:
    off_moves = _wall_moves(_run_continuous(False))
    on_moves = _wall_moves(_run_continuous(True))

    check(len(off_moves) == len(on_moves) and len(on_moves) > 500,
          "toggling the feature changes no move COUNT (geometry untouched)",
          f"{len(off_moves)} vs {len(on_moves)}")

    # Same XY path, different feedrates.
    same_xy = all(abs(a[0] - b[0]) < 1e-9 for a, b in zip(off_moves, on_moves))
    check(same_xy, "every point sits at the same radius with the toggle on")
    check(any(b[1] != a[1] for a, b in zip(off_moves, on_moves)),
          "some emitted F values differ once the toggle is on")

    # Never faster than before. This is the safety property: the compensation
    # is slow-down-only, so no move may come out above its own OFF feedrate.
    faster = [(a, b) for a, b in zip(off_moves, on_moves) if b[1] > a[1] + 1e-9]
    check(not faster,
          "no move is FASTER with the toggle on than with it off",
          f"{len(faster)} faster moves, first {faster[0] if faster else ''}")

    # Direction: within the SAME output, a smaller local radius must carry a
    # lower feedrate. Compare the star's valleys against its tips, skipping
    # the first turn (first_layer_speed, deliberately not compensated) and
    # the last (retract/wipe tail).
    body = [m for m in on_moves if m[2] > 1.0]
    tips = [m for m in body if m[0] > _R - 0.5]
    valleys = [m for m in body if m[0] < _R * 0.45]
    check(len(tips) > 20 and len(valleys) > 20,
          "the star gives enough tip and valley samples to compare",
          f"{len(tips)} tips, {len(valleys)} valleys")
    check(max(m[1] for m in valleys) < min(m[1] for m in tips),
          "EVERY small-radius (valley) move is slower than EVERY "
          "large-radius (tip) move in the same output",
          f"valleys {_fmt(valleys)} | tips {_fmt(tips)}")

    # And the OFF output must NOT have that property -- otherwise the check
    # above could be passing for some unrelated reason.
    off_body = [m for m in off_moves if m[2] > 1.0]
    off_tips = [m for m in off_body if m[0] > _R - 0.5]
    off_valleys = [m for m in off_body if m[0] < _R * 0.45]
    check(abs(max(m[1] for m in off_valleys)
              - max(m[1] for m in off_tips)) < 1e-6,
          "with the toggle OFF, valleys and tips share the same feedrate")

    # Quantitative: the deepest valley should land near
    # print_speed * (valley_radius / base_radius), within the Z-velocity and
    # flow clamps that apply to both runs alike.
    vmin = min(valleys, key=lambda m: m[0])
    expected = radius_speed_scale(vmin[0], _R)
    got = vmin[1] / max(m[1] for m in tips)
    check(abs(got - expected) < 0.05,
          "the deepest valley's F ratio matches radius_speed_scale",
          f"expected ~{expected:.3f}, got {got:.3f}")


# ---------------------------------------------------------------------------
# 4. Same, through profile_spiral (the hybrid / mesh-hybrid wall generator)
# ---------------------------------------------------------------------------

def test_profile_spiral_feedrates() -> None:
    off_moves = _wall_moves(_run_profile(None))
    on_moves = _wall_moves(_run_profile(_R))

    check(len(off_moves) == len(on_moves) and len(on_moves) > 500,
          "profile_spiral: move count unchanged by the toggle",
          f"{len(off_moves)} vs {len(on_moves)}")
    faster = [(a, b) for a, b in zip(off_moves, on_moves) if b[1] > a[1] + 1e-9]
    check(not faster,
          "profile_spiral: no move is faster with the toggle on",
          f"{len(faster)} faster moves")

    body = [m for m in on_moves if m[2] > 1.0]
    tips = [m for m in body if m[0] > _R - 0.5]
    valleys = [m for m in body if m[0] < _R * 0.45]
    check(len(tips) > 20 and len(valleys) > 20,
          "profile_spiral: enough tip and valley samples",
          f"{len(tips)} tips, {len(valleys)} valleys")
    check(max(m[1] for m in valleys) < min(m[1] for m in tips),
          "profile_spiral: every valley move is slower than every tip move",
          f"valleys {_fmt(valleys)} | tips {_fmt(tips)}")


# ---------------------------------------------------------------------------
# 5. Compatibility: orthogonal to Zone Overrides
# ---------------------------------------------------------------------------

def test_zone_override_compatible() -> None:
    """Zone Overrides crossfade the RADIUS (a texture displacement); this
    scales the FEEDRATE. They are independent axes, so enabling both must
    leave the zone geometry bit-identical and only change F -- which is what
    makes the two safe to ship together rather than mutually exclusive.
    """
    zones = [ZoneOverride(t_lo=0.30, t_hi=0.70, blend=0.05,
                          r_pattern="diamond", r_amp=2.0)]
    off = _wall_moves(_run_continuous(False, zones))
    on = _wall_moves(_run_continuous(True, zones))
    check(len(off) == len(on) and all(abs(a[0] - b[0]) < 1e-12
                                      for a, b in zip(off, on)),
          "zones + radius speed comp: the zone's geometry is untouched")
    check(any(a[1] != b[1] for a, b in zip(off, on)),
          "zones + radius speed comp: feedrates still respond inside a zone")

    # A zone's own radial displacement legitimately feeds the local radius the
    # scale is computed from -- confirm that is what happens (the textured
    # band must not be excluded from compensation).
    pts = spiral_path(_spec(True, zones))
    banded = [p.speed_scale for p, in zip(pts) if 0.4 < p.z / 10.0 < 0.6]
    check(banded and all(s is not None and 0.0 < s <= 1.0 for s in banded),
          "points inside a zone still carry a valid speed_scale")


# ---------------------------------------------------------------------------
# 6. A circle is (almost) unaffected -- the compensation is about ASYMMETRY
# ---------------------------------------------------------------------------

def test_circle_barely_changes() -> None:
    profile = PrinterProfile()
    w_off = _new_writer(profile)
    build_continuous_spiral(
        w_off,
        SpiralSpec(base_radius=_R, height=10.0, layer_height=0.5,
                   points_per_turn=60),
        shape=circle(_R))
    w_on = _new_writer(profile)
    build_continuous_spiral(
        w_on,
        SpiralSpec(base_radius=_R, height=10.0, layer_height=0.5,
                   points_per_turn=60, radius_speed_comp=True),
        shape=circle(_R))
    check(w_off.text() == w_on.text(),
          "a plain circle at its own base_radius is unchanged by the toggle "
          "(scale is exactly 1.0 everywhere)")


def main() -> int:
    test_default_is_noop()
    test_scale_function()
    test_continuous_spiral_feedrates()
    test_profile_spiral_feedrates()
    test_zone_override_compatible()
    test_circle_barely_changes()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S): " + ", ".join(_FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
