#!/usr/bin/env python3
"""Optional live-Orca integration test for the hybrid planar+non-planar
print feature. NOT part of the required test commands (check_regression.py /
test_printer_import.py) -- Orca version drift will change exact bytes even
with an unchanged shape, so this is not byte-compared and is not something
CI should run by default. It's the harness a developer with OrcaSlicer
installed runs by hand before/after touching orca_slice.py/hybrid.py.

Plain ``python tools/test_orca_live_integration.py``. Skips immediately
(exit 0, "SKIP") if OrcaSlicer isn't found on this machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.orca import orca_binary_path
from trident_gcode.profile import PrinterProfile
from trident_gcode.gcode import GcodeWriter
from trident_gcode.paths import circle
from trident_gcode.hybrid import build_hybrid_print
from trident_gcode.analyze import analyze_gcode


def main() -> int:
    orca_path = orca_binary_path()
    if orca_path is None:
        print("SKIP: OrcaSlicer not found (set TRIDENT_ORCA_PATH or install it)")
        return 0

    print(f"Using OrcaSlicer at: {orca_path}")
    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    report = build_hybrid_print(
        writer, shape_fn=circle(10.0), radius=10.0, height=20.0,
        transition_height=3.0, layer_height=0.3, points_per_turn=120,
        wall_count=2, infill_density=0.15, infill_pattern="grid",
        orca_path=orca_path,
        z_amp=0.8, z_waves=5,
    )
    print(f"achieved_base_height_mm: {report['achieved_base_height_mm']}")
    print(f"orca_base_layers: {report['orca_base_layers']}")
    print(f"base extrude_count: {report['extrude_count']}")
    print(f"wall contours/points: {report.get('contours')}/{report.get('points')}")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out_path = str(Path(tmp) / "hybrid_live_test.gcode")
        writer.save(out_path)
        analysis = analyze_gcode(out_path, profile)

    ok = not analysis.issues and analysis.probe_hits == 0
    if ok:
        print("PASS  analyze_gcode reports zero issues and zero probe hits")
        print("\nALL PASS")
        return 0
    print(f"FAIL  analyze_gcode issues: {list(analysis.issues)}")
    print(f"FAIL  probe_hits: {analysis.probe_hits}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
