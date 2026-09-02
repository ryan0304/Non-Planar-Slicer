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

# The remaining Orca enums this module exposes. Every value below was read
# back out of a LOCALLY INSTALLED OrcaSlicer (2.4.x) rather than typed from
# memory: the shipped process profiles under resources/profiles/*/process/
# were scanned for the values actually in use, and each literal was then
# confirmed to exist in the OrcaSlicer binary itself. An enum value Orca does
# not recognise is a silent-failure mode (the CLI can exit nonzero with no
# useful message -- see _PROFILE_FROM's note), so these are allow-lists for
# exactly the same reason _ALLOWED_BRIM_TYPES is one.
_ALLOWED_SEAM_POSITIONS = frozenset({"nearest", "aligned", "back", "random"})

# Orca 2.x replaced the older single wall_infill_order key with wall_sequence
# (+ is_infill_first). The base profiles this module inherits from still carry
# wall_infill_order; emitting wall_sequence ONLY when the caller actually picks
# one leaves the inherited default in play for every untouched request, so the
# two keys never end up fighting over a request nobody asked to change.
_ALLOWED_WALL_SEQUENCES = frozenset({
    "inner wall/outer wall", "outer wall/inner wall", "inner-outer-inner wall",
})

# Which perimeter generator Orca uses. "classic" keeps every wall one nozzle
# wide; "arachne" varies the bead width so thin features that fall between
# whole multiples of the line width are still filled rather than dropped.
# Both values appear in the shipped profiles (315 classic / 60 arachne) and in
# the binary. Emitted only when the caller picks one, so an untouched request
# keeps whatever the inherited base profile chose.
_ALLOWED_WALL_GENERATORS = frozenset({"classic", "arachne"})

# Shared by top_surface_pattern and bottom_surface_pattern -- Orca uses one
# enum for both.
_ALLOWED_SURFACE_PATTERNS = frozenset({
    "monotonic", "monotonicline", "concentric", "zig-zag", "alignedrectilinear",
    "rectilinear", "hilbertcurve", "archimedeanchords", "octagramspiral",
})

# Only the two AUTOMATIC support types are offered. Orca's "normal(manual)"
# and "tree(manual)" build support solely where the user has painted enforcers
# in Orca's own 3-D view -- something a CLI slice of a generated STL can never
# have. Offering them would be a control that silently produces no support at
# all, which is the dead-control failure mode this panel exists to avoid.
_ALLOWED_SUPPORT_TYPES = frozenset({"normal(auto)", "tree(auto)"})


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
    # --- Quality -----------------------------------------------------------
    outer_wall_line_width: float | None = None,
    inner_wall_line_width: float | None = None,
    top_surface_line_width: float | None = None,
    sparse_infill_line_width: float | None = None,
    internal_solid_infill_line_width: float | None = None,
    seam_position: str | None = None,
    wall_sequence: str | None = None,
    wall_generator: str | None = None,
    # --- Strength ----------------------------------------------------------
    top_surface_pattern: str | None = None,
    bottom_surface_pattern: str | None = None,
    bridge_angle: float | None = None,
    # --- Speed -------------------------------------------------------------
    initial_layer_infill_speed: float | None = None,
    internal_solid_infill_speed: float | None = None,
    top_surface_speed: float | None = None,
    overhang_speed: float | None = None,
    acceleration: float | None = None,
    # --- Support -----------------------------------------------------------
    support_type: str | None = None,
    support_top_z_distance: float | None = None,
    support_bottom_z_distance: float | None = None,
    support_interface_top_layers: int | None = None,
    support_interface_spacing: float | None = None,
    support_object_xy_distance: float | None = None,
    support_object_first_layer_gap: float | None = None,
    # --- Others ------------------------------------------------------------
    skirt_loops: int | None = None,
    brim_object_gap: float | None = None,
) -> dict:
    """Build an Orca process-settings dict, clamping every speed/accel key
    to this printer's own limits before serializing -- defense in depth on
    top of whatever clamping Orca itself would apply, since CLAUDE.md's
    "server clamps must be at least as strict" spirit extends naturally to
    "don't hand an external tool a number our own safety story says is
    unsafe" (this repo's own generators are held to the exact same rule).

    Every parameter below ``support_threshold_angle`` is OPTIONAL and
    defaults to None, meaning "emit nothing for this key and let the
    inherited base profile's own value stand". That is what keeps the
    parametric hybrid's output unchanged by this expansion: a caller that
    does not pass them produces exactly the JSON it produced before they
    existed. It is also what makes a blank UI field honest -- an untouched
    control cannot silently rewrite a setting it never showed a value for.

    Two different kinds of ceiling appear here and conflating them is the
    mistake to avoid. Speeds and acceleration are MACHINE limits, so they come
    from *profile* (``max_velocity`` / ``max_accel``) and never from a module
    constant, per CLAUDE.md. The support and brim distances are not machine
    limits at all -- they are slicer geometry, bounded here only to stop an
    absurd number reaching the subprocess -- so a fixed sanity range is the
    right thing for them. Line widths sit in between: they are bounded as a
    MULTIPLE of this profile's own nozzle diameter rather than in millimetres,
    because "how wide a bead can this nozzle lay" is a nozzle property.
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

    # Non-finite rejection for the non-speed numerics too, for the same reason
    # as _speed: a NaN "clamped" by max()/min() is still a NaN, so it is
    # refused at this boundary rather than carried into the JSON handed to the
    # subprocess.
    def _num(name, val, lo, hi):
        val = float(val)
        if not math.isfinite(val):
            raise ValueError(
                f"{name} must be a finite number, got {val!r} -- a non-finite "
                "value passes every comparison downstream instead of tripping "
                "it, so it is rejected here rather than clamped."
            )
        return max(lo, min(val, hi))

    def _enum(name, val, allowed):
        val = str(val)
        if val not in allowed:
            raise ValueError(
                f"Unknown {name} {val!r}; must be one of {sorted(allowed)}"
            )
        return val

    # The bound is a multiple of the profile's own nozzle diameter, not a
    # millimetre figure typed into this module. GcodeWriter's volumetric-flow
    # cap still applies to every replayed move regardless -- this is the outer
    # of two independent guards, not the only one.
    def _line_width(name, val):
        return _num(name, val, 0.05, profile.nozzle_diameter * 3.0)

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
    out = {
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

    # --- Optional keys: absent stays absent. See the docstring. -------------
    # Quality.
    if outer_wall_line_width is not None:
        out["outer_wall_line_width"] = (
            f"{_line_width('outer_wall_line_width', outer_wall_line_width):g}")
    if inner_wall_line_width is not None:
        out["inner_wall_line_width"] = (
            f"{_line_width('inner_wall_line_width', inner_wall_line_width):g}")
    if top_surface_line_width is not None:
        out["top_surface_line_width"] = (
            f"{_line_width('top_surface_line_width', top_surface_line_width):g}")
    if sparse_infill_line_width is not None:
        out["sparse_infill_line_width"] = (
            f"{_line_width('sparse_infill_line_width', sparse_infill_line_width):g}")
    if internal_solid_infill_line_width is not None:
        out["internal_solid_infill_line_width"] = (
            f"{_line_width('internal_solid_infill_line_width', internal_solid_infill_line_width):g}")
    if seam_position is not None:
        out["seam_position"] = _enum("seam position", seam_position, _ALLOWED_SEAM_POSITIONS)
    if wall_sequence is not None:
        out["wall_sequence"] = _enum("wall sequence", wall_sequence, _ALLOWED_WALL_SEQUENCES)
    if wall_generator is not None:
        out["wall_generator"] = _enum(
            "wall generator", wall_generator, _ALLOWED_WALL_GENERATORS)

    # Strength.
    if top_surface_pattern is not None:
        out["top_surface_pattern"] = _enum(
            "top surface pattern", top_surface_pattern, _ALLOWED_SURFACE_PATTERNS)
    if bottom_surface_pattern is not None:
        out["bottom_surface_pattern"] = _enum(
            "bottom surface pattern", bottom_surface_pattern, _ALLOWED_SURFACE_PATTERNS)
    if bridge_angle is not None:
        # Orca reads 0 as "pick the bridge direction automatically", which is
        # why the range starts at 0 instead of rejecting it.
        out["bridge_angle"] = f"{_num('bridge_angle', bridge_angle, 0.0, 360.0):g}"

    # Speed. Each of these is a machine-limited value, so it goes through
    # _speed and lands under profile.max_velocity exactly like the five above.
    if initial_layer_infill_speed is not None:
        out["initial_layer_infill_speed"] = (
            f"{_speed('initial_layer_infill_speed', initial_layer_infill_speed, None):g}")
    if internal_solid_infill_speed is not None:
        out["internal_solid_infill_speed"] = (
            f"{_speed('internal_solid_infill_speed', internal_solid_infill_speed, None):g}")
    if top_surface_speed is not None:
        out["top_surface_speed"] = (
            f"{_speed('top_surface_speed', top_surface_speed, None):g}")
    if overhang_speed is not None:
        # Orca splits overhang slowdown into four bands by how much of the
        # extrusion is unsupported. The 1/4 band ships as "0", which Orca reads
        # as "no slowdown, print at the normal wall speed" -- so it is left
        # alone deliberately, and this single control drives the three bands
        # that are genuinely overhanging. One number instead of four is a
        # simplification; the UI tooltip says so rather than implying this is
        # Orca's own one-to-one control.
        ov = _speed("overhang_speed", overhang_speed, None)
        out["overhang_2_4_speed"] = f"{ov:g}"
        out["overhang_3_4_speed"] = f"{ov:g}"
        out["overhang_4_4_speed"] = f"{ov:g}"
    if acceleration is not None:
        # profile.max_accel, NOT the derived `accel` above: a caller may ask
        # for more than this module's own conservative default, but never for
        # more than the machine itself declares.
        a = float(acceleration)
        if not math.isfinite(a):
            raise ValueError(
                f"acceleration must be a finite number, got {acceleration!r} "
                "-- a non-finite value passes every comparison downstream "
                "instead of tripping it, so it is rejected here rather than "
                "clamped."
            )
        if a <= 0.0:
            raise ValueError(f"acceleration must be positive, got {a!r}")
        out["default_acceleration"] = f"{min(a, profile.max_accel):g}"

    # Support. These only take effect while enable_support is on; they are
    # still emitted whenever supplied, because Orca ignoring a setting it is
    # not currently using is harmless, whereas dropping them here would make a
    # saved design's values silently vanish the moment support was toggled off.
    if support_type is not None:
        out["support_type"] = _enum("support type", support_type, _ALLOWED_SUPPORT_TYPES)
        if out["support_type"].startswith("tree"):
            # Orca refuses to slice organic/tree support whose tip diameter is
            # narrower than the support extrusion width, with the hard error
            # "Organic support tree tip diameter must not be smaller than
            # support material extrusion width". Its own default tip is 0.8 mm
            # while the inherited base profile's support_line_width is "96%",
            # which resolves against the line width -- so on a 0.8 mm nozzle
            # that is 0.96 * 0.84 = 0.806 mm and the two cross over. Verified
            # against a real OrcaSlicer 2.4 install: the same request slices on
            # a 0.4 nozzle and fails on a 0.8.
            #
            # So the tip is derived from the line width actually in use rather
            # than left to a default that only holds for small nozzles. The
            # 0.8 floor is ORCA'S OWN default, kept deliberately so the common
            # 0.4-nozzle case emits the value it already had and nothing about
            # its support geometry changes; the 1.2 factor is headroom over the
            # 0.96 the comparison uses. Neither is a machine limit -- both are
            # ratios against a width the caller supplied -- so per CLAUDE.md
            # they belong here rather than on the PrinterProfile.
            out["tree_support_tip_diameter"] = f"{max(0.8, line_width * 1.2):g}"
    if support_top_z_distance is not None:
        out["support_top_z_distance"] = (
            f"{_num('support_top_z_distance', support_top_z_distance, 0.0, 5.0):g}")
    if support_bottom_z_distance is not None:
        out["support_bottom_z_distance"] = (
            f"{_num('support_bottom_z_distance', support_bottom_z_distance, 0.0, 5.0):g}")
    if support_interface_top_layers is not None:
        out["support_interface_top_layers"] = str(int(
            _num("support_interface_top_layers", support_interface_top_layers, 0, 10)))
    if support_interface_spacing is not None:
        out["support_interface_spacing"] = (
            f"{_num('support_interface_spacing', support_interface_spacing, 0.0, 10.0):g}")
    if support_object_xy_distance is not None:
        out["support_object_xy_distance"] = (
            f"{_num('support_object_xy_distance', support_object_xy_distance, 0.0, 10.0):g}")
    if support_object_first_layer_gap is not None:
        out["support_object_first_layer_gap"] = (
            f"{_num('support_object_first_layer_gap', support_object_first_layer_gap, 0.0, 5.0):g}")

    # Others.
    if skirt_loops is not None:
        out["skirt_loops"] = str(int(_num("skirt_loops", skirt_loops, 0, 10)))
    if brim_object_gap is not None:
        out["brim_object_gap"] = f"{_num('brim_object_gap', brim_object_gap, 0.0, 5.0):g}"

    return out

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
