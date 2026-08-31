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
--
-- `tool_text` is the verified tool surface: the real tool names and descriptions
-- read back off the running server, not the publisher's prose. Every other
-- registry indexes the marketing blurb; this column is the thing an agent
-- actually has to match on.
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  key UNINDEXED, display_name, description, rep_queries, tags, capabilities,
  tool_text,
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

-- Verified tools. One row per tool actually returned by `tools/list` on a live
-- MCP endpoint. Every registry in this field indexes servers; the tool names and
-- input schemas an agent must match on exist in no other index. A tool here is
-- evidence, not a claim: it was read off the running server at `checked`.
CREATE TABLE IF NOT EXISTS tools (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_key    TEXT NOT NULL,          -- FK to entries.key
  name         TEXT NOT NULL,
  title        TEXT,
  description  TEXT,
  input_schema TEXT,                   -- json, verbatim
  checked      INTEGER,
  UNIQUE(entry_key, name)
);
CREATE INDEX IF NOT EXISTS idx_tools_entry ON tools(entry_key);
CREATE INDEX IF NOT EXISTS idx_tools_name  ON tools(name);

CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
  tool_id UNINDEXED, entry_key UNINDEXED, name, title, description,
  tokenize='porter unicode61 remove_diacritics 2'
);

-- Dense vectors for the semantic leg. Stored as raw float32 so the whole matrix
-- loads with one read and no per-row parse.
CREATE TABLE IF NOT EXISTS vectors (
  key   TEXT PRIMARY KEY,
  model TEXT,
  dim   INTEGER,
  vec   BLOB,
  ts    INTEGER
);

-- ARD adoption: does a host that ships a callable resource also publish a
-- manifest? Measured, not asserted.
CREATE TABLE IF NOT EXISTS adoption (
  host        TEXT PRIMARY KEY,
  has_manifest INTEGER,               -- 1 yes, 0 no
  status       INTEGER,
  entries      INTEGER,
  notable      INTEGER DEFAULT 0,     -- 1 = on the watchlist we publish
  checked      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_adoption_notable ON adoption(notable);

-- Benchmark runs. Kept so a published number can always be traced to the run
-- that produced it.
CREATE TABLE IF NOT EXISTS bench_runs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER,
  tasks    INTEGER,
  k        INTEGER,
  results  TEXT                       -- json: per-target metrics
);
"""

# Columns added after the first deployment. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so this is applied by inspection in `init`.
_ADD_COLUMNS = {
    "mcp_checked":     "INTEGER",   # when we last introspected
    "mcp_tools":       "INTEGER",   # verified tool count
    "mcp_auth":        "INTEGER",   # 1 = endpoint demanded credentials
    "mcp_server_name": "TEXT",      # serverInfo.name it reported
    "mcp_status":      "TEXT",      # ok | auth | error:<kind>
}


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=45, check_same_thread=False)
    c.row_factory = sqlite3.Row
    # Long maintenance jobs run alongside a web process that writes a row on
    # every search. Eight seconds was not enough patience: it killed a domain
    # crawl at 62,000 of 372,058 and lost a nine minute benchmark run, both on
    # `database is locked`. WAL already allows concurrent readers, so waiting
    # here costs nothing that matters and prevents losing hours of work.
    c.execute("PRAGMA busy_timeout=45000")
    return c


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current shape.

    Two kinds of change. New `entries` columns are added by inspection. The FTS
    table is different: adding an indexed column changes its arity, and FTS5
    cannot alter one in place, so it is dropped and rebuilt from `entries`,
    which is safe because every byte in it is derived data.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
    for col, decl in _ADD_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {decl}")

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(entries_fts)")]
    if "tool_text" not in cols:
        conn.execute("DROP TABLE IF EXISTS entries_fts")
        conn.executescript(_SCHEMA)
        rebuild_fts(conn)


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """Reindex every entry. Used after a schema change to entries_fts."""
    conn.execute("DELETE FROM entries_fts")
    n = 0
    for r in conn.execute("SELECT key FROM entries").fetchall():
        _reindex(conn, r["key"])
        n += 1
        if n % 2000 == 0:
            conn.commit()
    conn.commit()
    return n


def _s(v: Any) -> str | None:
    """Coerce any manifest value to something SQLite can bind.

    Publishers write what they like. A field the specification describes as a
    string arrives as an object, a number, or a list, and an unbindable type
    raises mid-transaction and kills the batch. Nothing about third-party input
    is guaranteed, so nothing is trusted.
    """
    if v is None or isinstance(v, str):
        return v or None
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        # the common shape: {"id": "did:web:..."} or {"value": "..."}
        for k in ("identity", "id", "value", "did", "uri", "url"):
            got = v.get(k)
            if isinstance(got, str) and got:
                return got
        return json.dumps(v, ensure_ascii=False)[:300]
    if isinstance(v, (list, tuple)):
        return _s(v[0]) if v else None
    return str(v)[:300]


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
    tm = e.get("trustManifest")
    trust = _s(tm.get("identity")) if isinstance(tm, dict) else None

    row = conn.execute("SELECT * FROM entries WHERE key=?", (key,)).fetchone()
    if row:
        srcs = set(json.loads(row["sources"] or "[]")); srcs.add(source)
        # Keep whichever description is more informative.
        desc = _s(e.get("description")) or ""
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
            (_s(e.get("displayName")) or "", _s(desc) or "", _s(e.get("type") or e.get("mediaType")) or "",
             fam, _s(url) or "", pub, json.dumps(tags), json.dumps(caps), json.dumps(rq),
             trust, _s(e.get("version")) or "", json.dumps(sorted(srcs)), now, key))
    else:
        conn.execute("""INSERT INTO entries(key,identifier,identifier_raw,display_name,description,
            type_raw,type_family,url,publisher,tags,capabilities,rep_queries,trust_identity,
            version,sources,first_seen,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, _s(ident or url), _s(ident_raw), _s(e.get("displayName")) or "",
             _s(e.get("description")) or "",
             _s(e.get("type") or e.get("mediaType")) or "", fam, _s(url) or "", pub,
             _jlist(e.get("tags")), _jlist(e.get("capabilities")), _jlist(e.get("representativeQueries")),
             trust, _s(e.get("version")) or "", json.dumps([source]), now, now))
    _reindex(conn, key)
    return key


def _reindex(conn: sqlite3.Connection, key: str) -> None:
    r = conn.execute("""SELECT key,display_name,description,rep_queries,tags,capabilities
                        FROM entries WHERE key=?""", (key,)).fetchone()
    if not r: return
    def flat(js: str) -> str:
        try: return " ".join(json.loads(js or "[]"))
        except Exception: return ""
    # The verified tool surface, folded into the entry's own document so a
    # server-level query matches on what the server can actually do.
    tt = conn.execute(
        """SELECT GROUP_CONCAT(name || ' ' || COALESCE(title,'') || ' '
                               || COALESCE(description,''), ' ')
           FROM tools WHERE entry_key=?""", (key,)).fetchone()[0] or ""
    conn.execute("DELETE FROM entries_fts WHERE key=?", (key,))
    conn.execute("""INSERT INTO entries_fts(key,display_name,description,rep_queries,
                                            tags,capabilities,tool_text)
                    VALUES(?,?,?,?,?,?,?)""",
                 (r["key"], r["display_name"] or "", r["description"] or "",
                  flat(r["rep_queries"]), flat(r["tags"]), flat(r["capabilities"]),
                  tt[:20000]))


def replace_tools(conn: sqlite3.Connection, entry_key: str,
                  tools: list[dict]) -> int:
    """Record the tools an endpoint actually exposed, replacing what we had.

    Replacement rather than merge is deliberate: a tool that has disappeared
    from `tools/list` is gone, and keeping it would recreate exactly the stale
    catalogue problem this whole feature exists to fix.
    """
    now = int(time.time())
    old = [r["id"] for r in conn.execute(
        "SELECT id FROM tools WHERE entry_key=?", (entry_key,))]
    if old:
        conn.executemany("DELETE FROM tools_fts WHERE tool_id=?", [(i,) for i in old])
    conn.execute("DELETE FROM tools WHERE entry_key=?", (entry_key,))
    n = 0
    for t in tools:
        name = _s(t.get("name"))
        if not name:
            continue
        schema = t.get("inputSchema") or t.get("input_schema")
        cur = conn.execute(
            """INSERT OR IGNORE INTO tools(entry_key,name,title,description,
                                           input_schema,checked)
               VALUES(?,?,?,?,?,?)""",
            (entry_key, name[:200], _s(t.get("title")),
             _s(t.get("description")),
             json.dumps(schema, ensure_ascii=False)[:8000] if schema else None, now))
        if cur.rowcount:
            conn.execute("""INSERT INTO tools_fts(tool_id,entry_key,name,title,description)
                            VALUES(?,?,?,?,?)""",
                         (cur.lastrowid, entry_key, name[:200],
                          _s(t.get("title")) or "", _s(t.get("description")) or ""))
            n += 1
    _reindex(conn, entry_key)
    return n


def mark_introspection(conn: sqlite3.Connection, key: str, status: str,
                       n_tools: int, auth: bool, server_name: str | None) -> None:
    conn.execute("""UPDATE entries SET mcp_checked=?, mcp_tools=?, mcp_auth=?,
                                       mcp_server_name=?, mcp_status=?
                    WHERE key=?""",
                 (int(time.time()), n_tools, 1 if auth else 0,
                  _s(server_name), status, key))


def tools_for(conn: sqlite3.Connection, entry_key: str) -> list[dict]:
    rows = conn.execute("""SELECT name,title,description,input_schema
                           FROM tools WHERE entry_key=? ORDER BY name""",
                        (entry_key,)).fetchall()
    out = []
    for r in rows:
        d = {"name": r["name"]}
        if r["title"]: d["title"] = r["title"]
        if r["description"]: d["description"] = r["description"]
        if r["input_schema"]:
            try: d["inputSchema"] = json.loads(r["input_schema"])
            except Exception: pass
        out.append(d)
    return out


def tool_counts(conn: sqlite3.Connection) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "tools":             q("SELECT COUNT(*) FROM tools"),
        "servers_with_tools": q("SELECT COUNT(DISTINCT entry_key) FROM tools"),
        "introspected":      q("SELECT COUNT(*) FROM entries WHERE mcp_checked IS NOT NULL"),
        "auth_required":     q("SELECT COUNT(*) FROM entries WHERE mcp_auth=1"),
        "by_status":         {r["mcp_status"]: r["n"] for r in conn.execute(
            "SELECT mcp_status, COUNT(*) n FROM entries WHERE mcp_status IS NOT NULL "
            "GROUP BY mcp_status ORDER BY n DESC")},
    }


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


def _label(identifier: str | None, url: str | None) -> str | None:
    """A readable name for an entry whose publisher gave none.

    The specification requires only `identifier` on an entry, so plenty of real
    manifests omit displayName. Returning null for those makes a listing read as
    a column of nulls, when the URN's last segment is a perfectly good label.
    """
    ident = str(identifier or "")
    if ident.startswith("urn:"):
        leaf = ident.rstrip(":").split(":")[-1]
        if leaf:
            return leaf.replace("-", " ").replace("_", " ").strip() or ident
    if url:
        return str(url).replace("https://", "").replace("http://", "").split("/")[0]
    return ident or None


def row_to_entry(r: sqlite3.Row) -> dict:
    """The public shape. `identifier` is the only term the spec requires."""
    j = lambda s: json.loads(s or "[]")
    out = {
        "identifier": r["identifier"],
        "displayName": r["display_name"] or _label(r["identifier"], r["url"]),
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
