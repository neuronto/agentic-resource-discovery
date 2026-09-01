"""SQLite storage and lexical retrieval.

SQLite with FTS5 rather than a vector database, deliberately. The latency claim
is sub-200 ms on a 2-core box, and an in-process BM25 index costs no network
hop, no second daemon and no memory we do not have. Ranking quality comes from
field weighting and a score transform that actually separates (see rank.py),
not from bolting on a heavier engine.
"""
from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .normalize import dedupe_key, media_family, normalize_identifier, publisher_of

# Stored input schemas are capped so one giant tool cannot bloat the index.
_SCHEMA_MAX = 8000

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
  domain TEXT PRIMARY KEY, last_crawl INTEGER, status TEXT, entries INTEGER,
  manifest_path TEXT           -- which well-known path actually served it
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
  checked      INTEGER,
  path         TEXT                   -- which well-known path actually served it
);
CREATE INDEX IF NOT EXISTS idx_adoption_notable ON adoption(notable);

-- Observation history. The liveness and introspection columns on `entries` hold
-- only the latest value and are overwritten on every probe, which silently
-- destroys the one asset that cannot be rebuilt later: the time series. An
-- index can be re-crawled; a year of "was this endpoint up, and how many tools
-- did it expose" cannot be reconstructed after the fact.
--
-- Transitions only, not every sample. A probe that agrees with the last
-- recorded state writes nothing, so a stable endpoint costs one row per change
-- rather than one row per hour, and the table answers "when did this change"
-- exactly rather than approximately.
CREATE TABLE IF NOT EXISTS observations (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_key TEXT NOT NULL,
  ts        INTEGER NOT NULL,
  kind      TEXT NOT NULL,        -- liveness | introspect
  live      INTEGER,
  status    INTEGER,              -- http status, liveness only
  ms        INTEGER,
  tools     INTEGER,              -- verified tool count, introspect only
  auth      INTEGER,
  detail    TEXT                  -- mcp_status for introspect
);
CREATE INDEX IF NOT EXISTS idx_obs_entry ON observations(entry_key, ts);
CREATE INDEX IF NOT EXISTS idx_obs_ts    ON observations(ts);

-- Impressions: which entries a query actually returned, and where they ranked.
-- Without this we can log that somebody searched for "pdf" but never tell a
-- publisher which queries surfaced their resource, which is the whole of the
-- reporting product. Capped to the visible page, and carries no identifier of
-- who asked: what was asked is a product signal, who asked is not our business.
CREATE TABLE IF NOT EXISTS impressions (
  search_id INTEGER NOT NULL,
  entry_key TEXT NOT NULL,
  rank      INTEGER NOT NULL,
  score     INTEGER,
  ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imp_entry ON impressions(entry_key, ts);
CREATE INDEX IF NOT EXISTS idx_imp_search ON impressions(search_id);

-- Proven domain ownership. A TXT record survives a change of hosting and is
-- the same mechanism the MCP registry uses for namespaces, so a publisher who
-- has done it once already understands it.
CREATE TABLE IF NOT EXISTS claims (
  domain      TEXT PRIMARY KEY,
  token       TEXT,
  verified    INTEGER DEFAULT 0,
  verified_at INTEGER,
  created     INTEGER
);

-- Keys are scoped to one verified domain and grant exactly one privilege:
-- seeing and writing that domain's private entries. Nothing global.
CREATE TABLE IF NOT EXISTS api_keys (
  key       TEXT PRIMARY KEY,
  domain    TEXT NOT NULL,
  label     TEXT,
  created   INTEGER,
  last_used INTEGER
);
CREATE INDEX IF NOT EXISTS idx_keys_domain ON api_keys(domain);

-- Benchmark runs. Kept so a published number can always be traced to the run
-- that produced it.
CREATE TABLE IF NOT EXISTS bench_runs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER,
  tasks    INTEGER,
  k        INTEGER,
  results  TEXT                       -- json: per-target metrics
);

-- An organisation's internal services, in a table of their own.
--
-- These were briefly kept in `entries` behind a `visibility` column. That was
-- wrong twice over. It leaked: every public count is a plain COUNT(*) over
-- `entries`, so a private row silently joined the published totals, and sealing
-- it would have meant adding the same predicate to thirty queries and to every
-- query written afterwards. And it modelled them as the same kind of thing when
-- they are not: an internal endpoint sits behind the customer's firewall, so it
-- can never be liveness-probed, introspected or embedded the way a public
-- resource is. Kept apart, a public query cannot reach them by construction,
-- and the crawl pipelines cannot waste probes on hosts they will never reach.
CREATE TABLE IF NOT EXISTS private_entries (
  key          TEXT PRIMARY KEY,
  owner_domain TEXT NOT NULL,
  identifier   TEXT,
  display_name TEXT,
  description  TEXT,
  url          TEXT,
  type_raw     TEXT,
  type_family  TEXT,
  tags         TEXT,
  capabilities TEXT,
  rep_queries  TEXT,
  created      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_private_owner ON private_entries(owner_domain);
-- Every publisher lookup is `lower(publisher)=?`, which the plain index on
-- publisher cannot serve: it was a full scan on each demand report, badge and
-- publisher page. An expression index matches the query as written.
CREATE INDEX IF NOT EXISTS idx_entries_publisher_lower ON entries(lower(publisher));

CREATE VIRTUAL TABLE IF NOT EXISTS private_fts USING fts5(
  key UNINDEXED, display_name, description, rep_queries, tags, tokenize='porter'
);
"""

# Columns added after the first deployment. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so this is applied by inspection in `init`.
_ADD_COLUMNS = {
    # Vestigial: private entries were briefly stored here behind these two
    # columns before moving to `private_entries`. Retained only so the one-time
    # migration in `init` can find any row left over from that design, and as a
    # belt-and-braces filter in search. Nothing writes them any more.
    "visibility":      "TEXT",
    "owner_domain":    "TEXT",
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
    # WAL plus NORMAL is the standard production setting: a commit no longer
    # waits for an fsync, the file cannot be corrupted by a crash, and at worst
    # the last few transactions before a power loss are lost. With FULL, every
    # logged search paid an fsync on the request path; measured as ten
    # concurrent searches taking 1.2 seconds each where one takes forty ms.
    c.execute("PRAGMA synchronous=NORMAL")
    return c


_tls = threading.local()
_tls_lock = threading.Lock()
_tls_ready = False


def tls_conn() -> sqlite3.Connection:
    """A connection for the calling thread, schema initialised exactly once.

    SQLite serialises every statement on a connection behind its own mutex, so
    one shared connection makes readers queue however many threads there are.
    One per thread is cheap and lets WAL do what it exists for.
    """
    global _tls_ready
    c = getattr(_tls, "conn", None)
    if c is not None:
        return c
    c = connect()
    with _tls_lock:
        if not _tls_ready:
            init(c)
            _tls_ready = True
    # The request path only reads. Every index write from a request runs in a
    # thread on a fresh connection (`main._index_write`), because a connection
    # that holds a read snapshot and then writes into a held lock is refused
    # at once: SQLite skips the busy handler to avoid a deadlock, so the 45 s
    # timeout above was never consulted and a publisher was refused in 200 ms
    # (2026-09-01). `query_only` makes that rule unbreakable rather than
    # remembered: a write on this connection raises instead of contending.
    c.execute("PRAGMA query_only=1")
    _tls.conn = c
    return c


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    _migrate_private(conn)
    conn.commit()


def _migrate_private(conn: sqlite3.Connection) -> int:
    """Move any row left over from the visibility-column design.

    Private entries used to live in `entries`. Nothing writes them there now, so
    this runs once and then finds nothing for the rest of the database's life.
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(entries)")]
    if "visibility" not in cols:
        return 0
    rows = conn.execute(
        "SELECT * FROM entries WHERE visibility='private'").fetchall()
    for r in rows:
        add_private_entry(conn, r["owner_domain"] or "", row_to_entry(r))
        conn.execute("DELETE FROM entries WHERE key=?", (r["key"],))
        conn.execute("DELETE FROM entries_fts WHERE key=?", (r["key"],))
    if rows:
        conn.commit()
    return len(rows)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current shape.

    Two kinds of change. New `entries` columns are added by inspection. The FTS
    table is different: adding an indexed column changes its arity, and FTS5
    cannot alter one in place, so it is dropped and rebuilt from `entries`,
    which is safe because every byte in it is derived data.
    """
    cs = {r["name"] for r in conn.execute("PRAGMA table_info(crawl_seen)")}
    if cs and "manifest_path" not in cs:
        conn.execute("ALTER TABLE crawl_seen ADD COLUMN manifest_path TEXT")

    ad = {r["name"] for r in conn.execute("PRAGMA table_info(adoption)")}
    if ad and "path" not in ad:
        conn.execute("ALTER TABLE adoption ADD COLUMN path TEXT")

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
             json.dumps(schema, ensure_ascii=False)[:_SCHEMA_MAX] if schema else None, now))
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
    prev = conn.execute("SELECT mcp_status, mcp_tools FROM entries WHERE key=?",
                        (key,)).fetchone()
    now = int(time.time())
    conn.execute("""UPDATE entries SET mcp_checked=?, mcp_tools=?, mcp_auth=?,
                                       mcp_server_name=?, mcp_status=?
                    WHERE key=?""",
                 (now, n_tools, 1 if auth else 0, _s(server_name), status, key))
    # A changed tool count is the interesting event: a server that gained or
    # lost capability, or drifted from what it advertises.
    if (prev is None or prev["mcp_status"] is None
            or prev["mcp_status"] != status or (prev["mcp_tools"] or 0) != n_tools):
        conn.execute("""INSERT INTO observations(entry_key,ts,kind,tools,auth,detail)
                        VALUES(?,?,'introspect',?,?,?)""",
                     (key, now, n_tools, 1 if auth else 0, status))


def demand_for(conn: sqlite3.Connection, host: str, days: int = 30,
               limit: int = 25) -> dict:
    """What agents asked that returned this publisher's resources.

    The question a publisher actually has is not "am I listed" but "did anyone
    come looking, and for what". We can answer it because every search records
    which entries it returned and at what rank, and we can answer it without
    tracking anyone: the query text is a product signal, the person who typed it
    is not our business, so no client identifier is stored to aggregate.
    """
    since = int(time.time()) - days * 86400
    host = host.lower().strip()
    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM entries WHERE lower(publisher)=?", (host,))]
    # A publisher looks themselves up by domain, but the publisher string is
    # often something else: the MCP Registry namespaces are reverse-DNS
    # (`ai.filegraph` for filegraph.ai), and a resource on api.example.com is
    # attributed to example.com. Asking for your own domain and being told
    # "no report" was the first thing a real publisher would have hit. So the
    # lookup also accepts the reversed two-label form and any entry whose
    # endpoint lives on that host, which is the same matching /audit uses.
    parts = host.split(".")
    if len(parts) == 2:
        keys += [r["key"] for r in conn.execute(
            "SELECT key FROM entries WHERE lower(publisher)=?",
            (".".join(reversed(parts)),))]
    keys += [r["key"] for r in conn.execute(
        "SELECT key FROM entries WHERE url LIKE ? OR url LIKE ?",
        (f"%//{host}/%", f"%//{host}"))]
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {"domain": host, "indexed": 0, "impressions": 0, "queries": [],
                "resources": [], "days": days}
    marks = ",".join("?" * len(keys))
    total = conn.execute(
        f"SELECT COUNT(*) FROM impressions WHERE ts>=? AND entry_key IN ({marks})",
        [since] + keys).fetchone()[0]
    queries = [{"query": r["q"], "times": r["n"], "best_rank": r["best"],
                "avg_rank": round(r["avg_r"], 1)}
               for r in conn.execute(
        f"""SELECT s.q, COUNT(*) n, MIN(i.rank) best, AVG(i.rank) avg_r
            FROM impressions i JOIN searches s ON s.id = i.search_id
            WHERE i.ts>=? AND i.entry_key IN ({marks})
            GROUP BY s.q ORDER BY n DESC, best ASC LIMIT ?""",
        [since] + keys + [limit])]
    resources = [{"name": r["display_name"] or r["identifier"],
                  "identifier": r["identifier"], "impressions": r["n"],
                  "best_rank": r["best"]}
                 for r in conn.execute(
        f"""SELECT e.display_name, e.identifier, COUNT(*) n, MIN(i.rank) best
            FROM impressions i JOIN entries e ON e.key = i.entry_key
            WHERE i.ts>=? AND i.entry_key IN ({marks})
            GROUP BY e.key ORDER BY n DESC LIMIT ?""", [since] + keys + [limit])]
    return {"domain": host, "indexed": len(keys), "impressions": total,
            "distinct_queries": len(queries), "queries": queries,
            "resources": resources, "days": days}


def history_counts(conn: sqlite3.Connection) -> dict:
    """Size of the accumulating record. Surfaced so it cannot rot unnoticed."""
    q = lambda s: conn.execute(s).fetchone()
    o = q("SELECT COUNT(*), MIN(ts), MAX(ts) FROM observations")
    i = q("SELECT COUNT(*) FROM impressions")
    return {"observations": o[0] or 0, "since": o[1], "latest": o[2],
            "impressions": i[0] or 0}


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
    prev = conn.execute("SELECT live FROM entries WHERE key=?", (key,)).fetchone()
    now = int(time.time())
    conn.execute("""UPDATE entries SET live=?, live_status=?, live_ms=?, live_checked=?
                    WHERE key=?""", (1 if alive else 0, status, ms, now, key))
    # Record the transition, not the sample. First observation always counts.
    if prev is None or prev["live"] is None or prev["live"] != (1 if alive else 0):
        conn.execute("""INSERT INTO observations(entry_key,ts,kind,live,status,ms)
                        VALUES(?,?,'liveness',?,?,?)""",
                     (key, now, 1 if alive else 0, status, ms))


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


PRIVATE_QUERY_PLACEHOLDER = "(authenticated search, text not recorded)"


_logq: "queue.Queue[tuple]" = queue.Queue(maxsize=50000)
_logthread: threading.Thread | None = None


def _log_writer() -> None:
    """Drain the search log in batches, one transaction per batch.

    Logging used to happen inline: an insert, an impressions insert and a
    commit on every search, on the event loop, with an fsync each. It is
    bookkeeping, and bookkeeping must never sit between a caller and their
    answer. A batch of two hundred searches now costs one commit on a thread
    that nobody is waiting for. A crash loses at most the unflushed batch of
    log rows, which is the right thing to lose.
    """
    c = connect()
    while True:
        first = _logq.get()
        batch = [first]
        try:
            while len(batch) < 200:
                batch.append(_logq.get_nowait())
        except queue.Empty:
            pass
        try:
            for (q, mode, results, ms, federated, ents, authenticated, now) in batch:
                cur = c.execute("""INSERT INTO searches(q,mode,results,ms,federated,ts)
                                   VALUES(?,?,?,?,?,?)""",
                                (PRIVATE_QUERY_PLACEHOLDER if authenticated else q[:200],
                                 mode, results, ms, federated, now))
                if ents and not authenticated:
                    c.executemany(
                        """INSERT INTO impressions(search_id,entry_key,rank,score,ts)
                           VALUES(?,?,?,?,?)""",
                        [(cur.lastrowid, k, i, sc, now) for i, (k, sc) in enumerate(ents, start=1)])
            c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass


def log_search(conn: sqlite3.Connection, q: str, mode: str, results: int,
               ms: int, federated: int, entries: list[dict] | None = None,
               authenticated: bool = False) -> None:
    """Record a query and what it returned, off the request path.

    `conn` is accepted for compatibility and unused: the writer thread holds
    its own connection. See `_log_writer` for why this is not inline, and
    `PRIVATE_QUERY_PLACEHOLDER` for why an authenticated search keeps neither
    its text nor its impressions.
    """
    global _logthread
    ents = [(e.get("_key"), e.get("score")) for e in (entries or [])[:10]
            if e.get("_key") and e.get("visibility") != "private"]
    try:
        _logq.put_nowait((q, mode, results, ms, federated, ents, authenticated, int(time.time())))
    except queue.Full:
        return
    if _logthread is None or not _logthread.is_alive():
        _logthread = threading.Thread(target=_log_writer, name="search-log", daemon=True)
        _logthread.start()


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
    """Recent activity, without the words anyone typed.

    This used to return `q` verbatim on the public `/stats` endpoint, which
    published every visitor's query to anyone who asked, including the query
    text of an authenticated search against a private registry. The demand
    signal that has actual product value is the scoped one, `/demand?domain=`,
    where a publisher sees the queries that returned *their* resources. A raw
    global feed of what strangers typed added nothing to that and carried the
    whole risk, so it is gone: shape only, no text.
    """
    rows = conn.execute("""SELECT results, ms, ts, mode FROM searches
                           ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Private entries
# ---------------------------------------------------------------------------

def _private_key(domain: str, identifier: str) -> str:
    return hashlib.sha1(f"private|{domain}|{identifier}".encode()).hexdigest()


def add_private_entry(conn: sqlite3.Connection, domain: str, e: dict) -> str:
    """Store one internal service for a verified domain."""
    ident = e.get("identifier") or ""
    if not ident:
        return ""
    key = _private_key(domain, ident)
    js = lambda v: json.dumps(v) if v else None
    conn.execute("""INSERT INTO private_entries
          (key,owner_domain,identifier,display_name,description,url,
           type_raw,type_family,tags,capabilities,rep_queries,created)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET
          display_name=excluded.display_name, description=excluded.description,
          url=excluded.url, type_raw=excluded.type_raw,
          type_family=excluded.type_family, tags=excluded.tags,
          capabilities=excluded.capabilities, rep_queries=excluded.rep_queries""",
        (key, domain.lower(), ident, e.get("displayName"), e.get("description"),
         e.get("url"), e.get("type"), media_family(e.get("type")),
         js(e.get("tags")), js(e.get("capabilities")), js(e.get("representativeQueries")),
         int(time.time())))
    conn.execute("DELETE FROM private_fts WHERE key=?", (key,))
    conn.execute("""INSERT INTO private_fts(key,display_name,description,rep_queries,tags)
                    VALUES(?,?,?,?,?)""",
                 (key, e.get("displayName") or "", e.get("description") or "",
                  " ".join(e.get("representativeQueries") or []),
                  " ".join(str(t) for t in (e.get("tags") or []))))
    conn.commit()
    return key


def private_count(conn: sqlite3.Connection, domain: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM private_entries WHERE owner_domain=?",
                        (domain.lower(),)).fetchone()[0]


def list_private(conn: sqlite3.Connection, domain: str) -> list[dict]:
    return [private_row_to_entry(r) for r in conn.execute(
        "SELECT * FROM private_entries WHERE owner_domain=? ORDER BY display_name",
        (domain.lower(),))]


def delete_private(conn: sqlite3.Connection, domain: str, identifier: str) -> bool:
    key = _private_key(domain.lower(), identifier)
    cur = conn.execute("DELETE FROM private_entries WHERE key=? AND owner_domain=?",
                       (key, domain.lower()))
    conn.execute("DELETE FROM private_fts WHERE key=?", (key,))
    conn.commit()
    return cur.rowcount > 0


def private_row_to_entry(r: sqlite3.Row) -> dict:
    j = lambda s: json.loads(s or "[]")
    out = {
        "identifier": r["identifier"],
        "displayName": r["display_name"],
        "type": r["type_raw"] or None,
        "url": r["url"] or None,
        "description": r["description"] or None,
        "tags": j(r["tags"]) or None,
        "capabilities": j(r["capabilities"]) or None,
        "representativeQueries": j(r["rep_queries"]) or None,
        "visibility": "private",
    }
    return {k: v for k, v in out.items() if v is not None}


def ard_publisher_counts(conn: sqlite3.Connection) -> dict:
    """The ARD publisher numbers, defined once so surfaces cannot disagree.

    Three surfaces were each computing their own and reporting three different
    figures for what looked like the same thing: `/metrics.json` said 240, the
    `/ard-publishers` page said 201, and the docs said 199. None was wrong. They
    were answering different questions under the same word.

      * `manifest_hosts` counts hosts where we fetched a manifest. That is the
        adoption measurement: how many places on the web serve one.
      * `publishers_indexed` counts publishers we hold entries for whose
        manifest we verified. That is the index measurement, and it is smaller
        because a manifest often lives on one host while the resources it
        declares belong to another: connectors-skills.zapier.com serves a
        manifest whose entries are, correctly, attributed to zapier.com.

    Both are true. Reporting either as "publishers" without saying which is not,
    so callers get both and the names say what they mean.
    """
    hosts = conn.execute(
        "SELECT COUNT(*) FROM crawl_seen WHERE manifest_path IS NOT NULL").fetchone()[0]
    indexed = conn.execute(
        """SELECT COUNT(DISTINCT e.publisher) FROM entries e
           JOIN crawl_seen cs ON cs.domain = lower(e.publisher)
           WHERE cs.manifest_path IS NOT NULL
             AND e.publisher IS NOT NULL AND e.publisher != ''""").fetchone()[0]
    by_path = {r[0]: r[1] for r in conn.execute(
        "SELECT manifest_path, COUNT(*) FROM crawl_seen "
        "WHERE manifest_path IS NOT NULL GROUP BY 1")}
    return {
        "manifest_hosts": hosts,
        "publishers_indexed": indexed,
        "hosts_serving_no_indexed_resource": hosts - indexed,
        "by_path": by_path,
        "domains_crawled": conn.execute("SELECT COUNT(*) FROM crawl_seen").fetchone()[0],
        "definitions": {
            "manifest_hosts": "hosts where we fetched a manifest that parsed",
            "publishers_indexed": ("publishers we hold entries for whose manifest we "
                                   "verified; smaller than manifest_hosts because a "
                                   "manifest may be served on a different host from the "
                                   "resources it declares"),
        },
    }
