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


def run_loop_fabric_case(tmpdir: Path, ref_name: str, xy_twist_turns: float) -> tuple[bool, str]:
    """build_loop_fabric has no CLI flag either, so construct it directly --
    same pattern as run_profile_spiral_case(). Two references exist:
    ref_loop_fabric.gcode (xy_twist_turns=0.0, generated from PRE-change code
    to prove the twist formula is a bit-exact no-op at zero) and
    ref_loop_fabric_twist.gcode (xy_twist_turns=1.5, locking the twist formula
    and its sign). See regression_ref/MANIFEST.md."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.blobs import LoopSpec
    from trident_gcode.paths import star
    from trident_gcode.generators.loop_fabric import build_loop_fabric

    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    spec = LoopSpec(loops_per_turn=24, row_mm=0.5, up_mm=0.8,
                    stitch_mode="dip")
    build_loop_fabric(writer, shape=star(30.0, 5, 0.3), height=30.0,
                      spec=spec, xy_twist_turns=xy_twist_turns)
    out = tmpdir / ref_name
    writer.save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_zone_override_case(tmpdir: Path) -> tuple[bool, str]:
    """SpiralSpec.zones has no CLI flag either (see paths.py's ZoneOverride) --
    same direct-construction pattern as run_loop_fabric_case(). Also asserts,
    in memory, that zones=None and zones=[] produce byte-identical output to
    each other -- proving an empty zone list is a genuine bit-exact no-op
    without needing a second reference file just to say so."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.paths import SpiralSpec, ZoneOverride, circle
    from trident_gcode.generators.continuous_spiral import build_continuous_spiral

    ref_name = "ref_zone_overrides.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    def _build(zones):
        profile = PrinterProfile()
        writer = GcodeWriter(
            profile=profile, line_width=0.45, layer_height=0.3,
            bed_temp=60.0, nozzle_temp=210.0, material="PLA",
            print_speed=40.0, first_layer_speed=20.0,
        )
        spec = SpiralSpec(
            base_radius=30.0, height=30.0, layer_height=0.3, points_per_turn=240,
            xy_twist_turns=0.0, r_pattern="vwave", r_amp=1.0, zones=zones,
        )
        build_continuous_spiral(writer, spec, shape=circle(30.0))
        return writer

    # In-memory text comparison (not the byte comparison below, which goes
    # through save()'s text-mode file write like every other case in this
    # module -- on Windows that applies its own newline translation, so
    # comparing raw .text() strings here keeps this check platform-neutral).
    none_text = _build(None).text()
    empty_text = _build([]).text()
    if none_text != empty_text:
        return False, "zones=None and zones=[] produced different output (should be an exact no-op)"

    zone = ZoneOverride(t_lo=0.35, t_hi=0.70, blend=0.02, r_pattern="diamond",
                        r_amp=2.0, xy_twist_turns=1.0)
    out = tmpdir / ref_name
    _build([zone]).save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_zone_overlap_case(tmpdir: Path) -> tuple[bool, str]:
    """Zones may OVERLAP (v2, see paths.py's spiral_path() texture-crossfade
    normalization). Two overlapping zones (0.45-0.60 shared), one of them
    also exercising the per-zone pattern_twist (r_twist_turns) added in the
    same change. No CLI flag routes to this either -- same direct-
    construction pattern as run_zone_override_case()."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.paths import SpiralSpec, ZoneOverride, circle
    from trident_gcode.generators.continuous_spiral import build_continuous_spiral

    ref_name = "ref_zone_overlap.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    zones = [
        ZoneOverride(t_lo=0.25, t_hi=0.60, blend=0.05, r_pattern="diamond", r_amp=3.0),
        ZoneOverride(t_lo=0.45, t_hi=0.80, blend=0.05, r_pattern="pleats",
                     r_amp=3.0, r_twist_turns=1.5),
    ]
    spec = SpiralSpec(
        base_radius=30.0, height=30.0, layer_height=0.3, points_per_turn=240,
        xy_twist_turns=0.0, r_pattern="vwave", r_amp=1.0, zones=zones,
    )
    build_continuous_spiral(writer, spec, shape=circle(30.0))
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

        for ref_name, xy_twist_turns in (
            ("ref_loop_fabric.gcode", 0.0),
            ("ref_loop_fabric_twist.gcode", 1.5),
        ):
            ok, msg = run_loop_fabric_case(tmpdir, ref_name, xy_twist_turns)
            print(f"{'PASS' if ok else 'FAIL'}  {ref_name:28s} {msg if not ok else ''}")
            all_ok &= ok

        ok, msg = run_zone_override_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_zone_overrides.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

        ok, msg = run_zone_overlap_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_zone_overlap.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

    print("ALL PASS" if all_ok else "REGRESSION DETECTED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
