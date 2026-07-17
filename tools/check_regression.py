#!/usr/bin/env python3
"""Byte-compare generated G-code against regression_ref/*.gcode.

Run this after any change to the generation pipeline (paths.py, gcode.py,
extrusion.py, trident_gcode/generators/*) to catch accidental drift. Each
case's exact invocation is documented in regression_ref/MANIFEST.md -- keep
that file and the CLI_CASES list below in sync if a reference is ever
regenerated on purpose.

Deliberately does not use --filament: OrcaSlicer profile import reads from
%APPDATA%, an external dependency the reference files must not rely on to
stay reproducible.

Exit code 0 if every case matches, 1 if any case fails.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "regression_ref"
GENERATE = ROOT / "generate.py"

CLI_CASES = [
    ("ref_circle.gcode", []),
    ("ref_star.gcode", ["--shape", "star"]),
    ("ref_base_brim.gcode", ["--base-layers", "2", "--brim", "2"]),
    ("ref_textured.gcode", ["--pattern", "ripple", "--pattern-amp", "1.0", "--pattern-waves", "8"]),
]


def run_cli_case(ref_name: str, argv: list[str], tmpdir: Path) -> tuple[bool, str]:
    out = tmpdir / ref_name
    cmd = [sys.executable, str(GENERATE), *argv, "--out", str(out)]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"generate.py exited {result.returncode}:\n{result.stderr}"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_profile_spiral_case(tmpdir: Path) -> tuple[bool, str]:
    """build_profile_spiral is only reachable today via serve.py's mesh-texture
    mode (no CLI flag routes to it), so this case constructs a GcodeWriter and
    a parametric contour stack directly -- mirroring calibrate.py's pattern of
    calling the generator as a library function rather than shelling out."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.profile_stack import stack_from_shape
    from trident_gcode.paths import circle
    from trident_gcode.generators.profile_spiral import build_profile_spiral

    ref_name = "ref_profile_spiral.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    profile = PrinterProfile()
    contours = stack_from_shape(circle(30.0), 30.0, 60.0, 0.3, 240)
    heights = [i * (60.0 / max(len(contours) - 1, 1)) for i in range(len(contours))]
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    build_profile_spiral(
        writer, contours, heights, points_per_turn=240,
        z_amp=0.8, z_waves=4, base_layers=2, brim_loops=0,
    )
    out = tmpdir / ref_name
    writer.save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    all_ok = True
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for ref_name, argv in CLI_CASES:
            ok, msg = run_cli_case(ref_name, argv, tmpdir)
            print(f"{'PASS' if ok else 'FAIL'}  {ref_name:28s} {msg if not ok else ''}")
            all_ok &= ok

        ok, msg = run_profile_spiral_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_profile_spiral.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

    print("ALL PASS" if all_ok else "REGRESSION DETECTED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
