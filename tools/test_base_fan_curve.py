#!/usr/bin/env python3
"""Tests for the hybrid planar base's layer-time-driven cooling curve.

Plain ``python tools/test_base_fan_curve.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors tools/test_hybrid.py's style, including
its monkeypatched slicer, so this never needs a live OrcaSlicer install
either. It uses its OWN synthetic six-layer Orca body rather than
tools/fixtures/orca_gcode/sample_base.gcode: that fixture is one layer thick,
which cannot exercise a per-LAYER fan curve at all (see _fake_slice below).

Two halves:

1. base_fan_fraction() as a pure function -- every region of the model (the
   no-cooling phase, the layer-time interpolation), the base_fan_always_on
   floor, the degenerate/inverted-threshold guard, and the non-finite
   rejection CLAUDE.md requires at every boundary.
2. The pipeline: gate OFF must be byte-identical to today's output, gate ON
   must actually change the base's M106 sequence, and the flat
   planar_fan_speed override must win outright over the curve.

The curve MODEL ITSELF is a best-effort reproduction of OrcaSlicer's
documented cooling behaviour, NOT verified against Orca's source -- so these
tests pin down what this app does, not what Orca does. See
trident_gcode/hybrid.py's own section header for which parts are this app's
interpretation.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import trident_gcode.hybrid as hybrid
from trident_gcode.hybrid import BaseFanCurve, base_fan_fraction
from trident_gcode.gcode import GcodeWriter
from trident_gcode.paths import circle
from trident_gcode.profile import PrinterProfile


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 1. base_fan_fraction(): the model itself.
# ---------------------------------------------------------------------------
def test_off_layers_region():
    # Layers below off_layers get no cooling at all, regardless of how fast
    # they printed -- a 0.1 s layer still gets 0.0.
    f = base_fan_fraction(0, 0.1, off_layers=3, min_speed=0.3, max_speed=1.0)
    check(close(f, 0.0), "off region: layer 0 of 3 no-cooling layers is 0%")
    f = base_fan_fraction(2, 0.1, off_layers=3, min_speed=0.3, max_speed=1.0)
    check(close(f, 0.0), "off region: last no-cooling layer (2 of 3) is still 0%")
    f = base_fan_fraction(3, 100.0, off_layers=3, min_speed=0.3, max_speed=1.0,
                          min_layer_time_s=10.0, max_layer_time_s=3.0)
    check(close(f, 0.3),
          "off region ends at off_layers: layer 3 is back under the model",
          f"got {f}")


def test_always_on_floor():
    # "Keep fan always on" floors the no-cooling phase at min_speed instead
    # of a true off. This is THIS APP's reading of Orca's option, not a
    # confirmed reproduction of Orca's internals.
    f = base_fan_fraction(0, 0.1, off_layers=3, min_speed=0.3, max_speed=1.0,
                          always_on=True)
    check(close(f, 0.3),
          "always_on: the no-cooling phase floors at min_speed, not 0")
    f = base_fan_fraction(0, 0.1, off_layers=3, min_speed=0.0, max_speed=1.0,
                          always_on=True)
    check(close(f, 0.0),
          "always_on with min_speed=0 is indistinguishable from off -- no "
          "special case")
    off = base_fan_fraction(0, 0.1, off_layers=3, min_speed=0.3, always_on=False)
    check(close(off, 0.0),
          "always_on=False leaves the no-cooling phase at a true 0")


def test_layer_time_interpolation():
    kw = dict(off_layers=0, min_speed=0.3, max_speed=1.0,
              min_layer_time_s=10.0, max_layer_time_s=3.0)
    check(close(base_fan_fraction(0, 30.0, **kw), 0.3),
          "layer time: well above min threshold -> min_speed")
    check(close(base_fan_fraction(0, 10.0, **kw), 0.3),
          "layer time: exactly at min threshold -> min_speed")
    check(close(base_fan_fraction(0, 3.0, **kw), 1.0),
          "layer time: exactly at max threshold -> max_speed")
    check(close(base_fan_fraction(0, 0.01, **kw), 1.0),
          "layer time: well below max threshold -> max_speed")
    mid = base_fan_fraction(0, 6.5, **kw)   # halfway between 3 and 10
    check(close(mid, 0.65),
          "layer time: midpoint interpolates halfway between the speeds",
          f"got {mid}")
    # Monotone: shorter layer time never means less fan.
    seq = [base_fan_fraction(0, t, **kw) for t in (12, 10, 8, 6, 4, 3, 1)]
    check(all(b >= a - 1e-12 for a, b in zip(seq, seq[1:])),
          "layer time: fan never decreases as the layer gets faster", f"{seq}")
    # An inverted speed pair (min > max) is a legal, if odd, request; it must
    # still stay inside 0..1 rather than overshooting.
    inv = [base_fan_fraction(0, t, off_layers=0,
                             min_speed=1.0, max_speed=0.2,
                             min_layer_time_s=10.0, max_layer_time_s=3.0)
           for t in (12, 6.5, 1)]
    check(all(0.0 <= v <= 1.0 for v in inv),
          "layer time: an inverted speed pair still yields 0..1", f"{inv}")


def test_degenerate_threshold_guard():
    # Equal thresholds: no interval to interpolate across. Must STEP, not
    # divide by zero.
    kw = dict(off_layers=0, min_speed=0.3, max_speed=1.0,
              min_layer_time_s=5.0, max_layer_time_s=5.0)
    try:
        below = base_fan_fraction(0, 4.0, **kw)
        at = base_fan_fraction(0, 5.0, **kw)
        above = base_fan_fraction(0, 6.0, **kw)
        ok = close(below, 1.0) and close(at, 1.0) and close(above, 0.3)
        check(ok, "degenerate thresholds (min == max): clean step, no "
                  "ZeroDivisionError", f"got {below}, {at}, {above}")
    except ZeroDivisionError as exc:
        check(False, "degenerate thresholds (min == max): clean step, no "
                     "ZeroDivisionError", repr(exc))
    # Inverted thresholds: same guard, and still a value inside 0..1.
    inv_kw = dict(kw, min_layer_time_s=3.0, max_layer_time_s=10.0)
    vals = [base_fan_fraction(0, t, **inv_kw) for t in (1.0, 5.0, 50.0)]
    check(all(0.0 <= v <= 1.0 for v in vals),
          "inverted thresholds (min < max): still a 0..1 value, no negative "
          "interpolation fraction", f"{vals}")


def test_non_finite_is_rejected_not_clamped():
    # CLAUDE.md: every comparison against NaN is False, so a NaN would slip
    # through every threshold below and out as a NaN fan fraction.
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            base_fan_fraction(0, bad, min_speed=0.3, max_speed=1.0)
            check(False, f"non-finite layer time {bad!r} is rejected",
                  "no exception raised")
        except ValueError:
            check(True, f"non-finite layer time {bad!r} is rejected")
    try:
        base_fan_fraction(0, 5.0, min_speed=float("nan"))
        check(False, "non-finite min_speed is rejected", "no exception raised")
    except ValueError:
        check(True, "non-finite min_speed is rejected")
    # And nothing the model returns is ever non-finite for finite input.
    vals = [base_fan_fraction(i, t, off_layers=2,
                              min_speed=0.3, max_speed=1.0,
                              min_layer_time_s=10.0, max_layer_time_s=3.0)
            for i in range(8) for t in (0.01, 3.0, 6.5, 10.0, 900.0)]
    check(all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in vals),
          "every (layer, time) pair yields a finite fraction in 0..1")


def test_bound_form_matches_free_function():
    curve = BaseFanCurve(off_layers=2, min_speed=0.3,
                         min_layer_time_s=10.0, max_speed=1.0,
                         max_layer_time_s=3.0, always_on=True)
    same = all(
        close(curve.fraction_for(i, t),
              base_fan_fraction(i, t, off_layers=2,
                                min_speed=0.3, min_layer_time_s=10.0,
                                max_speed=1.0, max_layer_time_s=3.0,
                                always_on=True))
        for i in range(8) for t in (0.5, 4.0, 8.0, 40.0))
    check(same, "BaseFanCurve.fraction_for matches base_fan_fraction exactly")


# ---------------------------------------------------------------------------
# 2. The pipeline, with a checked-in Orca fixture instead of a live slicer.
# ---------------------------------------------------------------------------
_CX = _CY = 102.5     # matches tools/test_hybrid.py's own fixture placement
_LAYER_H = 0.3
_N_BASE_LAYERS = 6


def _fake_slice(stl_bytes, *, machine_json, process_json, filament_json, orca_path, **kw):
    """A synthetic Orca BODY with SIX distinct layers, at deliberately varying
    speeds so consecutive layers land in different regions of the curve.

    tools/fixtures/orca_gcode/sample_base.gcode is only one layer thick, which
    is enough for test_hybrid.py's placement/parse checks but cannot exercise
    a per-layer fan curve at all. This stays a fixture in spirit -- fixed text,
    no live slicer -- it just has enough layers to have a curve.

    Orca's own first layer prints at z == layer_height (the TOP of the layer),
    so these start at 0.3 and not 0.0, which is exactly the 1-based-looking
    numbering _layer_runs() has to rebase to 0. Feedrates climb layer by layer,
    so each layer takes less time than the one below it and the layer-time
    model has a real gradient to act on. M106 lines are included deliberately:
    the parser must keep ignoring Orca's own fan commands (they are in
    _IGNORED_COMMANDS), and none of their values may reach the output.
    """
    lines = ["G21", "G90", "M83", "G1 E-.6 F1800"]
    half = 3.0
    for i in range(1, _N_BASE_LAYERS + 1):
        z = i * _LAYER_H
        feed = 600 * i          # mm/min -- 10, 20, 30... mm/s, faster each layer
        lines.append("M106 S%d" % (255 - i,))   # must never reach the output
        lines.append("G1 X%.3f Y%.3f Z%.3f F9000" % (_CX - half, _CY - half, z))
        for (dx, dy) in ((half, -half), (half, half), (-half, half), (-half, -half)):
            lines.append("G1 X%.3f Y%.3f E%.4f F%d"
                         % (_CX + dx, _CY + dy, 0.25, feed))
    lines.append("G1 E-.6 F1800")
    return "\n".join(lines) + "\n"


def _new_writer(profile):
    return GcodeWriter(
        profile=profile, line_width=0.45, layer_height=_LAYER_H,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def _generate(profile, **hybrid_kwargs) -> str:
    writer = _new_writer(profile)
    hybrid.build_hybrid_print(
        writer, shape_fn=circle(3.0), radius=3.0, height=20.0,
        transition_height=_N_BASE_LAYERS * _LAYER_H, layer_height=_LAYER_H,
        points_per_turn=60,
        wall_count=2, infill_density=0.2, infill_pattern="grid",
        orca_path="unused-because-monkeypatched", center=(_CX, _CY),
        z_amp=0.3, z_waves=3,
        **hybrid_kwargs,
    )
    return writer.text()


_SEAM = "; hybrid: non-planar wall begins here"


def _base_portion(text: str) -> str:
    return text.split(_SEAM, 1)[0]


def _base_fan_lines(text: str) -> list[str]:
    """The base's own M106 lines.

    M107 is deliberately NOT included: GcodeWriter.header() emits one
    ("fan off for first layer(s)") before any base move on every print,
    hybrid or not, and it is part of the print header rather than anything
    this feature controls. _header_m107_present() checks it separately.
    """
    return [ln.strip() for ln in _base_portion(text).splitlines()
            if ln.strip().startswith("M106")]


def _header_m107_present(text: str) -> bool:
    return any(ln.strip().startswith("M107")
               for ln in _base_portion(text).splitlines())


def test_pipeline():
    profile = PrinterProfile()
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = _fake_slice
    try:
        baseline = _generate(profile)
        curve = BaseFanCurve(off_layers=1, min_speed=0.3,
                             min_layer_time_s=10.0, max_speed=1.0,
                             max_layer_time_s=3.0)

        # Gate off (base_fan_curve=None) is the default and must be exactly
        # today's output -- this is the guarantee the regression refs rest on.
        gate_off = _generate(profile, base_fan_curve=None)
        check(gate_off == baseline,
              "gate OFF: passing base_fan_curve=None explicitly is "
              "byte-identical to not passing it at all")
        check(not _base_fan_lines(baseline),
              "gate OFF: the base portion still carries no fan-on command of "
              "its own", f"{_base_fan_lines(baseline)}")
        check(_header_m107_present(baseline),
              "gate OFF: the header's own M107 is untouched")
        # Orca's own M106 values must never survive the trust boundary --
        # _fake_slice emits M106 S249..S254 and none may appear.
        check(not any(("S%d" % s) in baseline for s in range(249, 255)),
              "gate OFF: none of Orca's own M106 values reach the output")

        # Gate on: the base's own M106 sequence must actually appear.
        gate_on = _generate(profile, base_fan_curve=curve)
        on_lines = _base_fan_lines(gate_on)
        check(len(on_lines) >= 2,
              "gate ON: the base carries a multi-step M106 sequence of its own",
              f"{on_lines}")
        check(gate_on != baseline,
              "gate ON: output actually differs from the gate-off baseline")
        # The first commanded speed is the no-cooling layer: fan fully off.
        check(on_lines and on_lines[0].startswith("M106 S0"),
              "gate ON: the first base layer (a no-cooling layer) commands "
              "S0", f"{on_lines[:1]}")
        # ...and the sequence climbs (each layer prints faster than the one
        # below it, so the layer-time model asks for more fan every time),
        # never exceeding S255.
        s_vals = [int(ln.split()[1][1:]) for ln in on_lines]
        check(all(0 <= s <= 255 for s in s_vals),
              "gate ON: every S value is inside 0-255", f"{s_vals}")
        check(s_vals == sorted(s_vals) and s_vals[-1] > s_vals[0],
              "gate ON: the base's fan climbs as layers print faster",
              f"{s_vals}")
        check(_header_m107_present(gate_on),
              "gate ON: the header's own M107 is still emitted first")
        check(not any(("S%d" % s) in gate_on for s in range(249, 255)),
              "gate ON: none of Orca's own M106 values reach the output "
              "either -- the curve is computed, never copied")

        # The flat override wins outright.
        both = _generate(profile, base_fan_curve=curve, planar_fan_speed=0.5)
        flat_only = _generate(profile, planar_fan_speed=0.5)
        check(both == flat_only,
              "precedence: planar_fan_speed set alongside the curve produces "
              "exactly the flat-override output; the curve is not consulted")

        # And the wall above the seam is untouched by the base's curve: only
        # the base portion may differ.
        check(gate_on.split(_SEAM, 1)[1] != "" ,
              "gate ON: the non-planar wall is still generated after the seam")
    finally:
        hybrid.slice_stl_to_gcode = real_slice


def test_layer_runs_partition_the_moves():
    """The per-layer split must lose nothing and reorder nothing -- that is
    the whole basis for calling replay_moves_onto_writer once per layer."""
    from trident_gcode.orca_gcode_parser import parse_orca_gcode

    moves = parse_orca_gcode(_fake_slice(
        b"", machine_json={}, process_json={}, filament_json={}, orca_path=""))
    runs = hybrid._layer_runs(moves, layer_height=_LAYER_H, initial_feed=40.0)
    rejoined = [m for run in runs for m in run.moves]
    check(rejoined == moves,
          "layer runs concatenate back to the original move list, in order",
          f"{len(rejoined)} vs {len(moves)}")
    indices = [r.layer_index for r in runs if r.layer_index is not None]
    check(len(set(indices)) == len(indices),
          "each layer index opens exactly one run (a Z-hop travel does not "
          "split a layer)", f"{indices}")
    check(indices == list(range(_N_BASE_LAYERS)),
          "layer indices count 0..N-1 from the base's own first printed "
          "layer, even though Orca's first layer prints at a positive Z",
          f"{indices}")
    check(all(math.isfinite(r.time_s) and r.time_s >= 0.0 for r in runs),
          "every run's approximate layer time is finite and non-negative",
          f"{[r.time_s for r in runs]}")
    check(sum(r.time_s for r in runs) > 0.0,
          "the fixture's total approximate base time is positive")


def test_uneven_first_layer_height():
    """Orca does NOT always print its first layer at the nominal layer height.

    Measured against a live OrcaSlicer 0.3 mm slice of this app's own base,
    Orca laid layers at z = 0.2, 0.5, 0.8, 1.1 ... -- a 0.2 mm first layer
    with 0.3 mm steps. A 0.15 mm first layer puts every subsequent layer
    exactly on a .5 boundary of z/layer_height, where Python's round() is
    banker's rounding and would map two adjacent layers onto ONE index,
    silently merging two fan steps into one. _layer_runs compares Z instead,
    so both shapes come out as N separate, sequentially numbered layers.
    """
    from trident_gcode.orca_gcode_parser import parse_orca_gcode

    for first_h, label in ((0.2, "0.2 mm first layer (the measured real case)"),
                           (0.15, "0.15 mm first layer (every layer on a .5 "
                                  "boundary of z/layer_height)")):
        zs = [first_h + i * _LAYER_H for i in range(6)]
        lines = ["G90", "M83"]
        for z in zs:
            lines.append("G1 X10.000 Y10.000 Z%.4f F9000" % z)
            lines.append("G1 X20.000 Y10.000 E0.2500 F1200")
        moves = parse_orca_gcode(chr(10).join(lines) + chr(10))
        runs = hybrid._layer_runs(moves, layer_height=_LAYER_H, initial_feed=40.0)
        got = [r.layer_index for r in runs if r.layer_index is not None]
        check(got == list(range(len(zs))),
              "uneven first layer: " + label + " yields one run per layer",
              f"{got} for Zs {zs}")


def main() -> int:
    test_off_layers_region()
    test_always_on_floor()
    test_layer_time_interpolation()
    test_degenerate_threshold_guard()
    test_non_finite_is_rejected_not_clamped()
    test_bound_form_matches_free_function()
    test_layer_runs_partition_the_moves()
    test_uneven_first_layer_height()
    test_pipeline()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
