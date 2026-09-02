#!/usr/bin/env python3
"""Tests for serve.py's _parse_mesh_hybrid_params -- the request boundary for
the right-hand Planar base bar.

Plain ``python tools/test_serve_mesh_params.py``: prints PASS/FAIL per case,
exits non-zero on any failure. Mirrors test_orca_slice.py's style.

This function had no test at all before the Planar base bar grew from six
settings to thirty, which is exactly the wrong direction: it is the point
where an untrusted request body becomes numbers handed to an external
subprocess, and CLAUDE.md holds it to "server clamps must be at least as
strict as UI clamps" and "reject non-finite values at the boundary".

The two cross-file contract tests at the bottom are the most valuable ones
here. The chain browser -> serve.py -> build_process_json -> OrcaSlicer is
held together by three hand-written lists of field names in three different
languages; a name that appears in two of them and not the third produces a
control that silently half-works rather than an error anyone would notice.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve
from trident_gcode.orca_slice import build_process_json

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def parse(**body):
    body.setdefault("mesh_base_id", "m1")
    return serve._parse_mesh_hybrid_params(body, 200.0)


# Request field -> the build_process_json kwarg it must become. This IS the
# contract; if a rename breaks it the mapping test below fails loudly instead
# of the setting quietly doing nothing.
_FIELD_MAP = [
    ("mesh_base_outer_wall_speed", "outer_wall_speed", 45),
    ("mesh_base_inner_wall_speed", "inner_wall_speed", 60),
    ("mesh_base_infill_speed", "infill_speed", 80),
    ("mesh_base_travel_speed", "travel_speed", 150),
    ("mesh_base_first_layer_speed", "initial_layer_speed", 20),
    ("mesh_base_first_layer_infill_speed", "initial_layer_infill_speed", 30),
    ("mesh_base_solid_infill_speed", "internal_solid_infill_speed", 80),
    ("mesh_base_top_surface_speed", "top_surface_speed", 40),
    ("mesh_base_overhang_speed", "overhang_speed", 15),
    ("mesh_base_acceleration", "acceleration", 2500),
    ("mesh_base_lw_outer", "outer_wall_line_width", 0.4),
    ("mesh_base_lw_inner", "inner_wall_line_width", 0.45),
    ("mesh_base_lw_top", "top_surface_line_width", 0.4),
    ("mesh_base_lw_infill", "sparse_infill_line_width", 0.45),
    ("mesh_base_lw_solid", "internal_solid_infill_line_width", 0.45),
    ("mesh_base_seam_position", "seam_position", "back"),
    ("mesh_base_wall_sequence", "wall_sequence", "outer wall/inner wall"),
    ("mesh_base_top_pattern", "top_surface_pattern", "concentric"),
    ("mesh_base_bottom_pattern", "bottom_surface_pattern", "monotonic"),
    ("mesh_base_bridge_angle", "bridge_angle", 45),
    ("mesh_base_brim_type", "brim_type", "outer_only"),
    ("mesh_base_brim_width", "brim_width", 5),
    ("mesh_base_brim_gap", "brim_object_gap", 0.1),
    ("mesh_base_skirt_loops", "skirt_loops", 1),
    ("mesh_base_support_threshold", "support_threshold_angle", 45),
    ("mesh_base_support_type", "support_type", "tree(auto)"),
    ("mesh_base_support_top_z", "support_top_z_distance", 0.2),
    ("mesh_base_support_bottom_z", "support_bottom_z_distance", 0.2),
    ("mesh_base_support_interface_layers", "support_interface_top_layers", 2),
    ("mesh_base_support_interface_spacing", "support_interface_spacing", 0.5),
    ("mesh_base_support_xy", "support_object_xy_distance", 0.35),
    ("mesh_base_support_first_layer_gap", "support_object_first_layer_gap", 0.2),
]


def test_off_and_untouched():
    check(serve._parse_mesh_hybrid_params({}, 200.0) is None,
          "params: absent mesh_base_id means the feature is off entirely")

    p = parse()
    check(p["process_overrides"] == {},
          "params: a request with no optional field sends NO override at all "
          "-- an untouched control cannot change today's output",
          p["process_overrides"])

    # Blank strings are what an emptied number input actually sends if the
    # client ever stops omitting them. They must read as "unset", not as 0.
    p = parse(mesh_base_top_surface_speed="", mesh_base_support_xy="",
              mesh_base_seam_position="", mesh_base_brim_type="")
    check(p["process_overrides"] == {},
          "params: an empty string is unset, never a zero",
          p["process_overrides"])


def test_field_mapping():
    for field, key, value in _FIELD_MAP:
        p = parse(**{field: value})
        got = p["process_overrides"]
        check(list(got.keys()) == [key],
              f"params: {field} -> {key}, and nothing else",
              f"got {sorted(got)}")


def test_non_finite_rejected():
    # CLAUDE.md's central trap: max()/min() do not clamp a NaN, and
    # json.loads accepts the bare tokens NaN/Infinity from a request body, so
    # every one of these must RAISE rather than clamp.
    for field in ("mesh_base_top_surface_speed", "mesh_base_acceleration",
                  "mesh_base_support_xy", "mesh_base_bridge_angle",
                  "mesh_base_lw_outer", "mesh_base_skirt_loops",
                  "mesh_base_brim_gap"):
        for bad in (float("nan"), float("inf"), float("-inf")):
            try:
                parse(**{field: bad})
                check(False, f"params: {field}={bad!r} rejected",
                      "no exception -- a non-finite value passed the boundary")
            except ValueError:
                check(True, f"params: {field}={bad!r} rejected, not clamped")


def test_enums_and_ranges():
    for field, bad in (("mesh_base_seam_position", "sideways"),
                       ("mesh_base_wall_sequence", "outer-then-inner"),
                       ("mesh_base_top_pattern", "spaghetti"),
                       ("mesh_base_bottom_pattern", "spaghetti"),
                       ("mesh_base_support_type", "normal(manual)"),
                       ("mesh_base_brim_type", "enormous")):
        try:
            parse(**{field: bad})
            check(False, f"params: {field}={bad!r} rejected", "no exception")
        except ValueError:
            check(True, f"params: {field}={bad!r} rejected by the allow-list")

    # Geometry ranges are clamped here (they are slicer settings, not machine
    # limits, so a fixed range is correct for them).
    p = parse(mesh_base_support_xy=999, mesh_base_support_top_z=-5,
              mesh_base_skirt_loops=99, mesh_base_bridge_angle=400,
              mesh_base_brim_gap=-1)
    o = p["process_overrides"]
    check(o["support_object_xy_distance"] == 10.0,
          "params: support XY distance clamped to its range", o)
    check(o["support_top_z_distance"] == 0.0,
          "params: a negative support Z distance clamps to 0", o)
    check(o["skirt_loops"] == 10, "params: skirt loops clamped to its range", o)
    check(o["bridge_angle"] == 360.0, "params: bridge angle clamped to 360", o)
    check(o["brim_object_gap"] == 0.0, "params: a negative brim gap clamps to 0", o)

    # Speeds and acceleration are NOT given a ceiling here on purpose: a
    # mm/s figure typed into serve.py would be the module-constant machine
    # limit CLAUDE.md forbids. They are rejected only for being non-positive;
    # build_process_json applies the selected printer's own ceiling.
    for field in ("mesh_base_top_surface_speed", "mesh_base_acceleration"):
        for bad in (0, -10):
            try:
                parse(**{field: bad})
                check(False, f"params: {field}={bad} rejected", "no exception")
            except ValueError:
                check(True, f"params: {field}={bad} rejected as non-positive")

    p = parse(mesh_base_top_surface_speed=100000)
    check(p["process_overrides"]["top_surface_speed"] == 100000.0,
          "params: an over-limit speed passes THIS layer unchanged -- the "
          "machine ceiling belongs to build_process_json, which has the "
          "profile; serve.py must not carry a speed constant of its own",
          p["process_overrides"])


def test_support_enable_is_a_real_boolean():
    check("enable_support" not in parse()["process_overrides"],
          "params: support off by default sends nothing")
    check(parse(mesh_base_enable_support=True)["process_overrides"]
          ["enable_support"] is True,
          "params: enable_support is sent when ticked")
    # Its distances are still forwarded while support is off, so a saved
    # design's support numbers survive a toggle.
    p = parse(mesh_base_support_xy=0.35)
    check(p["process_overrides"].get("support_object_xy_distance") == 0.35,
          "params: support distances are forwarded even while support is off, "
          "so unticking the box does not silently discard them")


# ---------------------------------------------------------------------------
# Cross-file contract tests. See this module's docstring for why these matter
# more than any single clamp above.
# ---------------------------------------------------------------------------
def test_every_override_is_a_real_build_process_json_kwarg():
    """Anything serve.py can put in process_overrides must be a parameter
    build_process_json actually accepts.

    hybrid.py splats the dict straight in (``**process_overrides``), so a
    misspelled key is a TypeError raised in the middle of a real request,
    after the upload and the pre-flight checks -- long past the point where
    the message is useful. Every mapped field is driven through the parser
    here and checked against the live signature."""
    accepted = set(inspect.signature(build_process_json).parameters)
    for field, key, value in _FIELD_MAP:
        got = parse(**{field: value})["process_overrides"]
        for k in got:
            check(k in accepted,
                  f"contract: override {k!r} (from {field}) is a real "
                  "build_process_json parameter",
                  f"accepted: {sorted(accepted)}")
    check("enable_support" in accepted,
          "contract: enable_support is a real build_process_json parameter")


def test_designer_js_sends_names_serve_py_reads():
    """Every field in designer.js's MESH_BASE_OPTIONAL table must be a name
    _parse_mesh_hybrid_params actually looks for.

    The browser sends ``body[designField] = value`` for each row of that
    table, so a name the server does not read is a control that binds,
    persists and posts -- and is then silently dropped. There is no runtime
    error to notice; the setting simply never happens. Read out of the source
    rather than duplicated here, so the test cannot agree with a stale copy."""
    js = (ROOT / "viewer" / "designer.js").read_text(encoding="utf-8")
    m = re.search(r"var MESH_BASE_OPTIONAL = \[(.*?)\n  \];", js, re.S)
    if m is None:
        check(False, "contract: MESH_BASE_OPTIONAL table found in designer.js",
              "table not found -- was it renamed or reshaped?")
        return
    fields = re.findall(r"'[^']+',\s*'([^']+)'\s*,\s*'(?:num|sel)'", m.group(1))
    check(len(fields) >= 30,
          f"contract: MESH_BASE_OPTIONAL parsed ({len(fields)} rows)",
          f"only found {fields}")

    src = inspect.getsource(serve._parse_mesh_hybrid_params)
    for field in fields:
        check(f'"{field}"' in src,
              f"contract: designer.js sends {field}, serve.py reads it")

    # And the reverse direction: the checkbox is the one field the table
    # deliberately does not carry, so it must be sent separately.
    check("mesh_base_enable_support" in js,
          "contract: the support checkbox is sent outside the table")


def main() -> int:
    test_off_and_untouched()
    test_field_mapping()
    test_non_finite_rejected()
    test_enums_and_ranges()
    test_support_enable_is_a_real_boolean()
    test_every_override_is_a_real_build_process_json_kwarg()
    test_designer_js_sends_names_serve_py_reads()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
