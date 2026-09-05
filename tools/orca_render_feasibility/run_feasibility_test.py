#!/usr/bin/env python3
"""Feasibility probe: does the real hybrid-planar-base code path survive
inside Render's free-tier resource envelope (512 MB RAM, 0.1 vCPU)?

This is NOT a correctness test -- tools/check_regression.py already locks
build_hybrid_print's/build_mesh_hybrid_print's output byte-for-byte using a
stubbed Orca subprocess, and that guarantee is untouched by anything here.
This script exists only to answer, for each of several real scenarios: does
a real OrcaSlicer 2.4.2 subprocess (a) start at all in this container image,
and (b) complete the slice, under the exact RAM/CPU caps this container is
run with (see run_feasibility.ps1 -- it sets those caps with
`docker run --memory=512m --cpus=0.1`, not this script).

Meant to run as the Dockerfile's CMD, inside the constrained container.
Running it unconstrained (plain `python3 run_feasibility_test.py`) only
tells you the code *can* run given enough resources -- not that it fits in
the free tier, which is the actual question.

Scenarios:
  1. small_circle  -- the original sanity check (10mm radius circle).
  2. star_zones    -- a star shape with THREE overlapping Zone Overrides
                      (r_pattern/r_amp/xy_twist_turns per band) through
                      build_hybrid_print, at a higher points_per_turn than
                      (1) to stress the pure-Python wall math (which the
                      0.1 vCPU cap throttles regardless of Orca) as well as
                      the Orca-sliced planar base.
  3. mesh_stl      -- a real user STL through build_mesh_hybrid_print (the
                      "user's own solid mesh as the planar base" path).
                      Skipped if TRIDENT_TEST_STL_PATH isn't set to an
                      existing file -- this scenario is optional so the
                      image still runs its core scenarios without a
                      personal file mounted in.
"""
from __future__ import annotations

import os
import resource
import sys
import time
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.profile import PrinterProfile
from trident_gcode.gcode import GcodeWriter
from trident_gcode.paths import circle, star, ZoneOverride
from trident_gcode.mesh import load_stl
from trident_gcode.hybrid import build_hybrid_print, build_mesh_hybrid_print
from trident_gcode.analyze import analyze_gcode
from trident_gcode.orca_slice import OrcaSliceError


# Tried in this order for every scenario. Direct AppRun first (the same
# invocation shape production orca_binary_path() would find via
# TRIDENT_ORCA_PATH); the xvfb-wrapped one only as a fallback -- the first
# run of this probe confirmed Orca's --slice path needs no display, so this
# is now belt-and-suspenders rather than an open question.
CANDIDATES = [
    ("direct (no xvfb)", os.environ.get("TRIDENT_ORCA_PATH_DIRECT")),
    ("xvfb-wrapped", os.environ.get("TRIDENT_ORCA_PATH_XVFB")),
]


def _new_writer() -> GcodeWriter:
    return GcodeWriter(
        profile=PrinterProfile(), line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )


def _scenario_small_circle(orca_path: str) -> dict:
    writer = _new_writer()
    return {"writer": writer, "report": build_hybrid_print(
        writer, shape_fn=circle(10.0), radius=10.0, height=20.0,
        transition_height=3.0, layer_height=0.3, points_per_turn=120,
        wall_count=2, infill_density=0.15, infill_pattern="grid",
        orca_path=orca_path,
        z_amp=0.8, z_waves=5,
    )}


def _scenario_star_zones(orca_path: str) -> dict:
    writer = _new_writer()
    zones = [
        ZoneOverride(t_lo=0.05, t_hi=0.35, blend=0.05, r_pattern="diamond", r_amp=1.5),
        ZoneOverride(t_lo=0.30, t_hi=0.65, blend=0.05, r_pattern="hammered",
                     r_amp=1.2, xy_twist_turns=0.5),
        ZoneOverride(t_lo=0.60, t_hi=0.95, blend=0.05, r_pattern="pleats",
                     r_amp=1.8, r_twist_turns=1.0),
    ]
    report = build_hybrid_print(
        writer, shape_fn=star(30.0, points=6, depth=0.4), radius=30.0,
        height=50.0, transition_height=3.0, layer_height=0.3,
        points_per_turn=240,
        wall_count=2, infill_density=0.15, infill_pattern="grid",
        orca_path=orca_path,
        z_amp=0.8, z_waves=6, zones=zones,
    )
    return {"writer": writer, "report": report}


def _scenario_mesh_stl(orca_path: str) -> dict | None:
    stl_path = os.environ.get("TRIDENT_TEST_STL_PATH")
    if not stl_path or not os.path.isfile(stl_path):
        return None  # signals SKIP to the caller
    tris = load_stl(stl_path)
    writer = _new_writer()
    writer.layer_height = 0.2
    report = build_mesh_hybrid_print(
        writer, tris=tris, scale=1.0, layer_height=0.2, points_per_turn=180,
        shape_fn=circle(32.0), radius=32.0, height=20.0, blend_height=5.0,
        wall_count=2, infill_density=0.2, infill_pattern="grid",
        orca_path=orca_path,
        z_amp=0.5, z_waves=4,
    )
    return {"writer": writer, "report": report}


SCENARIOS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("small_circle", _scenario_small_circle),
    ("star_zones", _scenario_star_zones),
    ("mesh_stl", _scenario_mesh_stl),
]


def _run_one(scenario_fn, orca_path: str) -> tuple[bool, str, float, int]:
    """Returns (ok, message, wall_seconds, peak_child_rss_kb)."""
    rss_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    t0 = time.monotonic()
    try:
        result = scenario_fn(orca_path)
    except OrcaSliceError as e:
        elapsed = time.monotonic() - t0
        rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return False, f"OrcaSliceError: {e}", elapsed, rss_after - rss_before
    except Exception as e:  # noqa: BLE001 -- report every failure mode, don't mask
        elapsed = time.monotonic() - t0
        rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return False, f"{type(e).__name__}: {e}", elapsed, rss_after - rss_before

    elapsed = time.monotonic() - t0
    rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_kb = rss_after - rss_before

    if result is None:
        return True, "SKIPPED (no input configured for this scenario)", elapsed, 0

    writer, report = result["writer"], result["report"]
    with tempfile.TemporaryDirectory() as tmp:
        out_path = str(Path(tmp) / "feasibility.gcode")
        writer.save(out_path)
        analysis = analyze_gcode(out_path, writer.profile)

    if analysis.issues or analysis.probe_hits:
        return (
            False,
            f"sliced, but analyze_gcode found issues={list(analysis.issues)} "
            f"probe_hits={analysis.probe_hits}",
            elapsed, peak_kb,
        )

    extra = ""
    if "achieved_base_height_mm" in report:
        extra = (f" achieved_base_height_mm={report['achieved_base_height_mm']} "
                 f"orca_base_layers={report['orca_base_layers']} "
                 f"extrude_count={report.get('extrude_count')}")
    return True, f"OK{extra}", elapsed, peak_kb


def main() -> int:
    print("Render free-tier feasibility probe for hybrid planar-base slicing")
    print("=" * 72)
    print("Expected caps for this run (set by the container launcher, not this")
    print("script): 512 MB RAM, 0.1 vCPU -- see run_feasibility.ps1.\n")

    overall_ok = True
    for scenario_name, scenario_fn in SCENARIOS:
        print(f"--- scenario: {scenario_name} ---")
        scenario_result = None
        for label, orca_path in CANDIDATES:
            if not orca_path:
                print(f"  [{label}] SKIP -- no path configured for this candidate")
                continue
            if not os.path.isfile(orca_path):
                print(f"  [{label}] SKIP -- {orca_path} does not exist in this image")
                continue

            print(f"  [{label}] trying {orca_path} ...")
            ok, msg, elapsed, peak_kb = _run_one(scenario_fn, orca_path)
            status = "PASS" if ok else "FAIL"
            print(f"  [{label}] {status}  wall={elapsed:.1f}s  "
                  f"peak_child_rss={peak_kb / 1024:.1f} MB")
            print(f"  [{label}] {msg}")

            if ok:
                scenario_result = (label, elapsed, peak_kb)
                break  # first working candidate answers the question

        if scenario_result is None:
            overall_ok = False
            print(f"  RESULT: {scenario_name} did not complete under any candidate\n")
        else:
            print(f"  RESULT: {scenario_name} OK\n")

    print("=" * 72)
    if overall_ok:
        print("RESULT: every scenario either completed or was cleanly skipped")
        print("inside this container's resource caps. Re-run with the exact")
        print("caps in run_feasibility.ps1 if you haven't already -- this")
        print("script cannot enforce them itself.")
        return 0

    print("RESULT: at least one scenario failed under these caps.")
    print("If it FAILed with an OrcaSliceError mentioning exit code -9, that")
    print("is very likely an OOM-kill by the container's memory cgroup --")
    print("i.e. 512 MB was not enough for that scenario, not a missing-")
    print("library issue. If it failed before even producing that error")
    print("(crash/segfault), check for missing shared libraries first (see")
    print("the Dockerfile's ldd suggestion). A ValueError from the hybrid")
    print("code itself (e.g. 'not star-convex') is a real modeling")
    print("constraint on that input, not a resource problem.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
