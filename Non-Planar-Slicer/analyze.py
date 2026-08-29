#!/usr/bin/env python3
"""Check an external G-code file (e.g. exported from FullControl) against the
Voron Trident's limits.

    python analyze.py path/to/design.gcode
    python analyze.py design.gcode --probe-clearance 2.5

Reports footprint, height, filament, peak Z-rate and flow, plus the probe
keep-out check (the Omron inductive sensor rides 29 mm behind the nozzle -
non-planar crests can strike it while the nozzle is in a trough). Flags
anything that won't fit or exceeds the machine limits. Then drop the same
file onto viewer/index.html to see it in 3D.
"""
from __future__ import annotations

import argparse
import sys

from trident_gcode.analyze import analyze_gcode, format_report
from trident_gcode.profile import PrinterProfile


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trident G-code safety report")
    ap.add_argument("file", help="G-code file to analyze")
    ap.add_argument("--probe-clearance", type=float, default=None,
                    help="measured gap (mm) between the probe body bottom and the "
                         "nozzle tip; overrides the default in PrinterProfile")
    args = ap.parse_args(argv)

    profile = PrinterProfile()
    if args.probe_clearance is not None:
        profile.probe_clearance = args.probe_clearance

    try:
        a = analyze_gcode(args.file, profile=profile)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Trident safety report for {args.file}")
    print(format_report(a, profile))
    print("\nDrop the file onto viewer/index.html for a 3D preview.")
    return 0 if not a.issues else 3


if __name__ == "__main__":
    raise SystemExit(main())
