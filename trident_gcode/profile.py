"""Machine profiles for supported printers.

Each profile carries the build volume, motion limits, hardware specs, and
firmware-specific start/end G-code templates.  Templates use Python
str.format_map() placeholders: {nozzle_temp}, {bed_temp}, {material}.

Defaults are taken from the user's Voron Trident printer.cfg.  Bambu Lab
profiles use conservative limits derived from public firmware specs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field


@dataclass
class PrinterProfile:
    # --- Identity ---
    name: str = "Voron Trident"
    firmware: str = "klipper"          # "klipper" | "marlin" | "bambu_marlin"

    # --- Build volume (mm) ---
    bed_size_x: float = 235.0
    bed_size_y: float = 235.0
    z_max: float = 160.0
    z_min: float = -8.0

    # Safe printable area (matches bed_mesh min/max in printer.cfg).
    print_min_x: float = 30.0
    print_min_y: float = 30.0
    print_max_x: float = 208.0
    print_max_y: float = 185.0

    # --- Motion limits ---
    max_velocity: float = 400.0      # mm/s
    max_z_velocity: float = 25.0     # mm/s  <-- key non-planar limiter
    max_accel: float = 8000.0        # mm/s^2
    max_z_accel: float = 500.0       # mm/s^2

    # --- Hardware ---
    nozzle_diameter: float = 0.40
    filament_diameter: float = 1.75

    # --- Toolhead keep-out: inductive probe behind the nozzle ---
    has_probe: bool = True
    probe_dx: float = 3.0
    probe_dy: float = 29.0
    probe_clearance: float = 3.8
    probe_radius: float = 8.0

    # Max Z excursion below already-printed material (mm). On the Trident the
    # probe trailing behind the nozzle strikes raised plastic beyond this
    # (empirical, user-revised: < 1mm). Caps BOTH the non-planar wave
    # amplitude AND the loop-fabric stitch dip / row height. Probe-less
    # printers can go much higher.
    z_amp_max: float = 0.95

    # --- Firmware-specific G-code templates ---
    # Placeholders: {nozzle_temp}, {bed_temp}, {material}
    start_gcode: str = (
        "PRINT_START EXTRUDER={nozzle_temp:.0f} BED={bed_temp:.0f} MATERIAL={material}\n"
        "M83            ; relative extrusion\n"
        "G92 E0\n"
        "M107           ; fan off for first layer(s)"
    )
    end_gcode: str = "PRINT_END"

    # Pressure advance G-code format. "klipper" or "marlin".
    pa_gcode_style: str = "klipper"

    @property
    def filament_area(self) -> float:
        r = self.filament_diameter / 2.0
        return math.pi * r * r

    @property
    def bed_center(self) -> tuple[float, float]:
        return (self.bed_size_x / 2.0, self.bed_size_y / 2.0)

    def clamp_xy(self, x: float, y: float) -> tuple[float, float]:
        return (
            min(max(x, self.print_min_x), self.print_max_x),
            min(max(y, self.print_min_y), self.print_max_y),
        )

    def is_inside_print_area(self, x: float, y: float) -> bool:
        return (
            self.print_min_x <= x <= self.print_max_x
            and self.print_min_y <= y <= self.print_max_y
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("start_gcode", None)
        d.pop("end_gcode", None)
        return d


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

TRIDENT = PrinterProfile()

_BAMBU_START = (
    "M140 S{bed_temp:.0f}      ; start bed heating\n"
    "G28                ; home all\n"
    "G29                ; auto bed level\n"
    "M190 S{bed_temp:.0f}      ; wait for bed\n"
    "M104 S{nozzle_temp:.0f}   ; start nozzle heating\n"
    "M109 S{nozzle_temp:.0f}   ; wait for nozzle\n"
    "M83                ; relative extrusion\n"
    "G92 E0\n"
    "G1 Z5 F3000        ; lift nozzle\n"
    "G1 X5 Y5 F6000     ; move to purge position\n"
    "G1 Z0.3 F600\n"
    "G1 X50 E8 F600     ; purge line\n"
    "G1 X80 E5 F900\n"
    "G1 E-0.5 F1800     ; retract\n"
    "G1 Z2 F3000\n"
    "G92 E0\n"
    "M107               ; fan off for first layer(s)"
)
def _bambu_end(park_y: int) -> str:
    # Park Y must stay on the bed: full-size Bambu beds park at the back
    # (Y200), the A1 Mini's 180 mm bed cannot reach that far.
    return (
        "G1 E-2 F1800       ; retract\n"
        "G91                ; relative positioning\n"
        "G1 Z10 F3000       ; lift nozzle\n"
        "G90                ; absolute positioning\n"
        f"G1 X5 Y{park_y} F6000   ; park\n"
        "M104 S0            ; heater off\n"
        "M140 S0            ; bed off\n"
        "M107               ; fan off\n"
        "M84                ; steppers off"
    )


_BAMBU_END = _bambu_end(200)
_BAMBU_END_MINI = _bambu_end(170)

BAMBU_A1 = PrinterProfile(
    name="Bambu Lab A1",
    firmware="bambu_marlin",
    bed_size_x=256.0, bed_size_y=256.0, z_max=256.0, z_min=0.0,
    print_min_x=5.0, print_min_y=5.0, print_max_x=251.0, print_max_y=251.0,
    max_velocity=500.0, max_z_velocity=30.0, max_accel=10000.0, max_z_accel=1000.0,
    has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
    z_amp_max=4.0,
    start_gcode=_BAMBU_START,
    end_gcode=_BAMBU_END,
    pa_gcode_style="marlin",
)

BAMBU_A1_MINI = PrinterProfile(
    name="Bambu Lab A1 Mini",
    firmware="bambu_marlin",
    bed_size_x=180.0, bed_size_y=180.0, z_max=180.0, z_min=0.0,
    print_min_x=5.0, print_min_y=5.0, print_max_x=175.0, print_max_y=175.0,
    max_velocity=500.0, max_z_velocity=30.0, max_accel=10000.0, max_z_accel=1000.0,
    has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
    z_amp_max=4.0,
    start_gcode=_BAMBU_START,
    end_gcode=_BAMBU_END_MINI,
    pa_gcode_style="marlin",
)

BAMBU_P1S = PrinterProfile(
    name="Bambu Lab P1S",
    firmware="bambu_marlin",
    bed_size_x=256.0, bed_size_y=256.0, z_max=256.0, z_min=0.0,
    print_min_x=5.0, print_min_y=5.0, print_max_x=251.0, print_max_y=251.0,
    max_velocity=500.0, max_z_velocity=30.0, max_accel=10000.0, max_z_accel=1000.0,
    has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
    z_amp_max=4.0,
    start_gcode=_BAMBU_START,
    end_gcode=_BAMBU_END,
    pa_gcode_style="marlin",
)

BAMBU_X1C = PrinterProfile(
    name="Bambu Lab X1C",
    firmware="bambu_marlin",
    bed_size_x=256.0, bed_size_y=256.0, z_max=256.0, z_min=0.0,
    print_min_x=5.0, print_min_y=5.0, print_max_x=251.0, print_max_y=251.0,
    max_velocity=500.0, max_z_velocity=30.0, max_accel=10000.0, max_z_accel=1200.0,
    has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
    z_amp_max=4.0,
    start_gcode=_BAMBU_START,
    end_gcode=_BAMBU_END,
    pa_gcode_style="marlin",
)

# Registry: key -> profile instance. Used by serve.py's printer selector.
PRINTER_PROFILES: dict[str, PrinterProfile] = {
    "trident": TRIDENT,
    "bambu_a1": BAMBU_A1,
    "bambu_a1_mini": BAMBU_A1_MINI,
    "bambu_p1s": BAMBU_P1S,
    "bambu_x1c": BAMBU_X1C,
}
