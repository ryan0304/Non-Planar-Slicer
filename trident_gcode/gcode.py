"""G-code emission for multiple printer firmwares.

Supports Klipper (Voron Trident) and Marlin-based (Bambu Lab) printers.
Start/end G-code comes from the PrinterProfile's templates so the writer
itself is firmware-neutral.  Every travel/extrude move runs through feedrate
clamping so the Z axis is never asked to exceed ``max_z_velocity``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .profile import PrinterProfile
from .extrusion import clamp_feedrate_for_z, extrusion_for_segment


@dataclass
class GcodeWriter:
    profile: PrinterProfile
    line_width: float = 0.45
    layer_height: float = 0.30
    flow_multiplier: float = 1.0

    # Print settings forwarded to PRINT_START.
    bed_temp: float = 60.0
    nozzle_temp: float = 210.0
    material: str = "PLA"

    # Default speeds (mm/s).
    print_speed: float = 40.0
    travel_speed: float = 200.0
    first_layer_speed: float = 20.0

    # Hard floor for any commanded Z during the print (mm above bed). The
    # machine's z_min (-8) is only for homing; during a print the nozzle must
    # never go below the bed surface.
    min_print_z: float = 0.0

    # Max volumetric flow (mm^3/s). When > 0, extrusion speed is capped so the
    # hotend is never asked to melt more than this — typically imported from an
    # OrcaSlicer filament profile. 0 disables the cap.
    max_volumetric_speed: float = 0.0

    # Part-cooling fan (0..1). Fan is kept off for the first ``fan_first_layers_off``
    # turns/layers, then set to this fraction. 1.0 = full blast.
    fan_speed: float = 1.0
    fan_first_layers_off: int = 1

    # Retraction / unretract settings.
    retraction_length: float = 0.6      # mm
    retraction_speed: float = 60.0      # mm/s  (fast retract)
    unretract_speed: float = 40.0       # mm/s  (slow prime-back)

    # Wipe-on-retract: instead of a plain E-only retract, pull back
    # `retract_before_wipe` (0..1) of retraction_length in place, then move
    # `wipe_distance` mm opposite the last extrusion direction while pulling
    # back the remainder -- reduces the ooze blob left at the exact spot
    # extrusion stopped. wipe_distance <= 0 (the default) is a plain E-only
    # retract, byte-identical to before this feature existed.
    wipe_enabled: bool = False
    wipe_distance: float = 0.0          # mm
    retract_before_wipe: float = 1.0    # fraction 0..1

    # Pressure advance (klipper). None = don't emit SET_PRESSURE_ADVANCE.
    pressure_advance: float | None = None

    _lines: list[str] = field(default_factory=list)
    _x: float = 0.0
    _y: float = 0.0
    _z: float = 0.0
    _has_position: bool = False
    _total_e: float = 0.0
    _max_z_rate: float = 0.0
    _lh_clamp_events: int = 0
    _width_clamp_events: int = 0
    _temp_clamp_events: int = 0
    _last_fan_frac: float | None = None
    # Unit XY direction of the last EXTRUDING move, for the wipe direction
    # (wipe moves opposite the bead just laid). None until the first
    # extruding move with nonzero XY length has happened.
    _last_extrude_dir: tuple[float, float] | None = None
    _moves: list = field(default_factory=list)   # (x,y,z,extruding,speed_mm_s) per move
    _bounds: list[float] = field(
        default_factory=lambda: [1e9, 1e9, 1e9, -1e9, -1e9, -1e9]
    )

    # ------------------------------------------------------------------ header
    def header(self) -> "GcodeWriter":
        p = self.profile
        self._emit(f"; {p.name} continuous non-planar G-code")
        self._emit(f"; nozzle={p.nozzle_diameter} line_width={self.line_width} "
                   f"layer_height={self.layer_height}")
        self._emit(f"; max_z_velocity={p.max_z_velocity} mm/s (feedrate clamped to honour this)")
        self._emit("")
        # Never hand the profile's start_gcode a temperature above what the
        # printer's own heaters are rated for -- a custom-imported profile's
        # max_nozzle_temp/max_bed_temp is the safety ceiling, and clamping
        # here (rather than raising) keeps a slightly-too-hot filament choice
        # from aborting generation; the user just gets a visible NOTE instead.
        nozzle_t = min(self.nozzle_temp, p.max_nozzle_temp)
        bed_t = min(self.bed_temp, p.max_bed_temp)
        if nozzle_t != self.nozzle_temp:
            self._emit(f"; NOTE: nozzle temp {self.nozzle_temp:.0f} clamped to printer max {p.max_nozzle_temp:.0f}")
            self._temp_clamp_events += 1
        if bed_t != self.bed_temp:
            self._emit(f"; NOTE: bed temp {self.bed_temp:.0f} clamped to printer max {p.max_bed_temp:.0f}")
            self._temp_clamp_events += 1
        rendered = p.start_gcode.format_map({
            "nozzle_temp": nozzle_t,
            "bed_temp": bed_t,
            "material": self.material,
        })
        for line in rendered.splitlines():
            self._emit(line)
        if self.pressure_advance is not None:
            if p.pa_gcode_style == "marlin":
                self._emit(f"M900 K{self.pressure_advance}")
            else:
                self._emit(f"SET_PRESSURE_ADVANCE ADVANCE={self.pressure_advance}")
        self._emit("")
        return self

    def footer(self) -> "GcodeWriter":
        self._emit("")
        p = self.profile
        for line in p.end_gcode.splitlines():
            self._emit(line)
        self._emit(f"; total filament: {self._total_e:.1f} mm")
        return self

    # -------------------------------------------------------------- primitives
    def travel(self, x: float, y: float, z: float | None = None) -> None:
        z = self._z if z is None else z
        speed = self._allowed_speed(x, y, z, self.travel_speed)
        self._move(x, y, z, e=None, speed=speed, comment="travel")

    def extrude_to(
        self,
        x: float,
        y: float,
        z: float,
        speed: float | None = None,
        layer_height_override: float | None = None,
        flow_override: float = 1.0,
        line_width_override: float | None = None,
    ) -> None:
        if not self._has_position:
            # Nothing to extrude from yet; just position.
            self.travel(x, y, z)
            return
        dx, dy, dz = x - self._x, y - self._y, z - self._z
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        # Gap-aware local layer height: use the override (true vertical gap under
        # this move) for BOTH the E volume and the volumetric-flow speed cap.
        # Clamp to [0.25, 1.5]x nominal so a pathological gap can't blow up flow
        # or starve the bead; count how often that clamp bites.
        lh = self.layer_height
        if layer_height_override is not None:
            lo, hi = 0.25 * self.layer_height, 1.5 * self.layer_height
            clamped = min(max(layer_height_override, lo), hi)
            if clamped != layer_height_override:
                self._lh_clamp_events += 1
            lh = clamped
        # Variable line width: same contract as layer_height_override -- an
        # absolute mm value clamped to a band around nominal, used for BOTH the
        # E volume and the volumetric-flow speed cap so the melt-limit clamp
        # always tracks the actual bead cross-section, never the nominal one.
        # Wider allowed band than layer-height's [0.25,1.5]x: width variation is
        # a deliberate feature here, not an incidental gap correction.
        lw = self.line_width
        if line_width_override is not None:
            lo, hi = 0.5 * self.line_width, 2.0 * self.line_width
            clamped = min(max(line_width_override, lo), hi)
            if clamped != line_width_override:
                self._width_clamp_events += 1
            lw = clamped
        e = extrusion_for_segment(
            length, lw, lh,
            self.profile, self.flow_multiplier * flow_override,
        )
        req = self.print_speed if speed is None else speed
        # Cap by volumetric flow first (filament melt limit), then by Z velocity.
        # The extra first-layer flow factor raises the volume per mm, so fold it
        # into the flow cap too or the hotend could be out-run.
        if self.max_volumetric_speed > 0.0:
            area = lw * lh * flow_override
            if area > 0.0:
                req = min(req, self.max_volumetric_speed / area)
        allowed = self._allowed_speed(x, y, z, req)
        self._move(x, y, z, e=e, speed=allowed, comment=None)

    def safe_lift(self, z: float) -> None:
        """Raise Z to a safe height without moving XY, before the first travel.

        Decouples the print from wherever PRINT_START left the toolhead — the
        nozzle climbs straight up before crossing the bed. Emitted to G-code only
        (not recorded as a path move, so exports stay clean).
        """
        z = min(z, self.profile.z_max)
        # Pure-Z move: the whole feedrate lands on the Z axis, so cap it at
        # max_z_velocity rather than travel speed (which would be 8x over).
        f = min(self.travel_speed, self.profile.max_z_velocity) * 60.0
        self._emit(f"G1 Z{z:.4f} F{f:.0f}  ; initial safe lift")
        self._z = z

    def retract(self, mm: float | None = None, speed: float | None = None) -> None:
        mm = self.retraction_length if mm is None else mm
        speed = self.retraction_speed if speed is None else speed
        if (self.wipe_enabled and self.wipe_distance > 0.0
                and self._has_position and self._last_extrude_dir is not None):
            self._wipe_retract(mm, speed)
            return
        self._emit(f"G1 E-{mm:.4f} F{speed * 60:.0f}")

    def _wipe_retract(self, mm: float, speed: float) -> None:
        """Split a retract into an in-place pull-back plus a short wipe move
        opposite the last extrusion direction, so the ooze blob left at the
        exact spot extrusion stopped gets dragged into the wipe line instead
        of sitting on the part. ``retract_before_wipe`` (0..1) is retracted
        in place first; the remainder is retracted DURING the wipe move.

        Routed through ``_move()`` -- the wipe move is the one new place a
        "retract" changes XY, and it must go through the same bounds/non-
        finite checks as every other move rather than a second, unchecked
        path. ``_move()`` also folds ``e`` into ``_total_e`` (its normal job
        for a real extrusion), which a retract's negative delta must NOT
        do -- no other retract/unretract call counts toward filament used,
        so this one is corrected back immediately after, keeping the wipe
        retract exactly as filament-neutral in the report as a plain one.
        """
        before_frac = min(max(self.retract_before_wipe, 0.0), 1.0)
        before_mm = mm * before_frac
        remain_mm = mm - before_mm
        if before_mm > 0.0:
            self._emit(f"G1 E-{before_mm:.4f} F{speed * 60:.0f}")
        dx, dy = self._last_extrude_dir
        wx = self._x - dx * self.wipe_distance
        wy = self._y - dy * self.wipe_distance
        e = -remain_mm if remain_mm > 1e-9 else None
        total_e_before = self._total_e
        self._move(wx, wy, self._z, e=e, speed=speed, comment="wipe")
        self._total_e = total_e_before

    def unretract(self, mm: float | None = None, speed: float | None = None) -> None:
        mm = self.retraction_length if mm is None else mm
        speed = self.unretract_speed if speed is None else speed
        self._emit(f"G1 E{mm:.4f} F{speed * 60:.0f}")

    def set_fan(self, fraction: float) -> None:
        """Emit M106 with S value clamped to 0-255 from a 0..1 fraction."""
        fraction = min(1.0, max(0.0, fraction))
        s = int(round(fraction * 255))
        self._emit(f"M106 S{s}  ; fan {fraction:.0%}")
        self._last_fan_frac = fraction

    def set_fan_if_changed(self, fraction: float, threshold: float = 0.03) -> None:
        """Like set_fan(), but only emits M106 when the fraction has moved by
        more than ``threshold`` since the last emitted value.

        Used for continuously-modulated fan speed (e.g. overhang-adaptive
        cooling, evaluated once per wall point) so the fan doesn't get one
        M106 line per point -- only when the commanded speed actually moves
        enough to matter.
        """
        fraction = min(1.0, max(0.0, fraction))
        if self._last_fan_frac is None or abs(fraction - self._last_fan_frac) >= threshold:
            self.set_fan(fraction)

    def dwell(self, ms: int) -> None:
        """Emit G4 dwell. No position change."""
        self._emit(f"G4 P{ms}")

    def comment(self, text: str) -> None:
        self._emit(f"; {text}")

    # ------------------------------------------------------------------ guts
    def _allowed_speed(self, x, y, z, requested) -> float:
        if not self._has_position:
            return min(requested, self.profile.max_velocity)
        return clamp_feedrate_for_z(
            x - self._x, y - self._y, z - self._z, requested, self.profile
        )

    def _move(self, x, y, z, e, speed, comment) -> None:
        self._check_bounds(x, y, z)
        # Track the Z-rate of *extruding* moves only. Pure-Z travel plunges are
        # always clamped to exactly max_z_velocity and would otherwise mask the
        # real figure of interest: how fast Z moves while laying plastic.
        if self._has_position and e is not None:
            dx, dy, dz = x - self._x, y - self._y, z - self._z
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5
            if dist > 0.0:
                self._max_z_rate = max(self._max_z_rate, speed * abs(dz) / dist)
                # Direction of the last EXTRUDING move (positive flow only --
                # a retract's own negative e has no XY component anyway), for
                # _wipe_retract()'s wipe direction (opposite the bead just laid).
                xy_dist = math.hypot(dx, dy)
                if e > 0.0 and xy_dist > 1e-9:
                    self._last_extrude_dir = (dx / xy_dist, dy / xy_dist)
        f = speed * 60.0  # mm/s -> mm/min
        parts = ["G1", f"X{x:.4f}", f"Y{y:.4f}", f"Z{z:.4f}"]
        if e is not None:
            parts.append(f"E{e:.5f}")
            self._total_e += e
        # Same NaN trap as _check_bounds' Z guard, on the two remaining numbers
        # that reach the machine. A non-finite feedrate formats as "Fnan" and a
        # non-finite extrusion as "Enan" -- neither is caught by any comparison
        # (every comparison against NaN is False), and a non-finite print_speed
        # used to produce tens of thousands of Fnan moves that the analyzer then
        # reported as having no issues. Checked here, at the single choke point
        # every emitted move passes through, so no caller can route around it.
        if not math.isfinite(f):
            raise ValueError(f"Move feedrate F={f!r} is not a finite number")
        if f <= 0:
            # Defense in depth, same choke point as the non-finite guard
            # above: a non-positive feedrate is not a slow move, it is one
            # Klipper cannot execute at all (F0) or a move direction the
            # firmware was never asked to make (negative F) -- and unlike
            # NaN, 0 and negative numbers pass every min()/max() clamp
            # upstream silently, so this must be a hard reject, not a clamp.
            # A caller-level bug (e.g. print_speed <= 0 reaching here despite
            # serve.py's own boundary check) must fail here rather than emit
            # thousands of unexecutable moves.
            raise ValueError(f"Move feedrate F={f!r} must be greater than 0")
        if e is not None and not math.isfinite(e):
            raise ValueError(f"Move extrusion E={e!r} is not a finite number")
        parts.append(f"F{f:.0f}")
        line = " ".join(parts)
        if comment:
            line += f"  ; {comment}"
        self._emit(line)
        # Record the move so the path can be re-exported (e.g. to FullControl).
        self._moves.append((x, y, z, e is not None, speed))
        self._x, self._y, self._z = x, y, z
        self._has_position = True

    def _check_bounds(self, x, y, z) -> None:
        p = self.profile
        if not (p.print_min_x - 1e-6 <= x <= p.print_max_x + 1e-6
                and p.print_min_y - 1e-6 <= y <= p.print_max_y + 1e-6):
            raise ValueError(
                f"Move to X{x:.2f} Y{y:.2f} is outside the safe print area "
                f"[{p.print_min_x}-{p.print_max_x}] x [{p.print_min_y}-{p.print_max_y}]"
            )
        # A non-finite Z must be rejected the same way X/Y already are. X/Y use
        # a single chained comparison (`lo <= v <= hi`), which is False for NaN
        # and so correctly falls into the "outside safe area" branch above.
        # This used to be two separate `>` / `<` tests instead -- both are
        # ALSO False for NaN, so neither ever fired and a non-finite Z sailed
        # through silently. This is the last guard between a bad number and
        # the machine; it must not depend on serve.py having caught it first.
        if not math.isfinite(z):
            raise ValueError(f"Move to Z={z!r} is not a finite number")
        if z > p.z_max + 1e-6:
            raise ValueError(f"Move to Z{z:.2f} exceeds Z max {p.z_max}")
        if z < self.min_print_z - 1e-6:
            raise ValueError(
                f"Move to Z{z:.2f} is below the print floor {self.min_print_z} "
                f"(nozzle would crash into the bed). Lower z-amp or increase the base height."
            )
        b = self._bounds
        b[0], b[1], b[2] = min(b[0], x), min(b[1], y), min(b[2], z)
        b[3], b[4], b[5] = max(b[3], x), max(b[4], y), max(b[5], z)

    def _emit(self, line: str) -> None:
        self._lines.append(line)

    # ------------------------------------------------------------------ output
    @property
    def bounds(self) -> dict:
        b = self._bounds
        return {"min": (b[0], b[1], b[2]), "max": (b[3], b[4], b[5])}

    @property
    def total_filament_mm(self) -> float:
        return self._total_e

    @property
    def max_z_rate(self) -> float:
        """Highest Z-axis velocity (mm/s) actually commanded. Must be <= max_z_velocity."""
        return self._max_z_rate

    @property
    def moves(self) -> list:
        """Recorded moves as (x, y, z, extruding, speed_mm_s) tuples."""
        return self._moves

    @property
    def position(self) -> tuple[float, float, float]:
        """Current (x, y, z) of the toolhead, as tracked internally."""
        return (self._x, self._y, self._z)

    @property
    def layer_height_clamp_events(self) -> int:
        """How many extrude moves had their gap-aware layer_height_override
        clamped to the [0.25, 1.5]x nominal band. >0 means some geometry asked
        for a local layer height outside safe bounds and flow was compensated."""
        return self._lh_clamp_events

    @property
    def width_clamp_events(self) -> int:
        """How many extrude moves had their line_width_override clamped to the
        [0.5, 2.0]x nominal band. >0 means a requested width curve exceeded the
        safe band and was compensated."""
        return self._width_clamp_events

    @property
    def temp_clamp_events(self) -> int:
        """How many of nozzle_temp/bed_temp were clamped down to the profile's
        max_nozzle_temp/max_bed_temp ceiling (0, 1, or 2). >0 means the
        requested print temperature exceeded what this printer's heaters are
        rated for and generation silently capped it instead of failing."""
        return self._temp_clamp_events

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.text())
