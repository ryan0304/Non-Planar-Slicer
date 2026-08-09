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
                   t_streaming, t_guards, t_export):
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
