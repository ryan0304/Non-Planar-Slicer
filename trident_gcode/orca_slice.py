"""Shell out to the OrcaSlicer CLI to slice a planar base STL.

OrcaSlicer (AGPL-3.0, github.com/OrcaSlicer/OrcaSlicer) is invoked as an
external subprocess only -- its C++ source is never linked, embedded, or
forked. This keeps the AGPL isolated ("mere aggregation": calling an
installed binary and reading its output, not linking against its code) and
keeps this app's own "Python: standard library only" rule intact, since
Orca becomes an optional external tool a user installs separately, not a
pip dependency. Its G-code output is never trusted or emitted verbatim --
see orca_gcode_parser.py/orca_replay.py, which parse it into moves and
replay them through this app's own GcodeWriter, so every existing safety
check applies to the planar portion exactly as it does to the non-planar
portion.

Every machine limit written into the JSON profiles below comes from the
already-selected PrinterProfile -- never a module constant -- per
CLAUDE.md's "no machine limit may be a module constant" rule.

CLI flags (--slice/--arrange/--load-settings/--load-filaments/--outputdir)
were live-verified against a real installed OrcaSlicer 2.x during
development: the subprocess runs, parses the JSON profiles below, and
slices. The three JSON profiles inherit from OrcaSlicer's own bundled
generic "Custom" vendor base profiles (fdm_klipper_common/fdm_machine_common
for the machine, fdm_process_klipper_common/fdm_process_marlin_common for
the process, "Generic PLA @System" for the filament) rather than trying to
fully specify every one of Orca's ~200 settings keys from scratch -- this is
also how OrcaSlicer's own user-profile files work (a sparse overlay of only
what differs from a named base), verified by inspecting real profile files
under a local OrcaSlicer config directory. These base profile names ship
with every Orca install (the "Custom"/"OrcaFilamentLibrary" vendors exist
specifically for generic/non-branded printers) so this does not depend on
any one user's own saved profiles.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from .orca import FilamentSettings
from .profile import PrinterProfile


class OrcaSliceError(RuntimeError):
    """Raised for every OrcaSlicer subprocess failure mode.

    Every failure raises this with an ASCII, actionable message. There is
    no silent fallback to non-planar-only generation anywhere in this
    module -- a hybrid request either gets a real planar base or a clear
    error, never a partially-built print.
    """


def _machine_name(profile: PrinterProfile) -> str:
    """The machine profile's own name, shared with build_process_json() so
    its ``compatible_printers`` list can never drift out of sync with
    build_machine_json()'s ``name`` -- Orca silently refuses to slice (no
    error message at all, confirmed empirically against a real install) when
    a process profile's compatible_printers doesn't list the machine
    profile's exact name, so this MUST be the single source of truth for
    that string, never duplicated inline at either call site."""
    return f"trident-hybrid-{profile.name}"


# Confirmed empirically against a real OrcaSlicer install: "from": "User" on
# a profile handed to --load-settings/--load-filaments makes the CLI try to
# resolve it against Orca's own user profile database (matching how a
# profile saved from the GUI works) -- and since these profiles are
# generated fresh for each request with no corresponding database entry,
# that resolution fails SILENTLY (no error message, no output, a bare
# nonzero exit). "system" tells it to trust the file as a complete,
# self-contained definition instead, which is what a one-off generated
# profile actually is. Do not "fix" this back to "User" without re-testing
# against a real install -- it looks more semantically correct but breaks
# the CLI path entirely.
_PROFILE_FROM = "system"


# ---------------------------------------------------------------- machine ---
def build_machine_json(profile: PrinterProfile) -> dict:
    """Map a PrinterProfile onto an OrcaSlicer machine/printer settings dict.

    Every value here is derived from *profile* -- never hardcoded -- so a
    Bambu profile and the Trident profile each produce a machine JSON that
    respects that specific printer's own limits.
    """
    x0, y0 = 0.0, 0.0
    x1, y1 = profile.bed_size_x, profile.bed_size_y
    # A LIST of "XxY" corner strings -- confirmed against real OrcaSlicer
    # profile files (e.g. system/Custom/machine/MyKlipper 0.4 nozzle.json),
    # NOT a single comma-joined string.
    printable_area = [f"{x0:g}x{y0:g}", f"{x1:g}x{y0:g}", f"{x1:g}x{y1:g}", f"{x0:g}x{y1:g}"]
    is_klipper = profile.firmware == "klipper"
    base = "fdm_klipper_common" if is_klipper else "fdm_machine_common"
    flavor = "klipper" if is_klipper else "marlin"
    return {
        "type": "machine",
        "from": _PROFILE_FROM,
        "inherits": base,
        "instantiation": "true",
        "name": _machine_name(profile),
        "printer_settings_id": _machine_name(profile),
        "gcode_flavor": flavor,
        "printable_area": printable_area,
        "printable_height": f"{profile.z_max:g}",
        "nozzle_diameter": [f"{profile.nozzle_diameter:g}"],
        "machine_max_speed_x": [f"{profile.max_velocity:g}", f"{profile.max_velocity:g}"],
        "machine_max_speed_y": [f"{profile.max_velocity:g}", f"{profile.max_velocity:g}"],
        "machine_max_speed_z": [f"{profile.max_z_velocity:g}", f"{profile.max_z_velocity:g}"],
        "machine_max_acceleration_x": [f"{profile.max_accel:g}", f"{profile.max_accel:g}"],
        "machine_max_acceleration_y": [f"{profile.max_accel:g}", f"{profile.max_accel:g}"],
        "machine_max_acceleration_z": [f"{profile.max_z_accel:g}", f"{profile.max_z_accel:g}"],
        "nozzle_temperature_range_high": [f"{profile.max_nozzle_temp:g}"],
        "machine_start_gcode": "",   # this app supplies its own PRINT_START via GcodeWriter.header()
        "machine_end_gcode": "",     # ...and its own PRINT_END via GcodeWriter.footer()
        # NOT blanked to "": Orca's own klipper base relies on this hook to
        # emit a periodic "G92 E0" (relative-extrusion floating-point drift
        # reset) and refuses to slice with relative extrusion and no reset
        # point at all ("Add G92 E0 to layer_gcode"). Our own parser already
        # treats a mid-body G92 E0 as a harmless position reset (no move),
        # so this is safe to keep -- just trimmed to the one line that
        # matters, without Orca's own ";BEFORE_LAYER_CHANGE" comment noise.
        "before_layer_change_gcode": "G92 E0",
        "layer_change_gcode": "",
        # No arc primitive in orca_gcode_parser.py -- must stay disabled so a
        # G2/G3 in the output is a real bug to fix upstream, not something
        # silently mis-replayed as a straight line.
        "enable_arc_fitting": "0",
    }


# ---------------------------------------------------------------- process ---
_ALLOWED_INFILL_PATTERNS = frozenset({
    "grid", "line", "triangles", "cubic", "gyroid", "honeycomb",
    "concentric", "rectilinear",
})

# Orca's own brim_type vocabulary. An allow-list, not a block-list, matching
# printer_validate.py's posture and _ALLOWED_INFILL_PATTERNS above: an
# unrecognised value must fail loudly here rather than reach the subprocess.
_ALLOWED_BRIM_TYPES = frozenset({
    "no_brim", "outer_only", "inner_only", "outer_and_inner", "auto_brim",
})


def build_process_json(
    profile: PrinterProfile,
    *,
    layer_height: float,
    first_layer_height: float,
    wall_count: int,
    infill_density: float,
    infill_pattern: str,
    line_width: float,
    top_shell_layers: int = 0,
    bottom_shell_layers: int = 3,
    outer_wall_speed: float | None = None,
    inner_wall_speed: float | None = None,
    infill_speed: float | None = None,
    travel_speed: float | None = None,
    initial_layer_speed: float | None = None,
    brim_type: str = "no_brim",
    brim_width: float = 0.0,
    enable_support: bool = False,
    support_threshold_angle: int = 30,
) -> dict:
    """Build an Orca process-settings dict, clamping every speed/accel key
    to this printer's own limits before serializing -- defense in depth on
    top of whatever clamping Orca itself would apply, since CLAUDE.md's
    "server clamps must be at least as strict" spirit extends naturally to
    "don't hand an external tool a number our own safety story says is
    unsafe" (this repo's own generators are held to the exact same rule).
    """
    if infill_pattern not in _ALLOWED_INFILL_PATTERNS:
        raise ValueError(
            f"Unknown infill pattern {infill_pattern!r}; must be one of "
            f"{sorted(_ALLOWED_INFILL_PATTERNS)}"
        )
    if brim_type not in _ALLOWED_BRIM_TYPES:
        raise ValueError(
            f"Unknown brim type {brim_type!r}; must be one of "
            f"{sorted(_ALLOWED_BRIM_TYPES)}"
        )
    wall_count = max(1, min(int(wall_count), 8))
    infill_density = max(0.0, min(float(infill_density), 1.0))
    speed = min(profile.max_velocity, 150.0)  # a conservative print speed, never above the machine's own ceiling
    accel = min(profile.max_accel, 3000.0)

    # Every caller-supplied speed is clamped to THIS printer's own ceiling, and
    # a non-finite one is rejected outright rather than clamped: NaN survives
    # min()/max() untouched (every comparison against it is False), which is
    # the exact trap CLAUDE.md documents. None means "keep the derived default
    # below", so an untouched control cannot change today's output.
    #
    # profile.max_velocity is the ONLY ceiling here -- deliberately not a
    # module constant, so a machine declaring a lower limit gets that limit
    # and one declaring more is not capped at some other printer's number.
    def _speed(name, val, fallback):
        if val is None:
            return fallback
        val = float(val)
        if not math.isfinite(val):
            raise ValueError(
                f"{name} must be a finite number, got {val!r} -- a non-finite "
                "value passes every comparison downstream instead of tripping "
                "it, so it is rejected here rather than clamped."
            )
        if val <= 0.0:
            raise ValueError(f"{name} must be positive, got {val!r}")
        return min(val, profile.max_velocity)

    outer_v = _speed("outer_wall_speed", outer_wall_speed, min(speed, 60.0))
    inner_v = _speed("inner_wall_speed", inner_wall_speed, speed)
    infill_v = _speed("infill_speed", infill_speed, speed)
    travel_v = _speed("travel_speed", travel_speed, min(profile.max_velocity, 200.0))
    first_v = _speed("initial_layer_speed", initial_layer_speed, min(speed, 20.0))

    brim_width = max(0.0, float(brim_width))
    if not math.isfinite(brim_width):
        raise ValueError("brim_width must be a finite number")
    support_threshold_angle = max(0, min(int(support_threshold_angle), 90))
    base = "fdm_process_klipper_common" if profile.firmware == "klipper" else "fdm_process_marlin_common"
    return {
        "type": "process",
        "from": _PROFILE_FROM,
        "inherits": base,
        "instantiation": "true",
        "name": f"trident-hybrid-process-{profile.name}",
        "print_settings_id": f"trident-hybrid-process-{profile.name}",
        # Confirmed empirically: Orca silently refuses to slice (no error
        # message) unless this list contains the paired machine profile's
        # exact name -- see _machine_name()'s docstring.
        "compatible_printers": [_machine_name(profile)],
        "layer_height": f"{layer_height:g}",
        "initial_layer_height": f"{first_layer_height:g}",
        "line_width": f"{line_width:g}",
        "wall_loops": str(wall_count),
        "sparse_infill_density": f"{infill_density * 100:.0f}%",
        "sparse_infill_pattern": infill_pattern,
        "outer_wall_speed": f"{outer_v:g}",
        "inner_wall_speed": f"{inner_v:g}",
        "sparse_infill_speed": f"{infill_v:g}",
        "travel_speed": f"{travel_v:g}",
        "initial_layer_speed": f"{first_v:g}",
        "default_acceleration": f"{accel:g}",
        "brim_type": brim_type,
        "brim_width": f"{brim_width:g}",
        # Solid skins. The default of 0 top layers is kept ONLY because the
        # parametric hybrid's byte-locked output depends on it; it is not a
        # good default. The original reasoning ("the base is fully covered by
        # the non-planar wall above it") does not hold: the wall is a single
        # bead tracing one outline, so everything inside that outline is left
        # as exposed sparse infill. A mesh base is wider than the wall's
        # footprint and shows it plainly -- which is exactly the "the planar is
        # missing its top layer" report this parameter exists to fix.
        # build_mesh_hybrid_print passes a real value; see its docstring.
        "top_shell_layers": str(max(0, int(top_shell_layers))),
        "bottom_shell_layers": str(max(0, int(bottom_shell_layers))),
        # Support defaults OFF (today's behaviour, so nothing changes for an
        # existing design); exposed because an imported part CAN have real
        # overhangs, unlike the parametric path's simple silhouette.
        "enable_support": "1" if enable_support else "0",
        "support_threshold_angle": str(support_threshold_angle),
    }


# ---------------------------------------------------------------- filament -
def build_filament_json(fs: FilamentSettings | None, profile: PrinterProfile) -> dict:
    """Thin adapter from this app's own FilamentSettings to Orca's flat
    filament-settings JSON shape. When *fs* is None, falls back to the same
    nozzle/bed temperature defaults GcodeWriter itself uses."""
    nozzle_temp = (fs.nozzle_temp if fs and fs.nozzle_temp is not None else 210.0)
    bed_temp = (fs.bed_temp if fs and fs.bed_temp is not None else 60.0)
    nozzle_temp = min(nozzle_temp, profile.max_nozzle_temp)
    bed_temp = min(bed_temp, profile.max_bed_temp)
    retraction = (fs.retraction_length if fs and fs.retraction_length is not None else 0.6)
    name = fs.name if fs else "Generic PLA"
    return {
        "type": "filament",
        "from": _PROFILE_FROM,
        "inherits": "Generic PLA @System",
        "instantiation": "true",
        "name": f"trident-hybrid-{name}",
        "filament_settings_id": [name],
        "filament_type": [fs.filament_type if fs else "PLA"],
        "nozzle_temperature": [f"{nozzle_temp:g}"],
        "nozzle_temperature_initial_layer": [f"{nozzle_temp:g}"],
        "hot_plate_temp": [f"{bed_temp:g}"],
        "hot_plate_temp_initial_layer": [f"{bed_temp:g}"],
        "filament_flow_ratio": [f"{fs.flow_ratio:g}" if fs and fs.flow_ratio else "1"],
        "filament_retraction_length": [f"{retraction:g}"],
        "filament_diameter": [f"{profile.filament_diameter:g}"],
    }


# ------------------------------------------------------------- subprocess ---
def slice_stl_to_gcode(
    stl_bytes: bytes,
    *,
    machine_json: dict,
    process_json: dict,
    filament_json: dict,
    orca_path: str,
    timeout_s: float = 120.0,
) -> str:
    """Run OrcaSlicer on *stl_bytes*, returning the resulting G-code text.

    Raises OrcaSliceError for every failure mode: binary not found, a
    non-zero exit code, a timeout, or no/empty .gcode output. Never falls
    back to anything else -- see module docstring.
    """
    import subprocess  # local import: only the one function that needs it

    if not orca_path or not os.path.isfile(orca_path):
        raise OrcaSliceError(
            "OrcaSlicer not found. Install it and put it on PATH, or set "
            "TRIDENT_ORCA_PATH to the full path of the orca-slicer binary."
        )

    with tempfile.TemporaryDirectory(prefix="trident_hybrid_") as tmp:
        tmp_path = Path(tmp)
        stl_path = tmp_path / "base.stl"
        machine_path = tmp_path / "machine.json"
        process_path = tmp_path / "process.json"
        filament_path = tmp_path / "filament.json"
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        stl_path.write_bytes(stl_bytes)
        machine_path.write_text(json.dumps(machine_json), encoding="ascii")
        process_path.write_text(json.dumps(process_json), encoding="ascii")
        filament_path.write_text(json.dumps(filament_json), encoding="ascii")

        # See the CAUTION note at the top of this module: these flags are
        # taken from OrcaSlicer's own CLI documentation, not live-verified
        # against a real install in this repo's development environment.
        cmd = [
            orca_path,
            "--slice", "1",
            "--arrange", "0",
            "--load-settings", f"{machine_path};{process_path}",
            "--load-filaments", str(filament_path),
            "--outputdir", str(out_dir),
            str(stl_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout_s, check=False,
            )
        except subprocess.TimeoutExpired:
            raise OrcaSliceError(
                f"OrcaSlicer did not finish slicing within {timeout_s:.0f}s; "
                "the shape may be too complex for a hybrid base."
            ) from None
        except OSError as e:
            raise OrcaSliceError(f"Failed to run OrcaSlicer: {e}") from None

        if result.returncode != 0:
            stderr = result.stderr.decode("ascii", errors="replace")[:2000]
            raise OrcaSliceError(
                f"OrcaSlicer exited with code {result.returncode}: {stderr}"
            )

        gcode_files = sorted(out_dir.glob("*.gcode"))
        if not gcode_files:
            raise OrcaSliceError("OrcaSlicer produced no .gcode output.")
        text = gcode_files[0].read_text(encoding="ascii", errors="replace")
        if not text.strip():
            raise OrcaSliceError("OrcaSlicer produced an empty .gcode file.")
        return text
