#!/usr/bin/env python3
"""Tests for trident_gcode/orca_slice.py's JSON-building functions.

Plain ``python tools/test_orca_slice.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors check_regression.py's/test_printer_import.py's
style.

Deliberately does NOT invoke the OrcaSlicer subprocess (slice_stl_to_gcode) --
that requires a real installed binary and is exercised only by the optional
tools/test_orca_live_integration.py, which skips when Orca isn't found. This
script only proves the pure JSON-shape/derivation functions behave correctly
for every PrinterProfile field they map, independent of whether Orca is
installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trident_gcode.profile import PrinterProfile, PRINTER_PROFILES
from trident_gcode.orca import FilamentSettings
from trident_gcode.orca_slice import (
    build_machine_json, build_process_json, build_filament_json, _machine_name,
)


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# build_machine_json: every value must derive from the given profile, never
# a hardcoded constant (CLAUDE.md's "no machine limit may be a module
# constant" rule).
# ---------------------------------------------------------------------------
def test_machine_json_derives_from_profile():
    trident = PrinterProfile()
    m = build_machine_json(trident)
    check(m["printable_area"] == ["0x0", "235x0", "235x235", "0x235"],
          "machine_json: bed shape matches TRIDENT bed size", m["printable_area"])
    # "from": "system" (not "User") is deliberate -- confirmed empirically
    # against a real OrcaSlicer install that "User" makes the CLI try to
    # resolve the profile against Orca's own user database, which silently
    # fails for a one-off generated file with no such database entry. See
    # orca_slice.py's _PROFILE_FROM docstring.
    check(m["type"] == "machine" and m["from"] == "system" and m["instantiation"] == "true",
          "machine_json: has the type/from/instantiation fields Orca requires",
          str({k: m.get(k) for k in ("type", "from", "instantiation")}))
    check(m["inherits"] == "fdm_klipper_common",
          "machine_json: klipper firmware inherits fdm_klipper_common", m["inherits"])
    check(float(m["printable_height"]) == trident.z_max,
          "machine_json: printable_height matches z_max", m["printable_height"])
    check(m["machine_max_speed_z"] == [f"{trident.max_z_velocity:g}"] * 2,
          "machine_json: max Z speed matches profile.max_z_velocity",
          m["machine_max_speed_z"])
    check(m["nozzle_diameter"] == [f"{trident.nozzle_diameter:g}"],
          "machine_json: nozzle diameter matches profile")
    check(m["gcode_flavor"] == "klipper",
          "machine_json: klipper firmware maps to klipper flavor")
    check(m["machine_start_gcode"] == "" and m["machine_end_gcode"] == "",
          "machine_json: start/end gcode left empty (this app supplies its own)")
    check(m["enable_arc_fitting"] == "0",
          "machine_json: arc fitting disabled (parser has no G2/G3 primitive)")

    bambu = PRINTER_PROFILES["bambu_a1"]
    m2 = build_machine_json(bambu)
    check(m2["printable_area"] != m["printable_area"],
          "machine_json: a different profile produces a different bed shape "
          "(never falls back to another machine's numbers)")
    check(m2["gcode_flavor"] == "marlin",
          "machine_json: non-klipper firmware maps to marlin flavor")
    check(m2["inherits"] == "fdm_machine_common",
          "machine_json: non-klipper firmware inherits fdm_machine_common", m2["inherits"])


# ---------------------------------------------------------------------------
# build_process_json: clamping and the infill-pattern allow-list.
# ---------------------------------------------------------------------------
def test_process_json_clamps_and_validates():
    profile = PrinterProfile()
    p = build_process_json(
        profile, layer_height=0.2, first_layer_height=0.24,
        wall_count=3, infill_density=0.15, infill_pattern="grid", line_width=0.42,
    )
    check(p["wall_loops"] == "3", "process_json: wall_loops passes through in range")
    check(p["sparse_infill_density"] == "15%", "process_json: infill density formatted as percent",
          p["sparse_infill_density"])
    # Confirmed empirically against a real OrcaSlicer install: without this,
    # slicing fails SILENTLY (no error message, no output, just a nonzero
    # exit) -- Orca never explains that it rejected the pairing because the
    # process profile's compatible_printers didn't list the machine's name.
    m = build_machine_json(profile)
    check(p["compatible_printers"] == [_machine_name(profile)] == [m["name"]],
          "process_json: compatible_printers matches the paired machine's "
          "exact name (silent-slice-failure prevention)",
          f"process={p['compatible_printers']} machine={m['name']}")

    p_over = build_process_json(
        profile, layer_height=0.2, first_layer_height=0.24,
        wall_count=99, infill_density=5.0, infill_pattern="gyroid", line_width=0.42,
    )
    check(p_over["wall_loops"] == "8", "process_json: wall_loops clamped to <= 8",
          p_over["wall_loops"])
    check(p_over["sparse_infill_density"] == "100%",
          "process_json: infill density clamped to <= 100%", p_over["sparse_infill_density"])

    try:
        build_process_json(
            profile, layer_height=0.2, first_layer_height=0.24,
            wall_count=3, infill_density=0.15, infill_pattern="not_a_real_pattern",
            line_width=0.42,
        )
        check(False, "process_json: unknown infill pattern is rejected", "no exception raised")
    except ValueError as e:
        check("not_a_real_pattern" in str(e),
              "process_json: unknown infill pattern is rejected", str(e))

    # Every speed/accel key must never exceed this printer's own ceiling,
    # even for a printer profile with unusually low limits.
    slow_profile = PrinterProfile(max_velocity=30.0, max_accel=500.0)
    p_slow = build_process_json(
        slow_profile, layer_height=0.2, first_layer_height=0.24,
        wall_count=3, infill_density=0.15, infill_pattern="grid", line_width=0.42,
    )
    check(float(p_slow["inner_wall_speed"]) <= slow_profile.max_velocity,
          "process_json: inner_wall_speed never exceeds profile.max_velocity",
          p_slow["inner_wall_speed"])
    check(float(p_slow["travel_speed"]) <= slow_profile.max_velocity,
          "process_json: travel_speed never exceeds profile.max_velocity",
          p_slow["travel_speed"])
    check(float(p_slow["default_acceleration"]) <= slow_profile.max_accel,
          "process_json: default_acceleration never exceeds profile.max_accel",
          p_slow["default_acceleration"])


# ---------------------------------------------------------------------------
# build_filament_json: defaults when no FilamentSettings given, and
# temperature clamping to the profile's own ceilings.
# ---------------------------------------------------------------------------
def test_filament_json_defaults_and_clamps():
    profile = PrinterProfile()
    f = build_filament_json(None, profile)
    check(f["nozzle_temperature"] == ["210"], "filament_json: default nozzle temp is 210",
          f["nozzle_temperature"])
    check(f["hot_plate_temp"] == ["60"], "filament_json: default bed temp is 60",
          f["hot_plate_temp"])

    hot_fs = FilamentSettings(name="TooHot", nozzle_temp=999.0, bed_temp=999.0)
    f_hot = build_filament_json(hot_fs, profile)
    check(float(f_hot["nozzle_temperature"][0]) <= profile.max_nozzle_temp,
          "filament_json: nozzle temp clamped to profile.max_nozzle_temp",
          f_hot["nozzle_temperature"])
    check(float(f_hot["hot_plate_temp"][0]) <= profile.max_bed_temp,
          "filament_json: bed temp clamped to profile.max_bed_temp",
          f_hot["hot_plate_temp"])

    cold_profile = PrinterProfile(max_nozzle_temp=50.0, max_bed_temp=10.0)
    f_cold = build_filament_json(None, cold_profile)
    check(float(f_cold["nozzle_temperature"][0]) <= cold_profile.max_nozzle_temp,
          "filament_json: even the DEFAULT 210 nozzle temp is clamped to a "
          "restrictive profile's ceiling, never a hardcoded 210",
          f_cold["nozzle_temperature"])



# ---------------------------------------------------------------------------
# The expanded OrcaSlicer process settings (Quality / Strength / Speed /
# Support / Others), added so the right-hand Planar base bar can offer the
# same groups OrcaSlicer's own Process tab does.
#
# The single most important property is the FIRST test below: a caller that
# passes none of them must produce byte-for-byte the same dict as before they
# existed. That is what lets a blank UI field honestly mean "OrcaSlicer's own
# default" and what keeps the parametric hybrid's behaviour untouched.
# ---------------------------------------------------------------------------
_BASE_KWARGS = dict(
    layer_height=0.2, first_layer_height=0.2, wall_count=3,
    infill_density=0.15, infill_pattern="grid", line_width=0.42,
)

# Every optional key this module can emit, with the kwarg that emits it.
# Listed explicitly rather than derived from the signature so that adding a
# parameter without adding it here is a visible omission in review, and so a
# renamed Orca key cannot slip through as "no test changed".
_OPTIONAL_EMITS = [
    ("outer_wall_line_width", "outer_wall_line_width", 0.4),
    ("inner_wall_line_width", "inner_wall_line_width", 0.45),
    ("top_surface_line_width", "top_surface_line_width", 0.4),
    ("sparse_infill_line_width", "sparse_infill_line_width", 0.45),
    ("internal_solid_infill_line_width", "internal_solid_infill_line_width", 0.45),
    ("seam_position", "seam_position", "back"),
    ("wall_sequence", "wall_sequence", "outer wall/inner wall"),
    ("wall_generator", "wall_generator", "arachne"),
    ("top_surface_pattern", "top_surface_pattern", "concentric"),
    ("bottom_surface_pattern", "bottom_surface_pattern", "monotonic"),
    ("bridge_angle", "bridge_angle", 45),
    ("initial_layer_infill_speed", "initial_layer_infill_speed", 30),
    ("internal_solid_infill_speed", "internal_solid_infill_speed", 80),
    ("top_surface_speed", "top_surface_speed", 40),
    ("acceleration", "default_acceleration", 2500),
    ("support_type", "support_type", "tree(auto)"),
    ("support_top_z_distance", "support_top_z_distance", 0.2),
    ("support_bottom_z_distance", "support_bottom_z_distance", 0.2),
    ("support_interface_top_layers", "support_interface_top_layers", 2),
    ("support_interface_spacing", "support_interface_spacing", 0.5),
    ("support_object_xy_distance", "support_object_xy_distance", 0.35),
    ("support_object_first_layer_gap", "support_object_first_layer_gap", 0.2),
    ("skirt_loops", "skirt_loops", 1),
    ("brim_object_gap", "brim_object_gap", 0.1),
    ("avoid_crossing_perimeters_max_detour", "avoid_crossing_perimeters_max_detour", "50%"),
]


def test_process_json_optional_settings():
    profile = PrinterProfile()

    # 1. Untouched controls change nothing. Every optional parameter defaults
    #    to None, and None must mean "emit no key at all" -- not "emit a
    #    zero", which would silently overwrite OrcaSlicer's own value.
    plain = build_process_json(profile, **_BASE_KWARGS)
    for kwarg, key, value in _OPTIONAL_EMITS:
        if key == "default_acceleration":
            continue  # always present; covered separately below
        check(key not in plain,
              f"process_json: {key} absent when {kwarg} is not passed",
              f"unexpectedly present as {plain.get(key)!r}")

    # 2. Each one, when passed, emits its own Orca key -- and ONLY its own.
    #    Catches a copy-paste that writes the neighbouring key's name.
    for kwarg, key, value in _OPTIONAL_EMITS:
        got = build_process_json(profile, **_BASE_KWARGS, **{kwarg: value})
        check(key in got, f"process_json: {kwarg} emits {key}")
        added = set(got) - set(plain)
        expected = set() if key == "default_acceleration" else {key}
        if kwarg == "support_type" and str(value).startswith("tree"):
            # The one deliberate pairing: tree support also pins a tip
            # diameter wide enough for the support extrusion width, or Orca
            # refuses the slice outright on a large nozzle. See test 8.
            expected.add("tree_support_tip_diameter")
        check(added == expected,
              f"process_json: {kwarg} adds only {sorted(expected)}",
              f"added {sorted(added)}")

    # 3. overhang_speed is the one control that is NOT one-to-one: it drives
    #    the three genuinely-overhanging bands and deliberately leaves Orca's
    #    own "no slowdown" 1/4 band alone. If that ever becomes four separate
    #    controls, this test is the thing that should fail.
    oh = build_process_json(profile, **_BASE_KWARGS, overhang_speed=15)
    check(oh.get("overhang_2_4_speed") == "15"
          and oh.get("overhang_3_4_speed") == "15"
          and oh.get("overhang_4_4_speed") == "15",
          "process_json: overhang_speed sets the 2/4, 3/4 and 4/4 bands",
          {k: v for k, v in oh.items() if k.startswith("overhang")})
    check("overhang_1_4_speed" not in oh,
          "process_json: overhang_speed leaves the 1/4 band at Orca's own "
          "'no slowdown' rather than slowing a fully-supported wall")

    # 4. Non-finite is REJECTED, never clamped -- CLAUDE.md's central trap.
    #    max()/min() let a NaN straight through, so each of these must raise.
    #    Checked on one parameter of every kind: a speed, an acceleration, a
    #    line width and a plain geometry number.
    for kwarg, bad in (
        ("top_surface_speed", float("nan")),
        ("top_surface_speed", float("inf")),
        ("acceleration", float("nan")),
        ("outer_wall_line_width", float("nan")),
        ("support_object_xy_distance", float("inf")),
        ("bridge_angle", float("nan")),
        ("avoid_crossing_perimeters_max_detour", "NaN"),
        ("avoid_crossing_perimeters_max_detour", "NaN%"),
        ("avoid_crossing_perimeters_max_detour", "not_a_number"),
    ):
        try:
            build_process_json(profile, **_BASE_KWARGS, **{kwarg: bad})
            check(False, f"process_json: {kwarg}={bad!r} rejected",
                  "no exception raised -- a non-finite value reached the JSON")
        except ValueError:
            check(True, f"process_json: {kwarg}={bad!r} rejected, not clamped")

    # 5. Unknown enum values fail loudly rather than reaching the subprocess.
    #    An unrecognised value makes the Orca CLI fail with no useful message,
    #    so the allow-list is the only place it can be diagnosed.
    for kwarg, bad in (
        ("seam_position", "sideways"),
        ("wall_sequence", "outer-then-inner"),
        ("wall_generator", "supersmart"),
        ("top_surface_pattern", "spaghetti"),
        ("bottom_surface_pattern", "spaghetti"),
        ("support_type", "normal(manual)"),   # real Orca value, deliberately not offered
    ):
        try:
            build_process_json(profile, **_BASE_KWARGS, **{kwarg: bad})
            check(False, f"process_json: {kwarg}={bad!r} rejected", "no exception")
        except ValueError:
            check(True, f"process_json: {kwarg}={bad!r} rejected by the allow-list")

    # 6. Machine ceilings come from the PROFILE, never a module constant.
    slow = PrinterProfile(max_velocity=40.0, max_accel=800.0)
    got = build_process_json(
        slow, **_BASE_KWARGS,
        top_surface_speed=500, internal_solid_infill_speed=500,
        initial_layer_infill_speed=500, overhang_speed=500, acceleration=99000)
    for key in ("top_surface_speed", "internal_solid_infill_speed",
                "initial_layer_infill_speed", "overhang_4_4_speed"):
        check(float(got[key]) <= slow.max_velocity,
              f"process_json: {key} clamped to this profile's max_velocity",
              got[key])
    check(float(got["default_acceleration"]) <= slow.max_accel,
          "process_json: acceleration clamped to this profile's max_accel",
          got["default_acceleration"])

    # A caller IS allowed to exceed this module's own conservative 3000 accel
    # default -- what it may never exceed is the machine's own figure.
    fast = PrinterProfile(max_accel=20000.0)
    got = build_process_json(fast, **_BASE_KWARGS, acceleration=9000)
    check(got["default_acceleration"] == "9000",
          "process_json: acceleration above the module's conservative default "
          "is allowed while under the machine's own max_accel",
          got["default_acceleration"])

    # 7. Line width is bounded by the NOZZLE, not by a millimetre constant --
    #    so the same request lands differently on a 0.4 and a 0.8 nozzle.
    narrow = build_process_json(
        PrinterProfile(nozzle_diameter=0.4), **_BASE_KWARGS,
        outer_wall_line_width=5.0)
    wide = build_process_json(
        PrinterProfile(nozzle_diameter=0.8), **_BASE_KWARGS,
        outer_wall_line_width=5.0)
    check(float(narrow["outer_wall_line_width"]) == 1.2,
          "process_json: line width capped at 3x a 0.4 nozzle",
          narrow["outer_wall_line_width"])
    check(float(wide["outer_wall_line_width"]) == 2.4,
          "process_json: the same request caps higher on a 0.8 nozzle -- the "
          "bound is the nozzle's, not a constant",
          wide["outer_wall_line_width"])

    # 8. Tree support must carry a tip diameter wide enough for the support
    #    extrusion width in use. Found by driving a real OrcaSlicer 2.4:
    #    picking Tree support on a 0.8 mm nozzle failed outright with
    #    "Organic support tree tip diameter must not be smaller than support
    #    material extrusion width", because Orca's default 0.8 mm tip is
    #    narrower than the inherited support_line_width of 96% (0.96 * 0.84 =
    #    0.806 mm). The same request slices on a 0.4 nozzle, so the bug is
    #    invisible until someone changes nozzle -- exactly the kind of
    #    machine-dependent break this repo derives from the profile instead.
    narrow_tree = build_process_json(
        PrinterProfile(nozzle_diameter=0.4),
        **{**_BASE_KWARGS, "line_width": 0.42},
        enable_support=True, support_type="tree(auto)")
    check(narrow_tree["tree_support_tip_diameter"] == "0.8",
          "process_json: a 0.4 nozzle keeps Orca's own 0.8 mm tree tip -- the "
          "fix must not change support geometry for the common case",
          narrow_tree.get("tree_support_tip_diameter"))

    wide_tree = build_process_json(
        PrinterProfile(nozzle_diameter=0.8),
        **{**_BASE_KWARGS, "line_width": 0.84},
        enable_support=True, support_type="tree(auto)")
    tip = float(wide_tree["tree_support_tip_diameter"])
    check(tip > 0.84 * 0.96,
          "process_json: a 0.8 nozzle widens the tree tip past the resolved "
          "support extrusion width, so Orca does not refuse the slice",
          f"tip {tip} vs support width {0.84 * 0.96}")

    # Normal support must NOT gain the key -- it is a tree-only setting, and
    # emitting it everywhere would be noise Orca has to ignore.
    normal = build_process_json(profile, **_BASE_KWARGS,
                                enable_support=True, support_type="normal(auto)")
    check("tree_support_tip_diameter" not in normal,
          "process_json: normal support does not get a tree tip diameter")

    # 9. bridge_angle 0 is a REAL value ("choose automatically"), not "unset".
    #    Absence is what means unset, so 0 must survive as an emitted key.
    zero = build_process_json(profile, **_BASE_KWARGS, bridge_angle=0)
    check(zero.get("bridge_angle") == "0",
          "process_json: bridge_angle 0 is emitted, not swallowed as unset",
          zero.get("bridge_angle"))


# ---------------------------------------------------------------------------
# Avoid crossing walls: avoid_crossing_perimeters is a hard-default bool
# (like enable_support -- ALWAYS emitted, unticked is a real value not
# "unset"); avoid_crossing_perimeters_max_detour is a normal optional
# mm-or-percent string, covered generically above but with its own
# percent-preservation and clamping specifics checked here.
# ---------------------------------------------------------------------------
def test_avoid_crossing_walls():
    profile = PrinterProfile()

    off = build_process_json(profile, **_BASE_KWARGS)
    check(off["avoid_crossing_perimeters"] == "0",
          "process_json: avoid_crossing_perimeters defaults to '0' (always "
          "emitted, like enable_support)", off["avoid_crossing_perimeters"])

    on = build_process_json(profile, **_BASE_KWARGS, avoid_crossing_perimeters=True)
    check(on["avoid_crossing_perimeters"] == "1",
          "process_json: avoid_crossing_perimeters=True emits '1'",
          on["avoid_crossing_perimeters"])

    pct = build_process_json(
        profile, **_BASE_KWARGS, avoid_crossing_perimeters_max_detour="50%")
    check(pct["avoid_crossing_perimeters_max_detour"] == "50%",
          "process_json: a percent detour round-trips with its '%' preserved",
          pct["avoid_crossing_perimeters_max_detour"])

    mm = build_process_json(
        profile, **_BASE_KWARGS, avoid_crossing_perimeters_max_detour="10")
    check(mm["avoid_crossing_perimeters_max_detour"] == "10",
          "process_json: a bare mm detour round-trips with no '%'",
          mm["avoid_crossing_perimeters_max_detour"])

    clamped_hi = build_process_json(
        profile, **_BASE_KWARGS, avoid_crossing_perimeters_max_detour="9999%")
    check(clamped_hi["avoid_crossing_perimeters_max_detour"] == "1000%",
          "process_json: an oversized percent detour clamps to 1000%, keeps '%'",
          clamped_hi["avoid_crossing_perimeters_max_detour"])

    clamped_lo = build_process_json(
        profile, **_BASE_KWARGS, avoid_crossing_perimeters_max_detour="-50")
    check(clamped_lo["avoid_crossing_perimeters_max_detour"] == "0",
          "process_json: a negative mm detour clamps to 0",
          clamped_lo["avoid_crossing_perimeters_max_detour"])

    try:
        build_process_json(
            profile, **_BASE_KWARGS, avoid_crossing_perimeters_max_detour="%")
        check(False, "process_json: a bare '%' with no number is rejected",
              "no exception raised")
    except ValueError:
        check(True, "process_json: a bare '%' with no number is rejected")


def main() -> int:
    test_machine_json_derives_from_profile()
    test_process_json_clamps_and_validates()
    test_process_json_optional_settings()
    test_avoid_crossing_walls()
    test_filament_json_defaults_and_clamps()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
