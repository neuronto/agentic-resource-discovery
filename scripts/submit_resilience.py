"""Proof that a submission cannot be lost.

Runs the real application code in-process against a throwaway data directory
and a fake MCP server whose behaviour this script controls. It asserts the two
failure classes that lose publishers, with nothing mocked on our side:

  * the endpoint is down (503) when submitted, and comes up later;
  * the index is locked by another writer when we try to write.

Plus the shapes a refusal must carry: a non-MCP page, a JSON-RPC error, an SSE
server, a duplicate submission, and a queue row that has run out of attempts.

    .venv/bin/python -m scripts.submit_resilience

Exits non-zero on the first failed assertion. Run before every deploy that
touches app/main.py, app/submissions.py or app/tools_index.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="neuronto-resilience-")
os.environ["NEURONTO_DATA"] = TMP
os.environ["NEURONTO_DB"] = f"{TMP}/neuronto.db"
os.environ["NEURONTO_LIMITS_DB"] = f"{TMP}/limits.db"
os.environ["NEURONTO_SUBMISSIONS_DB"] = f"{TMP}/submissions.db"
os.environ["NEURONTO_FEDCACHE_DB"] = f"{TMP}/fedcache.db"
os.environ["NEURONTO_EVENT_SINK"] = ""
os.environ["NEURONTO_TELEGRAM"] = "0"
os.environ["NEURONTO_INTROSPECT_TIMEOUT"] = "4"

from app import main, submissions, store  # noqa: E402  (after the env is set)

# ---------------------------------------------------------------- fake server
MODE = {"state": "down"}


class _MCP(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # The domain route: an ARD manifest on the well-known path.
        if self.path == "/.well-known/ard.json" and MODE["state"] != "down":
            man = {"entries": [{"identifier": f"urn:air:127.0.0.1:mcp:manifest-test",
                                "displayName": "manifest test",
                                "type": "application/mcp-server-card+json",
                                "url": f"{BASE}/mcp",
                                "description": "from the manifest"}]}
            return self._send(200, json.dumps(man).encode())
        return self._send(404, b"nope", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        st = MODE["state"]
        if st == "down":
            return self._send(503, b"deploying, back in a moment", "text/plain")
        if st == "html":
            return self._send(200, b"<!doctype html><title>Home</title>not an mcp server", "text/html")
        if st == "rpcerror":
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                               "error": {"code": -32601, "message": "wrong path, use /v2"}}).encode())
        m = req.get("method")
        if m == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "resilience-test"}}
        elif m == "tools/list":
            if st == "up" and self.headers.get("mcp-protocol-version") is None:
                return self._send(400, b"missing MCP-Protocol-Version", "text/plain")
            result = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]}
        else:
            result = {}
        msg = json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}, indent=2)
        if st == "sse":
            body = f"event: message\ndata: {msg.replace(chr(10), chr(10) + 'data: ')}\n\n".encode()
            return self._send(200, body, "text/event-stream")
        return self._send(200, msg.encode())


_srv = ThreadingHTTPServer(("127.0.0.1", 0), _MCP)
PORT = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

# ---------------------------------------------------------------- helpers
_n = 0


def check(cond, what):
    global _n
    _n += 1
    print(("  PASS  " if cond else "  FAIL  ") + what, flush=True)
    if not cond:
        sys.exit(1)


async def submit(body):
    resp = await main.submit_endpoint(body)
    return resp.status_code, json.loads(bytes(resp.body).decode())


def force_due(sid):
    c = sqlite3.connect(os.environ["NEURONTO_SUBMISSIONS_DB"])
    c.execute("UPDATE submissions SET next_at=0, claimed_until=0 WHERE id=?", (sid,))
    c.commit(); c.close()


def set_attempts(sid, n):
    c = sqlite3.connect(os.environ["NEURONTO_SUBMISSIONS_DB"])
    c.execute("UPDATE submissions SET attempts=? WHERE id=?", (n, sid))
    c.commit(); c.close()


def entry_for(url):
    c = store.connect()
    r = c.execute("SELECT key, mcp_tools FROM entries WHERE url=?", (url,)).fetchone()
    c.close()
    return dict(r) if r else None


async def run():
    main.db()  # schema
    print(f"\n  submission resilience against fake MCP server on :{PORT}")
    print("  --------------------------------------------------------------")

    # 1. endpoint is down when submitted -------------------------------------
    MODE["state"] = "down"
    url = f"{BASE}/mcp"
    t0 = time.perf_counter()
    code, d = await submit({"endpoint": url})
    took = time.perf_counter() - t0
    check(code == 202 and d["status"] == "pending", f"a down endpoint is queued, not refused ({code} {d['status']})")
    check(d["indexed"] is False and d["refusal"] == "not_an_mcp_server", "the answer says it is not indexed and why")
    ev = d.get("evidence") or {}
    check(ev.get("http") == 503 and "deploying" in (ev.get("body") or ""), f"evidence carries what the endpoint returned: {ev}")
    check(took >= 1.4, f"the handshake was retried once before recording a failure ({took:.1f}s)")
    sub = d["submission"]
    sid = sub["id"]
    check(sub["attempts"] == 1 and 50 <= sub["next_attempt_in_s"] <= 60, f"first retry is due in about a minute ({sub['next_attempt_in_s']}s)")
    check(sub["attempts_left"] == 7 and sub["status_url"].endswith(f"/submit/status/{sid}"), "seven attempts left and a status url")

    # 2. the same target submitted again joins the same row -------------------
    code, d2 = await submit({"endpoint": url})
    check(d2["submission"]["id"] == sid and d2["submission"]["attempts"] == 2, "re-submitting reuses the queue row and counts an attempt")
    check(d2["submission"]["attempts_left"] == 7, f"...but a hand re-submit does not spend the schedule (attempts_left={d2['submission']['attempts_left']})")

    # 3. retrier: still down ---------------------------------------------------
    force_due(sid)
    done = await main.retry_due_submissions()
    row = submissions.get(sid)
    check(len(done) == 1 and row["status"] == "pending" and row["attempts"] == 3, f"retrier attempted it and it stays pending (attempts={row['attempts']})")
    check(entry_for(url) is None, "nothing was indexed while it was down")

    # 4. endpoint comes up: the retrier indexes it, nobody touched anything ----
    MODE["state"] = "up"
    force_due(sid)
    done = await main.retry_due_submissions()
    row = submissions.get(sid)
    e = entry_for(url)
    check(row["status"] == "indexed" and row["attempts"] == 4, f"indexed on attempt {row['attempts']} once the endpoint came up")
    check(e is not None and e["mcp_tools"] == 1 and row["entry_key"] == e["key"], "the entry exists in the index with its verified tool and the row points at it")
    check(done[0]["status"] == "indexed" and done[0]["submission"]["attempts"] == 4, "the retrier's own answer says indexed")
    resp = main.submit_status(sid)
    st = json.loads(bytes(resp.body).decode())
    check(resp.status_code == 200 and st["status"] == "indexed" and "attempt 4" in st["note"], "status url tells the story")
    s = submissions.stats()
    check(s["indexed_on_retry"] == 1 and s["pending"] == 0, f"metrics count the recovery: {s}")

    # 5. already indexed: submitting again is idempotent and immediate --------
    code, d = await submit({"endpoint": url})
    check(code == 200 and d["status"] == "indexed" and d["submission"]["id"] != sid, "a fresh submission of an indexed target indexes again with a new row")

    # 6. a page that is not an MCP server: evidence says so -------------------
    MODE["state"] = "html"
    url2 = f"{BASE}/page"
    code, d = await submit({"endpoint": url2})
    ev = d.get("evidence") or {}
    check(code == 202 and d["reason"] == "error:handshake", f"an HTML page is a handshake failure ({d['reason']})")
    check(ev.get("content_type", "").startswith("text/html") and "not an mcp" in ev.get("body", ""), f"evidence shows the HTML: {ev}")
    # 6b. a publisher hammering re-submit while they fix things cannot exhaust
    # the queue: nine hand re-submits in a row, still pending, still 7 left.
    # (on 2026-09-02 the e2e suite's own probe target was gave_up after 59 min
    # because each run's re-submit spent one of the eight scheduled attempts)
    for _ in range(9):
        code, d = await submit({"endpoint": url2})
    check(code == 202 and d["status"] == "pending" and d["submission"]["attempts_left"] == 7 and d["submission"]["attempts"] == 10,
          f"nine hand re-submits later it is still pending with the schedule intact ({code} {d['status']}, attempts={d['submission']['attempts']}, left={d['submission']['attempts_left']})")

    # 7. a JSON-RPC error is recorded as the server's own answer --------------
    MODE["state"] = "rpcerror"
    url3 = f"{BASE}/wrong"
    code, d = await submit({"endpoint": url3})
    check(code == 202 and d["reason"] == "error:rpc-32601" and "use /v2" in (d["evidence"] or {}).get("rpc_error", ""), f"a JSON-RPC error carries its message: {d['reason']} {d.get('evidence')}")

    # 8. an SSE server with multi-line frames is read correctly ---------------
    MODE["state"] = "sse"
    url4 = f"{BASE}/sse"
    code, d = await submit({"endpoint": url4})
    check(code == 200 and d["status"] == "indexed" and d["verified_tools"] == 1, f"an SSE server indexes ({code} {d.get('status')} {d.get('reason')})")

    # 9. the index is locked when we write: busy, not refused, and fast -------
    MODE["state"] = "up"
    url5 = f"{BASE}/locked"
    lock = sqlite3.connect(os.environ["NEURONTO_DB"], timeout=1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute("INSERT OR REPLACE INTO stats(k,v) VALUES('resilience-lock','1')")
    t0 = time.perf_counter()
    code, d = await submit({"endpoint": url5})
    took = time.perf_counter() - t0
    check(code == 202 and d["status"] == "pending" and d["reason"] == "busy", f"a locked index answers busy ({code} {d['status']} {d.get('reason')})")
    check(took < 20, f"and answers within seconds, not the 45s lock wait ({took:.1f}s)")
    check(d.get("verified") is True, "the answer says the server itself verified fine")
    sub = d["submission"]
    check(sub["attempts"] == 0 and sub["next_attempt_in_s"] <= 30, f"a busy costs the publisher no attempt and retries in 30s (attempts={sub['attempts']}, in {sub['next_attempt_in_s']}s)")
    lock.rollback(); lock.close()
    force_due(sub["id"])
    await main.retry_due_submissions()
    row = submissions.get(sub["id"])
    check(row["status"] == "indexed" and row["attempts"] == 1 and entry_for(url5), "indexed on the first counted attempt once the lock cleared")

    # 10. running out of attempts is said out loud ----------------------------
    MODE["state"] = "down"
    url6 = f"{BASE}/dead"
    code, d = await submit({"endpoint": url6})
    sid6 = d["submission"]["id"]
    set_attempts(sid6, len(submissions.BACKOFF_S))
    force_due(sid6)
    done = await main.retry_due_submissions()
    row = submissions.get(sid6)
    check(row["status"] == "gave_up" and done[0]["status"] == "gave_up", f"after the last attempt the row is gave_up ({row['status']})")
    resp = main.submit_status(sid6)
    st = json.loads(bytes(resp.body).decode())
    check("never verified" in st["note"] and st["evidence"]["http"] == 503, "the status page says we tried and keeps the last evidence")
    code, d = await submit({"endpoint": url6})
    check(code == 202 and d["submission"]["id"] != sid6 and d["submission"]["attempts"] == 1, "submitting a gave_up target starts a fresh set of attempts")

    # 11. a bad request is not queued -----------------------------------------
    code, d = await submit({"endpoint": "not a url"})
    check(code == 400 and submissions.latest_for("endpoint", "not a url") is None, "a malformed request is refused outright and never queued")

    # 12. two retriers cannot take the same row -------------------------------
    MODE["state"] = "down"
    code, d = await submit({"endpoint": f"{BASE}/race"})
    force_due(d["submission"]["id"])
    a = submissions.due(5)
    b = submissions.due(5)
    check(len(a) == 1 and len(b) == 0, "a claimed row is invisible to a second retrier")

    # 13. a DOMAIN submission meets a locked index: busy, fast, indexed later --
    # The domain path had the same shape as the endpoint path until
    # 2026-09-02: the manifest's entries were written on the request
    # connection with a 45 second wait. Now: fetch on the loop, write in a
    # thread, and a lock is a queued busy.
    MODE["state"] = "up"
    # A hostname, because the route validates one; DNS is the only thing
    # faked here (the fetch is pointed at the local server), the write path
    # is the real code.
    from app import ingest, publisher
    host = "resilience.test"
    _real_fetch = ingest.fetch_manifest
    ingest.fetch_manifest = lambda dom, client=None: _real_fetch(f"http://127.0.0.1:{PORT}", client)
    lock = sqlite3.connect(os.environ["NEURONTO_DB"], timeout=1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute("INSERT OR REPLACE INTO stats(k,v) VALUES('resilience-lock','2')")
    t0 = time.perf_counter()
    code, d = await submit({"domain": host})
    took = time.perf_counter() - t0
    check(code == 202 and d["status"] == "pending" and d["reason"] == "busy",
          f"a domain submission against a locked index answers busy ({code} {d['status']} {d.get('reason')})")
    check(took < 20 and d["submission"]["attempts"] == 0,
          f"fast, and it costs the publisher no attempt ({took:.1f}s, attempts={d['submission']['attempts']})")
    lock.rollback(); lock.close()
    force_due(d["submission"]["id"])
    await main.retry_due_submissions()
    row = submissions.get(d["submission"]["id"])
    c = store.connect()
    n = c.execute("SELECT COUNT(*) FROM entries WHERE url=? AND sources LIKE '%crawl%'",
                  (f"{BASE}/mcp",)).fetchone()[0]
    seen = c.execute("SELECT manifest_path FROM crawl_seen WHERE domain=?", (host,)).fetchone()
    c.close()
    check(row["status"] == "indexed" and n == 1 and seen and seen[0] == "/.well-known/ard.json",
          f"the manifest's entry is indexed on retry and the crawl records the path ({row['status']}, entries={n}, path={seen and seen[0]})")

    ingest.fetch_manifest = _real_fetch

    # 14. a manifest build against a locked index still returns the manifest -
    # (the probing half is replaced by a fixed finding; what is under test is
    # the write that gave two visitors a 500 on 2026-09-01)
    _real_infer = publisher.infer_resources
    async def _fake_infer(h):
        return [{"identifier": f"urn:air:{h}:mcp:t", "displayName": "t",
                 "type": "application/mcp-server-card+json", "url": f"https://{h}/mcp",
                 "description": "t", "_evidence": "fixed by the test"}]
    publisher.infer_resources = _fake_infer
    lock = sqlite3.connect(os.environ["NEURONTO_DB"], timeout=1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute("INSERT OR REPLACE INTO stats(k,v) VALUES('resilience-lock','3')")
    t0 = time.perf_counter()
    resp = await main.manifest_build({"domain": host})
    took = time.perf_counter() - t0
    d = json.loads(bytes(resp.body).decode())
    check(resp.status_code == 200 and d.get("manifest") and d.get("hosted_at") is None and "busy" in d.get("hosted_note", ""),
          f"manifest build under a lock is a 200 with the manifest and an honest hosted_note, not a 500 ({resp.status_code}, {took:.1f}s)")
    lock.rollback(); lock.close()
    publisher.infer_resources = _real_infer

    # 15. the request-path connection cannot write at all. This is the guard
    # behind every check above: a write on the loop connection would sit on a
    # stale read snapshot, skip the busy handler, and fail in 200 ms exactly
    # the way the 2026-09-01 refusals did. So it must raise, not contend.
    loop_conn = main.db()
    ro = loop_conn.execute("PRAGMA query_only").fetchone()[0]
    refused = False
    try:
        loop_conn.execute("INSERT INTO stats(k,v) VALUES('resilience-ro','1')")
    except sqlite3.OperationalError as e:
        refused = "readonly" in str(e)
    check(ro == 1 and refused,
          f"the loop connection is read-only and a write on it raises (query_only={ro}, refused={refused})")
    # 16. ...while the same process still writes through the sanctioned path
    got = await main._index_write(lambda c: c.execute(
        "INSERT OR REPLACE INTO stats(k,v) VALUES('resilience-rw','1')").rowcount)
    await main._index_write(lambda c: c.execute("DELETE FROM stats WHERE k='resilience-rw'"))
    check(got == 1, f"_index_write on a fresh connection still writes (rowcount={got})")

    print("  --------------------------------------------------------------")
    print(f"  {_n} passed, 0 failed\n")


if __name__ == "__main__":
    asyncio.run(run())
