#!/usr/bin/env python3
"""End-to-end self-test of the HTTP API surface.

Complements the other two runners rather than repeating them:

    tools/check_regression.py   byte-compares emitted G-code against
                                regression_ref/ -- catches "the machine output
                                changed".
    tools/test_printer_import.py  parser/validator/startup units -- catches
                                "a config or a guard broke".
    tools/self_test.py          THIS FILE. Drives a real server over HTTP the
                                way the browser does -- catches "a mode, a
                                clamp, or an endpoint broke" even when the
                                G-code writer itself is fine.

It starts its OWN server on a scratch port (never touching one you already
have running), exercises every generate mode, and asserts that each safety
guard actually fires. A guard that never fires in this file is a guard nobody
is checking.

Usage:  python tools/self_test.py [--port N] [--keep-going]
Exit code 0 = all pass. Standard library only, like everything else here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_results: list[tuple[bool, str, str]] = []
_stop_on_fail = True


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label, detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                        ("" if ok else ("   -> " + detail if detail else ""))))
    return bool(ok)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    """Our own server on a scratch port. Never reuses an existing one: a stale
    process running older code would make every assertion below a lie."""

    def __init__(self, port: int):
        self.port = port
        self.proc = None

    def __enter__(self):
        env = dict(os.environ, PORT=str(self.port))
        self.proc = subprocess.Popen(
            [sys.executable, "serve.py"], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = (self.proc.stdout.read() or b"").decode("utf-8", "replace")
                raise RuntimeError("server exited during startup:\n" + out)
            try:
                urllib.request.urlopen(self.url("/api/printers"), timeout=2).read()
                return self
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("server did not come up on port %d" % self.port)

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def url(self, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (self.port, path)


def post(srv: Server, path: str, body, raw: bool = False, timeout: int = 600):
    """POST JSON. Returns (status, parsed_or_bytes). A 4xx/5xx is data here,
    not an exception -- half these assertions are about rejections."""
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    req = urllib.request.Request(srv.url(path), data,
                                 {"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        payload = r.read()
        return r.status, (payload if raw else json.loads(payload))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except ValueError:
            return e.code, payload


def get(srv: Server, path: str, timeout: int = 60):
    try:
        r = urllib.request.urlopen(srv.url(path), timeout=timeout)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def gen(srv: Server, body, timeout: int = 600):
    return post(srv, "/api/generate", body, timeout=timeout)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def t_profiles(srv: Server) -> None:
    print("\n-- printer profiles ------------------------------------------")
    st, j = get(srv, "/api/printers")
    check(st == 200 and j.get("printers"), "printers listed", "status %s" % st)
    by = {p["key"]: p for p in j.get("printers", [])}
    check("trident" in by, "trident profile present")
    tri = by.get("trident", {})
    # Every ceiling the client is allowed to trust must be served, not derived
    # browser-side -- that is the whole point of _printer_entry_json.
    for field in ("z_max", "z_amp_max", "loop_row_max", "loop_up_max",
                  "max_nozzle_temp", "max_bed_temp", "max_z_velocity"):
        check(field in tri, "trident exposes %s" % field)
    # Ceilings must differ BETWEEN profiles, or a hardcoded constant would pass
    # every per-profile assertion in this file without anyone noticing.
    amps = {k: v.get("z_amp_max") for k, v in by.items()}
    check(len(set(amps.values())) > 1, "z_amp_max varies across profiles",
          "all equal: %s" % sorted(set(amps.values())))


def t_parametric(srv: Server) -> None:
    print("\n-- parametric shapes + textures ------------------------------")
    for shape in ("circle", "star", "square"):
        st, j = gen(srv, {"shape": shape, "radius": 30, "height": 40,
                          "layer_height": 0.3})
        ok = st == 200 and j.get("gcode", "").count("\n") > 1000
        check(ok, "generate shape=%s" % shape,
              "status %s %s" % (st, str(j)[:120]))

    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 40,
                      "layer_height": 0.3, "pattern": "ripple",
                      "pattern_amp": 2.0, "pattern_waves": 12})
    check(st == 200, "generate pattern=ripple", str(j)[:120])

    # Loop fabric derives its row/dip from the SAME z_amp_max ceiling as the
    # wave amplitude (CLAUDE.md: "z_amp_max has two consumers"). Note it is
    # enabled by loop_per_turn / loop_spacing_mm, NOT by pattern="loops" --
    # that string is a UI dropdown value the client deliberately does not
    # forward as an r_pattern (see designer.js's buildGenerateBody).
    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 40,
                      "layer_height": 0.3, "loop_per_turn": 8,
                      "loop_row": 0.6, "loop_up": 0.8})
    check(st == 200, "generate loop fabric", str(j)[:160])

    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 40,
                      "layer_height": 0.3, "blob_every": 8, "blob_mm": 0.6})
    check(st == 200, "generate with blobs", str(j)[:120])

    # A wall design must actually report a solid base when one is asked for.
    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 40,
                      "layer_height": 0.3, "base_layers": 2, "brim": 2})
    check(st == 200, "generate with base + brim", str(j)[:120])


def t_surface(srv: Server) -> None:
    print("\n-- surface (conformal shell) ---------------------------------")
    for name, amp, rad in (("dome", 4.0, 39.7), ("ripple", 3.0, 30),
                           ("saddle", 1.5, 30), ("waves", 2.5, 30)):
        st, j = gen(srv, {"mode": "surface", "surface": name,
                          "surface_amp": amp, "radius": rad,
                          "surface_wavelength": 15, "surface_shells": 1})
        check(st == 200, "surface %s generates" % name, str(j)[:140])
        if st == 200:
            check(j["stats"]["probe_hits"] == 0,
                  "surface %s has no probe hits" % name,
                  "probe_hits=%s" % j["stats"]["probe_hits"])

    # The probe-slope refusal. This is the guard the whole mode was gated on;
    # if it stops firing, the mode silently becomes unguarded.
    st, j = gen(srv, {"mode": "surface", "surface": "dome",
                      "surface_amp": 4.0, "radius": 30, "surface_shells": 1})
    check(st == 400 and "probe" in str(j.get("error", "")).lower(),
          "steep surface REFUSED on a probe-equipped printer",
          "status %s %s" % (st, str(j)[:140]))

    # Same design, printer without a probe: the check must stand down rather
    # than apply the Trident's geometry to a machine that has none.
    st, j = gen(srv, {"mode": "surface", "surface": "dome",
                      "surface_amp": 4.0, "radius": 30, "surface_shells": 1,
                      "printer": "bambu_a1"})
    check(st == 200, "same steep surface ACCEPTED on a probe-less printer",
          "status %s %s" % (st, str(j)[:140]))

    # The derived ceiling must be labelled a guess, per CLAUDE.md.
    st, j = gen(srv, {"mode": "surface", "surface": "dome",
                      "surface_amp": 4.0, "radius": 39.7, "surface_shells": 1})
    issues = " ".join(j.get("issues", [])) if st == 200 else ""
    check("NOT PRINT-TESTED" in issues.upper(),
          "surface report labels the derived ceiling as an untested guess",
          issues[:160])


def t_mesh(srv: Server) -> None:
    print("\n-- mesh_texture (uploaded STL) -------------------------------")
    stl = os.path.join(ROOT, "examples", "cylinder.stl")
    if not os.path.exists(stl):
        check(False, "cylinder.stl fixture present", stl)
        return
    req = urllib.request.Request(
        srv.url("/api/upload_mesh"), open(stl, "rb").read(),
        {"Content-Type": "application/octet-stream", "X-Filename": "cylinder.stl"})
    try:
        up = json.loads(urllib.request.urlopen(req, timeout=180).read())
    except Exception as e:
        check(False, "upload cylinder.stl", str(e)[:140])
        return
    mid = up.get("mesh_id")
    check(bool(mid), "upload cylinder.stl -> mesh_id")
    if not mid:
        return

    base = {"mode": "mesh_texture", "mesh_id": mid, "layer_height": 0.3}
    st, j = gen(srv, dict(base))
    check(st == 200, "mesh_texture generates", str(j)[:140])

    # Sculpt controls must CHANGE the output, not merely be accepted. A
    # parameter that parses and does nothing is the failure mode worth testing.
    def top_bottom_angles(gcode):
        pts = []
        x = y = z = 0.0
        for ln in gcode.splitlines():
            if ln[:3] not in ("G1 ", "G0 "):
                continue
            d = {t[0]: t[1:] for t in ln.split()[1:] if t and t[0] in "XYZE"}
            try:
                x = float(d.get("X", x)); y = float(d.get("Y", y)); z = float(d.get("Z", z))
            except ValueError:
                continue
            if "E" in d:
                pts.append((x, y, z))
        if len(pts) < 10:
            return None
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        ang = lambda p: math.degrees(math.atan2(p[1] - cy, p[0] - cx))
        return ang(pts[0]), ang(pts[-1])

    st0, j0 = gen(srv, dict(base, xy_twist=0))
    st2, j2 = gen(srv, dict(base, xy_twist=0.25))
    if st0 == 200 and st2 == 200:
        a0 = top_bottom_angles(j0["gcode"])
        a2 = top_bottom_angles(j2["gcode"])
        if a0 and a2:
            d0 = ((a0[1] - a0[0] + 180) % 360) - 180
            d2 = ((a2[1] - a2[0] + 180) % 360) - 180
            twist = abs(d2 - d0)
            check(abs(twist - 90.0) < 12.0,
                  "mesh xy_twist=0.25 really rotates the wall ~90 deg",
                  "measured %.1f deg" % twist)
        else:
            check(False, "mesh xy_twist measurable", "could not sample points")
    else:
        check(False, "mesh xy_twist requests succeeded", "%s / %s" % (st0, st2))

    for label, extra in (("ovality", {"ovality": 0.3}),
                         ("spine", {"spine_mm": 10, "spine_deg": 0}),
                         ("point_radial_push",
                          {"point_radial_push": {"amp_mm": 3.0}})):
        st, j = gen(srv, dict(base, **extra))
        check(st == 200, "mesh_texture accepts %s" % label, str(j)[:140])


def t_streaming(srv: Server) -> None:
    print("\n-- streaming progress ----------------------------------------")
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=600)
    c.request("POST", "/api/generate_stream",
              json.dumps({"shape": "star", "radius": 30, "height": 60,
                          "layer_height": 0.2, "z_waves": 5}),
              {"Content-Type": "application/json"})
    r = c.getresponse()
    check(r.status == 200, "stream opens 200", "status %s" % r.status)
    fracs, stages, done, backwards, over = [], set(), False, 0, 0
    last = 0.0
    while True:
        line = r.fp.readline()
        if not line:
            break
        o = json.loads(line)
        if o.get("type") == "progress":
            f = o["frac"]
            if f < last:
                backwards += 1
            if f > 0.99:
                over += 1
            last = f
            fracs.append(f)
            stages.add(o.get("stage"))
        elif o.get("type") == "done":
            done = True
            check(bool(o["result"].get("gcode")), "stream done carries gcode")
            break
        elif o.get("type") == "error":
            check(False, "stream completed without error", str(o)[:140])
            break
    c.close()
    check(done, "stream reached done")
    check(len(fracs) >= 3, "stream emitted progress lines",
          "%d lines" % len(fracs))
    check(backwards == 0, "progress never goes backwards",
          "%d regressions" % backwards)
    check(over == 0, "progress never exceeds 0.99 before done",
          "%d over" % over)
    check("toolpath" in stages and "verify" in stages,
          "both stages reported", "stages=%s" % sorted(stages))

    # An error must arrive as a typed line, since the 200 header is already
    # sent by the time generation fails.
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=120)
    c.request("POST", "/api/generate_stream",
              json.dumps({"shape": "circle", "radius": 30, "height": 99999,
                          "layer_height": 0.3}),
              {"Content-Type": "application/json"})
    r = c.getresponse()
    saw_err = False
    while True:
        line = r.fp.readline()
        if not line:
            break
        o = json.loads(line)
        if o.get("type") == "error":
            saw_err = True
            break
    c.close()
    check(saw_err, "stream reports a rejected design as an error line")


def t_guards(srv: Server) -> None:
    print("\n-- safety guards ---------------------------------------------")
    # Non-finite must be REJECTED at the boundary, never clamped: every
    # comparison against NaN is False, so it survives min()/max() silently.
    for tok in ("NaN", "Infinity", "-Infinity"):
        body = ('{"shape":"circle","radius":%s,"height":40,"layer_height":0.3}'
                % tok).encode()
        st, j = post(srv, "/api/generate", body)
        check(st == 400, "%s rejected at the boundary" % tok,
              "status %s %s" % (st, str(j)[:100]))

    # Fail fast, and fail at all, on an impossible height.
    t0 = time.time()
    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 9000,
                      "layer_height": 0.3})
    dt = time.time() - t0
    check(st == 400, "absurd height rejected", "status %s" % st)
    check(dt < 5.0, "absurd height rejected FAST (no full toolpath build)",
          "took %.1fs" % dt)

    # A height just over the profile's z_max must still be refused.
    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 300,
                      "layer_height": 0.3})
    check(st == 400, "height above trident z_max rejected", "status %s" % st)

    # Footprint that cannot fit the bed.
    st, j = gen(srv, {"shape": "circle", "radius": 300, "height": 40,
                      "layer_height": 0.3})
    check(st == 400, "oversize footprint rejected", "status %s" % st)

    # A wave that dips below the bed must be refused, not clamped into it.
    # Asking for amplitude at t=0 drives the very first turn under the print
    # floor, and GcodeWriter's floor check is the last guard before the nozzle
    # meets the plate.
    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 40,
                      "layer_height": 0.3, "z_waves": 6,
                      "amp_profile": [[0, 10.0], [1, 10.0]]})
    check(st == 400 and "floor" in str(j.get("error", "")).lower(),
          "wave dipping below the print floor is refused",
          "status %s %s" % (st, str(j)[:120]))

    # The amplitude ceiling must come from the PROFILE. Ramp from 0 at the base
    # (the realistic shape, and what keeps us off the floor guard above) and
    # ask for 10mm at the top -- the Trident allows 0.95.
    st, j = gen(srv, {"shape": "circle", "radius": 30, "height": 40,
                      "layer_height": 0.3, "z_waves": 6,
                      "amp_profile": [[0, 0.0], [1, 10.0]]})
    if check(st == 200, "over-limit amplitude accepted-and-clamped", str(j)[:120]):
        zs = []
        for ln in j["gcode"].splitlines():
            if ln[:3] in ("G1 ", "G0 "):
                for t in ln.split()[1:]:
                    if t[:1] == "Z":
                        try:
                            zs.append(float(t[1:]))
                        except ValueError:
                            pass
        if len(zs) > 100:
            # Wave excursion is measured against the local trend, so compare
            # the spread of consecutive Z values rather than global min/max
            # (which legitimately spans the whole print height).
            steps = [abs(b - a) for a, b in zip(zs, zs[1:])]
            steps.sort()
            p999 = steps[int(len(steps) * 0.999)]
            check(p999 < 3.0, "clamped Z steps stay far below the 10mm asked for",
                  "99.9th pct step %.3fmm" % p999)

    # Unknown filament / unknown shape are user errors, not 500s.
    st, j = gen(srv, {"shape": "trapezoid", "radius": 30, "height": 40})
    check(st == 400, "unknown shape rejected as 400", "status %s" % st)


def t_star(srv: Server) -> None:
    """star_points / star_depth must be clamped server-side, not just by the
    UI's <input min/max>. A raw HTTP client (or a loaded design JSON that
    skips bindNumber's clamp) can send anything; the server used to pass it
    straight to the generator unclamped.

    Generation is fully deterministic (no randomness, no timestamps in the
    output), so two requests with the same effective parameters produce
    byte-identical G-code -- that lets an out-of-range request be checked
    against the clamped-in-range one it should equal, without parsing
    geometry at all.
    """
    print("\n-- star points / depth clamp -----------------------------------")
    base = {"shape": "star", "radius": 25, "height": 20, "layer_height": 0.3}

    def gcode_of(**extra):
        st, j = gen(srv, dict(base, **extra))
        return st, (j.get("gcode") if st == 200 else j)

    st_hi, g_hi = gcode_of(star_points=999)
    st_max, g_max = gcode_of(star_points=12)
    if check(st_hi == 200 and st_max == 200,
             "over-max star_points accepted (clamped, not rejected)",
             "status %s / %s" % (st_hi, st_max)):
        check(g_hi == g_max, "star_points=999 clamps to the same output as 12")

    st_lo, g_lo = gcode_of(star_points=0)
    st_neg, g_neg = gcode_of(star_points=-7)
    st_min, g_min = gcode_of(star_points=3)
    if check(st_lo == 200 and st_neg == 200 and st_min == 200,
             "star_points<=0 accepted (clamped, not rejected)",
             "status %s / %s / %s" % (st_lo, st_neg, st_min)):
        check(g_lo == g_min, "star_points=0 clamps to the same output as 3")
        check(g_neg == g_min, "star_points=-7 clamps to the same output as 3")

    st_dhi, g_dhi = gcode_of(star_depth=50)
    st_dmax, g_dmax = gcode_of(star_depth=1.0)
    if check(st_dhi == 200 and st_dmax == 200,
             "over-max star_depth accepted (clamped, not rejected)",
             "status %s / %s" % (st_dhi, st_dmax)):
        check(g_dhi == g_dmax, "star_depth=50 clamps to the same output as 1.0")

    st_dlo, g_dlo = gcode_of(star_depth=-1)
    st_dmin, g_dmin = gcode_of(star_depth=0.0)
    if check(st_dlo == 200 and st_dmin == 200,
             "negative star_depth accepted (clamped, not rejected)",
             "status %s / %s" % (st_dlo, st_dmin)):
        check(g_dlo == g_dmin, "star_depth=-1 clamps to the same output as 0.0")

    # The clamp must land on the ceiling, not silently collapse to the
    # default -- prove star_points actually varies the output at all.
    st_def, g_def = gcode_of(star_points=5)
    if check(st_def == 200, "default star_points=5 generates", "status %s" % st_def):
        check(g_hi != g_def,
              "clamped star_points=999 output differs from the default (5)")

    # Direct geometric confirmation: count lobes on the actual toolpath,
    # mirroring how the amplitude-clamp test above inspects emitted Z steps.
    def star_lobes(gcode):
        pts = []
        x = y = 0.0
        for ln in gcode.splitlines():
            if ln[:3] not in ("G1 ", "G0 "):
                continue
            d = {t[0]: t[1:] for t in ln.split()[1:] if t and t[0] in "XYE"}
            try:
                x = float(d.get("X", x)); y = float(d.get("Y", y))
            except ValueError:
                continue
            if "E" in d:
                pts.append((x, y))
        if len(pts) < 240:
            return None
        window = pts[len(pts) // 2: len(pts) // 2 + 240]
        cx = sum(p[0] for p in window) / len(window)
        cy = sum(p[1] for p in window) / len(window)
        radii = [math.hypot(px - cx, py - cy) for px, py in window]
        mean_r = sum(radii) / len(radii)
        crossings = 0
        below = radii[0] < mean_r
        for r in radii[1:]:
            now_below = r < mean_r
            if now_below != below and not now_below:
                crossings += 1
            below = now_below
        return crossings

    # 12 / 3 are serve.py's STAR_POINTS_MAX / STAR_POINTS_MIN; kept as literals
    # here (this file talks to the server only over HTTP, never imports it).
    lobes_hi = star_lobes(g_hi) if st_hi == 200 else None
    lobes_lo = star_lobes(g_lo) if st_lo == 200 else None
    if lobes_hi is not None:
        check(abs(lobes_hi - 12) <= 1,
              "a 999-point star really prints 12 lobes", "measured %s" % lobes_hi)
    else:
        check(False, "could not sample lobes for star_points=999")
    if lobes_lo is not None:
        check(abs(lobes_lo - 3) <= 1,
              "a 0-point star really prints 3 lobes", "measured %s" % lobes_lo)
    else:
        check(False, "could not sample lobes for star_points=0")

    # Both call sites that build a star cross-section (live generate and the
    # STL export path) must apply the same clamp, or the exported solid could
    # differ from the printed wall just because one path validated harder.
    st_ehi, g_ehi = post(srv, "/api/export_stl", dict(base, star_points=999), raw=True)
    st_emax, g_emax = post(srv, "/api/export_stl", dict(base, star_points=12), raw=True)
    if check(st_ehi == 200 and st_emax == 200,
             "export_stl accepts over-max star_points (clamped, not rejected)",
             "status %s / %s" % (st_ehi, st_emax)):
        check(g_ehi == g_emax,
              "export_stl: star_points=999 clamps to the same STL as 12")


def t_loop_fabric(srv: Server) -> None:
    """Loop fabric must lay rows the machine can actually build on.

    Two separate holes lived here, both silent -- the generator emitted a
    clean-looking file either way -- so both are pinned by geometry, not by a
    constant anyone could edit in sympathy with the code.
    """
    print("\n-- loop fabric -----------------------------------------------")
    st, j = get(srv, "/api/printers")
    by = {p["key"]: p for p in j.get("printers", [])}

    # 1. The row/stitch range must not be a single point. The floor used to be
    #    min(1.0, ceiling), which EQUALS the ceiling on any profile capped at
    #    or below 1.0 -- ten of the fifteen shipped ones -- so every loop
    #    style resolved to identical geometry and picking a style did nothing.
    for key in ("trident", "creality_ender3", "bambu_x1c"):
        p = by.get(key, {})
        lo, hi = p.get("loop_min_mm"), p.get("loop_row_max")
        check(lo is not None and hi is not None,
              "%s serves a loop row floor and ceiling" % key,
              "min %s max %s" % (lo, hi))
        if lo is None or hi is None:
            continue
        check(lo < hi, "%s loop row range is not a single point" % key,
              "[%s, %s]" % (lo, hi))

    # 2. Rows must stay within depositing reach of the row beneath. A profile
    #    with lots of Z clearance used to take the styles' authored 2.5-4.0mm
    #    pitches as-is, and 5-28% of the print became extrusion into air.
    #    z_amp_max is NOT the bound here -- support reach is, and it binds
    #    independently (serve.py's loop_up_ceiling).
    bam = by.get("bambu_x1c", {})
    check(bam.get("z_amp_max", 0) > bam.get("loop_up_max", 0),
          "a high-clearance profile's loop ceiling is below its z_amp_max",
          "z_amp_max %s vs loop_up_max %s"
          % (bam.get("z_amp_max"), bam.get("loop_up_max")))
    # ...while the WAVE amplitude keeps the machine's full range: z_amp_max's
    # other consumer must not be narrowed by a fabric-only bound.
    check(bam.get("z_amp_max") == 4.0,
          "the same profile still reports its full wave-amplitude ceiling",
          "got %s" % bam.get("z_amp_max"))

    # 3. The end-to-end proof: generate at each profile's own ceiling and let
    #    the analyzer judge the result. Under the 2% threshold is the claim.
    for key in ("trident", "creality_ender3", "bambu_x1c"):
        p = by.get(key, {})
        row, up = p.get("loop_row_max"), p.get("loop_up_max")
        if row is None or up is None:
            continue
        bed = p.get("bed") or [235, 235]
        radius = min(30.0, min(bed[0], bed[1]) / 2.0 - 15.0)
        # The widest-spaced style is the hardest case: fewest stitches, so the
        # least material anywhere for the next row to land on.
        st, j = gen(srv, {"shape": "circle", "radius": radius, "height": 40,
                          "layer_height": 0.3, "printer": key,
                          "loop_spacing_mm": 8.0, "loop_row": row,
                          "loop_up": up, "loop_out": 1.0,
                          "loop_align": "stagger", "loop_cuff": 3})
        if not check(st == 200, "%s: generate at the loop ceiling" % key,
                     str(j)[:160]):
            continue
        stats = j.get("stats", {})
        ext = max(1, stats.get("extrude_moves", 1))
        pct = 100.0 * stats.get("unsupported_moves", 0) / ext
        check(pct <= 2.0,
              "%s: widest-spaced fabric stays under 2%% unsupported" % key,
              "got %.2f%% (%d of %d)"
              % (pct, stats.get("unsupported_moves", 0), ext))
        check(stats.get("probe_hits", 0) == 0,
              "%s: no probe strike in loop fabric" % key,
              "got %s" % stats.get("probe_hits"))

    # 4. Interlock. A dipped loop MUST end up taller than the row pitch or it
    #    never reaches the row beneath, and the "fabric" prints as separate
    #    unconnected rings -- a clean-looking file and a part that falls apart.
    #    Below 0.8mm of clearance the dip clamp used to drive loop_h under
    #    row_mm and emit exactly that. Unreachable with a shipped profile (the
    #    Trident's 0.95 is the tightest), reachable with an imported one, so
    #    this drives the generator directly rather than through /api/generate.
    # This file otherwise talks to the server only over HTTP and never imports
    # the package, so ROOT is not on the path yet -- put it there for this one
    # in-process check. The condition is unreachable through the API (no
    # shipped profile is tight enough), so there is no HTTP route to it.
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import dataclasses
    from trident_gcode.blobs import LoopSpec
    from trident_gcode.gcode import GcodeWriter
    from trident_gcode.generators.loop_fabric import (
        _MIN_DIP_CLEARANCE_MM, build_loop_fabric)
    from trident_gcode.profile import TRIDENT

    def _fabric(z_amp_max, mode):
        prof = dataclasses.replace(TRIDENT, name="Custom", z_amp_max=z_amp_max)
        spec = LoopSpec(loops_per_turn=24, row_mm=0.5, up_mm=0.8,
                        stitch_mode=mode)
        return build_loop_fabric(GcodeWriter(prof), shape=lambda t: 30.0,
                                 height=30.0, spec=spec)

    tight = round(_MIN_DIP_CLEARANCE_MM - 0.01, 3)
    try:
        rep = _fabric(tight, "dip")
        check(False, "a dip fabric under the interlock minimum is refused",
              "built anyway: row %s loop %s"
              % (rep["row_mm_effective"], rep["loop_mm_effective"]))
    except ValueError as e:
        check("interlock" in str(e) or "row below" in str(e),
              "a dip fabric under the interlock minimum is refused",
              "wrong message: %s" % str(e)[:120])

    rep = _fabric(_MIN_DIP_CLEARANCE_MM, "dip")
    check(rep["loop_mm_effective"] > rep["row_mm_effective"],
          "at exactly the minimum, the loop still clears the row pitch",
          "loop %s vs row %s"
          % (rep["loop_mm_effective"], rep["row_mm_effective"]))

    # Spike mode is exempt on purpose: its stitch stands ABOVE the row line,
    # so only (loop_h - row_mm) is a Z excursion and it interlocks in any
    # clearance. Refusing it too would be a limit with no cause behind it.
    rep = _fabric(0.2, "spike")
    check(rep["loop_mm_effective"] > rep["row_mm_effective"],
          "spike mode still builds an interlocking fabric in tiny clearance",
          "loop %s vs row %s"
          % (rep["loop_mm_effective"], rep["row_mm_effective"]))

    # 5. xy_twist must actually rotate the PARAMETRIC loop-fabric wall -- the
    #    exact bug this fix addresses: a twist parameter that parses and does
    #    nothing. "A parameter that parses and does nothing is the failure
    #    mode worth testing" (see t_mesh above) -- but top_bottom_angles there
    #    is not reusable as-is: the fabric's first/last extruding points are
    #    cuff and rim ring points whose angle comes from the ring
    #    parametrisation, not the shape.
    #
    #    Naively picking "the point of maximum radius" per Z band is NOT
    #    robust here: a 5-point star sampled on a 48-stitch / 240-point grid
    #    hits exact ties between symmetric lobes (cos() is even, so two
    #    stitches equidistant in angle from a lobe peak score identically),
    #    so argmax silently picks whichever tied candidate comes first in
    #    emission order -- noise, not signal. Instead measure the PHASE of
    #    the shape's dominant angular harmonic (5, matching the default star
    #    point count): sum r(theta)*exp(i*5*theta) over a Z band and take the
    #    angle of the resulting complex number, divided by 5. This is the
    #    standard way to read off a periodic pattern's rotation and is
    #    immune to per-stitch ties since it is an aggregate over every point
    #    in the band, not a single argmax. Verified against a debug run: it
    #    reproduces exactly 0.00 deg for xy_twist=0 (bit-for-bit symmetric,
    #    no noise) and a stable, sign-correct reading for xy_twist=0.25.
    #
    #    The phase is only defined mod (360/5)=72 deg (a 5-fold pattern looks
    #    the same after 1/5 turn), so 0.25 turns (90 deg of raw rotation)
    #    reads back as 90 mod 72 = 18 deg -- not 90. Tolerance stays loose
    #    (~15 deg): stitches wander in radius by design (loop_out, the loop
    #    bell), so this is a coarse "did it rotate at all, in the right
    #    direction, by roughly the right amount" check, not a precision one.
    def _extrusion_points(gcode):
        pts = []
        x = y = z = 0.0
        for ln in gcode.splitlines():
            if ln[:3] not in ("G1 ", "G0 "):
                continue
            d = {t[0]: t[1:] for t in ln.split()[1:] if t and t[0] in "XYZE"}
            try:
                x = float(d.get("X", x)); y = float(d.get("Y", y)); z = float(d.get("Z", z))
            except ValueError:
                continue
            if "E" in d:
                pts.append((x, y, z))
        return pts

    _STAR_POINTS = 5    # default star_points (serve.py's _parse_star), matches lf_base below
    _PITCH_DEG = 360.0 / _STAR_POINTS

    def _harmonic_phase_deg(points_group, cx, cy):
        sr = si = 0.0
        for (px, py, _pz) in points_group:
            r = math.hypot(px - cx, py - cy)
            th = math.atan2(py - cy, px - cx)
            sr += r * math.cos(_STAR_POINTS * th)
            si += r * math.sin(_STAR_POINTS * th)
        if sr == 0.0 and si == 0.0:
            return None
        return math.degrees(math.atan2(si, sr) / _STAR_POINTS)

    def _bottom_top_swing_deg(gcode):
        """Phase (deg, mod _PITCH_DEG, wrapped to (-_PITCH_DEG/2, _PITCH_DEG/2])
        of the star's dominant harmonic at the top of the extruded Z range
        minus the bottom. Bands are 2% of the Z span -- narrow, so each end
        sits close to its true height fraction (t~0 / t~1) rather than being
        smeared across a chunk of accumulated twist."""
        pts = _extrusion_points(gcode)
        if len(pts) < 20:
            return None
        zs = [p[2] for p in pts]
        zlo, zhi = min(zs), max(zs)
        band = 0.02 * max(zhi - zlo, 1e-6)
        bottom = [p for p in pts if p[2] <= zlo + band]
        top = [p for p in pts if p[2] >= zhi - band]
        if not bottom or not top:
            return None
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        ab = _harmonic_phase_deg(bottom, cx, cy)
        at = _harmonic_phase_deg(top, cx, cy)
        if ab is None or at is None:
            return None
        return ((at - ab + _PITCH_DEG / 2) % _PITCH_DEG) - _PITCH_DEG / 2

    lf_base = {"shape": "star", "radius": 32, "height": 60, "layer_height": 0.3,
               "loop_spacing_mm": 4.0, "loop_align": "stagger", "loop_cuff": 3}
    st_t, j_t = gen(srv, dict(lf_base, xy_twist=0.25))
    st_0, j_0 = gen(srv, dict(lf_base, xy_twist=0))
    lf_swing_t = None
    if st_t == 200 and st_0 == 200:
        lf_swing_t = _bottom_top_swing_deg(j_t["gcode"])
        lf_swing_0 = _bottom_top_swing_deg(j_0["gcode"])
        expected = ((0.25 * 360.0 + _PITCH_DEG / 2) % _PITCH_DEG) - _PITCH_DEG / 2
        if lf_swing_t is not None and lf_swing_0 is not None:
            check(abs(lf_swing_t - expected) < 15.0,
                  "parametric loop-fabric xy_twist=0.25 rotates the fabric "
                  "(expected ~%.0f deg mod %.0f)" % (expected, _PITCH_DEG),
                  "measured %.1f deg (untwisted baseline %.1f deg)"
                  % (lf_swing_t, lf_swing_0))
            check(abs(lf_swing_0) < 15.0,
                  "parametric loop-fabric xy_twist=0 stays straight (~0 deg)",
                  "measured %.1f deg" % lf_swing_0)
        else:
            check(False, "loop-fabric twist measurable",
                  "could not sample harmonic phase (swing_t=%s swing_0=%s)"
                  % (lf_swing_t, lf_swing_0))
    else:
        check(False, "loop-fabric xy_twist requests succeeded",
              "%s / %s" % (st_t, st_0))

    # Sign check: same body WITHOUT loop_spacing_mm routes to the normal wall
    # (build_continuous_spiral, already correct); twist must advance the SAME
    # way as loop fabric, or the sign in loop_fabric.py's _radius is flipped.
    if lf_swing_t is not None:
        wall_base = dict(lf_base)
        del wall_base["loop_spacing_mm"]
        st_wt, j_wt = gen(srv, dict(wall_base, xy_twist=0.25))
        st_w0, j_w0 = gen(srv, dict(wall_base, xy_twist=0))
        if st_wt == 200 and st_w0 == 200:
            wall_swing_t = _bottom_top_swing_deg(j_wt["gcode"])
            wall_swing_0 = _bottom_top_swing_deg(j_w0["gcode"])
            if wall_swing_t is not None and wall_swing_0 is not None:
                check((lf_swing_t > 0) == (wall_swing_t > 0),
                      "loop-fabric twist direction matches the normal wall",
                      "loop fabric %.1f deg vs wall %.1f deg"
                      % (lf_swing_t, wall_swing_t))
            else:
                check(False, "wall twist measurable", "could not sample harmonic phase")
        else:
            check(False, "wall xy_twist requests succeeded",
                  "%s / %s" % (st_wt, st_w0))


def t_export(srv: Server) -> None:
    print("\n-- STL export ------------------------------------------------")
    st, data = post(srv, "/api/export_stl",
                    {"shape": "circle", "radius": 30, "height": 40,
                     "layer_height": 0.3}, raw=True)
    ok = st == 200 and isinstance(data, (bytes, bytearray)) and len(data) > 1000
    check(ok, "export_stl returns a binary mesh",
          "status %s len %s" % (st, len(data) if hasattr(data, "__len__") else "?"))
    if ok:
        # Binary STL: 80-byte header then a uint32 triangle count.
        import struct
        (ntri,) = struct.unpack("<I", data[80:84])
        check(len(data) == 84 + ntri * 50,
              "export_stl length matches its own triangle count",
              "ntri=%d len=%d" % (ntri, len(data)))


def main() -> int:
    global _stop_on_fail
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--keep-going", action="store_true",
                    help="run every section even after a failure")
    args = ap.parse_args()
    _stop_on_fail = not args.keep_going

    port = args.port or _free_port()
    print("self-test: starting a private server on port %d" % port)
    t0 = time.time()
    with Server(port) as srv:
        for fn in (t_profiles, t_parametric, t_surface, t_mesh,
                   t_streaming, t_guards, t_star, t_loop_fabric, t_export):
            try:
                fn(srv)
            except Exception as e:
                check(False, "%s raised" % fn.__name__, repr(e)[:200])
                if _stop_on_fail:
                    break

    failed = [r for r in _results if not r[0]]
    print("\n%d checks, %d failed, %.1fs" % (len(_results), len(failed),
                                             time.time() - t0))
    if failed:
        print("\nFAILURES:")
        for _, label, detail in failed:
            print("  - %s%s" % (label, ("   " + detail) if detail else ""))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
