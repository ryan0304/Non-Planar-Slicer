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

    # Peak printable wave-wall slope: amp * waves / (radius * silhouette).
    # 0.25 (about 14 deg) is the Trident's own measured value -- the
    # 2026-07-05 test print (R3D PETG) showed slopes beyond it collapsing
    # above half height. This is a print-QUALITY ceiling, distinct from
    # z_amp_max's hardware clearance above: the two gate each other
    # independently, and either can be the binding one. At radius 32 with 5
    # waves this caps amplitude at 1.60 mm regardless of a 4.0 mm z_amp_max.
    #
    # Every non-Trident profile below inherits 0.25 as a CONSERVATIVE
    # PLACEHOLDER, not a measurement -- no print has been run on any of them.
    # Lower is stricter here, so inheriting it over-restricts rather than
    # risking a collapsed wall on hardware nobody has tested. Per CLAUDE.md
    # ("print-tested claims need a print"), do NOT invent a different number
    # for any built-in profile below; only a real test print earns one.
    quality_slope_max: float = 0.25

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

    # --- Thermal ceilings (from the config's heater max_temp) ---
    max_nozzle_temp: float = 300.0
    max_bed_temp: float = 120.0
    # Klipper's max_extrude_cross_section (mm^2). 0 = unknown/unset.
    max_extrude_cross_section: float = 0.0

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

def _bambu_start(max_z_velocity: float) -> str:
    # Same reasoning as the Creality templates below: a bare F3000 on a pure-Z
    # lift is 50 mm/s, well over every Bambu profile's 30 mm/s Z ceiling. The
    # firmware would clamp it harmlessly, but analyze_gcode reads the emitted
    # file and reported a peak Z-rate of 50/30 on EVERY Bambu print -- a
    # warning that fires unconditionally is a warning users learn to ignore,
    # which is worse than no warning at all.
    z_f = min(3000.0, max_z_velocity * 60.0)
    return (
        "M140 S{bed_temp:.0f}      ; start bed heating\n"
        "G28                ; home all\n"
        "G29                ; auto bed level\n"
        "M190 S{bed_temp:.0f}      ; wait for bed\n"
        "M104 S{nozzle_temp:.0f}   ; start nozzle heating\n"
        "M109 S{nozzle_temp:.0f}   ; wait for nozzle\n"
        "M83                ; relative extrusion\n"
        "G92 E0\n"
        f"G1 Z5 F{z_f:.0f}        ; lift nozzle\n"
        "G1 X5 Y5 F6000     ; move to purge position\n"
        "G1 Z0.3 F600\n"
        "G1 X50 E8 F600     ; purge line\n"
        "G1 X80 E5 F900\n"
        "G1 E-0.5 F1800     ; retract\n"
        f"G1 Z2 F{z_f:.0f}\n"
        "G92 E0\n"
        "M107               ; fan off for first layer(s)"
    )


def _bambu_end(park_y: int, max_z_velocity: float) -> str:
    # Park Y must stay on the bed: full-size Bambu beds park at the back
    # (Y200), the A1 Mini's 180 mm bed cannot reach that far.
    z_f = min(3000.0, max_z_velocity * 60.0)
    return (
        "G1 E-2 F1800       ; retract\n"
        "G91                ; relative positioning\n"
        f"G1 Z10 F{z_f:.0f}       ; lift nozzle\n"
        "G90                ; absolute positioning\n"
        f"G1 X5 Y{park_y} F6000   ; park\n"
        "M104 S0            ; heater off\n"
        "M140 S0            ; bed off\n"
        "M107               ; fan off\n"
        "M84                ; steppers off"
    )


# Every current Bambu profile shares a 30 mm/s Z ceiling.
_BAMBU_Z_VEL = 30.0
_BAMBU_START = _bambu_start(_BAMBU_Z_VEL)
_BAMBU_END = _bambu_end(200, _BAMBU_Z_VEL)
_BAMBU_END_MINI = _bambu_end(170, _BAMBU_Z_VEL)

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
    max_nozzle_temp=300.0, max_bed_temp=100.0,
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
    max_nozzle_temp=300.0, max_bed_temp=100.0,
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
    max_nozzle_temp=300.0, max_bed_temp=110.0,
)

# Bambu markets the P2S at 600 mm/s and 20,000 mm/s^2. Those are PEAK toolhead
# figures, and this profile deliberately does not adopt them: max_velocity and
# max_accel here are ceilings used to clamp emitted feedrates, so taking the
# marketing peak would widen a limit rather than describe a sustained one. The
# shipped P1S profile already de-rates Bambu's own 20,000 to 10,000 for the
# same reason, and the P2S shares that chassis, bed and 256^3 volume -- so it
# gets the same numbers rather than a new convention.
#
# Verified against Bambu's published specs: 256 x 256 x 256 mm volume, 300 C
# hot end, 110 C bed, 1.75 mm filament, 0.4 mm stock nozzle, CoreXY, direct
# drive, no physical probe (bed levelling is nozzle-based, so nothing trails
# the hotend).
#
# NOT published by Bambu for any of their machines, and therefore inherited
# from the rest of this family rather than measured:
#   * max_z_velocity (30 mm/s) -- the single most important number in a
#     non-planar app, since it is what clamps every Z-modulated feedrate.
#   * max_z_accel (1000 mm/s^2).
#   * z_amp_max (4.0 mm) -- not a vendor spec at all. It is this app's max Z
#     excursion below already-printed material. 4.0 assumes the probe-less
#     geometry the other Bambu profiles assume; it has NOT been print-tested
#     on a P2S by anyone. Treat it as a starting point, not a measurement.
BAMBU_P2S = PrinterProfile(
    name="Bambu Lab P2S",
    firmware="bambu_marlin",
    bed_size_x=256.0, bed_size_y=256.0, z_max=256.0, z_min=0.0,
    print_min_x=5.0, print_min_y=5.0, print_max_x=251.0, print_max_y=251.0,
    max_velocity=500.0, max_z_velocity=30.0, max_accel=10000.0, max_z_accel=1000.0,
    has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
    z_amp_max=4.0,
    start_gcode=_BAMBU_START,
    end_gcode=_BAMBU_END,
    pa_gcode_style="marlin",
    max_nozzle_temp=300.0, max_bed_temp=110.0,
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
    max_nozzle_temp=300.0, max_bed_temp=110.0,
)

# ---------------------------------------------------------------------------
# Creality profiles
#
# Stock Marlin Creality firmware ships DEFAULT_MAX_FEEDRATE { 500, 500, 5, 25 }
# and a Z acceleration around 100 mm/s^2 -- a Z max feedrate 5x slower than the
# Trident's. This app's whole premise is non-planar Z modulation, so that one
# number changes the character of every print on these machines: feedrates
# get clamped hard and print times run much longer. These are conservative
# figures derived from public stock-firmware specs, not measured on real
# hardware -- deliberately NOT rounded up to make prints look faster. Where a
# machine is commonly flashed to Klipper with higher limits, the user can
# raise these in the review dialog; the shipped default has to be honest
# about the stock machine.
# ---------------------------------------------------------------------------
def _creality_marlin_start(bed_size_y: float, max_z_velocity: float) -> str:
    # Purge line Y extent is parameterised off the bed depth so it fits every
    # bed in the table (220mm-350mm deep) without running off the back.
    purge_y = min(200.0, bed_size_y - 20.0)
    # The lift and the diagonal move to/from the purge line both have a Z
    # component. A generic F3000/F5000 (50/83 mm/s) is what most slicers
    # print unconditionally and stock Marlin silently clamps to
    # DEFAULT_MAX_FEEDRATE -- but this app's whole point is being honest
    # about what the machine can actually do, so the built-in template
    # itself must never ask for more Z speed than the profile's own
    # max_z_velocity. Capping the *whole* move's feedrate (not just its Z
    # component) at the Z limit is conservative but simple, and keeps every
    # Z-bearing move's effective Z-rate safely under the ceiling regardless
    # of how much of the move is XY vs Z.
    z_f = min(3000.0, max_z_velocity * 60.0)
    return (
        "M140 S{bed_temp:.0f}     ; start bed heating\n"
        "G28                      ; home all axes\n"
        "M190 S{bed_temp:.0f}     ; wait for bed\n"
        "M104 S{nozzle_temp:.0f}  ; start nozzle heating\n"
        "M109 S{nozzle_temp:.0f}  ; wait for nozzle\n"
        "M83                      ; relative extrusion\n"
        "G92 E0\n"
        f"G1 Z2.0 F{z_f:.0f}            ; lift\n"
        f"G1 X5.1 Y20 Z0.28 F{z_f:.0f}  ; move to purge start\n"
        f"G1 X5.1 Y{purge_y:.1f} Z0.28 F1500 E15   ; purge line up\n"
        f"G1 X5.4 Y{purge_y:.1f} Z0.28 F5000\n"
        "G1 X5.4 Y20 Z0.28 F1500 E15          ; purge line back\n"
        "G92 E0\n"
        f"G1 Z2.0 F{z_f:.0f}            ; lift away from the purge\n"
        "M107                     ; fan off for first layer(s)"
    )


def _creality_marlin_end(bed_size_y: float, max_z_velocity: float) -> str:
    # Park Y must stay on the bed -- reuse the same formula as the purge line
    # so every bed depth in the table parks safely inside its travel.
    park_y = min(200.0, bed_size_y - 20.0)
    # Same reasoning as the start template: the Z lift here is a pure-Z move
    # (zf=1.0), so its F must not exceed the profile's real max_z_velocity.
    z_f = min(3000.0, max_z_velocity * 60.0)
    return (
        "G91                      ; relative positioning\n"
        "G1 E-2 F2700             ; retract\n"
        f"G1 Z10 F{z_f:.0f}             ; lift\n"
        "G90                      ; absolute positioning\n"
        f"G1 X5 Y{park_y:.1f} F3000    ; park, present the print\n"
        "M104 S0                  ; heater off\n"
        "M140 S0                  ; bed off\n"
        "M107                     ; fan off\n"
        "M84                      ; steppers off"
    )


# Creality's stock Klipper firmware exposes START_PRINT/END_PRINT macros
# (the way Creality's own K1/K1 Max/Ender 3 V3 KE gcode_macro.cfg does),
# analogous to how the Trident profile calls PRINT_START/PRINT_END.
_CREALITY_KLIPPER_START = (
    "START_PRINT EXTRUDER={nozzle_temp:.0f} BED={bed_temp:.0f}\n"
    "M83\n"
    "G92 E0\n"
    "M107"
)
_CREALITY_KLIPPER_END = "END_PRINT"


def _creality_marlin(**kwargs) -> PrinterProfile:
    bed_size_y = kwargs.get("bed_size_y", 220.0)
    max_z_velocity = kwargs["max_z_velocity"]
    return PrinterProfile(
        firmware="marlin",
        z_min=0.0,
        print_min_x=5.0, print_min_y=5.0,
        print_max_x=kwargs["bed_size_x"] - 5.0, print_max_y=bed_size_y - 5.0,
        nozzle_diameter=0.40, filament_diameter=1.75,
        has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
        z_amp_max=1.0,
        start_gcode=_creality_marlin_start(bed_size_y, max_z_velocity),
        end_gcode=_creality_marlin_end(bed_size_y, max_z_velocity),
        pa_gcode_style="marlin",
        **kwargs,
    )


def _creality_klipper(**kwargs) -> PrinterProfile:
    bed_size_y = kwargs.get("bed_size_y", 220.0)
    return PrinterProfile(
        firmware="klipper",
        z_min=0.0,
        print_min_x=5.0, print_min_y=5.0,
        print_max_x=kwargs["bed_size_x"] - 5.0, print_max_y=bed_size_y - 5.0,
        nozzle_diameter=0.40, filament_diameter=1.75,
        has_probe=False, probe_dx=0.0, probe_dy=0.0, probe_clearance=0.0, probe_radius=0.0,
        z_amp_max=1.0,
        start_gcode=_CREALITY_KLIPPER_START,
        end_gcode=_CREALITY_KLIPPER_END,
        pa_gcode_style="klipper",
        **kwargs,
    )


CREALITY_ENDER3 = _creality_marlin(
    name="Creality Ender 3 / Pro",
    bed_size_x=220.0, bed_size_y=220.0, z_max=250.0,
    max_velocity=200.0, max_z_velocity=5.0, max_accel=500.0, max_z_accel=100.0,
    max_nozzle_temp=260.0, max_bed_temp=100.0,
)

CREALITY_ENDER3_V2 = _creality_marlin(
    name="Creality Ender 3 V2",
    bed_size_x=220.0, bed_size_y=220.0, z_max=250.0,
    max_velocity=200.0, max_z_velocity=5.0, max_accel=500.0, max_z_accel=100.0,
    max_nozzle_temp=260.0, max_bed_temp=100.0,
)

CREALITY_ENDER3_S1 = _creality_marlin(
    name="Creality Ender 3 S1 / S1 Pro",
    bed_size_x=220.0, bed_size_y=220.0, z_max=270.0,
    max_velocity=200.0, max_z_velocity=5.0, max_accel=500.0, max_z_accel=100.0,
    max_nozzle_temp=300.0, max_bed_temp=110.0,
)

CREALITY_ENDER3_V3_SE = _creality_marlin(
    name="Creality Ender 3 V3 SE",
    bed_size_x=220.0, bed_size_y=220.0, z_max=250.0,
    max_velocity=250.0, max_z_velocity=10.0, max_accel=2500.0, max_z_accel=100.0,
    max_nozzle_temp=260.0, max_bed_temp=100.0,
)

CREALITY_ENDER3_V3_KE = _creality_klipper(
    name="Creality Ender 3 V3 KE",
    bed_size_x=220.0, bed_size_y=220.0, z_max=240.0,
    max_velocity=500.0, max_z_velocity=10.0, max_accel=8000.0, max_z_accel=100.0,
    max_nozzle_temp=300.0, max_bed_temp=100.0,
)

CREALITY_ENDER5_PLUS = _creality_marlin(
    name="Creality Ender 5 Plus",
    bed_size_x=350.0, bed_size_y=350.0, z_max=400.0,
    max_velocity=200.0, max_z_velocity=5.0, max_accel=500.0, max_z_accel=100.0,
    max_nozzle_temp=260.0, max_bed_temp=110.0,
)

CREALITY_CR10 = _creality_marlin(
    name="Creality CR-10",
    bed_size_x=300.0, bed_size_y=300.0, z_max=400.0,
    max_velocity=200.0, max_z_velocity=5.0, max_accel=500.0, max_z_accel=100.0,
    max_nozzle_temp=260.0, max_bed_temp=100.0,
)

CREALITY_K1 = _creality_klipper(
    name="Creality K1 / K1C",
    bed_size_x=220.0, bed_size_y=220.0, z_max=250.0,
    max_velocity=500.0, max_z_velocity=15.0, max_accel=20000.0, max_z_accel=500.0,
    max_nozzle_temp=300.0, max_bed_temp=100.0,
)

CREALITY_K1_MAX = _creality_klipper(
    name="Creality K1 Max",
    bed_size_x=300.0, bed_size_y=300.0, z_max=300.0,
    max_velocity=500.0, max_z_velocity=15.0, max_accel=20000.0, max_z_accel=500.0,
    max_nozzle_temp=300.0, max_bed_temp=100.0,
)


# Registry: key -> profile instance. Used by serve.py's printer selector.
PRINTER_PROFILES: dict[str, PrinterProfile] = {
    "trident": TRIDENT,
    "bambu_a1": BAMBU_A1,
    "bambu_a1_mini": BAMBU_A1_MINI,
    "bambu_p1s": BAMBU_P1S,
    "bambu_p2s": BAMBU_P2S,
    "bambu_x1c": BAMBU_X1C,
    "creality_ender3": CREALITY_ENDER3,
    "creality_ender3_v2": CREALITY_ENDER3_V2,
    "creality_ender3_s1": CREALITY_ENDER3_S1,
    "creality_ender3_v3_se": CREALITY_ENDER3_V3_SE,
    "creality_ender3_v3_ke": CREALITY_ENDER3_V3_KE,
    "creality_ender5_plus": CREALITY_ENDER5_PLUS,
    "creality_cr10": CREALITY_CR10,
    "creality_k1": CREALITY_K1,
    "creality_k1_max": CREALITY_K1_MAX,
}
