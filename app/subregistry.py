"""The MCP Registry v0.1 read API: this index as an MCP subregistry.

MCP hosts that take a custom registry URL (GitHub Copilot's MCP registry policy,
VS Code's `chat.mcp.gallery.serviceUrl`, Visual Studio, JetBrains, Eclipse, Xcode,
Copilot CLI) append `/v0.1/servers` to a base URL and read the shape defined in
the registry's OpenAPI (modelcontextprotocol/registry, docs/reference/api). Point
one at this deployment's base URL and its server gallery is this index.

Two galleries, one code path:

  {base}/v0.1/servers            every server we hold with real connection info:
                                 the official registry's latest records, verbatim,
                                 plus independently published remote servers whose
                                 endpoint answered an MCP handshake
  {base}/verified/v0.1/servers   only the servers whose remote endpoint answered an
                                 MCP handshake (or a 401/403 sign-in or x402 402 answer)
                                 and was reachable at the last liveness check

Nothing is invented. An official record is passed through as the official registry
serves it; our own findings go beside it under `_meta["com.neuronto/registry"]`. An
independent server is listed only when its endpoint told us its version in the
initialize handshake (`serverInfo.version`, which the OpenAPI says `version` is the
equivalent of); a server that never told us one is not listed, rather than listed
with a version we made up.

The build runs outside the serving process (`python -m app.subregistry build`) and
writes a separate SQLite file, swapped in atomically, so a request is one indexed
range scan and a rebuild never holds the index's write lock or a worker's memory.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import config

DB = config.DATA_DIR / "subregistry.db"
OFFICIAL = "https://registry.modelcontextprotocol.io"
SPEC = "https://raw.githubusercontent.com/modelcontextprotocol/registry/main/docs/reference/api/openapi.yaml"
META = "com.neuronto/registry"
OFFICIAL_META = "io.modelcontextprotocol.registry/official"
NAME_RE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
REMOTE_URL_RE = re.compile(r"^(https?://[^\s]+|\{[a-zA-Z_][a-zA-Z0-9_]*\}[^\s]*)$")
RANGE_RE = re.compile(r"^[\^~><=]|\.x$|\.\*$|^latest$")
DEFAULT_LIMIT, MAX_LIMIT = 30, 100
VERIFIED_STATUSES = ("ok", "ok:notools", "auth", "payment")
CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type"}
CACHE = {"Cache-Control": "public, max-age=300"}

router = APIRouter()


# ----------------------------------------------------------------------------- serving

def _conn() -> sqlite3.Connection | None:
    if not DB.exists():
        return None
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _error(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status, headers=CORS)


def _rfc3339(v: str) -> int | None:
    try:
        return int(datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _body(docs: list[str], metadata: dict) -> Response:
    """Splice the stored JSON rather than parse and re-serialise a hundred records."""
    body = '{"servers":[' + ",".join(docs) + '],"metadata":' + json.dumps(metadata) + "}"
    return Response(body, media_type="application/json", headers={**CORS, **CACHE})


def _list(request: Request, verified: bool) -> Response:
    q = request.query_params
    raw = q.get("limit")
    try:
        limit = DEFAULT_LIMIT if raw in (None, "") else int(raw)
    except ValueError:
        return _error(400, "limit must be an integer")
    if limit < 1:
        return _error(400, "limit must be at least 1")
    limit = min(limit, MAX_LIMIT)
    where, args = [], []
    cursor = q.get("cursor") or ""
    if cursor:
        # Our cursor is "name:version", the official registry's own format; a name
        # never contains a colon, so everything before the first one is the name.
        where.append("name > ?")
        args.append(cursor.split(":", 1)[0])
    if verified:
        where.append("verified = 1")
    search = (q.get("search") or "").strip().lower()
    if search:
        esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("search LIKE ? ESCAPE '\\'")
        args.append(f"%{esc}%")
    if q.get("updated_since"):
        ts = _rfc3339(q["updated_since"])
        if ts is None:
            return _error(400, "updated_since must be an RFC 3339 date-time")
        where.append("updated >= ?")
        args.append(ts)
    version = (q.get("version") or "").strip()
    if version and version != "latest":
        # Every record here is the latest version of its server.
        where.append("version = ?")
        args.append(version)
    sql = "SELECT name, version, doc FROM servers"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name LIMIT ?"
    c = _conn()
    if c is None:
        return _body([], {"count": 0})
    try:
        rows = c.execute(sql, args + [limit + 1]).fetchall()
    finally:
        c.close()
    more = len(rows) > limit
    rows = rows[:limit]
    meta: dict = {"count": len(rows)}
    if more:
        meta["nextCursor"] = f"{rows[-1]['name']}:{rows[-1]['version']}"
    return _body([r["doc"] for r in rows], meta)


def _split(rest: str) -> tuple[str, str | None] | None:
    """`{name}/versions` or `{name}/versions/{version}`, name already URL-decoded.

    The server decodes `%2F` before routing, so `io.github.acme%2Fweather` arrives
    as two segments; a name has exactly one slash, which is what makes this exact."""
    parts = urllib.parse.unquote(rest).split("/")
    if len(parts) < 3 or parts[2] != "versions":
        return None
    name = f"{parts[0]}/{parts[1]}"
    if len(parts) == 3:
        return name, None
    return name, "/".join(parts[3:]) or None


def _one(rest: str, verified: bool) -> Response:
    sp = _split(rest)
    if sp is None:
        return _error(404, "Not found")
    name, version = sp
    c = _conn()
    if c is None:
        return _error(404, "Server not found")
    try:
        row = c.execute("SELECT name, version, doc, verified FROM servers WHERE name = ?", (name,)).fetchone() \
            or c.execute("SELECT name, version, doc, verified FROM servers WHERE name = ? COLLATE NOCASE",
                         (name,)).fetchone()
    finally:
        c.close()
    if row is None or (verified and not row["verified"]):
        return _error(404, "Server not found")
    if version is None:
        return _body([row["doc"]], {"count": 1})
    if version != "latest" and version != row["version"]:
        return _error(404, "Server version not found")
    return Response(row["doc"], media_type="application/json", headers={**CORS, **CACHE})


def _options() -> Response:
    return Response(status_code=204, headers=CORS)


def _read_only() -> JSONResponse:
    return JSONResponse({"error": "This registry is read-only. Publish to the official MCP Registry; "
                                  "servers published there are picked up here on the next build."},
                        status_code=405, headers={**CORS, "Allow": "OPTIONS"})


def _register() -> None:
    for prefix, verified in (("", False), ("/verified", True)):
        def lst(request: Request, _v=verified):
            return _list(request, _v)

        def one(rest: str, _v=verified):
            return _one(rest, _v)

        router.add_api_route(f"{prefix}/v0.1/servers", lst, methods=["GET"], include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers", _options, methods=["OPTIONS"], include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers/{{rest:path}}", one, methods=["GET"],
                             include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/servers/{{rest:path}}", _options, methods=["OPTIONS"],
                             include_in_schema=False)
        router.add_api_route(f"{prefix}/v0.1/publish", _read_only, methods=["POST", "GET", "PUT"],
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
    if not s.get("packages") and not s.get("remotes"):
        return False                       # clients drop a record they cannot connect to
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


async def build(max_pages: int = 2000) -> dict:
    src = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=30)
    src.row_factory = sqlite3.Row
    cols = ("key, identifier, display_name, description, url, publisher, sources, live, live_checked, "
            "probe_n, probe_ok, mcp_status, mcp_tools, mcp_checked, updated_at, first_seen")
    ours: dict[str, sqlite3.Row] = {}
    for r in src.execute(f"SELECT {cols} FROM entries WHERE type_family='mcp-server'"):
        ours[(r["identifier"] or r["key"]).lower()] = r
    claimed = {d.lower() for (d,) in src.execute("SELECT domain FROM claims WHERE verified=1")}
    src.close()
    try:
        pages = json.loads((config.DATA_DIR / "servers_index.json").read_text())["servers"]
        page_of = {v["key"]: v["slug"] for v in pages.values()}
    except Exception:
        page_of = {}
    base = config.PUBLIC_BASE
    schema = _schema()

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
            if ".".join(parts[i:]) in claimed:
                return ".".join(parts[i:])
        return None

    tmp = DB.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(tmp)
    out.execute("""CREATE TABLE servers (name TEXT PRIMARY KEY, version TEXT NOT NULL, doc TEXT NOT NULL,
                   verified INTEGER NOT NULL, updated INTEGER, search TEXT, source TEXT)""")
    out.execute("CREATE INDEX servers_nocase ON servers(name COLLATE NOCASE)")
    out.execute("CREATE INDEX servers_verified ON servers(verified, name)")
    out.execute("CREATE TABLE info (k TEXT PRIMARY KEY, v TEXT)")
    stats = {"official_seen": 0, "official_listed": 0, "official_not_indexed": 0, "no_connection_info": 0, "invalid": 0,
             "independent_candidates": 0, "independent_listed": 0, "independent_no_version": 0,
             "independent_name_clash": 0, "verified": 0, "pages": 0, "complete": False}
    names: set[str] = set()

    def put(doc: dict, verified: bool, updated: int | None, source: str) -> bool:
        if not doc["server"].get("packages") and not doc["server"].get("remotes"):
            stats["no_connection_info"] += 1      # clients drop these; so do we
            return False
        if not _valid(doc) or (schema is not None and next(schema.iter_errors(doc), None) is not None):
            stats["invalid"] += 1
            return False
        s = doc["server"]
        if s["name"].lower() in names:
            return False
        names.add(s["name"].lower())
        text = " ".join(x for x in (s["name"], s.get("title") or "", s["description"]) if x).lower()
        out.execute("INSERT INTO servers VALUES (?,?,?,?,?,?,?)",
                    (s["name"], s["version"], json.dumps(doc, separators=(",", ":"), ensure_ascii=False),
                     int(verified), updated, text, source))
        stats["verified"] += int(verified)
        return True

    # 1. The official registry, latest version of each server, verbatim.
    headers = {"user-agent": config.USER_AGENT}
    cursor = None
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for _ in range(max_pages):
            url = f"{OFFICIAL}/v0.1/servers?limit=100&version=latest"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor, safe="")
            d = None
            for attempt in range(3):
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
            for rec in d.get("servers") or []:
                sv = rec.get("server") or {}
                name = sv.get("name") or ""
                off = (rec.get("_meta") or {}).get(OFFICIAL_META) or {}
                if not name or off.get("status") == "deleted":
                    continue
                stats["official_seen"] += 1
                # The identifier ingest gave this server (app/ingest.py, from_mcp_registry).
                ns, _, leaf = name.partition("/")
                pub = ns if "." in ns else "registry.modelcontextprotocol.io"
                ident = f"urn:air:{pub}:mcp:{(leaf or ns).replace('/', '.')}".lower()
                row = ours.get(ident)
                if row is None:
                    stats["official_not_indexed"] += 1
                    continue
                remotes = {x.get("url") for x in sv.get("remotes") or []}
                checked = row if row["url"] in remotes else None     # our observation is of THIS server
                repo = sv.get("repository")
                fixed = []
                if isinstance(repo, dict) and not (repo.get("url") and repo.get("source")) and not \
                        any(v for k, v in repo.items() if k not in ("url", "source")):
                    # `"repository": {}` says nothing; the schema requires url and source
                    # when the object is present, so the empty object is removed and said so.
                    sv = {k: v for k, v in sv.items() if k != "repository"}
                    fixed.append("removed an empty repository object")
                rmeta = dict(rec.get("_meta") or {})
                rmeta[META] = meta_for(row, source="official-registry") if checked else \
                    {"identifier": row["identifier"], "source": "official-registry",
                     **({"page": f"{base}/mcp-servers/{page_of[row['key']]}"} if row["key"] in page_of else {})}
                updated = _rfc3339(off.get("updatedAt") or "") or row["updated_at"]
                if fixed:
                    rmeta[META]["normalized"] = fixed
                if put({"server": sv, "_meta": rmeta}, _is_verified(checked), updated, "official"):
                    stats["official_listed"] += 1
            out.commit()
            cursor = (d.get("metadata") or {}).get("nextCursor")
            if not cursor:
                stats["complete"] = True
                break

        # 2. Independently published remote servers that answered a handshake.
        cands = [r for r in ours.values()
                 if "mcp-registry" not in (r["sources"] or "") and r["mcp_status"] in ("ok", "ok:notools")
                 and r["live"] == 1 and (r["url"] or "").startswith("https://")]
        stats["independent_candidates"] = len(cands)
        sem = asyncio.Semaphore(8)

        async def ver(r):
            async with sem:
                return r, await _server_version(client, r["url"])

        found = await asyncio.gather(*(ver(r) for r in cands))

    for r, v in sorted(found, key=lambda x: x[0]["key"]):
        if not v:
            stats["independent_no_version"] += 1
            continue
        host = urllib.parse.urlsplit(r["url"]).hostname or ""
        leaf = _leaf(r["identifier"] or r["key"])
        name = f"{_rev(host)}/{leaf}"
        if name.lower() in names:
            mid = (r["identifier"] or "").split(":")
            name = f"{_rev(host)}/{mid[-2]}-{leaf}" if len(mid) >= 5 else name
        if name.lower() in names or not NAME_RE.match(name):
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
        if put(doc, True, r["updated_at"], "independent"):
            stats["independent_listed"] += 1

    stats["listed"] = stats["official_listed"] + stats["independent_listed"]
    stats["schema_checked"] = schema is not None
    stats["built"] = int(time.time())
    out.execute("INSERT INTO info VALUES ('stats', ?)", (json.dumps(stats),))
    out.commit()
    out.close()
    if not stats["complete"] and DB.exists():
        # A half-walked registry would silently drop every server after the break.
        tmp.unlink()
        stats["kept_previous"] = True
        return stats
    os.replace(tmp, DB)
    return stats


def info() -> dict:
    c = _conn()
    if c is None:
        return {}
    try:
        row = c.execute("SELECT v FROM info WHERE k='stats'").fetchone()
        return json.loads(row["v"]) if row else {}
    finally:
        c.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        t = time.time()
        res = asyncio.run(build())
        res["seconds"] = round(time.time() - t, 1)
        print(json.dumps(res), flush=True)
    else:
        print(json.dumps(info(), indent=2))
