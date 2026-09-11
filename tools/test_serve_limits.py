#!/usr/bin/env python3
"""Tests for serve.py's two request-boundary resource limits:

  * ``_read_json_body``'s JSON_BODY_MAX_BYTES ceiling, and
  * the per-session mesh cache (``_mesh_cache_put`` / ``_mesh_cache_get``).

Plain ``python tools/test_serve_limits.py``: prints PASS/FAIL per case, exits
non-zero on any failure. Mirrors test_serve_mesh_params.py's style.

Neither area had a single test before. Both are memory limits on a shared
512 MB box, which makes them safety limits for every OTHER visitor rather than
for the sender: an unbounded Content-Length read is an out-of-memory kill for
everyone mid-print-generation, and a global mesh cache of four meant the fifth
STL uploaded site-wide silently deleted the first user's upload.

The eviction tests below are written against counts and bucket membership, not
against wall-clock or memory, so they stay deterministic.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  {detail}")
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# A minimal stand-in for the BaseHTTPRequestHandler surface _read_json_body
# touches: .headers, .rfile, ._send_json and .close_connection. Deliberately
# NOT a real socket -- this is a unit test of the boundary, and the end-to-end
# version runs against a live server separately.
# ---------------------------------------------------------------------------
class FakeHandler:
    def __init__(self, body: bytes, content_length=None):
        self.headers = {"Content-Length": str(
            len(body) if content_length is None else content_length)}
        # rfile holds `body`; a lying Content-Length is simulated by declaring
        # a different number in the header than the stream actually carries.
        self.rfile = io.BytesIO(body)
        self.sent = None
        self.status = None
        self.close_connection = False

    def _send_json(self, obj, status=200):
        self.sent = obj
        self.status = status


def _design_body(mask_rows=0):
    """A body shaped like what the viewer actually posts to /api/generate."""
    body = {
        "mode": "parametric",
        "shape": "circle",
        "radius": 40.0,
        "height": 120.0,
        "layer_height": 0.30,
        "amp_profile": [[0.0, 0.2], [0.5, 0.8], [1.0, 0.4]],
        "radius_profile": [[0.0, 1.0], [1.0, 0.9]],
        "zone_overrides": [
            {"z0": 10.0, "z1": 40.0, "pattern": "ribs", "pattern_amp": 1.0}
            for _ in range(4)
        ],
    }
    if mask_rows:
        body["point_mask"] = {
            "channel": "none",
            "image": [[0.12345678901234567] * mask_rows
                      for _ in range(mask_rows)],
        }
    return body


def test_json_body_accepts_a_real_design():
    raw = json.dumps(_design_body()).encode("utf-8")
    h = FakeHandler(raw)
    got = serve._read_json_body(h)
    check(isinstance(got, dict) and got["shape"] == "circle",
          "json body: an ordinary design body parses",
          f"status={h.status} sent={h.sent}")
    check(h.sent is None, "json body: a good body sends no error", h.sent)

    # The real worst case a client can legitimately send: the 64x64 Point Edit
    # mask image at full float precision, plus the FFD cage. The ceiling has
    # to clear this comfortably or the cap breaks a supported feature.
    body = _design_body(mask_rows=64)
    body["point_ffd"] = {
        "cage": [[[0.123456789, 0.987654321, 0.55555555] for _ in range(24)]
                 for _ in range(12)],
        "strength": 1.0,
    }
    raw = json.dumps(body).encode("utf-8")
    check(len(raw) < serve.JSON_BODY_MAX_BYTES,
          f"json body: the largest legitimate body ({len(raw)} bytes) fits "
          f"under the {serve.JSON_BODY_MAX_BYTES}-byte ceiling with room to "
          "spare",
          f"{len(raw)} vs {serve.JSON_BODY_MAX_BYTES}")
    h = FakeHandler(raw)
    got = serve._read_json_body(h)
    check(isinstance(got, dict) and h.sent is None,
          "json body: the max-feature design body is accepted",
          f"status={h.status} sent={h.sent}")


def test_json_body_rejects_oversized():
    over = serve.JSON_BODY_MAX_BYTES + 1
    # Honest header, genuinely oversized payload.
    raw = b'{"pad":"' + b"a" * over + b'"}'
    h = FakeHandler(raw)
    got = serve._read_json_body(h)
    check(got is None,
          "json body: an oversized body returns the None sentinel callers "
          "already check for", repr(got)[:80])
    check(h.status == 400, "json body: oversized -> 400", h.status)
    check(isinstance(h.sent, dict) and "error" in h.sent
          and isinstance(h.sent["error"], str),
          "json body: oversized sends the same {'error': str} shape as "
          "malformed JSON does", h.sent)
    check("too large" in (h.sent or {}).get("error", ""),
          "json body: the 400 says what was wrong", h.sent)
    # A moderate overage is drained so the connection is left consistent and
    # the 400 is actually readable by the client. Found the hard way against a
    # live server: refusing without draining resets the socket on Windows,
    # which discards the response body already written and leaves the browser
    # with a bare network error instead of the message.
    check(h.close_connection is False,
          "json body: a moderately oversized body is drained, so the "
          "connection stays usable and the 400 reaches the client",
          h.close_connection)
    check(h.rfile.tell() == len(raw),
          "json body: the declared body was consumed exactly", h.rfile.tell())


def test_lying_content_length_cannot_exhaust_memory():
    """A hostile client can claim any Content-Length it likes."""
    # 1. Absurd claim, tiny real body. Must be refused on the header alone,
    #    with no read attempted.
    h = FakeHandler(b"{}", content_length=8 * 1024 * 1024 * 1024)
    got = serve._read_json_body(h)
    check(got is None and h.status == 400,
          "lying length: an 8 GB Content-Length is refused", h.status)
    check(h.rfile.tell() == 0,
          "lying length: refused without reading a single byte -- an 8 GB "
          "read() is the out-of-memory kill this ceiling exists to stop",
          h.rfile.tell())
    check(h.close_connection is True,
          "lying length: an absurd claim is NOT drained (draining it is the "
          "resource the attacker wants spent); the connection is dropped",
          h.close_connection)
    check(serve.JSON_BODY_DRAIN_MAX_BYTES > serve.JSON_BODY_MAX_BYTES,
          "lying length: the drain limit leaves headroom above the ceiling, "
          "so an honest client that is merely a bit over still gets a "
          "readable error",
          f"{serve.JSON_BODY_DRAIN_MAX_BYTES} vs {serve.JSON_BODY_MAX_BYTES}")

    # The drain is chunked, not one read(length): a 7 MB overage must not be
    # pulled into memory whole just to be thrown away.
    src = __import__("inspect").getsource(serve._read_json_body)
    check("min(remaining, 65536)" in src,
          "lying length: the drain reads fixed-size chunks and discards them, "
          "so memory stays flat while draining", src)

    # 2. A negative Content-Length must not become a read(-1), which on a
    #    file-like object means "read everything".
    huge = b'{"pad":"' + b"a" * (serve.JSON_BODY_MAX_BYTES + 1) + b'"}'
    h = FakeHandler(huge, content_length=-1)
    got = serve._read_json_body(h)
    check(h.rfile.tell() == 0,
          "lying length: a negative Content-Length reads nothing, it does NOT "
          "become read(-1) == read-until-EOF", h.rfile.tell())
    check(got == {},
          "lying length: a negative Content-Length is treated as an empty "
          "body", got)

    # 3. Header under the cap, stream much longer (the classic desync probe).
    #    Only the declared bytes may be consumed.
    h = FakeHandler(huge, content_length=2)
    serve._read_json_body(h)
    check(h.rfile.tell() <= 2,
          "lying length: an understated Content-Length consumes only what it "
          "declared, never the rest of the stream", h.rfile.tell())

    # 4. The read call itself is capped independently of the header check --
    #    the guarantee must not rest on one `if`. Assert the source says so.
    import inspect
    src = inspect.getsource(serve._read_json_body)
    check("min(length, JSON_BODY_MAX_BYTES)" in src,
          "lying length: the read() argument is min()'d against the ceiling, "
          "so the bound survives even if the header check is edited away",
          src)


# ---------------------------------------------------------------------------
# Mesh cache. Triangles are stand-ins: _mesh_cache_put only stores and counts
# them, so a 1-triangle list exercises every path a 400k-triangle one does.
# ---------------------------------------------------------------------------
_TRI = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]

SESSION_A = "aaaaaaaa1111"
SESSION_B = "bbbbbbbb2222"


def _reset_cache():
    with serve._mesh_cache_lock:
        serve._mesh_cache.clear()


def _ids_for(session):
    """Every mesh_id visible to `session`, in bucket order."""
    key = serve._mesh_session_key(session)
    with serve._mesh_cache_lock:
        return list(serve._mesh_cache.get(key, {}).keys())


def test_session_isolation():
    _reset_cache()
    serve._mesh_cache_put("b_keeper", _TRI, SESSION_B)

    # Session A uploads well past the OLD global cap of 4. Under the old flat
    # cache this evicted B's mesh outright; that was the bug.
    for i in range(12):
        serve._mesh_cache_put("a_%d" % i, _TRI, SESSION_A)

    check(serve._mesh_cache_get(SESSION_B, "b_keeper") is not None,
          "isolation: session A uploading 12 meshes does NOT evict session "
          "B's upload", _ids_for(SESSION_B))
    check(len(_ids_for(SESSION_A)) == serve.MESH_CACHE_MAX_PER_SESSION,
          "isolation: session A is capped at MESH_CACHE_MAX_PER_SESSION "
          "entries of its own", _ids_for(SESSION_A))
    check(serve._mesh_cache_get(SESSION_A, "a_0") is None
          and serve._mesh_cache_get(SESSION_A, "a_11") is not None,
          "isolation: a visitor's own 5th upload evicts their OWN oldest, LRU",
          _ids_for(SESSION_A))

    # And A cannot read B's mesh through its own session bucket lookup order.
    _reset_cache()
    serve._mesh_cache_put("private_to_b", _TRI, SESSION_B)
    check(serve._mesh_cache_get(SESSION_A, "private_to_b") is None,
          "isolation: session A does not see session B's session-scoped mesh",
          _ids_for(SESSION_A))


def test_lru_touch_protects_an_active_mesh():
    _reset_cache()
    for i in range(serve.MESH_CACHE_MAX_PER_SESSION):
        serve._mesh_cache_put("m_%d" % i, _TRI, SESSION_A)
    # Use the oldest, then upload one more: the used one must survive.
    serve._mesh_cache_get(SESSION_A, "m_0")
    serve._mesh_cache_put("m_new", _TRI, SESSION_A)
    check(serve._mesh_cache_get(SESSION_A, "m_0") is not None,
          "lru: looking a mesh up marks it recently used, so an active "
          "design's mesh is not the first thing evicted", _ids_for(SESSION_A))
    check(serve._mesh_cache_get(SESSION_A, "m_1") is None,
          "lru: the genuinely least-recently-used entry went instead",
          _ids_for(SESSION_A))


def test_global_ceiling_still_bounds_total():
    _reset_cache()
    # More sessions than the global ceiling can hold, each filling its
    # per-session quota.
    n_sessions = (serve.MESH_CACHE_MAX_TOTAL
                  // serve.MESH_CACHE_MAX_PER_SESSION) + 4
    for s in range(n_sessions):
        sid = "s%09d" % s
        for i in range(serve.MESH_CACHE_MAX_PER_SESSION):
            serve._mesh_cache_put("s%d_m%d" % (s, i), _TRI, sid)

    total, tris = serve._mesh_cache_total()
    check(total <= serve.MESH_CACHE_MAX_TOTAL,
          f"global: {n_sessions} sessions x "
          f"{serve.MESH_CACHE_MAX_PER_SESSION} uploads stays under the "
          f"global ceiling of {serve.MESH_CACHE_MAX_TOTAL} entries "
          "(a per-session cap alone would let N sessions multiply)",
          f"total={total}")
    check(tris <= serve.MESH_CACHE_MAX_TOTAL_TRIANGLES,
          "global: the triangle budget also holds", f"tris={tris}")

    # The triangle budget is the honest memory bound, and it must be able to
    # bite on its own -- a count ceiling bounds nothing when one upload can be
    # MESH_MAX_TRIANGLES and another 200 triangles.
    check(serve.MESH_CACHE_MAX_TOTAL_TRIANGLES >= serve.MESH_MAX_TRIANGLES,
          "global: the cache triangle budget is at least one max-size mesh, "
          "so a legal upload is never uncacheable on arrival",
          f"{serve.MESH_CACHE_MAX_TOTAL_TRIANGLES} vs "
          f"{serve.MESH_MAX_TRIANGLES}")

    _reset_cache()
    big = _TRI * (serve.MESH_CACHE_MAX_TOTAL_TRIANGLES // 2)
    serve._mesh_cache_put("big_a", big, SESSION_A)
    serve._mesh_cache_put("big_b", big, SESSION_B)
    _, tris = serve._mesh_cache_total()
    serve._mesh_cache_put("big_c", big, "cccccccc3333")
    total_after, tris_after = serve._mesh_cache_total()
    check(tris_after <= serve.MESH_CACHE_MAX_TOTAL_TRIANGLES,
          "global: a third half-budget mesh evicts on the TRIANGLE budget, "
          "well before the entry-count ceiling of "
          f"{serve.MESH_CACHE_MAX_TOTAL} is anywhere near",
          f"tris={tris_after} entries={total_after}")
    check(total_after < 3,
          "global: memory, not entry count, is what triggered that eviction",
          total_after)


def test_no_session_fallback_works():
    _reset_cache()
    # The local single-user CLI case and any older client: no header at all.
    check(serve._mesh_cache_put("local_mesh", _TRI, None) is True,
          "fallback: an upload with no session id is accepted")
    check(serve._mesh_cache_get(None, "local_mesh") is not None,
          "fallback: and is found again by a request with no session id")

    # A malformed session id is the same case, never an error and never
    # another session's bucket (mirrors printer_store.session_list).
    check(serve._mesh_cache_get("!!not-a-session!!", "local_mesh") is not None,
          "fallback: a malformed session id falls back rather than erroring")
    check(serve._mesh_session_key("short") == serve._MESH_NO_SESSION,
          "fallback: a too-short id is not a valid session",
          serve._mesh_session_key("short"))

    # A session-carrying client still finds a mesh it uploaded before it had
    # a session id -- otherwise the header arriving mid-visit would dead-end
    # the user on 're-upload the STL'.
    check(serve._mesh_cache_get(SESSION_A, "local_mesh") is not None,
          "fallback: a session request falls through to the shared bucket, so "
          "a pre-session upload is not orphaned")
    # ...but its own bucket wins, so the shared bucket can never shadow it.
    serve._mesh_cache_put("local_mesh", _TRI * 7, SESSION_A)
    entry = serve._mesh_cache_get(SESSION_A, "local_mesh")
    check(entry is not None and len(entry["tris"]) == 7,
          "fallback: the session's OWN entry wins over a same-id entry in the "
          "shared bucket -- the shared bucket cannot shadow it",
          None if entry is None else len(entry["tris"]))


def test_no_session_fallback_cannot_evict_a_session():
    """The fallback bucket must not be a weapon.

    If flooding it evicted session-scoped meshes, then simply omitting the
    session header would hand any visitor the exact cross-user eviction this
    whole change removes.
    """
    _reset_cache()
    serve._mesh_cache_put("victim", _TRI, SESSION_B)

    for i in range(200):
        serve._mesh_cache_put("flood_%d" % i, _TRI, None)

    check(serve._mesh_cache_get(SESSION_B, "victim") is not None,
          "fallback: 200 no-session uploads do not evict a session-scoped "
          "mesh", _ids_for(SESSION_B))
    check(len(_ids_for(None)) <= serve.MESH_CACHE_MAX_PER_SESSION,
          "fallback: the shared bucket is itself capped, so it cannot grow "
          "into the global budget either", len(_ids_for(None)))

    # Same story on the triangle budget, which is the axis that actually
    # costs memory: a huge no-session upload is refused rather than allowed
    # to push session meshes out.
    _reset_cache()
    big = _TRI * (serve.MESH_CACHE_MAX_TOTAL_TRIANGLES - 1)
    serve._mesh_cache_put("session_big", big, SESSION_B)
    ok = serve._mesh_cache_put("anon_big", big, None)
    check(ok is False,
          "fallback: an over-budget no-session upload is REFUSED, not granted "
          "at a session's expense", ok)
    check(serve._mesh_cache_get(SESSION_B, "session_big") is not None,
          "fallback: the session's mesh survived that attempt")
    check(serve._mesh_cache_get(None, "anon_big") is None,
          "fallback: and the refused upload is not left half-cached, so the "
          "client is never handed a mesh_id that is already gone")


def test_put_signature_stays_backwards_compatible():
    """Other test scripts call _mesh_cache_put(mesh_id, tris) positionally."""
    _reset_cache()
    serve._mesh_cache_put("two_arg", _TRI)
    check(serve._mesh_cache_get(None, "two_arg") is not None,
          "compat: the two-argument call still works and lands in the shared "
          "bucket (test_printer_import.py and test_report_extra_issues.py "
          "both use it)")


def test_lookup_sites_all_go_through_the_helper():
    """No call site may reach into _mesh_cache directly any more.

    A bare ``_mesh_cache.get(mesh_id)`` against the nested structure now
    returns a whole session bucket rather than an entry -- it would not raise,
    it would quietly hand a dict to code expecting {"tris": [...]}.
    """
    import inspect
    src = inspect.getsource(serve)
    helper_names = ("_mesh_cache_put", "_mesh_cache_get", "_mesh_cache_total",
                    "_mesh_cache_over_budget", "_mesh_cache_drop_lru_from")
    helper_src = "".join(inspect.getsource(getattr(serve, n))
                         for n in helper_names)
    outside = src.count("_mesh_cache.") - helper_src.count("_mesh_cache.")
    check(outside == 0,
          "contract: every _mesh_cache access outside the helpers goes "
          "through _mesh_cache_get/_mesh_cache_put",
          f"{outside} direct access(es) remain")

    # And every generate entry point must inject _session, or a session's own
    # upload is invisible to it.
    for name in ("_handle_generate_stream", "_handle_export_stl"):
        fn_src = inspect.getsource(getattr(serve.Handler, name))
        check('body["_session"] = _session_id(self)' in fn_src,
              f"contract: {name} injects the server-owned _session")


def main() -> int:
    test_json_body_accepts_a_real_design()
    test_json_body_rejects_oversized()
    test_lying_content_length_cannot_exhaust_memory()
    test_session_isolation()
    test_lru_touch_protects_an_active_mesh()
    test_global_ceiling_still_bounds_total()
    test_no_session_fallback_works()
    test_no_session_fallback_cannot_evict_a_session()
    test_put_signature_stays_backwards_compatible()
    test_lookup_sites_all_go_through_the_helper()
    _reset_cache()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
