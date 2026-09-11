#!/usr/bin/env python3
"""Cross-component integration tests for this development phase.

Every other tools/test_*.py in this repo exercises one unit in isolation, with
the Orca subprocess monkeypatched away. This file is the opposite on purpose:
it starts a REAL server on a scratch port and drives it over HTTP, asserting
that the pieces built in this phase behave correctly *together with the
features that already existed*, not just on their own.

The four things this phase touched:
  1. the preset bar (client-only; its server-visible effect is that a preset's
     field set generates cleanly, which is asserted here as a real request)
  2. uploaded meshes surviving + a request-size ceiling + a per-session mesh
     cache (serve.py)
  3. issue severity / "issues_detail" (serve.py, consumed by the viewer)
  4. the surface/conformal adhesion package (generators/surface_spiral.py,
     wired through serve.py)

...and the combinations that matter, e.g. a mesh planar base AND loop fabric
AND zone overrides in one request, which has to produce a hybrid seam, real
fabric, the right scope notes, and the right severities all at once.

Requires no OrcaSlicer for most cases; the hybrid cases are skipped with an
explicit SKIP line when no Orca binary is available, rather than failing.

Plain ``python tools/test_phase_integration.py``: prints PASS/FAIL per case,
exits non-zero on any failure.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from self_test import Server, post, get  # real-server harness, reused

PORT = 8791          # scratch port: never the dev server's 8777
MESH = ROOT / "tools" / "fixtures" / "meshes" / "hex_mount.stl"

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


def skip(label: str, why: str) -> None:
    print(f"SKIP  {label}  ({why})")


def upload(srv: Server, path: Path, session: str | None = None):
    headers = {"Content-Type": "application/octet-stream",
               "X-Filename": path.name}
    if session:
        headers["X-Trident-Session"] = session
    req = urllib.request.Request(srv.url("/api/upload_mesh"),
                                 path.read_bytes(), headers)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=180).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def gen(srv: Server, body, session: str | None = None, timeout: int = 600):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if session:
        headers["X-Trident-Session"] = session
    req = urllib.request.Request(srv.url("/api/generate"), data, headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def sevs(j):
    return [d["severity"] for d in (j.get("issues_detail") or [])]


def orca_available(srv: Server) -> bool:
    st, j = get(srv, "/api/orca_status")
    return st == 200 and bool(j.get("available"))


# ---------------------------------------------------------------------------
# 1. The severity payload exists on EVERY generate endpoint, not just one.
# ---------------------------------------------------------------------------
def t_severity_on_all_endpoints(srv: Server) -> None:
    print("\n-- issues_detail is present on every generate path ------------")

    # (a) parametric
    st, j = gen(srv, {"shape": "circle", "radius": 25, "height": 40,
                      "layer_height": 0.3, "printer": "trident",
                      "base_layers": 2})
    check(st == 200, "parametric generate succeeds", str(st))
    check("issues_detail" in j, "parametric response carries issues_detail")
    check(all(isinstance(x, str) for x in j.get("issues", [])),
          "parametric 'issues' is STILL a flat list of strings "
          "(existing readers must not break)")

    # (b) surface -- also the Item 4 path
    st, j = gen(srv, {"mode": "surface", "surface": "dome", "radius": 20,
                      "surface_amp": 2.0, "layer_height": 0.3,
                      "printer": "trident"})
    check(st == 200, "surface generate succeeds", str(st))
    check("issues_detail" in j, "surface response carries issues_detail")
    check(len(j.get("issues_detail") or []) == len(j.get("issues") or []),
          "surface: detail and flat arrays are the same length",
          f"{len(j.get('issues_detail') or [])} vs {len(j.get('issues') or [])}")

    # (c) mesh_texture
    up = upload(srv, MESH)
    mid = up.get("mesh_id")
    check(bool(mid), "mesh uploads for the mesh_texture path")
    if mid:
        st, j = gen(srv, {"mode": "mesh_texture", "mesh_id": mid,
                          "layer_height": 0.3, "points_per_turn": 120,
                          "printer": "trident"})
        check(st == 200, "mesh_texture generate succeeds", str(st))
        check("issues_detail" in j, "mesh_texture response carries issues_detail")


# ---------------------------------------------------------------------------
# 2. Severity actually discriminates on real designs.
# ---------------------------------------------------------------------------
def t_severity_discriminates(srv: Server) -> None:
    print("\n-- severity separates risk from advice on REAL output ---------")

    # A fast, heavily-waved design trips machine-limit findings (warn),
    # never mere notes.
    st, j = gen(srv, {"shape": "circle", "radius": 32, "height": 60,
                      "layer_height": 0.3, "printer": "trident",
                      "z_waves": 25, "print_speed": 150, "base_layers": 3,
                      "amp_profile": [[0, 0], [0.5, 0.9], [1, 0.2]]})
    check(st == 200, "aggressive design still generates", str(st))
    got = sevs(j)
    check("warn" in got,
          "an aggressive design yields at least one WARN", str(got))
    check("danger" not in got,
          "...and nothing here is a DANGER (the generator refuses those "
          "outright rather than warning)", str(got))

    # Loop fabric + settings it cannot honour yields NOTES, not warnings --
    # that difference is the entire point of the severity work.
    st, j = gen(srv, {"shape": "circle", "radius": 32, "height": 45,
                      "layer_height": 0.3, "printer": "trident",
                      "loop_per_turn": 16, "loop_row": 0.7, "loop_up": 1,
                      "loop_mode": "dip", "loop_align": "stagger",
                      "loop_cuff": 3, "loop_flow": 1.2, "loop_speed": 10,
                      "loop_out": 0.5, "loop_rejoin": 2, "loop_waves": 12,
                      "loop_fade_in": 0.1, "loop_jitter": 0.5,
                      "loop_turn_stride": 1,
                      "base_layers": 2,
                      "zone_overrides": [{"t_lo": 0.2, "t_hi": 0.6,
                                          "pattern": "diamond"}],
                      "radius_speed_comp": True})
    check(st == 200, "loop fabric with unhonoured settings generates", str(st))
    got = sevs(j)
    check(got and all(s == "note" for s in got),
          "every scope restriction is a NOTE, never a warning",
          str(list(zip(got, [d["text"][:40] for d in j["issues_detail"]]))))

    # And each of those notes points at a control that exists.
    html = (ROOT / "viewer" / "index.html").read_text(encoding="utf-8")
    for d in j["issues_detail"]:
        if d.get("control"):
            check(('id="%s"' % d["control"]) in html,
                  f"note points at a real control (#{d['control']})",
                  d["text"][:50])


# ---------------------------------------------------------------------------
# 3. The mesh cache and the body cap, with everything else still working.
# ---------------------------------------------------------------------------
def t_mesh_isolation_with_real_generates(srv: Server) -> None:
    print("\n-- per-session mesh cache under real generate load ------------")
    A, B = "aaaaaaaa1111", "bbbbbbbb2222"

    up_a = upload(srv, MESH, session=A)
    mid_a = up_a.get("mesh_id")
    check(bool(mid_a), "session A uploads a mesh")

    # B floods well past the OLD global cap of 4.
    for _ in range(8):
        upload(srv, MESH, session=B)

    # A's mesh must still drive a real generate, not just a lookup.
    st, j = gen(srv, {"mode": "mesh_texture", "mesh_id": mid_a,
                      "layer_height": 0.3, "points_per_turn": 120,
                      "printer": "trident"}, session=A)
    check(st == 200,
          "session A can still GENERATE with its mesh after B flooded the "
          "cache (the old global bucket of 4 evicted it)", str(st))
    check(bool(j.get("gcode")), "...and real G-code came back")


def t_body_cap_does_not_break_normal_use(srv: Server) -> None:
    print("\n-- request-size ceiling ---------------------------------------")
    big = {"shape": "circle", "radius": 25, "height": 40,
           "printer": "trident", "junk": "x" * (2 * 1024 * 1024)}
    st, j = post(srv, "/api/generate", big)
    check(st == 400, "a 2 MB body is refused with 400", str(st))
    check("too large" in str(j.get("error", "")).lower(),
          "...with a readable reason, not a dropped connection",
          str(j)[:120])

    # The server must still be healthy afterwards -- this is the whole point.
    st, _ = get(srv, "/api/orca_status")
    check(st == 200, "server still serving after refusing an oversized body")
    st, j = gen(srv, {"shape": "circle", "radius": 20, "height": 20,
                      "layer_height": 0.3, "printer": "trident"})
    check(st == 200 and bool(j.get("gcode")),
          "...and a normal generate still works", str(st))


# ---------------------------------------------------------------------------
# 4. Item 4's adhesion package, through the server, alongside everything else.
# ---------------------------------------------------------------------------
def t_surface_adhesion_end_to_end(srv: Server) -> None:
    print("\n-- surface/conformal adhesion through the API -----------------")
    base_body = {"mode": "surface", "surface": "dome", "radius": 20,
                 "surface_amp": 2.0, "layer_height": 0.3, "printer": "trident"}

    st, plain = gen(srv, dict(base_body))
    check(st == 200, "surface design without adhesion generates", str(st))

    st, withbase = gen(srv, dict(base_body, base_layers=2, brim=3))
    check(st == 200, "surface design WITH a base + brim generates", str(st))

    if st == 200 and plain.get("gcode") and withbase.get("gcode"):
        check(len(withbase["gcode"]) > len(plain["gcode"]),
              "adding a base and brim emits strictly more G-code "
              "(the adhesion package is really in the output)",
              f"{len(plain['gcode'])} -> {len(withbase['gcode'])}")
        check(withbase.get("stats", {}).get("filament_m", 0)
              > plain.get("stats", {}).get("filament_m", 0),
              "...and uses more filament",
              f"{plain.get('stats',{}).get('filament_m')} -> "
              f"{withbase.get('stats',{}).get('filament_m')}")
        check("issues_detail" in withbase,
              "the adhesion path still reports severities")


# ---------------------------------------------------------------------------
# 5. The big one: a mesh planar base AND loop fabric AND scope notes together.
# ---------------------------------------------------------------------------
def t_mesh_base_plus_loop_fabric(srv: Server) -> None:
    print("\n-- mesh planar base + loop fabric + notes, all at once --------")
    if not orca_available(srv):
        skip("mesh planar base + loop fabric", "no OrcaSlicer binary available")
        return

    up = upload(srv, MESH)
    mid = up.get("mesh_id")
    check(bool(mid), "mesh uploads for the hybrid case")
    if not mid:
        return

    st, j = gen(srv, {
        "shape": "circle", "radius": 25, "height": 40, "layer_height": 0.3,
        "printer": "trident",
        "mesh_base_id": mid, "mesh_base_scale": 1,
        "mesh_base_wall_count": 3, "mesh_base_infill_density": 0.15,
        "mesh_base_infill_pattern": "grid", "mesh_base_blend_height": 8,
        "mesh_base_seam_style": "fillet", "mesh_base_seam_coverage": 100,
        "loop_per_turn": 16, "loop_row": 0.7, "loop_up": 1, "loop_mode": "dip",
        "loop_align": "stagger", "loop_cuff": 3, "loop_flow": 1.2,
        "loop_speed": 10, "loop_out": 0.5, "loop_rejoin": 2, "loop_waves": 12,
        "loop_fade_in": 0.1, "loop_jitter": 0.5, "loop_turn_stride": 1,
        "base_layers": 0,
    })
    check(st == 200, "mesh base + loop fabric generates", str(st))
    if st != 200:
        return
    g = j.get("gcode", "")
    check("hybrid: non-planar wall begins here" in g,
          "the Orca-sliced planar base really is in the output (seam marker)")
    check("loop fabric (" in g, "...and the knitted fabric wall is above it")
    check("anchor cuff" not in g,
          "...and the fabric did NOT print its own cuff (the base anchors it)")
    got = sevs(j)
    check("danger" not in got,
          "this combination raises no DANGER-level finding", str(got))
    check(any("GUESS, NOT PRINT-TESTED" in d["text"] for d in j["issues_detail"]),
          "the untested-seam caveat is present")
    for d in j["issues_detail"]:
        if "GUESS, NOT PRINT-TESTED" in d["text"]:
            check(d["severity"] == "note",
                  "...carried as a NOTE, not dressed up as a machine warning",
                  d["severity"])


def main() -> int:
    if not MESH.exists():
        print("FAIL  mesh fixture missing: %s" % MESH)
        return 1
    with Server(PORT) as srv:
        t_severity_on_all_endpoints(srv)
        t_severity_discriminates(srv)
        t_mesh_isolation_with_real_generates(srv)
        t_body_cap_does_not_break_normal_use(srv)
        t_surface_adhesion_end_to_end(srv)
        t_mesh_base_plus_loop_fabric(srv)

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
