"""Results from upstreams that answered too late to be waited for.

One federated upstream consistently takes longer than the whole federation
budget while the rest finish inside 700ms. Waiting for it would cost every
caller more than a second; cancelling it, which is what we did, threw away an
entire registry's coverage on every query.

So the slow ones are allowed to finish after the budget has expired, off the
request path, and their answers are kept here for the next caller asking the
same thing. A registry served from this cache is reported as `cached` with the
age of the answer, never as though it replied in time: this project's claim is
that it really does query every registry, and quietly presenting a stored
answer as a live one would make that claim false.

Its own database file, deliberately. This is written from the request path, and
the index takes exactly one writer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

TTL_S = int(os.getenv("NEURONTO_FEDCACHE_TTL", "1800"))
MAX_ROWS = int(os.getenv("NEURONTO_FEDCACHE_ROWS", "5000"))
DB_PATH = Path(os.getenv("NEURONTO_FEDCACHE_DB",
                         os.getenv("NEURONTO_DB", "data/neuronto.db")).rsplit("/", 1)[0]
               ) / "fedcache.db"

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fed (
  k    TEXT PRIMARY KEY,
  uid  TEXT NOT NULL,
  ts   INTEGER NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fed_ts ON fed(ts);
"""


def _conn() -> sqlite3.Connection | None:
    c = getattr(_local, "c", None)
    if c is not None:
        return c
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=2, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=1500")
        c.executescript(_SCHEMA)
        c.commit()
    except Exception:
        return None
    _local.c = c
    return c


def key(uid: str, text: str) -> str:
    """Normalised, so "Read a PDF" and "read  a pdf" share an answer."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(f"{uid}\x00{norm}".encode()).hexdigest()


def get(uid: str, text: str) -> tuple[list, int] | None:
    """Returns (results, age_seconds), or None. Never raises: a cache that
    fails must degrade to no cache, never to a failed search."""
    c = _conn()
    if c is None:
        return None
    try:
        row = c.execute("SELECT ts, body FROM fed WHERE k=?", (key(uid, text),)).fetchone()
        if not row:
            return None
        age = int(time.time()) - int(row[0])
        if age > TTL_S:
            return None
        return json.loads(row[1]), age
    except Exception:
        return None


def put(uid: str, text: str, results: list) -> None:
    c = _conn()
    if c is None or not results:
        return
    try:
        now = int(time.time())
        c.execute("INSERT INTO fed(k,uid,ts,body) VALUES(?,?,?,?) "
                  "ON CONFLICT(k) DO UPDATE SET ts=excluded.ts, body=excluded.body",
                  (key(uid, text), uid, now, json.dumps(results)))
        # Bounded rather than swept on a timer: the cheapest moment to evict is
        # the one where we are already writing.
        c.execute("DELETE FROM fed WHERE ts < ?", (now - TTL_S,))
        n = c.execute("SELECT COUNT(*) FROM fed").fetchone()[0]
        if n > MAX_ROWS:
            c.execute("DELETE FROM fed WHERE k IN "
                      "(SELECT k FROM fed ORDER BY ts ASC LIMIT ?)", (n - MAX_ROWS,))
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass


def stats() -> dict:
    c = _conn()
    if c is None:
        return {"rows": 0, "ttl_s": TTL_S}
    try:
        rows = c.execute("SELECT COUNT(*) FROM fed").fetchone()[0]
        by = {r[0]: r[1] for r in c.execute("SELECT uid, COUNT(*) FROM fed GROUP BY uid")}
        return {"rows": rows, "ttl_s": TTL_S, "by_upstream": by}
    except Exception:
        return {"rows": 0, "ttl_s": TTL_S}
