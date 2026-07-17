#!/usr/bin/env python3
"""CLI for the Trident continuous non-planar G-code generator.

Examples
--------
A wavy non-planar vase (default demo)::

    python generate.py --out vase.gcode

A taller, twisting star column with bigger waves::

    python generate.py --shape star --radius 30 --height 80 \
        --z-amp 3 --z-waves 6 --z-twist 1.5 --out star.gcode

Then open viewer/index.html in a browser and drag the .gcode file onto it.
"""
from __future__ import annotations

import argparse
import sys

from trident_gcode import TRIDENT, GcodeWriter, PrinterProfile
from trident_gcode.paths import SpiralSpec, circle, star, superellipse
from trident_gcode.generators import (
    build_continuous_spiral, build_mesh_spiral, build_surface_spiral,
)


def make_shape(name: str, radius: float):
    if name == "circle":
        return circle(radius)
    if name == "star":
        return star(radius, points=5, depth=0.35)
    if name == "square":
        return superellipse(radius, n=4.0)
    raise SystemExit(f"unknown shape: {name}")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Config I/O (handled before the main parse; listed here for --help).
    ap.add_argument("--config", default=None, metavar="FILE.json",
                    help="load default settings from a JSON config file "
                         "(CLI flags given explicitly still win)")
    ap.add_argument("--save-config", default=None, metavar="FILE.json",
                    help="write fully-resolved settings to a JSON config file, "
                         "then continue generating")

    # Geometry -- pick ONE mode: parametric --shape, --stl wrap, or --surface follow.
    ap.add_argument("--stl", default=None, help="path to an STL mesh to wrap a spiral around")
    ap.add_argument("--stl-scale", type=float, default=1.0, help="scale factor applied to the STL")
    ap.add_argument("--stl-points", type=int, default=200, help="points per layer when wrapping an STL")
    # Non-planar surface-following: pave a curved surface with a conformal spiral.
    ap.add_argument("--surface", default=None,
                    choices=["dome", "ripple", "saddle", "waves", "stl"],
                    help="follow a non-planar surface as a conformal shell")
    ap.add_argument("--surface-stl", default=None, help="STL whose top surface to drape (with --surface stl)")
    ap.add_argument("--surface-amp", type=float, default=10.0, help="surface height/amplitude (mm)")
    ap.add_argument("--surface-wavelength", type=float, default=20.0, help="wavelength for ripple/waves (mm)")
    ap.add_argument("--surface-shells", type=int, default=1, help="stacked conformal shells (thickness)")
    ap.add_argument("--surface-scale", type=float, default=1.0, help="scale for --surface stl")
    ap.add_argument("--shape", default="circle", choices=["circle", "star", "square"])
    ap.add_argument("--radius", type=float, default=35.0, help="base radius (mm)")
    ap.add_argument("--height", type=float, default=60.0, help="total height (mm)")
    ap.add_argument("--layer-height", type=float, default=0.30, help="spiral pitch (mm/turn)")
    ap.add_argument("--nozzle", type=float, default=0.40,
                    help="nozzle diameter (mm). Sets a sensible default line width if --line-width is omitted")
    ap.add_argument("--line-width", type=float, default=None,
                    help="bead width (mm). Defaults to ~1.1x nozzle if omitted")
    ap.add_argument("--points-per-turn", type=int, default=240)

    # Radial surface texture (probe-safe: displaces radius, never Z)
    ap.add_argument("--pattern", default=None,
                    choices=["vwave", "hwave", "ripple", "diamond", "bubbles",
                             "pleats", "hammered"],
                    help="radial wall texture - costs no Z budget, cannot hit the probe")
    ap.add_argument("--pattern-amp", type=float, default=1.0, help="texture depth (mm)")
    ap.add_argument("--pattern-waves", type=int, default=12, help="repeats around the perimeter")
    ap.add_argument("--pattern-bands", type=float, default=6.0, help="cycles over the height")
    ap.add_argument("--pattern-twist", type=float, default=0.0, help="pattern rotation over height (turns)")
    ap.add_argument("--pattern-fade-in", type=float, default=0.10,
                    help="height fraction to fade the texture in (keeps the base clean)")
    ap.add_argument("--pattern-fade-out", type=float, default=0.0)

    # Twist + non-planar modulation
    ap.add_argument("--xy-twist", type=float, default=0.0,
                    help="rotate the cross-section over height (turns) for a twisted column")
    ap.add_argument("--z-amp", type=float, default=0.8, help="wave amplitude (mm); 0 = flat top, max 0.95")
    ap.add_argument("--z-waves", type=int, default=4, help="waves around perimeter")
    ap.add_argument("--z-twist", type=float, default=0.0, help="wave rotation over height (turns)")

    # Process / temps
    ap.add_argument("--filament", default=None,
                    help="import temps/flow/max-volumetric-speed from an OrcaSlicer "
                         "filament profile by name (overrides --material/--nozzle-temp/--bed-temp). "
                         "List options: python -m trident_gcode.orca --list")
    ap.add_argument("--material", default="PLA")
    ap.add_argument("--nozzle-temp", type=float, default=210.0)
    ap.add_argument("--bed-temp", type=float, default=60.0)
    ap.add_argument("--print-speed", type=float, default=40.0)
    ap.add_argument("--first-layer-speed", type=float, default=18.0)
    ap.add_argument("--flow", type=float, default=1.0, help="flow multiplier")
    ap.add_argument("--fan", type=float, default=None, metavar="PCT",
                    help="part-cooling fan speed override (0-100 pct; 100 = full)")
    ap.add_argument("--no-fan", action="store_true",
                    help="force fan off for the entire print (overrides --fan)")

    # First-layer adhesion package (parametric + mesh generators)
    ap.add_argument("--first-layer-squish", type=float, default=1.0,
                    help="first layer prints at SQUISH x layer_height off the bed "
                         "while extruding full-height volume (presses plastic in). "
                         "1.0 = off (default); 0.75 recommended for adhesion")
    ap.add_argument("--first-layer-spacing-factor", type=float, default=1.25,
                    help="extra spacing between first-layer fill lines. Default 1.25 = "
                         "the winner revised to 1.25 after further prints on this "
                         "machine (R3D PETG); re-run the ladder if filament/nozzle changes")
    ap.add_argument("--first-layer-flow", type=float, default=1.1,
                    help="extra flow multiplier over the first turn (adhesion)")
    # Solid base + brim (one continuous bead)
    ap.add_argument("--base-layers", type=int, default=0,
                    help="stacked solid base disks under the wall (0 = none)")
    ap.add_argument("--brim", type=int, default=0,
                    help="outward brim loops beyond the outline, printed first (0 = none)")

    # Placement / output
    ap.add_argument("--center-x", type=float, default=None)
    ap.add_argument("--center-y", type=float, default=None)
    ap.add_argument("--out", default="output.gcode")
    ap.add_argument("--format", choices=["gcode", "fullcontrol"], default="gcode",
                    help="'gcode' (default) or 'fullcontrol' to emit a FullControl Python script")

    return ap


def main(argv: list[str] | None = None) -> int:
    # ---- two-pass parse so config-file defaults lose to explicit CLI flags ----
    # Pass 1: parse ONLY --config (ignore everything else) to learn the config
    # file path before building parser defaults.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre_args, _ = pre.parse_known_args(argv)

    ap = _build_parser()

    if pre_args.config:
        from trident_gcode.config import load_config, validate_flag_keys
        flag_raw, machine_raw = load_config(pre_args.config)
        # Collect all known argparse dest names so we can validate config keys.
        valid_dests = {a.dest for a in ap._actions}
        validate_flag_keys(flag_raw, valid_dests, pre_args.config)
        # Apply config values as parser defaults — CLI wins because argparse
        # applies explicitly-given args AFTER set_defaults.
        ap.set_defaults(**flag_raw)

    # Pass 2: full parse (with config defaults baked in).
    args = ap.parse_args(argv)

    # ---- machine profile ----------------------------------------------------
    profile = PrinterProfile(nozzle_diameter=args.nozzle)

    # Apply "machine" block from config file if present.
    if pre_args.config:
        from trident_gcode.config import load_config, apply_machine_overrides
        _, machine_raw = load_config(pre_args.config)
        if machine_raw:
            apply_machine_overrides(profile, machine_raw)

    # ---- line width ---------------------------------------------------------
    line_width = args.line_width if args.line_width is not None else round(args.nozzle * 1.125, 3)
    if line_width < args.nozzle:
        print(f"WARNING: line width {line_width} is narrower than the {args.nozzle}mm nozzle "
              f"- extrusion may be unreliable.", file=sys.stderr)
    elif line_width > args.nozzle * 2.0:
        print(f"WARNING: line width {line_width} exceeds 2x the {args.nozzle}mm nozzle "
              f"- the nozzle may not be able to lay it down cleanly.", file=sys.stderr)

    # ---- filament / writer kwargs -------------------------------------------
    fil_kwargs = dict(
        flow_multiplier=args.flow,
        bed_temp=args.bed_temp,
        nozzle_temp=args.nozzle_temp,
        material=args.material,
    )
    if args.filament:
        from trident_gcode.orca import FilamentSettings
        try:
            fs = FilamentSettings.from_orca(args.filament)
        except KeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        fil_kwargs.update(fs.writer_kwargs())
        print(f"Imported OrcaSlicer filament: {fs.name}")
        print("  " + fs.summary().replace("\n", "\n  ") + "\n")

    # Fan override: --no-fan wins over --fan wins over filament profile default.
    if args.no_fan:
        fil_kwargs["fan_speed"] = 0.0
    elif args.fan is not None:
        fil_kwargs["fan_speed"] = min(1.0, max(0.0, args.fan / 100.0))

    writer = GcodeWriter(
        profile=profile,
        line_width=line_width,
        layer_height=args.layer_height,
        print_speed=args.print_speed,
        first_layer_speed=args.first_layer_speed,
        **fil_kwargs,
    )

    # ---- save config BEFORE generating (captures fully-resolved settings) ---
    if args.save_config:
        from trident_gcode.config import save_config
        save_config(args.save_config, args, profile)
        print(f"Saved config to {args.save_config}")

    center = None
    if args.center_x is not None and args.center_y is not None:
        center = (args.center_x, args.center_y)

    try:
        if args.surface:
            from trident_gcode import surface as surf
            if args.surface == "stl":
                if not args.surface_stl:
                    print("ERROR: --surface stl requires --surface-stl PATH", file=sys.stderr)
                    return 1
                from trident_gcode.mesh import load_stl
                field, radius = surf.surface_from_stl(
                    load_stl(args.surface_stl), scale=args.surface_scale)
            else:
                field = surf.make_parametric(
                    args.surface, args.surface_amp, args.radius, args.surface_wavelength)
                radius = args.radius
            report = build_surface_spiral(
                writer, field, radius,
                shells=args.surface_shells,
                center=center,
            )
        elif args.stl:
            from trident_gcode.mesh import load_stl
            tris = load_stl(args.stl)
            report = build_mesh_spiral(
                writer, tris,
                points_per_turn=args.stl_points,
                scale=args.stl_scale,
                z_amp=args.z_amp,
                z_waves=args.z_waves,
                center=center,
                first_layer_squish=args.first_layer_squish,
                first_layer_spacing_factor=args.first_layer_spacing_factor,
                first_layer_flow=args.first_layer_flow,
                base_layers=args.base_layers,
                brim_loops=args.brim,
            )
        else:
            spec = SpiralSpec(
                base_radius=args.radius,
                height=args.height,
                layer_height=args.layer_height,
                points_per_turn=args.points_per_turn,
                xy_twist_turns=args.xy_twist,
                z_amp=args.z_amp,
                z_waves=args.z_waves,
                z_twist_turns=args.z_twist,
                r_pattern=args.pattern,
                r_amp=args.pattern_amp,
                r_waves=args.pattern_waves,
                r_bands=args.pattern_bands,
                r_twist_turns=args.pattern_twist,
                r_fade_in=args.pattern_fade_in,
                r_fade_out=args.pattern_fade_out,
            )
            shape = make_shape(args.shape, args.radius)
            report = build_continuous_spiral(
                writer, spec, shape=shape, center=center,
                first_layer_squish=args.first_layer_squish,
                first_layer_spacing_factor=args.first_layer_spacing_factor,
                first_layer_flow=args.first_layer_flow,
                base_layers=args.base_layers,
                brim_loops=args.brim,
            )
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.format == "fullcontrol":
        from trident_gcode.fullcontrol_export import writer_to_fc_script
        out = args.out
        if out.endswith(".gcode"):
            out = out[:-6] + ".py"
        name = args.surface or (args.stl and "mesh") or args.shape
        script = writer_to_fc_script(writer, design_name=f"trident_{name}")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(script)
        print(f"Wrote {out}  (FullControl script - paste into fullcontrol.xyz or run with the fullcontrol package)")
        print(f"  moves           : {len(writer.moves)}")
        print(f"  line/layer      : {writer.line_width}mm / {writer.layer_height}mm")
        print(f"  max Z-rate      : {report['max_z_rate_mm_s']}mm/s (clamps baked into feedrates)")
        return 0

    writer.save(args.out)

    print(f"Wrote {args.out}")
    if args.surface:
        src = args.surface_stl if args.surface == "stl" else args.surface
        print(f"  surface         : {src}  (conformal shell)")
        print(f"  shells          : {report['shells']}")
    elif args.stl:
        print(f"  source          : {args.stl}  (scale {args.stl_scale})")
        print(f"  layers          : {report['layers_printed']} printed, "
              f"{report['layers_skipped']} skipped")
        print(f"  non-planar      : amp={args.z_amp}mm waves={args.z_waves}")
    else:
        print(f"  shape           : {args.shape}  r={args.radius}mm")
        print(f"  height          : {args.height}mm  (layer/pitch {args.layer_height}mm)")
        print(f"  xy-twist        : {args.xy_twist} turns")
        print(f"  non-planar      : amp={args.z_amp}mm waves={args.z_waves} z-twist={args.z_twist}")
        if args.pattern:
            print(f"  texture         : {args.pattern}  depth={args.pattern_amp}mm "
                  f"waves={args.pattern_waves} bands={args.pattern_bands} (radial, probe-safe)")
    print(f"  points          : {report['points']}")
    if report.get("base_layers") or report.get("brim_loops"):
        print(f"  base/brim       : {report.get('base_layers', 0)} base layer(s), "
              f"{report.get('brim_loops', 0)} brim loop(s)")
    if args.first_layer_squish < 1.0:
        print(f"  first layer     : squish={args.first_layer_squish} "
              f"flow x{args.first_layer_flow}")
    print(f"  footprint radius: {report['footprint_radius_mm']}mm")
    print(f"  top Z           : {report['top_z_mm']}mm")
    print(f"  filament        : {report['filament_mm']}mm")
    if report.get("layer_height_clamp_events"):
        print(f"  WARNING         : {report['layer_height_clamp_events']} moves had "
              f"local layer height clamped (steep non-planar geometry)")
    zr = report['max_z_rate_mm_s']
    ok = "OK" if zr <= profile.max_z_velocity + 0.05 else "OVER LIMIT"
    print(f"  max Z-rate      : {zr}mm/s / {profile.max_z_velocity}mm/s [{ok}]")
    b = report["bounds"]
    print(f"  bbox min/max    : {tuple(round(v,1) for v in b['min'])} / "
          f"{tuple(round(v,1) for v in b['max'])}")
    print("\nOpen viewer/index.html in a browser and drop the .gcode file on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
