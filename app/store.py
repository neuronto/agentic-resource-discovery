"""SQLite storage and lexical retrieval.

SQLite with FTS5 rather than a vector database, deliberately. The latency claim
is sub-200 ms on a 2-core box, and an in-process BM25 index costs no network
hop, no second daemon and no memory we do not have. Ranking quality comes from
field weighting and a score transform that actually separates (see rank.py),
not from bolting on a heavier engine.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from . import config
from .normalize import dedupe_key, media_family, normalize_identifier, publisher_of

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS entries (
  key            TEXT PRIMARY KEY,   -- dedupe_key: identity across registries
  identifier     TEXT NOT NULL,      -- normalised urn:air:...
  identifier_raw TEXT,               -- exactly what the publisher wrote
  display_name   TEXT,
  description    TEXT,
  type_raw       TEXT,               -- their spelling, never rewritten
  type_family    TEXT,               -- ours, for matching
  url            TEXT,
  publisher      TEXT,
  tags           TEXT,               -- json array
  capabilities   TEXT,               -- json array
  rep_queries    TEXT,               -- json array; the strongest ranking signal
  trust_identity TEXT,
  version        TEXT,
  sources        TEXT,               -- json array of registries that carry it
  first_seen     INTEGER,
  updated_at     INTEGER,
  -- liveness
  live           INTEGER,            -- 1 alive, 0 dead, NULL never probed
  live_status    INTEGER,
  live_ms        INTEGER,
  live_checked   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_entries_family    ON entries(type_family);
CREATE INDEX IF NOT EXISTS idx_entries_publisher ON entries(publisher);
CREATE INDEX IF NOT EXISTS idx_entries_live      ON entries(live);
CREATE INDEX IF NOT EXISTS idx_entries_updated   ON entries(updated_at);

-- Separate columns so rank.py can weight them independently. An entry is found
-- through its representative queries far more often than through its name, and
-- FTS5 cannot express that without the split.
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  key UNINDEXED, display_name, description, rep_queries, tags, capabilities,
  tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS registries (
  id TEXT PRIMARY KEY, name TEXT, search_url TEXT, source TEXT,
  entries_seen INTEGER DEFAULT 0, last_ok INTEGER, last_ms INTEGER
);

CREATE TABLE IF NOT EXISTS crawl_seen (
  domain TEXT PRIMARY KEY, last_crawl INTEGER, status TEXT, entries INTEGER
);

CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, v TEXT);

-- Query log. A discovery service that cannot say what it was asked has no way
-- to know whether its index matches demand, and the front page has nothing
-- true to show. Text only, no client identifiers: what was asked is a product
-- signal, who asked is not our business.
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  q TEXT, mode TEXT, results INTEGER, ms INTEGER, federated INTEGER, ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_searches_ts ON searches(ts);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=8000")
    return c


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _jlist(v: Any) -> str:
    if v is None: return "[]"
    if isinstance(v, str): v = [v]
    return json.dumps([str(x) for x in v if x is not None][:40], ensure_ascii=False)


def upsert_entry(conn: sqlite3.Connection, e: dict, source: str) -> str:
    """Insert or merge one entry.

    Merging matters: the same MCP server shows up in three registries under
    three different type spellings. We keep one row, union the sources, and
    prefer the richest description we have seen rather than the last one.
    """
    ident_raw = e.get("identifier") or e.get("@id") or e.get("url")
    ident = normalize_identifier(ident_raw) or ident_raw
    url = e.get("url")
    if not ident and not url:
        return ""
    key = dedupe_key(ident, url)
    now = int(time.time())
    fam = media_family(e.get("type") or e.get("mediaType"))
    pub = publisher_of(ident, url)
    trust = (e.get("trustManifest") or {}).get("identity") if isinstance(e.get("trustManifest"), dict) else None

    row = conn.execute("SELECT * FROM entries WHERE key=?", (key,)).fetchone()
    if row:
        srcs = set(json.loads(row["sources"] or "[]")); srcs.add(source)
        # Keep whichever description is more informative.
        desc = e.get("description") or ""
        if len(desc) <= len(row["description"] or ""):
            desc = row["description"]
        rq = json.loads(row["rep_queries"] or "[]")
        for q in (e.get("representativeQueries") or []):
            if q not in rq: rq.append(str(q))
        tags = json.loads(row["tags"] or "[]")
        for t in (e.get("tags") or []):
            if t not in tags: tags.append(str(t))
        caps = json.loads(row["capabilities"] or "[]")
        for c_ in (e.get("capabilities") or []):
            if c_ not in caps: caps.append(str(c_))
        conn.execute("""UPDATE entries SET display_name=COALESCE(NULLIF(?,''),display_name),
            description=?, type_raw=COALESCE(NULLIF(?,''),type_raw), type_family=?,
            url=COALESCE(NULLIF(?,''),url), publisher=COALESCE(?,publisher),
            tags=?, capabilities=?, rep_queries=?, trust_identity=COALESCE(?,trust_identity),
            version=COALESCE(NULLIF(?,''),version), sources=?, updated_at=?
            WHERE key=?""",
            (e.get("displayName") or "", desc, e.get("type") or e.get("mediaType") or "",
             fam, url or "", pub, json.dumps(tags), json.dumps(caps), json.dumps(rq),
             trust, str(e.get("version") or ""), json.dumps(sorted(srcs)), now, key))
    else:
        conn.execute("""INSERT INTO entries(key,identifier,identifier_raw,display_name,description,
            type_raw,type_family,url,publisher,tags,capabilities,rep_queries,trust_identity,
            version,sources,first_seen,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, ident or url, ident_raw, e.get("displayName") or "", e.get("description") or "",
             e.get("type") or e.get("mediaType") or "", fam, url or "", pub,
             _jlist(e.get("tags")), _jlist(e.get("capabilities")), _jlist(e.get("representativeQueries")),
             trust, str(e.get("version") or ""), json.dumps([source]), now, now))
    _reindex(conn, key)
    return key


def _reindex(conn: sqlite3.Connection, key: str) -> None:
    r = conn.execute("""SELECT key,display_name,description,rep_queries,tags,capabilities
                        FROM entries WHERE key=?""", (key,)).fetchone()
    if not r: return
    def flat(js: str) -> str:
        try: return " ".join(json.loads(js or "[]"))
        except Exception: return ""
    conn.execute("DELETE FROM entries_fts WHERE key=?", (key,))
    conn.execute("""INSERT INTO entries_fts(key,display_name,description,rep_queries,tags,capabilities)
                    VALUES(?,?,?,?,?,?)""",
                 (r["key"], r["display_name"] or "", r["description"] or "",
                  flat(r["rep_queries"]), flat(r["tags"]), flat(r["capabilities"])))


def mark_liveness(conn: sqlite3.Connection, key: str, alive: bool,
                  status: int | None, ms: int | None) -> None:
    conn.execute("""UPDATE entries SET live=?, live_status=?, live_ms=?, live_checked=?
                    WHERE key=?""", (1 if alive else 0, status, ms, int(time.time()), key))


def counts(conn: sqlite3.Connection) -> dict:
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    return {
        "entries":    q("SELECT COUNT(*) FROM entries"),
        "publishers": q("SELECT COUNT(DISTINCT publisher) FROM entries WHERE publisher IS NOT NULL"),
        "live":       q("SELECT COUNT(*) FROM entries WHERE live=1"),
        "dead":       q("SELECT COUNT(*) FROM entries WHERE live=0"),
        "unprobed":   q("SELECT COUNT(*) FROM entries WHERE live IS NULL"),
        "families":   {r["type_family"]: r["n"] for r in conn.execute(
            "SELECT type_family, COUNT(*) n FROM entries GROUP BY type_family ORDER BY n DESC")},
        "sources":    _source_counts(conn),
    }


def _source_counts(conn: sqlite3.Connection) -> dict:
    out: dict[str, int] = {}
    for r in conn.execute("SELECT sources FROM entries"):
        for s in json.loads(r["sources"] or "[]"):
            out[s] = out.get(s, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def row_to_entry(r: sqlite3.Row) -> dict:
    """The public shape. `identifier` is the only term the spec requires."""
    j = lambda s: json.loads(s or "[]")
    out = {
        "identifier": r["identifier"],
        "displayName": r["display_name"] or None,
        "type": r["type_raw"] or None,
        "url": r["url"] or None,
        "description": r["description"] or None,
        "tags": j(r["tags"]) or None,
        "capabilities": j(r["capabilities"]) or None,
        "representativeQueries": j(r["rep_queries"]) or None,
    }
    if r["trust_identity"]:
        out["trustManifest"] = {"identity": r["trust_identity"]}
    if r["version"]:
        out["version"] = r["version"]
    return {k: v for k, v in out.items() if v is not None}


def log_search(conn: sqlite3.Connection, q: str, mode: str, results: int,
               ms: int, federated: int) -> None:
    """Record a query. Best effort: never let bookkeeping fail a search."""
    try:
        conn.execute("""INSERT INTO searches(q,mode,results,ms,federated,ts)
                        VALUES(?,?,?,?,?,?)""",
                     (q[:200], mode, results, ms, federated, int(time.time())))
        conn.commit()
    except Exception:
        pass


def daily_series(conn: sqlite3.Connection, days: int = 30) -> dict:
    """Per-day counts for the front page sparklines.

    Buckets are filled for every day in the window, including zeros, so a chart
    never implies activity on a day that had none.
    """
    now = int(time.time())
    start = now - days * 86400
    def bucket(sql: str) -> list[int]:
        got = {int(r[0]): int(r[1]) for r in conn.execute(sql, (start,))}
        base = start - (start % 86400)
        return [got.get(base + i * 86400, 0) for i in range(days + 1)]
    return {
        "days": days,
        "indexed":  bucket("SELECT (first_seen/86400)*86400 d, COUNT(*) "
                           "FROM entries WHERE first_seen>=? GROUP BY d"),
        "searches": bucket("SELECT (ts/86400)*86400 d, COUNT(*) "
                           "FROM searches WHERE ts>=? GROUP BY d"),
        "probed":   bucket("SELECT (live_checked/86400)*86400 d, COUNT(*) "
                           "FROM entries WHERE live_checked>=? GROUP BY d"),
    }


def top_publishers(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    rows = conn.execute("""
        SELECT publisher,
               COUNT(*) n,
               SUM(CASE WHEN live=1 THEN 1 ELSE 0 END) live,
               MAX(updated_at) seen,
               GROUP_CONCAT(DISTINCT type_family) fams
        FROM entries WHERE publisher IS NOT NULL AND publisher != ''
        GROUP BY publisher ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
    return [{"publisher": r["publisher"], "entries": r["n"], "live": r["live"],
             "last_seen": r["seen"],
             "kinds": sorted((r["fams"] or "").split(","))[:3]} for r in rows]


def recent_searches(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute("""SELECT q, results, ms, ts FROM searches
                           ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]
