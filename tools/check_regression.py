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


def run_profile_spiral_resume_case(tmpdir: Path) -> tuple[bool, str]:
    """Locks build_profile_spiral's new resume=True parameter (added for the
    hybrid planar+non-planar print feature, trident_gcode/hybrid.py): with
    resume=True, header()/safe_lift() are skipped -- simulated here by
    emitting the header manually and moving the toolhead to a non-default
    position first, as if an Orca-sliced planar base had just finished --
    and the wall must pick up from wherever the toolhead already is. Same
    contour stack/z_amp as ref_profile_spiral.gcode (resume=False's own
    regression case), proving resume=False stays byte-identical to it while
    resume=True produces this new, distinct reference."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.profile_stack import stack_from_shape
    from trident_gcode.paths import circle
    from trident_gcode.generators.profile_spiral import build_profile_spiral

    ref_name = "ref_profile_spiral_resume.gcode"
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
    writer.header()
    writer.travel(50.0, 50.0, 10.0)
    writer.retract()
    build_profile_spiral(
        writer, contours, heights, points_per_turn=240,
        z_amp=0.8, z_waves=4, base_z=10.0, base_layers=0, brim_loops=0,
        resume=True,
    )
    out = tmpdir / ref_name
    writer.save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_hybrid_stitch_case(tmpdir: Path) -> tuple[bool, str]:
    """Locks the hybrid planar+non-planar print feature's full stitch
    pipeline (trident_gcode/hybrid.py) WITHOUT touching orca_slice.py's
    subprocess code: a small, realistic, checked-in sample of "whatever
    Orca would have produced" (tools/fixtures/orca_gcode/sample_base.gcode,
    also used by tools/test_orca_gcode_parser.py) stands in for a live
    slice, so this case never needs a live OrcaSlicer install and can't be
    broken by Orca version drift. Feeds it through
    parse_orca_gcode() -> replay_moves_onto_writer() ->
    build_profile_spiral(resume=True, base_z=<fixture's own top Z>, ...),
    the same sequence build_hybrid_print() runs after slicing."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.orca_gcode_parser import parse_orca_gcode
    from trident_gcode.orca_replay import replay_moves_onto_writer
    from trident_gcode.profile_stack import stack_from_shape
    from trident_gcode.paths import circle
    from trident_gcode.generators.profile_spiral import build_profile_spiral

    ref_name = "ref_hybrid_stitch.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    fixture = ROOT / "tools" / "fixtures" / "orca_gcode" / "sample_base.gcode"
    if not fixture.exists():
        return False, f"fixture missing: {fixture}"

    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    moves = parse_orca_gcode(fixture.read_text(encoding="utf-8"))
    writer.header()
    replay_report = replay_moves_onto_writer(writer, moves, profile=profile)
    if not replay_report.get("ends_retracted", False):
        writer.retract()

    # The fixture's own top Z (its second/last layer) is the seam this wall
    # resumes from -- mirrors build_hybrid_print() step 12's achieved_base_height.
    achieved_base_height = max(m.z for m in moves)
    contours = stack_from_shape(circle(3.0), 3.0, 20.0, 0.3, 60)
    heights = [i * (20.0 / max(len(contours) - 1, 1)) for i in range(len(contours))]
    build_profile_spiral(
        writer, contours, heights, points_per_turn=60,
        z_amp=0.3, z_waves=3, base_z=achieved_base_height, base_layers=0,
        center=(102.5, 102.5), resume=True,
    )
    out = tmpdir / ref_name
    writer.save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_hybrid_print_case(tmpdir: Path) -> tuple[bool, str]:
    """Locks build_hybrid_print()'s OWN orchestration, byte-for-byte.

    run_hybrid_stitch_case() above hand-inlines the post-slice sequence and
    so never actually calls trident_gcode/hybrid.py -- it would keep passing
    even if build_hybrid_print's own ordering (header -> replay -> placement
    check -> conditional retract -> seam marker -> wall) were broken. This
    case calls the real build_hybrid_print() end-to-end instead, so any
    refactor of that function has to be output-preserving to pass.

    The one thing stubbed out is the slice subprocess: hybrid's module-level
    `slice_stl_to_gcode` name is monkeypatched to return the checked-in
    tools/fixtures/orca_gcode/sample_base.gcode text -- the same technique
    tools/test_hybrid.py uses -- so this case needs no live OrcaSlicer
    install and can't be broken by Orca version drift. Everything else
    (STL build, JSON build, parse, replay, placement check, retract
    decision, seam marker, wall) is the real code path.

    center=(102.5, 102.5) is required, not arbitrary: it must match the
    fixture's own footprint or build_hybrid_print's step-10 placement sanity
    check raises. Arguments otherwise mirror test_hybrid.py's happy path --
    a small, fast, fully deterministic design."""
    import trident_gcode.hybrid as hybrid
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.paths import circle

    ref_name = "ref_hybrid_print.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    fixture = ROOT / "tools" / "fixtures" / "orca_gcode" / "sample_base.gcode"
    if not fixture.exists():
        return False, f"fixture missing: {fixture}"

    def _fake_slice(stl_bytes, *, machine_json, process_json, filament_json,
                    orca_path, **kw):
        return fixture.read_text(encoding="utf-8")

    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = _fake_slice
    try:
        hybrid.build_hybrid_print(
            writer,
            shape_fn=circle(3.0), radius=3.0, height=20.0,
            transition_height=0.4, layer_height=0.3, points_per_turn=60,
            wall_count=2, infill_density=0.2, infill_pattern="grid",
            orca_path="unused-because-monkeypatched",
            center=(102.5, 102.5),
            z_amp=0.3, z_waves=3,
        )
    except Exception as e:  # noqa: BLE001 -- report, don't mask
        return False, f"build_hybrid_print raised {type(e).__name__}: {e}"
    finally:
        hybrid.slice_stl_to_gcode = real_slice

    out = tmpdir / ref_name
    writer.save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_mesh_hybrid_print_case(tmpdir: Path) -> tuple[bool, str]:
    """Locks build_mesh_hybrid_print()'s orchestration, byte-for-byte.

    The mesh sibling of run_hybrid_print_case(): the planar base is the
    user's OWN solid STL (tools/fixtures/meshes/hex_mount.stl, a 25 mm x
    4.5 mm hexagonal prism standing in for a Fusion-modelled mount), handed
    to Orca as-is -- no contours_to_mesh round trip -- and the non-planar
    wall continues from the mesh's real top contour rather than from a
    parametric ring.

    Same one stub as run_hybrid_print_case(): the module-level name
    trident_gcode.hybrid.slice_stl_to_gcode is monkeypatched to return
    tools/fixtures/orca_gcode/sample_base.gcode, so no live OrcaSlicer
    install is needed. Everything else is the real code path (scale,
    layer-snapped seam height, translation, pre-flight checks, seam ring
    extraction, STL build, parse, replay, placement check, Orca-Z-drift
    check, blend_stack, wall).

    Two arguments are load-bearing rather than arbitrary:
      * center=(102.5, 102.5) must match the Orca fixture's own footprint or
        the placement sanity check raises (same constraint as
        run_hybrid_print_case()).
      * scale=0.1 shrinks the checked-in 4.5 mm mesh to 0.45 mm, whose
        layer-snapped base height (2 x 0.2 = 0.4 mm) is exactly the top of
        the Orca fixture's extruding moves -- otherwise the Orca-Z-drift
        check raises."""
    import trident_gcode.hybrid as hybrid
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.mesh import load_stl
    from trident_gcode.paths import circle

    ref_name = "ref_mesh_hybrid_print.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    fixture = ROOT / "tools" / "fixtures" / "orca_gcode" / "sample_base.gcode"
    if not fixture.exists():
        return False, f"fixture missing: {fixture}"
    mesh_fixture = ROOT / "tools" / "fixtures" / "meshes" / "hex_mount.stl"
    if not mesh_fixture.exists():
        return False, f"fixture missing: {mesh_fixture}"

    def _fake_slice(stl_bytes, *, machine_json, process_json, filament_json,
                    orca_path, **kw):
        return fixture.read_text(encoding="utf-8")

    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.2,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    real_slice = hybrid.slice_stl_to_gcode
    hybrid.slice_stl_to_gcode = _fake_slice
    try:
        hybrid.build_mesh_hybrid_print(
            writer,
            tris=load_stl(str(mesh_fixture)), scale=0.1,
            layer_height=0.2, points_per_turn=60,
            shape_fn=circle(2.5), radius=2.5, height=3.0, blend_height=1.0,
            wall_count=2, infill_density=0.2, infill_pattern="grid",
            orca_path="unused-because-monkeypatched",
            center=(102.5, 102.5),
            z_amp=0.3, z_waves=3,
        )
    except Exception as e:  # noqa: BLE001 -- report, don't mask
        return False, f"build_mesh_hybrid_print raised {type(e).__name__}: {e}"
    finally:
        hybrid.slice_stl_to_gcode = real_slice

    out = tmpdir / ref_name
    writer.save(str(out))
    generated = out.read_bytes()
    expected = ref.read_bytes()
    if generated != expected:
        return False, f"byte mismatch: generated {len(generated)}b vs reference {len(expected)}b"
    return True, "OK"


def run_orca_replay_case(tmpdir: Path) -> tuple[bool, str]:
    """orca_replay.replay_moves_onto_writer() has no CLI flag either -- same
    direct-construction pattern as the other in-process cases. Feeds a small
    hand-built list[OrcaMove] (not parsed from text -- that's
    orca_gcode_parser.py's own test suite, tools/test_orca_gcode_parser.py)
    through the replay layer onto a bare GcodeWriter: a travel to the start,
    a 4-edge perimeter, a retract, a travel, an unretract, a 3-edge zig-zag
    infill, and a final retract. Locks the derived line_width_override math
    (E-delta + 3D segment length -> extrusion_for_segment() reproduces the
    same volume) and the retract/unretract/travel/extrude classification."""
    from trident_gcode.profile import PrinterProfile
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.orca_gcode_parser import OrcaMove
    from trident_gcode.orca_replay import replay_moves_onto_writer

    ref_name = "ref_orca_replay.gcode"
    ref = REF_DIR / ref_name
    if not ref.exists():
        return False, f"reference file missing: {ref}"

    profile = PrinterProfile()
    writer = GcodeWriter(
        profile=profile, line_width=0.45, layer_height=0.3,
        bed_temp=60.0, nozzle_temp=210.0, material="PLA",
        print_speed=40.0, first_layer_speed=20.0,
    )
    moves = [
        OrcaMove(100.0, 100.0, 0.2, None, 150.0),
        OrcaMove(105.0, 100.0, 0.2, 0.21, 20.0),
        OrcaMove(105.0, 105.0, 0.2, 0.21, 20.0),
        OrcaMove(100.0, 105.0, 0.2, 0.21, 20.0),
        OrcaMove(100.0, 100.0, 0.2, 0.21, 20.0),
        OrcaMove(100.0, 100.0, 0.2, -0.6, 40.0),
        OrcaMove(101.0, 101.0, 0.2, None, 150.0),
        OrcaMove(101.0, 101.0, 0.2, 0.6, 40.0),
        OrcaMove(104.0, 101.0, 0.2, 0.15, 20.0),
        OrcaMove(101.0, 104.0, 0.2, 0.15, 20.0),
        OrcaMove(104.0, 104.0, 0.2, 0.15, 20.0),
        OrcaMove(104.0, 104.0, 0.2, -0.6, 40.0),
    ]
    writer.header()
    replay_moves_onto_writer(writer, moves, profile=profile)
    writer.footer()
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

        ok, msg = run_profile_spiral_resume_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_profile_spiral_resume.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

        ok, msg = run_orca_replay_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_orca_replay.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

        ok, msg = run_hybrid_stitch_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_hybrid_stitch.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

        ok, msg = run_hybrid_print_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_hybrid_print.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

        ok, msg = run_mesh_hybrid_print_case(tmpdir)
        print(f"{'PASS' if ok else 'FAIL'}  {'ref_mesh_hybrid_print.gcode':28s} {msg if not ok else ''}")
        all_ok &= ok

    print("ALL PASS" if all_ok else "REGRESSION DETECTED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
