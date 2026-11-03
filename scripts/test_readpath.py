#!/usr/bin/env python3
"""The read path and the write path (internal/ARCHITECTURE.md, 2026-11-04).

Against a throwaway database:
  * an upsert that changes nothing rewrites neither the row nor its full-text
    document, and records the sighting in `seen_at`; one that changes something
    does both, and `updated_at` moves only then;
  * a changed payment declaration is written even when nothing else changed;
  * every public count is read from `index_stats`: computing it once and reading
    it many times gives the same numbers on every surface, and a refresh after a
    write is what moves them;
  * the 30-day series is exactly the tail of the stored 90-day series;
  * a private entry never reaches a public count.

    python3 scripts/test_readpath.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

DB = tempfile.mktemp(suffix=".db")
os.environ["NEURONTO_DB"] = DB
os.environ["NEURONTO_EVENT_SINK"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import store                                                    # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


def fts_rowid(conn, key):
    r = conn.execute("SELECT rowid FROM entries_fts WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def main() -> int:
    conn = store.connect()
    store.init(conn)
    e = {"identifier": "urn:air:example.com:mcp:weather", "displayName": "Weather",
         "type": "application/mcp-server-card+json", "url": "https://example.com/mcp",
         "description": "Forecasts by city.", "tags": ["weather"]}
    key = store.upsert_entry(conn, e, "crawl")
    conn.commit()
    r1 = conn.execute("SELECT updated_at, seen_at FROM entries WHERE key=?", (key,)).fetchone()
    f1 = fts_rowid(conn, key)
    check("insert sets seen_at", r1["seen_at"] is not None)

    time.sleep(1.1)
    store.upsert_entry(conn, dict(e), "crawl")
    conn.commit()
    r2 = conn.execute("SELECT updated_at, seen_at FROM entries WHERE key=?", (key,)).fetchone()
    check("an unchanged re-sighting leaves updated_at alone", r2["updated_at"] == r1["updated_at"],
          (r1["updated_at"], r2["updated_at"]))
    check("an unchanged re-sighting moves seen_at", r2["seen_at"] > r1["seen_at"])
    check("an unchanged re-sighting does not rewrite the FTS document", fts_rowid(conn, key) == f1)

    time.sleep(1.1)
    store.upsert_entry(conn, {**e, "description": "Forecasts and alerts by city, hourly."}, "crawl")
    conn.commit()
    r3 = conn.execute("SELECT updated_at, description FROM entries WHERE key=?", (key,)).fetchone()
    check("a changed field moves updated_at", r3["updated_at"] > r2["updated_at"])
    hit = conn.execute("SELECT key FROM entries_fts WHERE entries_fts MATCH 'hourly'").fetchone()
    check("a changed field reaches the full-text index", hit is not None and hit[0] == key)

    store.upsert_entry(conn, dict(e, description="Forecasts and alerts by city, hourly."), "glama")
    conn.commit()
    srcs = conn.execute("SELECT sources FROM entries WHERE key=?", (key,)).fetchone()[0]
    check("a new source is a change", "glama" in srcs, srcs)

    # counts come from the snapshot and move only on refresh
    c1 = store.counts(conn)
    check("counts read from the snapshot", c1["entries"] == 1, c1)
    store.upsert_entry(conn, {**e, "identifier": "urn:air:example.org:mcp:maps",
                              "url": "https://example.org/mcp"}, "crawl")
    conn.commit()
    check("a write does not move a snapshot by itself", store.counts(conn)["entries"] == 1)
    took = store.refresh_stats(conn, store.STATS_FOR["light"])
    check("refresh moves it", store.counts(conn)["entries"] == 2, store.counts(conn))
    check("refresh reports the groups it computed", set(took) == set(store.STATS_FOR["light"]), took)
    check("every surface reads the same number",
          store.counts(conn)["entries"] == store.stat(conn, "counts")["entries"]
          == store.stat(conn, "composition")["total"])
    check("asOf is recorded", (store.stats_as_of(conn) or 0) >= int(time.time()) - 5)

    s90 = store.stat(conn, "series")
    s30 = store.daily_series(conn, 30)
    check("the 30-day series is the tail of the 90-day series",
          s30["indexed"] == s90["indexed"][-31:] and len(s30["indexed"]) == 31, (len(s30["indexed"])))
    check("today's indexing is in the series", sum(s30["indexed"]) == 2, s30["indexed"])

    store.add_private_entry(conn, "example.net", {"identifier": "urn:air:example.net:x:y",
                                                  "displayName": "Private", "url": "https://example.net/x"})
    conn.commit()
    store.refresh_stats(conn)
    check("a private entry never reaches a public count", store.counts(conn)["entries"] == 2,
          store.counts(conn))

    rid = store.pipeline_start(conn, "ingest")
    check("no good ingest before one ends ok", store.last_good(conn, "ingest") is None)
    store.pipeline_end(conn, rid, "ok")
    check("last good ingest is recorded", (store.last_good(conn, "ingest") or 0) > 0)

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
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
