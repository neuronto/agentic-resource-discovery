"""The MCP Registry v0.1 read API: this index as an MCP subregistry.

MCP hosts that take a custom registry URL (GitHub Copilot's MCP registry policy,
VS Code's `chat.mcp.gallery.serviceUrl`, Visual Studio, JetBrains, Eclipse, Xcode,
Copilot CLI) append `/v0.1/servers` to a base URL and read the shape defined in
the registry's OpenAPI (modelcontextprotocol/registry, docs/reference/api). Point
one at this deployment's base URL and its server gallery is this index.

Two galleries, one code path:

  {base}/v0.1/servers            every server we hold with real connection info:
                                 the official registry's records, every version,
                                 verbatim, plus independently published remote
                                 servers whose endpoint answered an MCP handshake
  {base}/verified/v0.1/servers   only server versions whose remote endpoint answered
                                 an MCP handshake (or a 401/403 sign-in or x402 402
                                 answer) and was reachable at the last liveness check

The contract is the OpenAPI's, not a reading of it:
  - the list returns one record per server VERSION, ordered by name then version as
    text; `version=latest` narrows it to the latest version of each server;
  - `search` is a case-insensitive substring match on the name;
  - deleted records are left out unless `include_deleted=true`, and are always in
    when `updated_since` is given, so an aggregator syncing from us learns removals;
  - `cursor` is opaque and is the last record's `name:version`;
  - writes are optional in the OpenAPI and answer 501 here: this registry is read-only.

Nothing is invented. An official record is passed through as the official registry
serves it; our own findings go beside it under `_meta["com.neuronto/registry"]`. An
independent server is listed only when its endpoint told us its version in the
initialize handshake (`serverInfo.version`); a server that never told us one is not
listed, rather than listed with a version we made up. A server we stop listing stays
as a tombstone (`_meta["com.neuronto/registry"].status = "deleted"`) for 90 days.

Architecture. The build runs outside the serving process (`python -m app.subregistry
build`), reads the index once, and writes a separate SQLite snapshot that is swapped
in atomically (os.replace), so a rebuild never holds the index's write lock or a
worker's memory. Serving keeps one read-only, memory-mapped connection per worker
thread and reopens it only when the snapshot file is replaced, so a request is an
indexed range scan on a small file and never touches the 2.7 GB index. Every
response names its snapshot (`X-Registry-Snapshot`, `Last-Modified`, `ETag`), and a
cursor from one snapshot stays valid in the next because it is a position in the
name and version order, not an offset.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import config

# NEURONTO_SUBREGISTRY_DB points a staging build (and a test client) at another file.
DB = Path(os.getenv("NEURONTO_SUBREGISTRY_DB", str(config.DATA_DIR / "subregistry.db")))
OFFICIAL = "https://registry.modelcontextprotocol.io"
SPEC = "https://raw.githubusercontent.com/modelcontextprotocol/registry/main/docs/reference/api/openapi.yaml"
META = "com.neuronto/registry"
OFFICIAL_META = "io.modelcontextprotocol.registry/official"
NAME_RE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
REMOTE_URL_RE = re.compile(r"^(https?://[^\s]+|\{[a-zA-Z_][a-zA-Z0-9_]*\}[^\s]*)$")
RANGE_RE = re.compile(r"^[\^~><=]|\.x$|\.\*$|^latest$")
DEFAULT_LIMIT, MAX_LIMIT = 30, 100
DENSE = 2000          # trigram hits above which a search walks the name order instead
VERIFIED_STATUSES = ("ok", "ok:notools", "auth", "payment")
TOMBSTONE_DAYS = 90
# A new snapshot that lists far fewer live servers than the last one is a failed
# build (an upstream outage, a broken handshake stage), not a mass removal.
MIN_KEEP_RATIO = 0.9
CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, If-None-Match",
        "Access-Control-Expose-Headers": "ETag, Last-Modified, X-Registry-Snapshot"}
CACHE = "public, max-age=300"

router = APIRouter()


# ----------------------------------------------------------------------------- snapshot access

class _Snapshot:
    """One read-only connection per worker thread, reopened when the file is replaced.

    os.replace gives the new snapshot a new inode, so comparing (inode, mtime) on each
    request is one stat() call and the only way a swap is noticed; an open connection
    keeps reading the old file, consistently, until then."""

    def __init__(self) -> None:
        self.local = threading.local()

    def get(self) -> tuple[sqlite3.Connection, dict] | None:
        try:
            st = os.stat(DB)
        except FileNotFoundError:
            return None
        key = (st.st_ino, st.st_mtime_ns)
        cur = getattr(self.local, "cur", None)
        if cur is None or cur[0] != key:
            if cur is not None:
                cur[1].close()
            c = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA mmap_size=536870912")
            info = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM info")}
            built = int(json.loads(info.get("stats", "{}")).get("built") or st.st_mtime)
            snap = {"built": built, "id": info.get("snapshot") or str(built),
                    "iso": datetime.fromtimestamp(built, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "http": formatdate(built, usegmt=True)}
            cur = (key, c, snap)
            self.local.cur = cur
        return cur[1], cur[2]


_SNAP = _Snapshot()


def _headers(snap: dict | None, etag: str | None = None) -> dict:
    h = {**CORS, "Cache-Control": CACHE}
    if snap:
        h["X-Registry-Snapshot"] = snap["iso"]
        h["Last-Modified"] = snap["http"]
    if etag:
        h["ETag"] = etag
    return h


def _error(status: int, msg: str, snap: dict | None = None, extra: dict | None = None) -> JSONResponse:
    h = {**CORS, **(extra or {})}
    if snap:
        h["X-Registry-Snapshot"] = snap["iso"]
    return JSONResponse({"error": msg}, status_code=status, headers=h)


def _etag(snap: dict, request: Request) -> str:
    q = request.url.path + "?" + "&".join(sorted(f"{k}={v}" for k, v in request.query_params.multi_items()))
    return 'W/"%s-%s"' % (snap["id"], hashlib.sha1(q.encode()).hexdigest()[:16])


def _not_modified(request: Request, etag: str, snap: dict) -> Response | None:
    inm = request.headers.get("if-none-match")
    if inm and etag in [x.strip() for x in inm.split(",")]:
        return Response(status_code=304, headers=_headers(snap, etag))
    return None


def _bool(v: str | None) -> bool | None:
    if v is None or v == "":
        return False
    v = v.strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    return None


def _rfc3339(v: str) -> int | None:
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None
        return int(d.timestamp())
    except Exception:
        return None


def _body(docs: list[str], metadata: dict, headers: dict) -> Response:
    """Splice the stored JSON rather than parse and re-serialise a hundred records."""
    body = '{"servers":[' + ",".join(docs) + '],"metadata":' + json.dumps(metadata) + "}"
    return Response(body, media_type="application/json", headers=headers)


def _list(request: Request, verified: bool) -> Response:
    got = _SNAP.get()
    if got is None:
        return _error(503, "The registry snapshot is being built; try again shortly.", extra={"Retry-After": "300"})
    c, snap = got
    etag = _etag(snap, request)
    nm = _not_modified(request, etag, snap)
    if nm is not None:
        return nm
    q = request.query_params
    raw = q.get("limit")
    try:
        limit = DEFAULT_LIMIT if raw in (None, "") else int(raw)
    except ValueError:
        return _error(400, "limit must be an integer", snap)
    if limit < 1:
        return _error(400, "limit must be at least 1", snap)
    limit = min(limit, MAX_LIMIT)
    include_deleted = _bool(q.get("include_deleted"))
    if include_deleted is None:
        return _error(400, "include_deleted must be true or false", snap)
    where, args = [], []
    since = q.get("updated_since")
    if since:
        ts = _rfc3339(since)
        if ts is None:
            return _error(400, "updated_since must be an RFC 3339 date-time", snap)
        where.append("updated >= ?")
        args.append(ts)
        include_deleted = True                     # the OpenAPI: always true with updated_since
    if not include_deleted:
        where.append("deleted = 0")
    if verified:
        where.append("verified = 1")
    version = (q.get("version") or "").strip()
    if version == "latest":
        where.append("latest = 1")
    elif version:
        where.append("version = ?")
        args.append(version)
    search = (q.get("search") or "").strip()
    if search:
        # The plan follows how dense the matches are. A rare term ("weather") is a
        # few hundred trigram hits, filtered directly. A common one ("github", half of
        # all names) would make the trigram index hand back a hundred thousand rows
        # to sort, so instead the scan walks the name order and stops at the limit,
        # which a dense term reaches at once. Both paths cost a bounded amount.
        hits = None
        if len(search) >= 3:
            hits = [r[0] for r in c.execute("SELECT rowid FROM name_tri WHERE name_tri MATCH ? LIMIT ?",
                                            ('"' + search.replace('"', '""') + '"', DENSE + 1))]
        if hits is not None and len(hits) <= DENSE:
            if not hits:
                return _body([], {"count": 0}, _headers(snap, etag))
            where.append(f"rowid IN ({','.join(map(str, hits))})")
        else:
            esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("name LIKE ? ESCAPE '\\'")
            args.append(f"%{esc}%")
    cursor = q.get("cursor") or ""
    if cursor:
        # Opaque to clients; here, the last record's "name:version". A name never
        # contains a colon, so everything before the first one is the name.
        name, _, ver = cursor.partition(":")
        where.append("(name > ? OR (name = ? AND version > ?))")
        args += [name, name, ver]
    sql = "SELECT name, version, doc FROM servers"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name, version LIMIT ?"
    rows = c.execute(sql, args + [limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    meta: dict = {"count": len(rows)}
    if more:
        meta["nextCursor"] = f"{rows[-1]['name']}:{rows[-1]['version']}"
    return _body([r["doc"] for r in rows], meta, _headers(snap, etag))


def _split(rest: str) -> tuple[str, str | None, str | None] | None:
    """`{name}/versions`, `{name}/versions/{version}` or `{name}/status`.

    The server decodes `%2F` before routing, so `io.github.acme%2Fweather` arrives
    as two segments; a name has exactly one slash, which is what makes this exact.
    Returns (name, version or None, trailing segment or None)."""
    parts = urllib.parse.unquote(rest).split("/")
    if len(parts) < 3:
        return None
    name = f"{parts[0]}/{parts[1]}"
    if parts[2] == "status" and len(parts) == 3:
        return name, None, "status"
    if parts[2] != "versions":
        return None
    if len(parts) == 3:
        return name, None, None
    if len(parts) >= 5 and parts[-1] == "status":
        return name, "/".join(parts[3:-1]), "status"
    return name, "/".join(parts[3:]) or None, None


def _one(request: Request, rest: str, verified: bool) -> Response:
    got = _SNAP.get()
    if got is None:
        return _error(503, "The registry snapshot is being built; try again shortly.", extra={"Retry-After": "300"})
    c, snap = got
    sp = _split(rest)
    if sp is None or sp[2] == "status":
        return _error(404, "Not found", snap)
    name, version, _ = sp
    include_deleted = _bool(request.query_params.get("include_deleted"))
    if include_deleted is None:
        return _error(400, "include_deleted must be true or false", snap)
    etag = _etag(snap, request)
    nm = _not_modified(request, etag, snap)
    if nm is not None:
        return nm
    where = ["name = ?"]
    if not include_deleted:
        where.append("deleted = 0")
    if verified:
        where.append("verified = 1")
    cond = " AND ".join(where)
    rows = c.execute(f"SELECT name, version, doc, latest FROM servers WHERE {cond} ORDER BY version", (name,)).fetchall()
    if not rows:
        # Names are case sensitive; a host that changed the case still finds the server.
        real = c.execute("SELECT name FROM servers WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
        if real is not None and real["name"] != name:
            rows = c.execute(f"SELECT name, version, doc, latest FROM servers WHERE {cond} ORDER BY version",
                             (real["name"],)).fetchall()
    if not rows:
        return _error(404, "Server not found", snap)
    if version is None:
        return _body([r["doc"] for r in rows], {"count": len(rows)}, _headers(snap, etag))
    if version == "latest":
        hit = next((r for r in rows if r["latest"]), None) or rows[-1]
    else:
        hit = next((r for r in rows if r["version"] == version), None)
    if hit is None:
        return _error(404, "Server version not found", snap)
    return Response(hit["doc"], media_type="application/json", headers=_headers(snap, etag))


def _options() -> Response:
    return Response(status_code=204, headers=CORS)


def _read_only() -> JSONResponse:
    # The OpenAPI marks every write as optional and documents 501 for a registry
    # that does not support it.
    return JSONResponse({"error": "Publishing is not supported by this registry: it is read-only. Publish "
                                  "to the official MCP Registry; servers published there are listed here "
                                  "from the next snapshot."},
                        status_code=501, headers=CORS)


def _register() -> None:
    for prefix, verified in (("", False), ("/verified", True)):
        def lst(request: Request, _v=verified):
            return _list(request, _v)

        def one(request: Request, rest: str, _v=verified):
            return _one(request, rest, _v)

        router.add_api_route(f"{prefix}/v0.1/servers", lst, methods=["GET"], include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers", _options, methods=["OPTIONS"], include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers/{{rest:path}}", one, methods=["GET"], include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers/{{rest:path}}", _options, methods=["OPTIONS"],
                             include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers/{{rest:path}}", _read_only,
                             methods=["PUT", "DELETE", "PATCH"], include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/publish", _read_only, methods=["POST", "PUT"],
                             include_in_schema=False)


_register()


# ----------------------------------------------------------------------------- the build

def _iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check(row: sqlite3.Row | None) -> dict:
    """What we observed about one endpoint, in words a client can show."""
    if row is None:
        return {}
    st = row["mcp_status"]
    result = {"ok": "handshake-ok", "ok:notools": "handshake-ok", "auth": "authorization-required",
              "payment": "payment-required"}.get(st) if st else None
    if st and result is None:
        result = "failed"
    out: dict = {}
    if result:
        out["remoteCheck"] = {k: v for k, v in {
            "url": row["url"], "result": result, "checkedAt": _iso(row["mcp_checked"]),
            "tools": int(row["mcp_tools"]) if result == "handshake-ok" and row["mcp_tools"] is not None else None,
        }.items() if v is not None}
    if row["live"] is not None:
        out["liveness"] = {k: v for k, v in {
            "reachable": bool(row["live"]), "checkedAt": _iso(row["live_checked"]),
            "probes": int(row["probe_n"] or 0), "succeeded": int(row["probe_ok"] or 0),
        }.items() if v is not None}
    return out


def _is_verified(row: sqlite3.Row | None) -> bool:
    return bool(row is not None and row["mcp_status"] in VERIFIED_STATUSES and row["live"] == 1)


def _rev(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return ".".join(reversed(host.split(".")))


def _leaf(identifier: str) -> str:
    leaf = identifier.rsplit(":", 1)[-1]
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", leaf).strip("-.") or "server"


def _short(s: str, n: int = 100) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    cut = s[: n - 1].rsplit(" ", 1)[0].rstrip(",;:.")
    return cut + "…"


def _valid(doc: dict) -> bool:
    """The OpenAPI's hard constraints on ServerDetail, checked before a record is stored."""
    s = doc.get("server") or {}
    if not isinstance(s.get("name"), str) or not NAME_RE.match(s["name"]) or not 3 <= len(s["name"]) <= 200:
        return False
    if not isinstance(s.get("description"), str) or not 1 <= len(s["description"]) <= 100:
        return False
    v = s.get("version")
    if not isinstance(v, str) or not v or len(v) > 255:
        return False
    for r in s.get("remotes") or []:
        if r.get("type") not in ("streamable-http", "sse") or not REMOTE_URL_RE.match(r.get("url") or ""):
            return False
    return True


async def _server_version(client: httpx.AsyncClient, url: str) -> str | None:
    """serverInfo.version from the initialize handshake, or None."""
    from .tools_index import _HEADERS, _INIT, _parse
    try:
        r = await client.post(url, json=_INIT, headers=_HEADERS, timeout=config.INTROSPECT_TIMEOUT_S)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    d = _parse(r.text) or {}
    v = (((d.get("result") or {}).get("serverInfo") or {}).get("version"))
    if isinstance(v, (int, float)):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or len(v) > 255 or RANGE_RE.search(v):
        return None
    return v


def _schema():
    """A validator for ServerResponse from the registry's own OpenAPI, fetched fresh.

    The official registry serves records its own schema rejects (an empty
    `repository` object, a package pinned to the forbidden version "latest",
    argument types the schema does not define); a strict client may refuse a whole
    page over one of them, so every record is checked before it is stored. None
    when the spec cannot be fetched: the structural check in `_valid` still runs."""
    try:
        import jsonschema
        import yaml
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        spec = yaml.safe_load(httpx.get(SPEC, timeout=30, headers={"user-agent": config.USER_AGENT}).text)
        reg = Registry().with_resource("urn:mcp-registry-openapi", Resource(
            contents={"$id": "urn:mcp-registry-openapi", "components": spec["components"]},
            specification=DRAFT202012))
        return jsonschema.Draft202012Validator(
            {"$ref": "urn:mcp-registry-openapi#/components/schemas/ServerResponse"}, registry=reg)
    except Exception as e:
        print(f"subregistry: schema unavailable, structural checks only ({type(e).__name__})", flush=True)
        return None


def _previous() -> tuple[int, list[dict]]:
    """From the snapshot being replaced: how many live records it held, and its rows
    that did not come from the official registry (independent servers and our
    tombstones), which is all phase 3 needs. The official registry's own rows are
    re-read from it every build, so they are never carried over."""
    if not DB.exists():
        return 0, []
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        cols = {r[1] for r in c.execute("PRAGMA table_info(servers)")}
        if "deleted" not in cols:              # a snapshot from before version history
            live = c.execute("SELECT count(*) FROM servers").fetchone()[0]
            rows = [dict(r) | {"deleted": 0, "latest": 1, "removed": None} for r in c.execute(
                "SELECT name, version, doc, verified, updated, source FROM servers WHERE source != 'official'")]
        else:
            live = c.execute("SELECT count(*) FROM servers WHERE deleted = 0").fetchone()[0]
            rows = [dict(r) for r in c.execute(
                "SELECT name, version, doc, verified, updated, source, deleted, latest, removed "
                "FROM servers WHERE source != 'official'")]
        c.close()
        return live, rows
    except sqlite3.Error:
        return 0, []


class _Index:
    """Our evidence about a server, read from the index on demand.

    The build used to load every MCP server row (150,000+) into memory up front; now
    it holds only an identifier -> key map (strings) and reads the rows it needs by
    primary key, a page at a time, each read its own short transaction, so the build
    neither holds the index's memory nor keeps a long read open against the WAL."""

    COLS = ("key, identifier, display_name, description, url, publisher, sources, live, live_checked, "
            "probe_n, probe_ok, mcp_status, mcp_tools, mcp_checked, updated_at")

    def __init__(self) -> None:
        self.c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.key_of = {i.lower(): k for k, i in self.c.execute(
            "SELECT key, coalesce(identifier, key) FROM entries WHERE type_family='mcp-server'")}
        self.claimed = {d.lower() for (d,) in self.c.execute("SELECT domain FROM claims WHERE verified=1")}

    def rows(self, idents: list[str]) -> dict[str, sqlite3.Row]:
        keys = {self.key_of[i]: i for i in idents if i in self.key_of}
        out: dict[str, sqlite3.Row] = {}
        ks = list(keys)
        for i in range(0, len(ks), 400):
            part = ks[i:i + 400]
            q = f"SELECT {self.COLS} FROM entries WHERE key IN ({','.join('?' * len(part))})"
            for r in self.c.execute(q, part):
                out[keys[r["key"]]] = r
        return out

    def independent(self) -> list[sqlite3.Row]:
        return [r for r in self.c.execute(
            f"SELECT {self.COLS} FROM entries WHERE type_family='mcp-server' AND mcp_status IN ('ok','ok:notools') "
            "AND live = 1 AND url LIKE 'https://%' AND coalesce(sources,'') NOT LIKE '%mcp-registry%'")]

    def close(self) -> None:
        self.c.close()


def _ident(name: str) -> str:
    """The identifier app/ingest.py (from_mcp_registry) gives an official server."""
    ns, _, leaf = name.partition("/")
    pub = ns if "." in ns else "registry.modelcontextprotocol.io"
    return f"urn:air:{pub}:mcp:{(leaf or ns).replace('/', '.')}".lower()


async def build(max_pages: int = 5000) -> dict:
    t0 = int(time.time())
    idx = _Index()
    try:
        pages = json.loads((config.DATA_DIR / "servers_index.json").read_text())["servers"]
        page_of = {v["key"]: v["slug"] for v in pages.values()}
    except Exception:
        page_of = {}
    base = config.PUBLIC_BASE
    schema = _schema()
    prev_live, prev = _previous()

    def meta_for(row, **extra) -> dict:
        m = {"identifier": row["identifier"]} if row is not None else {}
        m.update(extra)
        m.update(_check(row))
        if row is not None and row["key"] in page_of:
            m["page"] = f"{base}/mcp-servers/{page_of[row['key']]}"
        return m

    def claim_of(host: str) -> str | None:
        host = host.lower()
        parts = host.split(".")
        for i in range(len(parts) - 1):
            if ".".join(parts[i:]) in idx.claimed:
                return ".".join(parts[i:])
        return None

    tmp = DB.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(tmp)
    out.execute("PRAGMA journal_mode=OFF")
    out.execute("PRAGMA synchronous=OFF")
    out.execute("""CREATE TABLE servers (name TEXT NOT NULL, version TEXT NOT NULL, doc TEXT NOT NULL,
                   verified INTEGER NOT NULL, latest INTEGER NOT NULL, deleted INTEGER NOT NULL,
                   updated INTEGER, source TEXT, removed INTEGER, UNIQUE(name, version))""")
    out.execute("CREATE TABLE info (k TEXT PRIMARY KEY, v TEXT)")
    stats = {"official_seen": 0, "official_versions": 0, "official_listed": 0, "official_deleted": 0,
             "official_not_indexed": 0, "no_connection_info": 0, "invalid": 0,
             "independent_candidates": 0, "independent_listed": 0, "independent_no_version": 0,
             "independent_name_clash": 0, "independent_history": 0, "tombstones": 0,
             "verified": 0, "pages": 0, "complete": False}
    live_names: set[str] = set()
    official_names: set[str] = set()

    def put(doc: dict, *, verified: bool, latest: bool, deleted: bool, updated: int | None, source: str,
            removed: int | None = None, check: bool = True) -> bool:
        s = doc["server"]
        if check:
            if not s.get("packages") and not s.get("remotes"):
                stats["no_connection_info"] += 1      # nothing a host can connect to
                return False
            if not _valid(doc) or (schema is not None and next(schema.iter_errors(doc), None) is not None):
                stats["invalid"] += 1
                return False
        cur = out.execute("INSERT OR IGNORE INTO servers VALUES (?,?,?,?,?,?,?,?,?)",
                          (s["name"], s["version"], json.dumps(doc, separators=(",", ":"), ensure_ascii=False),
                           int(verified), int(latest), int(deleted), updated, source, removed))
        if cur.rowcount != 1:                         # (name, version) already stored
            return False
        if not deleted:
            live_names.add(s["name"].lower())
        stats["verified"] += int(verified and not deleted)
        return True

    def official_group(name: str, recs: list[dict], row) -> None:
        """Every version of one official server, written as soon as the walk has passed it."""
        stats["official_seen"] += 1
        official_names.add(name)
        if row is None:
            stats["official_not_indexed"] += 1
        # The latest version is the one the official registry marks; if none is
        # marked, the most recently updated one.
        offs = [((r.get("_meta") or {}).get(OFFICIAL_META) or {}) for r in recs]
        marked = [i for i, o in enumerate(offs) if o.get("isLatest")]
        latest_i = marked[-1] if marked else max(range(len(recs)), key=lambda i: offs[i].get("updatedAt") or "")
        listed_any = False
        for i, rec in enumerate(recs):
            stats["official_versions"] += 1
            sv = dict(rec.get("server") or {})
            off = offs[i]
            deleted = off.get("status") == "deleted"
            remotes = {x.get("url") for x in sv.get("remotes") or []}
            checked = row if row is not None and row["url"] in remotes else None   # our check is of THIS endpoint
            fixed = []
            repo = sv.get("repository")
            if isinstance(repo, dict) and not (repo.get("url") and repo.get("source")) and not \
                    any(v for k, v in repo.items() if k not in ("url", "source")):
                # `"repository": {}` says nothing; the schema requires url and source
                # when the object is present, so the empty object is removed and said so.
                sv.pop("repository")
                fixed.append("removed an empty repository object")
            rmeta = dict(rec.get("_meta") or {})
            ours_meta = meta_for(checked, source="official-registry") if checked is not None else \
                {**({"identifier": row["identifier"]} if row is not None else {}), "source": "official-registry",
                 **({"page": f"{base}/mcp-servers/{page_of[row['key']]}"}
                    if row is not None and row["key"] in page_of else {})}
            if fixed:
                ours_meta["normalized"] = fixed
            rmeta[META] = ours_meta
            updated = _rfc3339(off.get("updatedAt") or "") or (row["updated_at"] if row is not None else None)
            if put({"server": sv, "_meta": rmeta}, verified=_is_verified(checked) and not deleted,
                   latest=i == latest_i, deleted=deleted, updated=updated, source="official"):
                listed_any = True
                stats["official_deleted"] += int(deleted)
        stats["official_listed"] += int(listed_any)

    def flush(groups: list[tuple[str, list[dict]]]) -> None:
        rows = idx.rows([_ident(n) for n, _ in groups])
        for n, recs in groups:
            official_group(n, recs, rows.get(_ident(n)))
        out.commit()

    # 1. The official registry: every version, deleted ones too, verbatim. Results come
    # ordered by name then version, so a server's versions are contiguous and each
    # server is written as soon as the walk has moved past it: memory holds one page.
    headers = {"user-agent": config.USER_AGENT}
    cursor = None
    pending: tuple[str, list[dict]] | None = None
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for _ in range(max_pages):
            url = f"{OFFICIAL}/v0.1/servers?limit=100&include_deleted=true"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor, safe="")
            d = None
            for attempt in range(4):
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        d = r.json()
                        break
                except Exception:
                    pass
                await asyncio.sleep(2 * (attempt + 1))
            if d is None:
                break
            stats["pages"] += 1
            done: list[tuple[str, list[dict]]] = []
            for rec in d.get("servers") or []:
                n = (rec.get("server") or {}).get("name")
                if not n:
                    continue
                if pending is not None and pending[0] == n:
                    pending[1].append(rec)
                else:
                    if pending is not None:
                        done.append(pending)
                    pending = (n, [rec])
            if done:
                flush(done)
            cursor = (d.get("metadata") or {}).get("nextCursor")
            if not cursor:
                stats["complete"] = True
                break
        if pending is not None:
            flush([pending])

        # 2. Independently published remote servers that answered a handshake.
        cands = idx.independent()
        stats["independent_candidates"] = len(cands)
        sem = asyncio.Semaphore(8)

        async def ver(r):
            async with sem:
                return r, await _server_version(client, r["url"])

        found = await asyncio.gather(*(ver(r) for r in cands))
    idx.close()                                       # the index is not read again

    official_lower = {n.lower() for n in official_names}
    for r, v in sorted(found, key=lambda x: x[0]["key"]):
        if not v:
            stats["independent_no_version"] += 1
            continue
        host = urllib.parse.urlsplit(r["url"]).hostname or ""
        leaf = _leaf(r["identifier"] or r["key"])
        name = f"{_rev(host)}/{leaf}"
        taken = lambda n: n.lower() in official_lower or n.lower() in live_names   # noqa: E731
        if taken(name):
            mid = (r["identifier"] or "").split(":")
            name = f"{_rev(host)}/{mid[-2]}-{leaf}" if len(mid) >= 5 else name
        if taken(name) or not NAME_RE.match(name):
            stats["independent_name_clash"] += 1
            continue
        title = _short(r["display_name"] or "", 100)
        sv = {"name": name, "description": _short(r["description"] or r["display_name"] or ""),
              "version": v, "remotes": [{"type": "streamable-http", "url": r["url"]}]}
        if title and title.lower() != name.lower():
            sv["title"] = title
        extra = {"source": "independent", "nameAssignedBy": "neuronto"}
        c = claim_of(host)
        if c:
            extra["domainClaimed"] = c
        doc = {"server": sv, "_meta": {META: meta_for(r, **extra)}}
        if put(doc, verified=True, latest=True, deleted=False, updated=r["updated_at"] or t0, source="independent"):
            stats["independent_listed"] += 1
    out.commit()

    # 3. History and removals carried over from the snapshot being replaced.
    # Older versions of an independent server we observed stay listed as superseded;
    # a server we no longer list at all becomes a tombstone, so a client syncing with
    # updated_since learns it is gone, and is forgotten after TOMBSTONE_DAYS.
    cutoff = t0 - TOMBSTONE_DAYS * 86400
    for p in prev:
        if p["name"] in official_names:
            continue                                  # the official registry speaks for its own
        if p["name"].lower() in live_names:
            # Listed again: an old tombstone is moot; an older observed version is history.
            if p["source"] == "independent" and not p["deleted"]:
                if put(json.loads(p["doc"]), verified=bool(p["verified"]), latest=False, deleted=False,
                       updated=p["updated"], source="independent", check=False):
                    stats["independent_history"] += 1
            continue
        removed = p["removed"] or t0
        if removed < cutoff:
            continue
        doc = json.loads(p["doc"])
        m = (doc.get("_meta") or {}).get(META) or {}
        m = {**m, "status": "deleted", "removedAt": _iso(removed),
             "statusMessage": "No longer listed: its endpoint stopped answering an MCP handshake."}
        doc.setdefault("_meta", {})[META] = m
        if put(doc, verified=False, latest=bool(p["latest"]), deleted=True, updated=removed,
               source=p["source"] or "independent", removed=removed, check=False):
            stats["tombstones"] += 1
    out.commit()

    out.execute("CREATE INDEX servers_order ON servers(name, version)")
    out.execute("CREATE INDEX servers_latest ON servers(latest, deleted, name, version)")
    out.execute("CREATE INDEX servers_verified ON servers(verified, latest, deleted, name, version)")
    out.execute("CREATE INDEX servers_nocase ON servers(name COLLATE NOCASE)")
    out.execute("CREATE INDEX servers_updated ON servers(updated)")
    out.execute("CREATE VIRTUAL TABLE name_tri USING fts5(name, tokenize='trigram', content='servers', "
                "content_rowid='rowid')")
    out.execute("INSERT INTO name_tri(rowid, name) SELECT rowid, name FROM servers")

    live_now = out.execute("SELECT count(*) FROM servers WHERE deleted = 0").fetchone()[0]
    stats["records"] = out.execute("SELECT count(*) FROM servers").fetchone()[0]
    stats["live_records"] = live_now
    stats["servers"] = out.execute("SELECT count(*) FROM servers WHERE latest = 1 AND deleted = 0").fetchone()[0]
    stats["listed"] = stats["official_listed"] + stats["independent_listed"]
    stats["schema_checked"] = schema is not None
    stats["built"] = int(time.time())
    out.execute("INSERT INTO info VALUES ('stats', ?)", (json.dumps(stats),))
    out.execute("INSERT INTO info VALUES ('snapshot', ?)",
                (hashlib.sha1(f"{stats['built']}:{stats['records']}".encode()).hexdigest()[:12],))
    out.commit()
    out.execute("VACUUM")
    out.close()

    reason = None
    if not stats["complete"]:
        reason = "the official registry was not walked to its end"
    elif prev_live and live_now < MIN_KEEP_RATIO * prev_live:
        reason = f"it lists {live_now} live records against {prev_live} before"
    if reason and DB.exists():
        tmp.unlink()
        stats["kept_previous"] = reason
        return stats
    os.replace(tmp, DB)
    return stats


def info() -> dict:
    got = _SNAP.get()
    if got is None:
        return {}
    c, _ = got
    row = c.execute("SELECT v FROM info WHERE k='stats'").fetchone()
    return json.loads(row["v"]) if row else {}


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        t = time.time()
        res = asyncio.run(build())
        res["seconds"] = round(time.time() - t, 1)
        print(json.dumps(res), flush=True)
        # A refused swap is a failed stage: the unit fails and its OnFailure alert fires.
        sys.exit(1 if res.get("kept_previous") else 0)
    else:
        print(json.dumps(info(), indent=2))
