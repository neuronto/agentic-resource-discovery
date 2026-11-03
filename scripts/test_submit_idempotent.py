#!/usr/bin/env python3
"""A submission is idempotent per target (internal/ARCHITECTURE.md, 2026-11-04).

Throwaway index and submissions databases, no network, no live analytics:
  * spellings of one URL normalise to one target;
  * a resubmission of a target verified inside the window is answered from the
    record (deduplicated, the verified tools, lastVerified, reverifyAfter), no probe;
  * `reverify` is refused inside the cooldown and honoured after it;
  * the window passing, or the entry leaving the index, means a real verification;
  * our own probes and the retrier are never answered from the record (_submit gate).

    python3 scripts/test_submit_idempotent.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
SUBS = tempfile.mktemp(suffix=".db")
os.environ["NEURONTO_DB"] = DB
os.environ["NEURONTO_SUBMISSIONS_DB"] = SUBS
os.environ["NEURONTO_EVENT_SINK"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import main, store, submissions                                  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


def body(resp):
    return None if resp is None else json.loads(bytes(resp.body).decode())


def main_() -> int:
    n = submissions.normalize
    check("spellings normalise to one target",
          n("endpoint", "HTTPS://Example.COM:443/mcp/") == n("endpoint", "https://example.com/mcp")
          == "https://example.com/mcp", n("endpoint", "HTTPS://Example.COM:443/mcp/"))
    check("a non-default port is kept", n("endpoint", "https://example.com:8443/mcp") ==
          "https://example.com:8443/mcp")
    check("normalising is idempotent", n("endpoint", n("endpoint", "https://A.b/x/")) == "https://a.b/x")
    check("a domain normalises to its host", n("domain", "https://Example.com/path") == "example.com")

    conn = store.connect()
    store.init(conn)
    key = store.upsert_entry(conn, {"identifier": "urn:air:example.com:mcp:weather",
                                    "displayName": "Weather", "type": "application/mcp-server-card+json",
                                    "url": "https://example.com/mcp"}, "submitted")
    conn.execute("UPDATE entries SET mcp_tools=2, mcp_server_name='weather' WHERE key=?", (key,))
    for t in ("forecast", "alerts"):
        conn.execute("INSERT INTO tools(entry_key, name) VALUES(?,?)", (key, t))
    conn.commit()

    sid = submissions.open("endpoint", "HTTPS://Example.com/mcp/", "http")
    submissions.record(sid, indexed=True, reason="indexed", entry_key=key, tools=2)

    d = body(main._already_verified("https://example.com/mcp", "", False))
    check("a repeat inside the window is answered from the record", d and d.get("deduplicated"), d)
    check("it carries the verified tools", d and d.get("verified_tools") == 2
          and sorted(d.get("tools") or []) == ["alerts", "forecast"], d)
    check("it says when it was verified and when it will be again",
          d and d.get("lastVerified") and d.get("reverifyAfter", 0) > d.get("lastVerified", 0), d)
    check("it carries the original receipt", d and d["submission"]["id"] == sid, d and d.get("submission"))

    d = body(main._already_verified("https://example.com/mcp", "", True))
    check("reverify inside the cooldown is still answered from the record", d and d.get("deduplicated"))
    old = submissions.REVERIFY_EVERY_S
    submissions.REVERIFY_EVERY_S = 0
    check("reverify after the cooldown verifies for real",
          main._already_verified("https://example.com/mcp", "", True) is None)
    submissions.REVERIFY_EVERY_S = old

    oldw = submissions.RESUBMIT_WINDOW_S
    submissions.RESUBMIT_WINDOW_S = -1
    check("after the window it verifies for real",
          main._already_verified("https://example.com/mcp", "", False) is None)
    submissions.RESUBMIT_WINDOW_S = oldw

    conn.execute("DELETE FROM entries WHERE key=?", (key,))
    conn.commit()
    check("an entry that left the index is verified again",
          main._already_verified("https://example.com/mcp", "", False) is None)

    check("never seen: verified for real", main._already_verified("https://other.example/mcp", "", False) is None)

    import inspect
    src = inspect.getsource(main._submit)
    check("probes, the retrier and dry runs bypass the record",
          'source != "retry" and not dry and not probe' in src)

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_())
    finally:
        for f in (DB, SUBS):
            for suf in ("", "-wal", "-shm"):
                try:
                    os.remove(f + suf)
                except OSError:
                    pass
