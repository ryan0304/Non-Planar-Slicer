#!/usr/bin/env python3
"""Tests for trident_gcode/mesh.py's STL loader boundary.

Plain ``python tools/test_mesh.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors tools/check_regression.py's and
tools/test_printer_import.py's style.

The subject is the uploaded-STL boundary, which is untrusted input reachable
today through serve.py's mesh upload paths. CLAUDE.md's rule is that
non-finite floats are rejected at the boundary and never clamped, because
every comparison against NaN is False: a NaN vertex survives min()/max(),
survives mesh_bounds (min(inf, nan) is inf, so the bounds look sane), and
survives GcodeWriter._check_bounds, which also compares. An Inf vertex is
just as bad -- it turns into NaN inside _edge_cross by subtraction.

Fixtures live in tools/fixtures/meshes/ (see README.md there).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tools" / "fixtures" / "meshes"
sys.path.insert(0, str(ROOT))

from trident_gcode.mesh import load_stl, mesh_bounds


_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def _fixture(name: str) -> str:
    return str(FIXTURES / name)


def _expect_value_error(name: str, label: str, tri_index: int) -> None:
    """Loading ``name`` must raise ValueError naming triangle ``tri_index``."""
    try:
        tris = load_stl(_fixture(name))
    except ValueError as exc:
        msg = str(exc)
        check(True, f"{label}: rejected with ValueError")
        check("non-finite" in msg,
              f"{label}: message says what was wrong (non-finite)", msg)
        check(f"triangle {tri_index}" in msg,
              f"{label}: message names the offending triangle index "
              f"({tri_index})", msg)
        check(msg.isascii(), f"{label}: message is ASCII-only", msg)
        return
    except Exception as exc:                      # noqa: BLE001 - report, don't mask
        check(False, f"{label}: rejected with ValueError",
              f"raised {type(exc).__name__}: {exc}")
        return
    # The load succeeding at all is the bug; say so with the evidence that
    # makes it dangerous rather than just "did not raise".
    bounds = mesh_bounds(tris)
    check(False, f"{label}: rejected with ValueError",
          f"loaded {len(tris)} triangles; mesh_bounds reports {bounds}")


# ---------------------------------------------------------------------------
# 1. valid_tetra.stl -- the control. A guard that also rejects good input is
#    not a fix, so this runs first and must load exactly as it always did.
# ---------------------------------------------------------------------------
def test_valid_control_mesh_still_loads():
    tris = load_stl(_fixture("valid_tetra.stl"))
    check(len(tris) == 4, "valid_tetra: loads 4 triangles", f"got {len(tris)}")

    flat = [c for tri in tris for v in tri for c in v]
    check(len(flat) == 36, "valid_tetra: 4 triangles x 3 vertices x 3 coords",
          f"got {len(flat)}")
    check(all(math.isfinite(c) for c in flat),
          "valid_tetra: every loaded coordinate is finite")

    lo, hi = mesh_bounds(tris)
    check(lo == (0.0, 0.0, 0.0), "valid_tetra: mesh_bounds low corner is (0,0,0)",
          str(lo))
    check(hi == (10.0, 8.0, 6.0), "valid_tetra: mesh_bounds high corner is (10,8,6)",
          str(hi))

    # The first facet, verbatim: the loader must keep the vertex triple and
    # drop the normal, not shift the window by three floats.
    check(tris[0] == ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (5.0, 8.0, 0.0)),
          "valid_tetra: first triangle's vertices are read (not the normal)",
          str(tris[0]))


# ---------------------------------------------------------------------------
# 2. The adversarial fixtures. Each must raise ValueError, not load and not
#    crash with something else -- the caller (serve.py) turns a ValueError
#    into an explained refusal.
# ---------------------------------------------------------------------------
def test_nan_binary_rejected():
    """A crafted binary STL carrying a NaN bit pattern.

    struct.unpack_from("<12f", ...) decodes NaN without complaint, and this
    is the silent case: with the vertex still in tris, mesh_bounds returns
    (0,0,0)-(10,8,6) exactly like the clean control, because min(inf, nan)
    is inf. Nothing downstream ever notices.
    """
    _expect_value_error("nan_binary.stl", "nan_binary", 2)


def test_inf_binary_rejected():
    """A crafted binary STL carrying +Inf in a Z coordinate."""
    _expect_value_error("inf_binary.stl", "inf_binary", 1)


def test_inf_ascii_rejected():
    """An ASCII STL whose Z coordinate is the token '1e999'.

    The vertex regex's character class is [\\d.eE+], which '1e999' is made
    entirely of, and float("1e999") is inf -- no exception, no warning. The
    textual twin of the json.loads-accepts-Infinity trap.
    """
    _expect_value_error("inf_ascii.stl", "inf_ascii", 1)


# ---------------------------------------------------------------------------
# 3. The rejection must be about the VALUES, not about the file format: both
#    loaders enforce it, so a mesh handed straight to either one is checked.
# ---------------------------------------------------------------------------
def test_both_loaders_enforce_the_rule():
    from trident_gcode import mesh as mesh_mod

    with open(_fixture("nan_binary.stl"), "rb") as fh:
        data = fh.read()
    raised = False
    try:
        mesh_mod._load_binary(data, 4)
    except ValueError:
        raised = True
    check(raised, "both_loaders: _load_binary rejects a NaN vertex directly")

    with open(_fixture("inf_ascii.stl"), encoding="utf-8") as fh:
        text = fh.read()
    raised = False
    try:
        mesh_mod._load_ascii(text)
    except ValueError:
        raised = True
    check(raised, "both_loaders: _load_ascii rejects a '1e999' vertex directly")

    # Other ASCII spellings of a non-finite value. The invariant under test is
    # not "every one of these raises" -- 'nan'/'inf' simply do not match the
    # vertex regex today, so that facet is dropped and an empty mesh comes
    # back, which later fails loudly as "no printable cross-sections found".
    # The invariant is the one that matters for the machine: NO spelling ever
    # results in a non-finite coordinate sitting in the returned triangles.
    # '1e999' is the one that both matches and converts, so it must raise.
    for token, must_raise in (("nan", False), ("inf", False), ("-inf", False),
                              ("Infinity", False), ("1e999", True),
                              ("+1e999", True)):
        text_t = ("solid s\n facet normal 0 0 1\n  outer loop\n"
                  "   vertex 0.0 0.0 0.0\n"
                  "   vertex 1.0 0.0 0.0\n"
                  f"   vertex 0.0 1.0 {token}\n"
                  "  endloop\n endfacet\nendsolid s\n")
        raised = False
        out: list = []
        try:
            out = mesh_mod._load_ascii(text_t)
        except ValueError:
            raised = True
        finite = all(math.isfinite(c) for tri in out for v in tri for c in v)
        check(raised or finite,
              f"both_loaders: ASCII vertex spelled {token!r} never yields a "
              f"non-finite coordinate", str(out))
        if must_raise:
            check(raised,
                  f"both_loaders: ASCII vertex spelled {token!r} is rejected "
                  f"outright", str(out))


# ---------------------------------------------------------------------------
# 4. The repo's own example meshes must be unaffected -- this change only
#    rejects previously-accepted INVALID input, so every real mesh in the
#    tree still loads. check_regression.py depends on that.
# ---------------------------------------------------------------------------
def test_repo_example_meshes_unaffected():
    for name in ("cylinder.stl", "star_prism.stl"):
        path = ROOT / "examples" / name
        if not path.is_file():
            check(False, f"examples/{name}: present", "file not found")
            continue
        try:
            tris = load_stl(str(path))
        except ValueError as exc:
            check(False, f"examples/{name}: still loads after the finite check",
                  str(exc))
            continue
        check(len(tris) > 0, f"examples/{name}: still loads after the finite check",
              f"got {len(tris)} triangles")
        lo, hi = mesh_bounds(tris)
        check(all(math.isfinite(v) for v in lo + hi),
              f"examples/{name}: mesh_bounds is finite", f"{lo} {hi}")


def main() -> int:
    test_valid_control_mesh_still_loads()
    test_nan_binary_rejected()
    test_inf_binary_rejected()
    test_inf_ascii_rejected()
    test_both_loaders_enforce_the_rule()
    test_repo_example_meshes_unaffected()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILED:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
