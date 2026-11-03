#!/usr/bin/env python3
"""A dead upstream costs the harvest five queries, not the whole battery.

Throwaway database. One stand-in upstream times out on every query, one answers.
Requires: the dead one is abandoned after HARVEST_GIVE_UP_AFTER attempts and its
last_ok is not stamped; the live one is harvested in full.

    python3 scripts/test_harvest_breaker.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import zlib

DB = tempfile.mktemp(suffix=".db")
os.environ["NEURONTO_DB"] = DB
os.environ["NEURONTO_EVENT_SINK"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx                                                              # noqa: E402
from app import config, ingest, store                                     # noqa: E402

FAILS: list[str] = []
CALLS = {"dead": 0, "live": 0}


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


async def fake_post(self, url, json=None, **kw):
    if "dead" in url:
        CALLS["dead"] += 1
        raise httpx.ReadTimeout("timed out")
    CALLS["live"] += 1
    q = json["query"]["text"]
    k = zlib.crc32(q.encode())
    return httpx.Response(200, json={"results": [{
        "identifier": f"urn:air:live.example:mcp:{k}", "displayName": q,
        "type": "application/mcp-server-card+json", "url": f"https://live.example/{k}"}]},
        request=httpx.Request("POST", url))


def main() -> int:
    conn = store.connect()
    store.init(conn)
    config.UPSTREAMS[:] = [("dead", "Dead Registry", "https://dead.example/search", "test"),
                           ("live", "Live Registry", "https://live.example/search", "test")]
    httpx.AsyncClient.post = fake_post
    qs = [f"query {i}" for i in range(20)]
    out = asyncio.run(ingest.from_upstreams(conn, qs))
    by = {s["source"]: s for s in out}
    check("a dead upstream is left after the give-up threshold",
          CALLS["dead"] == ingest.HARVEST_GIVE_UP_AFTER, CALLS)
    check("it is reported as abandoned, with why", "abandoned" in by["dead"], by["dead"])
    check("its last_ok is not stamped",
          conn.execute("SELECT 1 FROM registries WHERE id=?", ("dead",)).fetchone() is None)
    check("a live upstream is harvested in full",
          CALLS["live"] == len(qs) and by["live"]["entries"] == len(qs), (CALLS, by["live"]))
    print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all passed"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(DB + suf)
            except OSError:
                pass
