#!/usr/bin/env python3
"""Curated, machine-safe presets for first non-planar test prints.

Run ``python presets.py`` to write every preset into ``examples/``. Each is tuned
to stay within the Trident's limits (footprint inside the safe area, top Z under
160 mm, Z-rate under max_z_velocity) while still showing off twist + non-planar
waves. Print one, see how it behaves, then tweak the matching CLI command.

Print times are deliberately short-ish (40-55 mm tall, modest footprint) so a
first test doesn't cost an hour before you learn something.
"""
from __future__ import annotations

import os

from trident_gcode import TRIDENT, GcodeWriter
from trident_gcode.paths import SpiralSpec, circle, star, superellipse
from trident_gcode.generators import build_continuous_spiral


# Each preset: name, human note, shape factory, SpiralSpec kwargs, writer kwargs.
PRESETS = [
    dict(
        name="01_gentle_wave_vase",
        note="Easiest first print. Round vase, soft 5-wave non-planar rim, no "
             "twist. Prints a 2-layer solid base for rock-solid adhesion.",
        shape=lambda r: circle(r),
        radius=32.0,
        spec=dict(height=45.0, layer_height=0.30, xy_twist_turns=0.0,
                  z_amp=0.8, z_waves=5, z_twist_turns=0.0),
        writer=dict(line_width=0.50, print_speed=40.0),
        build=dict(base_layers=2, first_layer_spacing_factor=1.25),
    ),
    dict(
        name="02_twisted_star",
        note="5-point star lobes spiral 1.5 turns up the column; mild waves.",
        shape=lambda r: star(r, points=5, depth=0.35),
        radius=30.0,
        spec=dict(height=50.0, layer_height=0.30, xy_twist_turns=1.5,
                  z_amp=0.8, z_waves=5, z_twist_turns=0.0),
        writer=dict(line_width=0.50, print_speed=45.0),
    ),
    dict(
        name="03_twisted_squircle",
        note="Square-ish column twisting 1 turn, 4-wave non-planar top.",
        shape=lambda r: superellipse(r, n=4.0),
        radius=30.0,
        spec=dict(height=55.0, layer_height=0.32, xy_twist_turns=1.0,
                  z_amp=0.9, z_waves=4, z_twist_turns=0.0),
        writer=dict(line_width=0.55, print_speed=45.0),
    ),
    dict(
        name="04_thick_wave_pot",
        note="Genuinely THICK bead (1.2 mm wide, 0.5 mm pitch) - the chunky look.",
        shape=lambda r: circle(r),
        radius=34.0,
        spec=dict(height=40.0, layer_height=0.50, xy_twist_turns=0.0,
                  z_amp=0.8, z_waves=6, z_twist_turns=0.0),
        writer=dict(line_width=1.20, print_speed=35.0),
    ),
    dict(
        name="05_aggressive_twist_star",
        note="Show-off: 6-point star, 3 twist turns, 8-wave rim, faster feed.",
        shape=lambda r: star(r, points=6, depth=0.40),
        radius=28.0,
        spec=dict(height=55.0, layer_height=0.30, xy_twist_turns=3.0,
                  z_amp=0.7, z_waves=8, z_twist_turns=1.0),
        writer=dict(line_width=0.50, print_speed=90.0),
    ),
]

# Shared print settings (override per preset via the "writer" dict above).
COMMON_WRITER = dict(
    bed_temp=60.0, nozzle_temp=210.0, material="PLA",
    first_layer_speed=18.0,
)


def main() -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Generate tuned preset prints")
    ap.add_argument("--filament", default=None,
                    help="OrcaSlicer filament profile (temps, flow, volumetric "
                         "limit, fan, retraction) applied to every preset. "
                         "Without it, presets use generic PLA 210C/60C.")
    args = ap.parse_args()

    if args.filament:
        from trident_gcode.orca import FilamentSettings
        try:
            fs = FilamentSettings.from_orca(args.filament)
        except KeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        COMMON_WRITER.update(fs.writer_kwargs())
        print(f"Using filament: {fs.name}  "
              f"({COMMON_WRITER.get('nozzle_temp', '?'):g}C / "
              f"{COMMON_WRITER.get('bed_temp', '?'):g}C bed, "
              f"{COMMON_WRITER.get('material', '?')})\n")
    else:
        print("NOTE: no --filament given; presets use generic PLA (210C/60C).")
        print("      Match your loaded filament: python presets.py --filament \"R3D PETG\"\n")

    os.makedirs("examples", exist_ok=True)
    print(f"{'preset':<26}{'top Z':>8}{'foot r':>9}{'fil':>9}{'Z-rate':>10}  status")
    print("-" * 78)

    any_fail = False
    for p in PRESETS:
        writer = GcodeWriter(profile=TRIDENT, **COMMON_WRITER, **p["writer"])
        spec = SpiralSpec(base_radius=p["radius"], **p["spec"])
        shape = p["shape"](p["radius"])
        out = os.path.join("examples", p["name"] + ".gcode")
        try:
            r = build_continuous_spiral(writer, spec, shape=shape, **p.get("build", {}))
        except ValueError as e:
            any_fail = True
            print(f"{p['name']:<26}  FAILED: {e}")
            continue
        writer.save(out)
        zr = r["max_z_rate_mm_s"]
        status = "OK" if zr <= TRIDENT.max_z_velocity + 0.05 else "OVER!"
        print(f"{p['name']:<26}{r['top_z_mm']:>7.1f}{r['footprint_radius_mm']:>9.1f}"
              f"{r['filament_mm']/1000:>8.2f}m{zr:>9.1f}  {status}")

    print("-" * 78)
    print("Files written to examples/. Open viewer/index.html and drop one in.")
    print("\nNotes:")
    for p in PRESETS:
        print(f"  - {p['name']}: {p['note']}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
