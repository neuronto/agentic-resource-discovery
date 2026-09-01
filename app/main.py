"""Neuronto - an ARD registry, publisher and index.

Implements the Registry REST API of the Agentic Resource Discovery
specification v0.91, including the parts nobody else does:

  * `federation: auto` (§5.4), the spec's default mode. Measured on 2026-08-31:
    all four existing registries returned identical results for `auto` and
    `none`. Fusing the four upstreams yields +68% coverage over the largest
    single index.
  * `GET /agents` as a properly paginated object. GitHub's Agent Finder returns
    200 with a body that is not one, which fails the official conformance tool;
    the other three return 404.
  * Type normalisation, so a filter finds MCP servers under all three media
    types in circulation instead of silently missing two of them.
  * Liveness, so the index does not serve entries that point at nothing.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import urllib.parse
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)

from . import (adoption, audit, badge, bench, catalog, config, embed, events,
               federation, ingest, liveness, limits, publisher, render, search,
               store, tools_index)
from .normalize import media_family

app = FastAPI(title="Neuronto ARD Registry: Agentic Resource Discovery (ARD) Index", version="1.0.0",
              docs_url="/api-docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"],
                   expose_headers=["X-Response-Time-Ms", "X-RateLimit-Limit",
                                   "X-RateLimit-Remaining", "X-RateLimit-Reset",
                                   "Retry-After"])


# Which routes spend something on a caller's behalf. Everything absent from this
# map is a local read and is not limited, which is most of the API.
#
# Done as middleware rather than per route so that a route added later is not
# silently unlimited because somebody forgot a decorator. Two rules cannot be
# decided from the path alone, `/search` (only the federated mode goes outbound)
# and `/mcp` (only one of its four tools writes), so those are applied at their
# call sites where the body is already parsed.
_LIMITED: dict[tuple[str, str], str] = {
    ("POST", "/audit"):           "audit",
    ("POST", "/manifest/build"):  "manifest_build",
    ("POST", "/submit"):          "submit",
    ("POST", "/claim"):           "claim",
    ("POST", "/claim/verify"):    "claim_verify",
    ("POST", "/private/entries"): "private_write",
    ("DELETE", "/private/entries"): "private_write",
}


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    rule = _LIMITED.get((request.method, request.url.path.rstrip("/") or "/"))
    if not rule:
        return await call_next(request)
    ok, retry, headers = limits.check(rule, request)
    if not ok:
        _, verified = limits.caller(request)
        events.emit("rate_limited", a=rule)
        return JSONResponse(status_code=429,
                            content=limits.too_many(rule, retry, headers, verified),
                            headers={**headers, "retry-after": str(retry)})
    resp = await call_next(request)
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


events.install(app)

_conn: sqlite3.Connection | None = None
WEB = Path(__file__).resolve().parent.parent / "web"


_dbtls = threading.local()
_db_init_lock = threading.Lock()
_db_ready = False


def db() -> sqlite3.Connection:
    """A connection for this thread.

    One shared connection was the original design, and it is the wrong one as
    soon as anything runs in a threadpool: SQLite serialises every statement on
    a connection behind its own mutex, so readers queue behind each other even
    though WAL was built so they would not have to. A connection per thread is
    cheap, and read-only handlers then genuinely run in parallel.

    Schema setup still happens exactly once, under a lock, on the first
    connection any thread opens.
    """
    global _db_ready
    c = getattr(_dbtls, "conn", None)
    if c is not None:
        return c
    c = store.connect()
    with _db_init_lock:
        if not _db_ready:
            store.init(c)
            _db_ready = True
    _dbtls.conn = c
    return c


# The expensive pages, with the TTL each is served at. Warmed on startup and
# refreshed on a timer, so the first visitor after a deploy is never the one who
# pays for a 22 second build.
def _warmable():
    return [
        ("tools-index", 1800, lambda: catalog.render_index(db())),
        ("pubs-index",  1800, lambda: catalog.render_publishers_index(db())),
        ("published",   3600, lambda: catalog.render_published(db())),
        ("adoption-html", 3600, lambda: catalog.render_adoption(adoption.report(db()))),
    ] + [(f"cat-{slug}", 1800, (lambda sl: lambda: catalog.render_category(db(), sl))(slug))
         for slug in sorted(catalog.published(db()))]


def _warm_all() -> None:
    """Build anything missing or stale. Runs in a thread, never on a request.

    One worker does this for all of them. Between renders it yields briefly:
    warming is background work and must never be the reason a real request
    waits, which on a two core box it otherwise is.
    """
    if not render.claim_warm(f"pid{os.getpid()}"):
        return
    built = 0
    try:
        for key, ttl, build in _warmable():
            try:
                if render.warm(key, ttl, build):
                    built += 1
                    time.sleep(0.25)
            except Exception:
                continue
    finally:
        render.release_warm()
    if built:
        print(f"page cache: warmed {built} page(s)", flush=True)


@app.on_event("startup")
async def _startup() -> None:
    db()
    # In a thread: warming touches every tool row and would otherwise block the
    # event loop, and therefore every request, for the whole of startup.
    threading.Thread(target=_warm_all, name="page-warm", daemon=True).start()

    async def _refresher():
        # Rebuilds ahead of expiry so the stale-while-revalidate path is a
        # fallback rather than the normal case.
        while True:
            await asyncio.sleep(600)
            await asyncio.to_thread(_warm_all)

    asyncio.ensure_future(_refresher())


@app.middleware("http")
async def _timing(request: Request, call_next):
    """Latency is the product claim, so every response carries its own measurement."""
    t0 = time.perf_counter()
    resp = await call_next(request)
    resp.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    return resp


# ─────────────────────────── Registry REST API (§5.3) ───────────────────────

@app.post("/search")
async def search_endpoint(body: dict, request: Request) -> JSONResponse:
    """POST /search - the one endpoint the spec mandates.

    Every result carries `identifier`, `score` (0-100) and `source`, which are
    the three fields the conformance tool requires of a SearchResultItem.
    """
    request_key = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    q = body.get("query") or {}
    text = (q.get("text") or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "query.text is required for Search (spec 5.3.2)."})
    # The spec nests filter inside query (5.3.2). A caller who puts it at the top
    # level previously got unfiltered results and no indication anything was
    # wrong, which is worse than either honouring it or rejecting it. We honour
    # it, the same courtesy already extended to /explore's facet shorthand.
    flt = q.get("filter") if isinstance(q.get("filter"), dict) else None
    if flt is None and isinstance(body.get("filter"), dict):
        flt = body["filter"]
    page_size = body.get("pageSize") or config.PAGE_SIZE_DEFAULT
    try:
        page_size = max(1, min(int(page_size), config.PAGE_SIZE_MAX))
    except (TypeError, ValueError):
        page_size = config.PAGE_SIZE_DEFAULT
    mode = str(body.get("federation") or "auto")
    # Only the federated modes go outbound, so only they are limited. Local
    # search is the product and stays unmetered.
    if mode in ("auto", "referrals"):
        ok, retry, hdrs = limits.check("search_fed", request)
        if not ok:
            _, verified = limits.caller(request)
            events.emit("rate_limited", a="search_fed")
            body_out = limits.too_many("search_fed", retry, hdrs, verified)
            body_out["detail"] += (" Set \"federation\": \"none\" to search this index "
                                   "only, which is not limited.")
            return JSONResponse(status_code=429, content=body_out,
                                headers={**hdrs, "retry-after": str(retry)})

    t0 = time.perf_counter()
    conn = db()
    # A bearer key admits exactly one domain's private entries into the ranking.
    # Resolved before the search rather than filtered after, so private rows
    # never enter a result set the caller is not entitled to.
    auth = (request_key or "")
    owner = publisher.domain_for_key(conn, auth)
    out = await search.search(conn, text, flt, page_size, mode, owner_domain=owner)
    took = int((time.perf_counter() - t0) * 1000)
    fed_ok = sum(1 for f in (out.get("_federated") or []) if f.get("ok"))
    store.log_search(conn, text, mode, len(out["results"]), took, fed_ok,
                     entries=out["results"], authenticated=bool(owner))
    cleaned = search.clean(out["results"])
    payload: dict[str, Any] = {"results": cleaned,
                               "queryMatch": search.query_match(text, cleaned)}
    events.emit("search", a=mode, b=payload["queryMatch"]["confidence"],
                n=len(cleaned), ok=bool(cleaned))
    if out.get("referrals"):
        payload["referrals"] = out["referrals"]
    fed = out.get("_federated") or []
    if fed:
        # Say plainly which upstreams answered. A federating registry that hides
        # a failing upstream is quietly serving a smaller index than it claims.
        payload["federation"] = {
            "mode": mode,
            "registries": [{"name": f["name"], "source": f["source"],
                            "ok": f["ok"], "ms": f["ms"],
                            "results": len(f["results"]),
                            **({"error": f["error"]} if f.get("error") else {})}
                           for f in fed],
        }
    return JSONResponse(payload)


@app.post("/explore")
def explore_endpoint(body: dict) -> JSONResponse:
    """POST /explore - optional introspection over facets (§5.3.3).

    Explore does not federate; it is scoped to this registry's own index.
    """
    rt = body.get("resultType") or {}
    facets = rt.get("facets")
    if not facets:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": 'resultType.facets is required, e.g. {"resultType":{"facets":[{"field":"type"}]}}'})
    q = (body.get("query") or {}).get("text") or ""
    conn = db()
    keys = None
    if q.strip():
        keys = {e["_key"] for e in search.local_search(conn, q, None, 5000)}

    COLUMN = {"type": "type_raw", "type_family": "type_family",
              "publisher": "publisher", "host": "publisher", "source": "sources",
              "tags": "tags", "capabilities": "capabilities"}
    out: dict[str, Any] = {}
    if not isinstance(facets, list):
        facets = [facets]
    for f in facets:
        # Accept both the object form, {"field":"type","limit":20}, and the bare
        # string shorthand, "type". The shorthand is the obvious thing to send
        # and it used to raise, which returned 500 to a caller who had simply
        # guessed a reasonable shape. A registry that crashes on plausible
        # input breaks federation for everyone downstream of it.
        if isinstance(f, str):
            field, limit = f, 50
        elif isinstance(f, dict):
            field, limit = f.get("field"), int(f.get("limit") or 50)
        else:
            continue
        col = COLUMN.get(field)
        if not col:
            continue
        counts: dict[str, int] = {}
        for r in conn.execute(f"SELECT key, {col} AS v FROM entries"):
            if keys is not None and r["key"] not in keys:
                continue
            v = r["v"]
            if v is None:
                continue
            vals = json.loads(v) if col in ("tags", "capabilities", "sources") else [v]
            for x in vals:
                if x:
                    counts[str(x)] = counts.get(str(x), 0) + 1
        out[field] = {"buckets": [{"value": k, "count": n} for k, n in
                                  sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]}
    return JSONResponse({"resultType": "facets", "facets": out})


@app.get("/agents")
def agents_endpoint(
    filter: str | None = Query(None),
    orderBy: str | None = Query(None),
    pageSize: int = Query(20, ge=1, le=100),
    pageToken: str | None = Query(None),
) -> JSONResponse:
    """GET /agents - deterministic, paginated browsing (§5.3.4).

    Optional in the spec, but the conformance tool checks the shape when it is
    answered: GitHub's Agent Finder returns 200 with a body that has no `items`
    array and fails on exactly this. Ours returns the paginated object.
    """
    conn = db()
    offset = 0
    if pageToken:
        try: offset = max(0, int(pageToken))
        except ValueError: offset = 0

    where, params = "WHERE 1=1", []
    if filter:
        # A small, safe subset of the EBNF filter grammar: field=value pairs.
        for part in filter.split(","):
            if "=" not in part:
                continue
            k, v = (x.strip() for x in part.split("=", 1))
            if k in ("type", "type_family"):
                where += " AND type_family=?"; params.append(media_family(v))
            elif k == "publisher":
                where += " AND publisher=?"; params.append(v.lower())
            elif k == "live":
                where += " AND live=?"; params.append(1 if v in ("1", "true", "yes") else 0)

    ORDER = {"name": "display_name ASC", "name DESC": "display_name DESC",
             "created_at": "first_seen ASC", "created_at DESC": "first_seen DESC",
             "updated_at": "updated_at ASC", "updated_at DESC": "updated_at DESC"}
    order = ORDER.get((orderBy or "").strip(), "display_name ASC")

    total = conn.execute(f"SELECT COUNT(*) FROM entries {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM entries {where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, pageSize, offset)).fetchall()
    items = [store.row_to_entry(r) for r in rows]
    nxt = offset + pageSize
    body: dict[str, Any] = {"items": items, "totalSize": total}
    if nxt < total:
        body["nextPageToken"] = str(nxt)
    return JSONResponse(body)


# ─────────────────────────── Publisher side (§5.1) ──────────────────────────

def _manifest() -> dict:
    B = config.PUBLIC_BASE
    return {
        "specVersion": "1.0",
        "host": {"displayName": "Neuronto ARD Registry: Agentic Resource Discovery (ARD) index",
                 "identifier": "did:web:neuronto.com",
                 "documentationUrl": f"{B}/about"},
        "entries": [{
            # §5.3: a registry's base URL is discovered by finding an entry of
            # this type. This is how Neuronto becomes findable AS a registry.
            "identifier": "urn:air:neuronto.com:registry:neuronto",
            "displayName": "Neuronto ARD Registry: Agentic Resource Discovery (ARD) registry",
            "type": "application/ai-registry+json",
            "url": f"{B}/search",
            "description": ("A federated ARD registry and index. Implements "
                            "federation: auto with reciprocal rank fusion across every other "
                            "public ARD registry, normalises the competing MCP media types and "
                            "URN prefixes so filters do not silently miss, and probes endpoints "
                            "so dead resources are demoted rather than served."),
            "representativeQueries": [
                "find an MCP server, skill or agent for a task",
                "search every ARD registry at once",
                "which tools can an agent call for this",
            ],
            "tags": ["registry", "discovery", "ard", "federation", "index", "mcp"],
            "trustManifest": {"identity": "did:web:neuronto.com"},
        }, {
            "identifier": "urn:air:neuronto.com:tool:ard-publish",
            "displayName": "ard-publish - build and verify an ARD manifest",
            "type": "application/ai-catalog+json",
            "url": f"{B}/publish",
            "description": ("Open source tool that builds, validates and verifies an Agentic "
                            "Resource Discovery manifest, then checks which registries actually "
                            "return your domain. pip install ard-publish."),
            "representativeQueries": [
                "how do I publish an ARD manifest",
                "make my API discoverable by AI agents",
                "validate my ai-catalog.json",
            ],
            "tags": ["ard", "sdk", "publishing", "validation", "python", "open-source"],
            "trustManifest": {"identity": "did:web:neuronto.com"},
        }, {
            "identifier": "urn:air:neuronto.com:mcp:discovery",
            "displayName": "Neuronto ARD Registry: ARD and MCP discovery (MCP server)",
            "type": "application/mcp-server-card+json",
            "url": f"{B}/.well-known/mcp/server-card.json",
            "description": ("The same federated discovery as an MCP server, so an agent can "
                            "search every ARD registry from the tool interface it already speaks."),
            "capabilities": ["find_resource", "registry_stats"],
            "representativeQueries": [
                "find me an MCP server for this task",
                "what skills exist for this job",
                "search the agent-readable web",
            ],
            "tags": ["mcp", "discovery", "search", "ard"],
            "trustManifest": {"identity": "did:web:neuronto.com"},
        }],
    }


CACHE = {"Cache-Control": "public, max-age=900"}

@app.get("/.well-known/ard.json", include_in_schema=False)
def ard_json(): return JSONResponse(_manifest(), headers=CACHE)

@app.get("/.well-known/ai-catalog.json", include_in_schema=False)
def ai_catalog(): return JSONResponse(_manifest(), headers=CACHE)

@app.get("/.well-known/did.json", include_in_schema=False)
def did_json():
    B = config.PUBLIC_BASE
    return JSONResponse({
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": "did:web:neuronto.com",
        "service": [
            {"id": "did:web:neuronto.com#ard", "type": "ArdCatalog",
             "serviceEndpoint": f"{B}/.well-known/ard.json"},
            {"id": "did:web:neuronto.com#registry", "type": "ArdRegistry",
             "serviceEndpoint": f"{B}/search"},
            {"id": "did:web:neuronto.com#mcp", "type": "MCPServer",
             "serviceEndpoint": f"{B}/mcp"},
        ]}, headers=CACHE)

@app.get("/.well-known/mcp/server-card.json", include_in_schema=False)
def mcp_card():
    from .mcp import server_card
    return JSONResponse(server_card(), headers=CACHE)

@app.get("/robots.txt", include_in_schema=False)
def robots():
    B = config.PUBLIC_BASE
    # Social crawlers named explicitly. They fetch a URL to build the preview
    # card when a link is shared, and several of them do not follow the wildcard
    # rule reliably. A link shared without a card is a link nobody clicks, and
    # sharing is the channel that demonstrably works in this ecosystem.
    social = ["Twitterbot", "facebookexternalhit", "Redditbot", "LinkedInBot",
              "Slackbot", "Slackbot-LinkExpanding", "Discordbot", "TelegramBot",
              "WhatsApp", "Applebot"]
    lines = ["User-agent: *", "Allow: /", ""]
    for ua in social:
        lines += [f"User-agent: {ua}", "Allow: /", ""]
    lines += [f"Sitemap: {B}/sitemap.xml",
              f"Agentmap: {B}/.well-known/ard.json", ""]
    return PlainTextResponse("\n".join(lines),
                             headers={"Cache-Control": "public, max-age=300"})

@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    B = config.PUBLIC_BASE
    urls = ["/", "/what-is-ard", "/publish", "/submit-mcp-server",
            "/ard-registries", "/ard-manifest-generator", "/ard-conformance",
            "/badge", "/console", "/blog",
            # The capability pages and the two measurement pages. These carry
            # the verified tool surface, which exists on no other site, so they
            # are the pages most worth discovering.
            "/tools/", "/bench", "/adoption", "/submit", "/published"]
    urls += [f"/tools/{slug}" for slug in catalog.published(db())]
    urls += ["/ard-publishers"] + [f"/ard-publishers/{p['publisher']}"
                                   for p in catalog.publisher_list(db())]
    blog = WEB / "blog"
    if blog.exists():
        urls += sorted(f"/blog/{p.stem}" for p in blog.glob("*.html")
                       if p.stem != "index")
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(f"  <url><loc>{B}{u}</loc></url>" for u in urls)
            + "\n</urlset>\n")
    return Response(body, media_type="application/xml")

@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    B = config.PUBLIC_BASE
    c = store.counts(db())
    return PlainTextResponse(f"""# Neuronto

> Neuronto is a federated Agentic Resource Discovery (ARD) registry and index.
> One search finds the MCP servers, skills, agents and APIs that can do a task,
> across this index and every other public ARD registry at once.

## What is ARD?

ARD stands for Agentic Resource Discovery: an open specification for how AI
agents find the tools, skills, agents and APIs they need, published in June 2026
by a working group including Google, Microsoft, Hugging Face, AWS, Cisco,
GitHub, Nvidia, Salesforce and Snowflake.

An agentic resource is anything an AI client can call to get work done. ARD
answers "what is available for this task?" and then gets out of the way. It is a
discovery layer, not a runtime, and does not replace MCP or A2A.

## What is an ARD registry?

A registry indexes the resource descriptions publishers serve on their own
domains and answers search queries over them. Publishers describe what they
offer at /.well-known/ard.json. Neuronto is an ARD registry and a publisher.

## Index

{c['entries']} resources from {c['publishers']} publishers, {c['live']} verified
to respond. Refreshed continuously.

## API (ARD v0.91, no key, no signup)

- POST {B}/search    {{"query":{{"text":"..."}},"federation":"auto"}}
- POST {B}/explore   facet counts over the index
- GET  {B}/agents    deterministic paginated listing
- POST {B}/mcp       the same search as an MCP tool

## What is different here

- federation: auto is implemented. The ARD specification makes it the default
  mode: query peer registries, merge, return one set. Neuronto fans out to every
  public ARD registry concurrently and fuses with reciprocal rank fusion,
  reporting which upstreams answered.
- Passes the specification's conformance tool as both registry and publisher
  with zero errors and zero warnings, including GET /agents as a paginated
  object.
- Media types and discovery URN prefixes are normalised on ingest, so a filter
  for MCP servers returns them under all three spellings in circulation.
- Indexed endpoints are probed; ones that do not answer are demoted.
- Relevance scores are semantic only, never a trust or safety rating.

## How to be discovered

Serve an ARD manifest at /.well-known/ard.json on your domain, with
representativeQueries on every entry. The crawler finds it. No submission form,
no allowlist.

## Discovery documents

- {B}/.well-known/ard.json
- {B}/.well-known/did.json
- {B}/robots.txt (carries the Agentmap: directive)
""", headers={"Cache-Control": "public, max-age=600"})


# ─────────────────────────── Operations ─────────────────────────────────────

@app.get("/health")
def health():
    c = store.counts(db())
    t = store.tool_counts(db())
    return {"status": "ok", "entries": c["entries"], "publishers": c["publishers"],
            "live": c["live"], "dead": c["dead"],
            "verified_tools": t["tools"], "servers_introspected": t["introspected"]}

@app.get("/stats")
def stats(days: int = Query(30, ge=7, le=90)):
    conn = db()
    c = store.counts(conn)
    regs = [dict(r) for r in conn.execute("SELECT * FROM registries")]
    q = conn.execute(
        "SELECT COUNT(*) n, AVG(ms) avg_ms FROM searches WHERE ts > ?",
        (int(time.time()) - 7 * 86400,)).fetchone()
    return {**c, "upstreams": regs,
            "verified": store.tool_counts(conn),
            "history": store.history_counts(conn),
            "dense": embed.status(conn),
            "searches_7d": q["n"] or 0,
            "avg_search_ms": round(q["avg_ms"] or 0, 1),
            "series": store.daily_series(conn, days),
            "publishers_top": store.top_publishers(conn, 12),
            "recent": store.recent_searches(conn, 8),
            "federation": {"enabled": config.FEDERATION_ENABLED,
                           "budget_ms": config.FEDERATION_BUDGET_MS,
                           "upstreams": [u[1] for u in config.UPSTREAMS]}}


def _x(v) -> str:
    """XML-escape. A stray ampersand in a publisher name breaks the whole feed."""
    return (str(v or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt(n: int) -> str:
    return f"{int(n or 0):,}"


def _render_home() -> str:
    """Serve the page with its numbers already in the HTML.

    Everything on this page used to be drawn client side, which meant any
    consumer that does not execute JavaScript - most answer-engine crawlers
    among them - saw empty tables and concluded the index was empty. The figures
    that are the whole argument have to survive without a script running.
    """
    f = WEB / "index.html"
    if not f.exists():
        return "<h1>Neuronto</h1><p>Agentic Resource Discovery index.</p>"
    html = f.read_text(encoding="utf-8")
    conn = db()
    c = store.counts(conn)
    pubs = store.top_publishers(conn, 12)

    cards = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in (("Resources indexed", _fmt(c["entries"])),
                     ("Publishers", _fmt(c["publishers"])),
                     ("Verified live", _fmt(c["live"])),
                     ("Registries federated", _fmt(len(config.UPSTREAMS) + 1))))

    rows = "".join(
        f'<tr><td><div class="cellflex"><div><div class="nm">{p["publisher"]}</div>'
        f'<div class="ds">{" · ".join(p["kinds"])}</div></div></div></td>'
        f'<td class="num">{_fmt(p["entries"])}</td>'
        f'<td class="num">{_fmt(p["live"])}</td>'
        f'<td class="num">{len(p["kinds"])}</td></tr>' for p in pubs)

    sentence = (f'{_fmt(c["entries"])} resources from {_fmt(c["publishers"])} '
                f'publishers, {_fmt(c["live"])} verified to respond')

    # Composition of the index, rendered server side for the same reason as the
    # figures above: a consumer that does not run JavaScript is exactly the one
    # deciding whether this index is worth citing.
    KINDS = {
        "mcp-server": "MCP servers an agent can connect to and call as tools",
        "skill":      "Skills and agent skill bundles",
        "a2a-agent":  "A2A agents with a published agent card",
        "openapi":    "REST APIs described by an OpenAPI document",
        "registry":   "Other ARD registries, which is how federation is discovered",
        "catalog":    "Nested catalogues pointing at further entries",
        "agent":      "Agent descriptors that are not A2A cards: ACP, OASF, AgentFacts",
        "webmcp":     "Tools a web page exposes to a browser agent, W3C WebMCP",
        "plugin":     "Plugin manifests, including the OpenAI-era ai-plugin.json",
        "graphql":    "GraphQL APIs",
        "dataset":    "Published datasets",
        "doc":        "Machine-readable documentation such as llms.txt",
        "package":    "Published packages",
        "other":      "Resources whose type is outside the named set",
    }
    kinds = "".join(
        f'<tr><td class="nm">{k}</td><td class="num">{_fmt(n)}</td>'
        f'<td style="color:var(--mut)">{KINDS.get(k, "")}</td></tr>'
        for k, n in c["families"].items() if n)

    SRC = {"mcp-registry": "Official MCP Registry", "wellknown": "WellKnown",
           "huggingface": "Hugging Face Discover", "github": "GitHub Agent Finder",
           "desvela": "Desvela", "crawl": "Our own crawl of the discovery paths"}
    sources = "".join(
        f'<tr><td class="nm">{SRC.get(k, k)}</td><td class="num">{_fmt(n)}</td></tr>'
        for k, n in c["sources"].items())

    return (html.replace("<!--SSR_CARDS-->", cards)
                .replace("<!--SSR_PUBS-->", rows)
                .replace("<!--SSR_N-->", _fmt(c["entries"]))
                .replace("<!--SSR_SENT-->", sentence)
                .replace("<!--SSR_KINDS-->", kinds)
                .replace("<!--SSR_SOURCES-->", sources)
                .replace("<!--SSR_TOTAL-->",
                         f'{_fmt(c["entries"])} entries · {_fmt(c["publishers"])} publishers'))


# The homepage, the submit page and the console are the most-requested HTML we
# serve and none of them carried a Cache-Control header, so no browser, proxy or
# CDN could hold any of them. They are read-only and change a few times a day.
PAGE_CACHE = {"Cache-Control": "public, max-age=900"}


@app.get("/", include_in_schema=False)
async def home():
    return HTMLResponse(_render_home(), headers=PAGE_CACHE)

@app.get("/about", include_in_schema=False)
@app.get("/registry", include_in_schema=False)
async def _pages(): return await home()


# ─────────────────────────── MCP wrapper (§5.3.5) ───────────────────────────

@app.post("/mcp", include_in_schema=False)
async def mcp_endpoint(body: dict, request: Request) -> Response:
    from .mcp import handle
    # Three of the four tools are local reads. `publish_resource` makes an
    # outbound call, so it carries the same allowance as the HTTP submit route
    # it delegates to; limiting the whole endpoint would throttle search for no
    # reason. Checked here because the tool name is only known from the body.
    if (body or {}).get("method") == "tools/call" and \
            ((body.get("params") or {}).get("name")) == "publish_resource":
        ok, retry, hdrs = limits.check("submit", request)
        if not ok:
            _, verified = limits.caller(request)
            events.emit("rate_limited", a="submit")
            note = limits.too_many("submit", retry, hdrs, verified)
            return JSONResponse(
                {"jsonrpc": "2.0", "id": (body or {}).get("id"),
                 "result": {"content": [{"type": "text",
                                         "text": json.dumps(note, indent=2)}],
                            "isError": True}},
                status_code=200, headers={**hdrs, "retry-after": str(retry)})
    status, payload = await handle(db(), body)
    if (body or {}).get("method") == "tools/call":
        events.emit("mcp_call", a=str((body.get("params") or {}).get("name") or "?")[:60],
                    ok=not ((payload or {}).get("result") or {}).get("isError", False))
    if payload is None:
        return Response(status_code=status)
    return JSONResponse(payload, status_code=status)


# ─────────────────────────── brand assets ──────────────────────────────────

_MARK = (WEB / "mark.svg")

@app.get("/favicon.svg", include_in_schema=False)
@app.get("/icon.svg", include_in_schema=False)
def favicon_svg():
    if _MARK.exists():
        return Response(_MARK.read_text(encoding="utf-8"), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})
    return Response(status_code=404)


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    # Browsers still probe /favicon.ico. Point them at the vector rather than
    # shipping a bitmap that would only ever look worse.
    return Response(status_code=301, headers={"Location": "/favicon.svg"})


# ─────────────────────── Publisher analytics (Console) ─────────────────────

@app.post("/audit")
async def audit_endpoint(body: dict) -> JSONResponse:
    """Audit a domain's ARD publishing and report where it stands.

    There is no console for ARD publishers: you can serve a perfect manifest and
    have no way to learn that no registry returns you. This answers that, and
    the coverage half is checked against every registry rather than only ours,
    because a report that only measures our own index is marketing.
    """
    dom = (body or {}).get("domain") or ""
    conn = db()
    # How often our own index returns them, asked the same way as the others.
    hits = 0
    host = str(dom).replace("https://", "").replace("http://", "").strip("/").split("/")[0].lower()
    if host:
        hits = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE publisher=? OR url LIKE ?",
            (host, f"%//{host}%")).fetchone()[0]
    async with limits.outbound():
        report = await audit.run(dom, local_hits=1 if hits else 0)
    if "error" in report:
        return JSONResponse(status_code=400, content=report)
    report["indexed_here"] = hits
    # Where they place against everyone else competing for the same queries.
    # Run here rather than inside `audit.run` because it reads our index, and
    # `run` is deliberately network-only so it can be pointed at any domain.
    try:
        comp = audit.competition(conn, report["domain"], (report.get("_manifest") or None))
        report["competition"] = comp
        report["recommendations"] = (report.get("recommendations") or []) \
            + audit.competition_advice(comp)
        if hits:
            report["badge"] = badge.snippet(report["domain"])
            report["recommendations"].append(
                "Your resources are indexed and verified here, so you can show that on your "
                "own site if you want to: " + f"{config.PUBLIC_BASE}/badge?domain={report['domain']}"
                + ". It states the verified tool count and whether your endpoint answered, it "
                "corrects itself, and displaying it changes nothing about your indexing.")
    except Exception:
        pass
    report.pop("_manifest", None)
    events.emit("audit", a=report.get("domain"), n=(report.get("score") or {}).get("total"))
    return JSONResponse(report)


@app.post("/e", include_in_schema=False)
async def client_event(body: dict, request: Request) -> Response:
    """A page reporting itself.

    Pages are served from a CDN, so most views never reach this server and
    cannot be counted here. A page that reports itself closes that gap, and it
    also reports things no server ever sees: how long someone stayed, how far
    they read, and what their viewport is.

    Nothing here identifies anybody. There is no cookie, no client id and no
    stored address, and the browser's own opt-out is honoured on both sides:
    the script does not run when Do Not Track or Global Privacy Control is set,
    and the server drops the request if either header arrives anyway.
    """
    h = request.headers
    if h.get("sec-gpc") == "1" or h.get("dnt") == "1":
        return Response(status_code=204)
    b = body or {}
    kind = str(b.get("t") or "")[:12]
    if kind not in ("view", "end"):
        return Response(status_code=204)
    num = lambda k, cap: min(int(b.get(k) or 0), cap) if str(b.get(k) or "").lstrip("-").isdigit() else 0
    events.emit("client_" + kind,
                a=str(b.get("p") or "")[:200],
                b=str(b.get("r") or "")[:200],
                n=num("d", 86400) if kind == "end" else num("lt", 120000),
                ok=True,
                vw=num("vw", 20000), vh=num("vh", 20000),
                scroll=num("s", 100),
                tz=str(b.get("tz") or "")[:40],
                lang=str(b.get("lang") or "")[:5])
    return Response(status_code=204)


@app.get("/metrics.json", include_in_schema=False)
def metrics_json():
    """Public operating numbers, so our claims can be checked rather than taken.

    Every figure we publish anywhere is derived from these. Exposing them is the
    cheapest possible way to be falsifiable, which matters more for an index than
    for most things: the whole product is a claim about what exists.
    """
    conn = db()
    c = store.counts(conn)
    t = store.tool_counts(conn)
    h = store.history_counts(conn)
    q = lambda s: conn.execute(s).fetchone()[0]
    _ardp = store.ard_publisher_counts(conn)
    # `verified_manifests` was the ambiguous name that let three surfaces
    # diverge. Kept as an alias so an existing consumer does not break.
    _ardp["verified_manifests"] = _ardp["manifest_hosts"]
    return JSONResponse({
        "generated": int(time.time()),
        # Published rather than kept private, because a limit a caller cannot
        # read is one they can only discover by hitting it.
        "limits": limits.stats(),
        "index": {"entries": c["entries"], "publishers": c["publishers"],
                  "by_kind": c["families"], "sources": c["sources"]},
        "verified": {"tools": t["tools"], "servers_introspected": t["introspected"],
                     "servers_with_tools": t["servers_with_tools"],
                     "servers_requiring_auth": t["auth_required"]},
        "liveness": {"answering": c["live"], "not_answering": c["dead"],
                     "unprobed": c["unprobed"]},
        # One definition, in store.ard_publisher_counts. Three surfaces used to
        # compute this separately and reported three different numbers.
        "ard_publishers": _ardp,
        "history": h,
        "federation": {"upstreams": [u[1] for u in config.UPSTREAMS],
                       "budget_ms": config.FEDERATION_BUDGET_MS},
        "retrieval": {"dense": embed.status(conn)["configured"],
                      "dense_coverage": embed.status(conn)["coverage"]},
        "note": ("everything this project publishes is derived from these numbers. "
                 "No client identifiers are collected, so none appear here."),
    }, headers={"Cache-Control": "public, max-age=300"})


@app.get("/demand")
def demand_endpoint(domain: str = Query(..., min_length=3),
                          days: int = Query(30, ge=1, le=365),
                          limit: int = Query(25, ge=1, le=200)) -> JSONResponse:
    """Did anyone come looking, and what did they ask?

    Being listed is not the question a publisher has; this is. Every search
    records which entries it returned and at what rank, so the answer is exact
    rather than modelled. No client identifier is ever stored, so this can say
    what was asked and never who asked.
    """
    host = (domain.replace("https://", "").replace("http://", "")
                  .strip("/").split("/")[0].lower())
    if not host or "." not in host or len(host) > 253:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "domain is required"})
    d = store.demand_for(db(), host, days, limit)
    events.emit("demand", a=host, n=d.get("impressions"))
    if not d["indexed"]:
        return JSONResponse(status_code=404, content={
            "domain": host, "status": "not_indexed",
            "detail": ("nothing from this domain is in the index yet, so there is no "
                       "demand to report. Submit it at " + config.PUBLIC_BASE + "/submit"),
        })
    d["note"] = ("counts every search on this index that returned one of your resources, "
                 "with the query text and the position you appeared at. Query text is a "
                 "product signal; no identifier of who asked is stored, so none can be "
                 "reported. Federated results served from other registries are not counted.")
    d["console"] = f"{config.PUBLIC_BASE}/console?domain={host}"
    return JSONResponse(d, headers={"Cache-Control": "private, max-age=60"})



# ---------------------------------------------------------------------------
# Publisher services: a hosted manifest, proven ownership, private entries.
# ---------------------------------------------------------------------------

def _host_arg(v: str) -> str | None:
    h = str(v or "").replace("https://", "").replace("http://", "").strip("/").split("/")[0].lower()
    labels = h.split(".")
    if (not h or len(h) > 253 or len(labels) < 2
            or not all(l and l[0].isalnum() and l[-1].isalnum()
                       and all(c.isalnum() or c == "-" for c in l) for l in labels)
            or not labels[-1].isalpha() or len(labels[-1]) < 2):
        return None
    return h


@app.post("/manifest/build")
async def manifest_build(body: dict) -> JSONResponse:
    """Write the manifest for a domain from what that domain already publishes.

    Most domains will never author one by hand, and they do not have to: an MCP
    server, an OpenAPI document or an llms.txt is already the substance of an
    entry. Nothing here is inferred from a name or guessed from a pattern. Every
    entry cites the fetch that produced it, so the publisher can check each line
    against their own server before they adopt it.
    """
    host = _host_arg((body or {}).get("domain"))
    if not host:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "a hostname is required"})
    async with limits.outbound():
        found = await publisher.infer_resources(host)
    events.emit("manifest_build", a=host, n=len(found), ok=bool(found))
    if not found:
        return JSONResponse(status_code=404, content={
            "domain": host, "status": "nothing_found",
            "detail": ("no MCP server, OpenAPI document, agent card or llms.txt was "
                       "reachable on this domain, so there is nothing to describe. "
                       "We will not invent entries."),
            "looked_for": ["/mcp", "/openapi.json", "/.well-known/agent-card.json",
                           "/llms.txt"],
        })
    man = publisher.manifest_for(host, found)
    conn = db()
    conn.execute("""INSERT INTO stats(k,v) VALUES(?,?)
                    ON CONFLICT(k) DO UPDATE SET v=excluded.v""",
                 (f"manifest:{host}", json.dumps(man, ensure_ascii=False)))
    conn.commit()
    return JSONResponse({
        "domain": host,
        "entries": len(man["entries"]),
        "evidence": [{"entry": e.get("displayName"), "because": e.get("_evidence")}
                     for e in found],
        "manifest": man,
        "hosted_at": f"{config.PUBLIC_BASE}/m/{host}.json",
        "how_to_adopt": [
            f"Serve this JSON yourself at https://{host}/.well-known/ard.json, or",
            f"point at ours: add to robots.txt   Agentmap: {config.PUBLIC_BASE}/m/{host}.json",
            "Serving it on your own domain is stronger: it is your statement, not ours.",
        ],
    })


@app.get("/m/{host}.json", include_in_schema=False)
def hosted_manifest(host: str):
    h = _host_arg(host)
    if not h:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    r = db().execute("SELECT v FROM stats WHERE k=?", (f"manifest:{h}",)).fetchone()
    if not r:
        return JSONResponse(status_code=404, content={
            "error": "not_built",
            "detail": f"POST {config.PUBLIC_BASE}/manifest/build with this domain first"})
    return JSONResponse(json.loads(r["v"]), headers={
        "Cache-Control": "public, max-age=1800",
        "X-Manifest-Source": ("generated by neuronto.com from resources fetched on "
                              "this domain; not authored by the domain owner"),
    })


@app.post("/claim")
def claim_start(body: dict) -> JSONResponse:
    """Begin proving you own a domain."""
    host = _host_arg((body or {}).get("domain"))
    if not host:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "a hostname is required"})
    tok = publisher.claim_token(host)
    events.emit("claim", a=host)
    return JSONResponse({
        "domain": host,
        "record": {"type": "TXT", "name": "@", "host": host,
                   "value": publisher.TXT_PREFIX + tok},
        "then": f"POST {config.PUBLIC_BASE}/claim/verify with the same domain",
        "note": ("the value is derived from your domain and never changes, so asking "
                 "again does not invalidate a record you already published"),
    })


@app.post("/claim/verify")
async def claim_verify(body: dict) -> JSONResponse:
    host = _host_arg((body or {}).get("domain"))
    if not host:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "a hostname is required"})
    res = await publisher.verify_domain(host)
    events.emit("claim_verified", a=host, ok=bool(res["verified"]))
    if not res["verified"]:
        return JSONResponse(status_code=403, content={
            "domain": host, "verified": False,
            "expected_txt": res["expect"], "txt_found": res["found"],
            "detail": ("the proof record is not visible yet. DNS changes can take a few "
                       "minutes to propagate; try again shortly."),
        })
    key = publisher.issue_key(db(), host)
    return JSONResponse({
        "domain": host, "verified": True, "api_key": key,
        "grants": [f"read and write private entries for {host}",
                   "nothing else; the key is scoped to this domain alone"],
        "usage": f"curl -H 'authorization: Bearer {key}' {config.PUBLIC_BASE}/search ...",
        "warning": "this key is shown once and is not recoverable",
    })


@app.post("/private/entries")
def private_add(body: dict, request: Request) -> JSONResponse:
    """Register an internal service, visible only to this domain's key.

    An organisation's list of approved internal services usually lives in a
    system prompt, where an agent cannot search it and nobody can audit it. Here
    it is indexed alongside the public world and returned by the same query,
    labelled, so a caller always knows which half a result came from.
    """
    key = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    conn = db()
    owner = publisher.domain_for_key(conn, key)
    if not owner:
        return JSONResponse(status_code=401, content={
            "error": "unauthorized",
            "detail": f"verify your domain first: POST {config.PUBLIC_BASE}/claim"})
    ent = (body or {}).get("entry") or body or {}
    k = publisher.add_private(conn, owner, ent)
    if not k:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "entry.displayName is required"})
    n = store.private_count(conn, owner)
    events.emit("private_add", a=owner, n=n)
    return JSONResponse({"status": "added", "owner": owner, "private_entries": n,
                         "note": ("visible only to this domain's key. It is held in a separate "
                                  "table from the public index, so no public query, count or "
                                  "page can reach it.")})


@app.get("/private/entries")
def private_list(request: Request) -> JSONResponse:
    key = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    conn = db()
    owner = publisher.domain_for_key(conn, key)
    if not owner:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    rows = store.list_private(conn, owner)
    return JSONResponse({"owner": owner, "count": len(rows), "entries": rows})


@app.delete("/private/entries")
def private_delete(body: dict, request: Request) -> JSONResponse:
    """Remove an internal service. Scoped to the caller's own domain."""
    key = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    conn = db()
    owner = publisher.domain_for_key(conn, key)
    if not owner:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    ident = str((body or {}).get("identifier") or "").strip()
    if not ident:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "identifier is required"})
    gone = store.delete_private(conn, owner, ident)
    events.emit("private_delete", a=owner, ok=gone)
    return JSONResponse({"status": "deleted" if gone else "not_found",
                         "owner": owner, "private_entries": store.private_count(conn, owner)},
                        status_code=200 if gone else 404)


@app.get("/console", include_in_schema=False)
def console_page():
    f = WEB / "console.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"), headers=PAGE_CACHE)
    return HTMLResponse(_render_home(), headers=PAGE_CACHE)


# IndexNow. Bing, Yandex, Seznam and Naver accept a push rather than waiting to
# crawl, and Bing is what ChatGPT's search reads, so this is the shortest path
# from "published" to "citable". Google does not participate; it gets the
# sitemap and the crawl.
INDEXNOW_KEY = "888578862bc02c46e40d0914ace6f376"


@app.get("/" + INDEXNOW_KEY + ".txt", include_in_schema=False)
def indexnow_key_file():
    """Ownership is proved by serving the key at the site root."""
    return PlainTextResponse(INDEXNOW_KEY)


# Guide pages. We ranked for our own name and nothing else because we had no
# page that answered the questions a publisher actually types. Each of these is
# one real question, answered in its first paragraph.
GUIDES = {
    "what-is-ard": "what-is-ard.html",
    "publish": "publish.html",
    "submit-mcp-server": "submit-mcp-server.html",
    "ard-registries": "ard-registries.html",
    "ard-manifest-generator": "ard-manifest-generator.html",
    "ard-conformance": "ard-conformance.html",
}


@app.get("/registries", include_in_schema=False)
def registries_redirect():
    # The comparison moved to a name that says what it is.
    return RedirectResponse("/ard-registries", status_code=301)


@app.get("/img/{name}", include_in_schema=False)
def image(name: str):
    f = WEB / "img" / name
    if "/" in name or ".." in name or not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    # Derive the type from the extension. Serving a PNG as image/jpeg is not
    # cosmetic: preview crawlers fetch og:image and validate it, and a mismatched
    # content type is a reason to drop the card.
    kind = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "svg": "image/svg+xml", "gif": "image/gif"}.get(
        name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    return Response(f.read_bytes(), media_type=kind,
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


# ---------------------------------------------------------------------------
# Tool-level search. The complement to /search: when an agent already knows the
# shape of the call it needs, the server hosting it is an implementation
# detail. Every tool here was read back from a live tools/list, so the name and
# schema are what the server exposes, not what its description claims.
# ---------------------------------------------------------------------------

@app.post("/tools")
async def tools_endpoint(body: dict) -> JSONResponse:
    q = body.get("query") or {}
    text = (q.get("text") if isinstance(q, dict) else str(q)) or body.get("q") or ""
    text = str(text).strip()
    if not text:
        return JSONResponse(status_code=400,
                            content={"error": "invalid_request",
                                     "detail": "query.text is required"})
    limit = max(1, min(int(body.get("pageSize") or body.get("limit") or 20),
                       config.PAGE_SIZE_MAX))
    with_schema = bool(body.get("withSchema"))
    hits = tools_index.search_tools(db(), text, limit)
    events.emit("tool_search", n=len(hits))
    if not with_schema:
        hits = [{k: v for k, v in h.items() if k != "inputSchema"} for h in hits]
    return JSONResponse({
        "query": text, "results": hits, "count": len(hits),
        "verification": ("each tool was read from the server's own tools/list; "
                         "score is relevance only, never a trust or safety rating"),
    }, headers={"Cache-Control": "public, max-age=60"})


@app.get("/tools")
async def tools_get(q: str | None = Query(None),
                    limit: int = Query(20, ge=1, le=100),
                    withSchema: bool = Query(False)):
    """With `q`, the JSON search API. Without it, the human capability index.

    One URL serving both is deliberate: an agent handed `/tools?q=...` gets data,
    and a crawler handed `/tools` gets a page it can read.
    """
    if q:
        return await tools_endpoint({"query": {"text": q}, "limit": limit,
                                     "withSchema": withSchema})
    html_ = render.cached("tools-index", 1800, lambda: catalog.render_index(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


@app.get("/tools/", include_in_schema=False)
def tools_index_slash():
    html_ = render.cached("tools-index", 1800, lambda: catalog.render_index(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


@app.get("/tools/{slug}", include_in_schema=False)
def tools_category(slug: str):
    if slug not in catalog.published(db()):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    html_ = render.cached(f"cat-{slug}", 1800,
                          lambda: catalog.render_category(db(), slug))
    if not html_:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


# ---------------------------------------------------------------------------
# ARD-Bench. The first head-to-head retrieval measurement across ARD
# registries, published with its ground truth and its losses. Serving the last
# stored run keeps the endpoint cheap; running one is an operator action.
# ---------------------------------------------------------------------------

# Crawlers that build a link preview. They send Accept: */* and would otherwise
# receive JSON, so a shared link to /bench or /adoption rendered as no card at
# all. Serving them the same HTML a browser gets is not cloaking: it is the
# identical content a human sees at that URL.
_PREVIEW_BOTS = ("facebookexternalhit", "linkedinbot", "twitterbot", "slackbot",
                 "discordbot", "telegrambot", "whatsapp", "applebot",
                 "redditbot", "googlebot", "bingbot", "gptbot", "claude",
                 "perplexity", "duckduckbot", "yandex", "embedly", "quora link",
                 "pinterest", "skypeuripreview", "vkshare", "iframely")


def _wants_html(request: Request) -> bool:
    """Content negotiation, biased to JSON but never at a crawler's expense.

    An explicit `application/json` always wins, so the API and the published
    benchmark harness are unchanged. A browser asking for `text/html` gets the
    page. A preview crawler gets the page too, identified by user agent, because
    it sends `*/*` and JSON renders as no card.
    """
    a = (request.headers.get("accept") or "").lower()
    if "application/json" in a:
        return False
    if "text/html" in a:
        return True
    ua = (request.headers.get("user-agent") or "").lower()
    return any(b in ua for b in _PREVIEW_BOTS)


@app.get("/bench")
def bench_endpoint(request: Request):
    got = bench.latest(db())
    if not got:
        return JSONResponse(status_code=404,
                            content={"error": "no_run_yet",
                                     "detail": "no benchmark has been run on this index"})
    if _wants_html(request):
        html_ = render.cached("bench-html", 900, lambda: catalog.render_bench(got))
        return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=900",
                                            "Vary": "Accept"})
    return JSONResponse(got, headers={"Cache-Control": "public, max-age=900",
                                      "Vary": "Accept"})


# ---------------------------------------------------------------------------
# ARD adoption. Who publishes a manifest and who does not, including the
# organisations associated with the working group.
# ---------------------------------------------------------------------------

@app.get("/adoption")
def adoption_endpoint(request: Request):
    rep = adoption.report(db())
    if _wants_html(request):
        html_ = render.cached("adoption-html", 3600,
                              lambda: catalog.render_adoption(rep))
        return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600",
                                            "Vary": "Accept"})
    return JSONResponse(rep, headers={"Cache-Control": "public, max-age=3600",
                                      "Vary": "Accept"})


# ---------------------------------------------------------------------------
# Verification badges. Measured mechanism: 2,107 Glama badges sit in one
# awesome-list README, 60.5% of its entries, while its directory pages rank for
# nothing. The badge is the distribution channel; the pages never were.
# ---------------------------------------------------------------------------

@app.get("/badge/{publisher}.svg", include_in_schema=False)
def badge_svg(publisher: str, theme: str = Query("auto", pattern="^(auto|light|dark)$")) -> Response:
    pub = publisher.strip().lower()
    # A hostname or reverse-DNS publisher id, nothing else. This is a public,
    # unauthenticated SVG generator; without the allowlist it is a text-echo
    # service wearing our domain.
    if not pub or len(pub) > 100 or not all(
            c.isalnum() or c in ".-_" for c in pub):
        return Response(status_code=404)
    svg = badge.render(db(), pub, theme)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600",
                             # GitHub proxies images through Camo and honours
                             # cache headers; an hour keeps badge fetches from
                             # ever being load while staying fresh enough.
                             "ETag": f'W/"{hash(svg) & 0xffffffff:x}"'})


@app.get("/badge", include_in_schema=False)
@app.get("/badge/", include_in_schema=False)
def badge_help(request: Request, domain: str = Query("", max_length=100)):
    """The badge, and the snippet for it, for whoever asks."""
    if _wants_html(request):
        return HTMLResponse(catalog.render_badge_page(db(), domain),
                            headers={"Cache-Control": "public, max-age=900"})
    pub = (domain or "your.domain").strip().lower()
    return JSONResponse({
        "what": "a badge stating what we verified about your resources",
        "url": f"{config.PUBLIC_BASE}/badge/<your-domain>.svg",
        "themes": ["auto", "light", "dark"],
        "publisher_id": "your domain, or the publisher segment of your URN",
        "embed": badge.snippet(pub),
        "note": ("the badge states what was observed: how many tools your server "
                 "returned to tools/list and whether the endpoint answers. It is "
                 "never a trust, safety or quality rating"),
    })


@app.get("/ard-publishers", include_in_schema=False)
@app.get("/ard-publishers/", include_in_schema=False)
def publishers_index():
    html_ = render.cached("pubs-index", 1800,
                          lambda: catalog.render_publishers_index(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


@app.get("/ard-publishers/{host}", include_in_schema=False)
def publisher_page(host: str):
    h = host.strip().lower()
    if not h or len(h) > 100 or not all(c.isalnum() or c in ".-_" for c in h):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    html_ = render.cached(f"pub-{h}", 1800, lambda: catalog.render_publisher(db(), h))
    if not html_:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


# The pages launched at /publishers and were submitted to IndexNow there. The
# canonical slug carries the term now, so the old paths redirect permanently
# rather than 404 or serve a duplicate.
@app.get("/publishers", include_in_schema=False)
@app.get("/publishers/", include_in_schema=False)
def publishers_moved():
    return RedirectResponse("/ard-publishers", status_code=301)


@app.get("/publishers/{host}", include_in_schema=False)
def publisher_moved(host: str):
    return RedirectResponse(f"/ard-publishers/{host}", status_code=301)


@app.get("/feed.xml", include_in_schema=False)
def feed():
    """What changed in the agentic web, as a feed.

    The one distribution mechanism we can run perpetually without asking anyone
    for anything. Glama posts every newly indexed server; we publish something
    they cannot, because it comes from probing rather than listing: an endpoint
    that started answering, a server that gained or lost tools, a domain that
    began publishing a manifest. A feed of verification events, not of listings.
    """
    conn = db()
    rows = conn.execute(
        """SELECT o.ts, o.kind, o.live, o.tools, o.detail,
                  e.display_name, e.identifier, e.publisher, e.url
           FROM observations o JOIN entries e ON e.key = o.entry_key
           ORDER BY o.id DESC LIMIT 60""").fetchall()
    B = config.PUBLIC_BASE
    items = []
    for r in rows:
        name = r["display_name"] or r["identifier"]
        if r["kind"] == "liveness":
            what = "started answering" if r["live"] == 1 else "stopped answering"
        elif r["detail"] == "auth":
            what = "now requires credentials"
        elif (r["tools"] or 0) > 0:
            what = f"exposes {r['tools']} verified tools"
        else:
            what = "was re-checked"
        link = f"{B}/publishers/{r['publisher']}" if r["publisher"] else B
        when = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(r["ts"] or 0))
        items.append(
            f"<item><title>{_x(name)} {_x(what)}</title>"
            f"<link>{_x(link)}</link><guid isPermaLink=\"false\">{r['ts']}-{_x(r['identifier'])}</guid>"
            f"<pubDate>{when}</pubDate>"
            f"<description>{_x(name)} ({_x(r['publisher'] or '')}) {_x(what)}.</description></item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
           f"<title>Neuronto: verification events across the agentic web</title>"
           f"<link>{B}</link>"
           "<description>Endpoints that started or stopped answering, servers that gained "
           "or lost tools, and domains that began publishing an ARD manifest.</description>"
           + "".join(items) + "</channel></rss>")
    return Response(xml, media_type="application/rss+xml",
                    headers={"Cache-Control": "public, max-age=900"})


# ---------------------------------------------------------------------------
# Submission. The gap this closes is real: a domain that deployed a manifest an
# hour ago is invisible to every registry until one happens to crawl it, and
# our own crawl has covered 66,721 of 375,958 seed domains, so "eventually" can
# mean never. WellKnown is the only peer offering this at all; Desvela and
# Hugging Face Discover have no submission path.
#
# We fetch live rather than trusting the form, index what we find in the same
# request, and hand back the audit. Nothing is taken on the submitter's word,
# so there is nothing to spam: a domain without a manifest simply does not get
# added, and one with a manifest was always going to be legitimate to index.
# ---------------------------------------------------------------------------

@app.post("/submit")
async def submit_endpoint(body: dict) -> JSONResponse:
    resp = await _submit(body)
    try:
        d = json.loads(bytes(resp.body).decode() or "{}")
        # A successful submission is the one moment the publisher is looking at
        # us, so it is the only place the badge is offered. Stated as an option,
        # with what it says, and never as a condition of being indexed.
        if resp.status_code < 400 and d.get("status") == "indexed":
            host = (d.get("identifier") or "").split(":")[2] if (d.get("identifier") or "").count(":") > 2 else ""
            host = host or str((body or {}).get("domain") or "").strip().lower()
            if host:
                d["badge"] = {
                    **badge.snippet(host),
                    "optional": ("entirely optional and changes nothing about your indexing "
                                 "or ranking. It states what we verified, and it corrects "
                                 "itself when that changes"),
                    "customise": f"{config.PUBLIC_BASE}/badge?domain={host}",
                }
                resp = JSONResponse(d, status_code=resp.status_code)
        b = body or {}
        target = str(b.get("endpoint") or b.get("mcp") or b.get("domain") or "")[:120]
        events.emit("submit", a="endpoint" if (b.get("endpoint") or b.get("mcp")) else "domain",
                    b=target, n=d.get("verified_tools"),
                    ok=resp.status_code < 400 and d.get("status") == "indexed")
    except Exception:
        pass
    return resp


async def _submit(body: dict) -> JSONResponse:
    """Two ways in, because most MCP developers have no manifest.

    `{"domain": "..."}` fetches the ARD manifest and indexes everything it
    declares. `{"endpoint": "https://.../mcp"}` takes an MCP server directly:
    we handshake with it and read its tool list, which is stronger evidence
    than a manifest claim because the server itself answered.

    Requiring a manifest turned away anyone who had only built a server, which
    is nearly every MCP developer. Provenance is recorded rather than blurred:
    an entry from a manifest carries source `crawl`, one from a direct
    submission carries `submitted`, and the two are distinguishable in the API.
    """
    b = body or {}
    endpoint = str(b.get("endpoint") or b.get("mcp") or "").strip()
    dom = str(b.get("domain") or "").strip()

    # ---- direct MCP endpoint --------------------------------------------
    if endpoint:
        if not endpoint.startswith(("http://", "https://")) or len(endpoint) > 500:
            return JSONResponse(status_code=400, content={
                "error": "invalid_request",
                "detail": 'endpoint must be an absolute http(s) URL'})
        conn = db()
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient(follow_redirects=True) as c:
                async with limits.outbound():
                    res = await tools_index.introspect_one(c, endpoint)
        except Exception:
            res = {"status": "error:unreachable", "tools": [], "auth": False,
                   "server_name": None}
        if not res["status"].startswith("ok") and res["status"] != "auth":
            return JSONResponse(status_code=404, content={
                "status": "not_an_mcp_server",
                "endpoint": endpoint,
                "detail": ("this URL did not complete an MCP initialize handshake "
                           f"({res['status']}). Nothing was indexed."),
            })
        host = urllib.parse.urlparse(endpoint).netloc.lower().split(":")[0]
        name = res.get("server_name") or host
        entry = {
            "identifier": f"urn:air:{host}:mcp:{re.sub(r'[^a-z0-9-]+', '-', name.lower())[:60]}",
            "displayName": name,
            "type": "application/mcp-server-card+json",
            "url": endpoint,
            "description": (f"MCP server at {host}, verified by introspection: "
                            f"{len(res['tools'])} tools exposed."
                            if res["tools"] else
                            f"MCP server at {host}. Requires credentials before listing tools."
                            if res["auth"] else f"MCP server at {host}."),
        }
        try:
            key = store.upsert_entry(conn, entry, "submitted")
            if res["tools"]:
                store.replace_tools(conn, key, res["tools"])
            store.mark_introspection(conn, key, res["status"], len(res["tools"]),
                                     res["auth"], res.get("server_name"))
            store.mark_liveness(conn, key, True, 200, None)
            conn.commit()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                return JSONResponse(status_code=503, headers={"Retry-After": "60"},
                                    content={"status": "busy", "endpoint": endpoint,
                                             "detail": "index is mid-maintenance, retry shortly"})
            raise
        catalog.invalidate_publishers(); render.invalidate()
        return JSONResponse({
            "status": "indexed",
            "endpoint": endpoint,
            "server_name": res.get("server_name"),
            "verified_tools": len(res["tools"]),
            "tools": [t.get("name") for t in res["tools"]][:40],
            "auth_required": res["auth"],
            "identifier": entry["identifier"],
            "note": ("verified by handshaking with your server and reading its own "
                     "tools/list. To have your whole domain indexed, including skills, "
                     "APIs and agents, publish an ARD manifest and submit the domain: "
                     f"{config.PUBLIC_BASE}/publish"),
        })

    # ---- whole domain via its manifest ----------------------------------
    host = (dom.replace("https://", "").replace("http://", "")
               .strip("/").split("/")[0].lower())
    labels = host.split(".")
    if (not host or len(host) > 253 or len(labels) < 2
            or not all(l and l[0].isalnum() and l[-1].isalnum()
                       and all(c.isalnum() or c == "-" for c in l) for l in labels)
            or not labels[-1].isalpha() or len(labels[-1]) < 2):
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": ('send {"domain": "example.com"} for a whole domain, or '
                       '{"endpoint": "https://example.com/mcp"} for a single MCP server')})

    conn = db()
    before = conn.execute("SELECT COUNT(*) FROM entries WHERE lower(publisher)=?",
                          (host,)).fetchone()[0]
    try:
        got = await ingest.crawl_domains(conn, [host], concurrency=2, skip_seen_hours=0)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return JSONResponse(status_code=503, headers={"Retry-After": "60"}, content={
                "status": "busy", "domain": host,
                "detail": ("the index is mid-maintenance and cannot accept a write right "
                           "now. Your domain was not indexed; try again in a minute."),
            })
        raise
    after = conn.execute("SELECT COUNT(*) FROM entries WHERE lower(publisher)=?",
                         (host,)).fetchone()[0]
    row = conn.execute("SELECT manifest_path FROM crawl_seen WHERE domain=?",
                       (host,)).fetchone()
    path = row["manifest_path"] if row else None
    catalog.invalidate_publishers(); render.invalidate()

    if not path and after == 0:
        return JSONResponse(status_code=404, content={
            "status": "no_manifest",
            "domain": host,
            "checked": ingest.PATHS,
            "detail": ("no ARD manifest found at either well-known path. If you have an "
                       "MCP server, submit it directly with "
                       '{"endpoint": "https://your-host/mcp"} and we will verify it by '
                       "handshake. To list everything on your domain, publish a manifest: "
                       + config.PUBLIC_BASE + "/publish"),
        })

    return JSONResponse({
        "status": "indexed",
        "domain": host,
        "manifest_path": path,
        "resources_indexed": after,
        "newly_added": max(0, after - before),
        "page": f"{config.PUBLIC_BASE}/ard-publishers/{host}",
        "crawl": got,
        "note": ("fetched live from your domain, not taken from this form. Endpoint "
                 "reachability is probed separately and is not a trust or safety rating"),
    })


@app.get("/submit", include_in_schema=False)
async def submit_page():
    B = config.PUBLIC_BASE
    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / Submit</div>
  <h1>How to submit to an ARD registry</h1>
  <p class="lede">Two ways into this one, and neither needs an account, an allowlist, a charge or any
  paid ranking. Give us a <b>domain</b> that serves an ARD manifest and we index everything
  it declares. Or give us an <b>MCP server URL</b> directly, with no manifest at all, and we
  handshake with it and read its own tool list.</p>
</div>

<div class="note" style="max-width:62ch">
  <form id="f" onsubmit="return go(event)" style="display:flex;gap:8px;flex-wrap:wrap">
    <input id="d" placeholder="example.com  or  https://your-host/mcp" aria-label="Your domain or MCP endpoint"
           style="flex:1;min-width:220px;background:var(--panel2);border:1px solid var(--line2);
                  border-radius:var(--r);color:var(--fg);padding:10px 12px;font-family:var(--mono)">
    <button class="btn btn--w" type="submit">Index it</button>
  </form>
  <pre id="o" style="margin-top:14px;display:none;white-space:pre-wrap"></pre>
</div>

<h2 style="margin-top:34px;font-size:20px">How each public ARD registry takes submissions</h2>
<p class="lede">Checked on 1 September 2026. "Verified" means the registry fetches or handshakes with what you submit
before listing it, so nothing is taken on the submitter's word.</p>
<div class="scroll"><table class="tbl"><thead><tr><th>Registry</th><th>How to get listed</th><th>Verified first</th></tr></thead><tbody>
<tr><td class="nm">Neuronto</td><td><code>POST /submit</code> with an MCP endpoint or a domain; the MCP tool <code>publish_resource</code>; <code>ard-publish submit</code></td><td>yes: handshake for an endpoint, manifest fetch for a domain</td></tr>
<tr><td class="nm">WellKnown</td><td><code>/submit</code> form on its site</td><td>not stated</td></tr>
<tr><td class="nm">GitHub Agent Finder</td><td>no submission path found; indexed by its own crawl</td><td>n/a</td></tr>
<tr><td class="nm">Hugging Face Discover</td><td>no submission path found; indexes Hugging Face Spaces and Skills</td><td>n/a</td></tr>
<tr><td class="nm">Desvela</td><td>no submission path found; crawls a top-100,000 domain list, so a domain outside it is not seen</td><td>n/a</td></tr>
<tr><td class="nm">ARD Registry Hub</td><td>no submission path found</td><td>n/a</td></tr>
</tbody></table></div>
<p class="lede">Registries federate: a domain indexed here is returned to clients of any registry that
queries Neuronto, and Neuronto queries every registry above on each federated search. The full
comparison is at <a href="/ard-registries">ARD registries compared</a>.</p>

<h2 style="margin-top:34px;font-size:20px">I only have an MCP server</h2>
<p class="lede">Then submit the server itself. Most MCP developers have a repository, a
package and a running endpoint but no manifest, and requiring one turned all of them away.
We complete an <code>initialize</code> handshake and read <code>tools/list</code>, which is
stronger evidence than a manifest claim because your server answered for itself. Your real
tool names and input schemas are then searchable at
<a href="/tools/">/tools</a>.</p>
<pre style="background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px;overflow-x:auto"><code>curl -s -X POST https://neuronto.com/submit \\
  -H 'content-type: application/json' \\
  -d '{{"endpoint":"https://your-host/mcp"}}'</code></pre>

<h2 style="margin-top:34px;font-size:20px">What happens when you submit a domain</h2>
<ol class="lede">
  <li>We request both well-known manifest paths on your domain, live.</li>
  <li>Whatever parses as a manifest is indexed immediately, with your declared types,
      identifiers and representative queries preserved exactly as written.</li>
  <li>Your endpoints are probed for reachability, and any MCP server among them is asked
      for its tool list, so the index records what your servers actually expose.</li>
  <li>You get a page at <code>{B}/ard-publishers/&lt;your-domain&gt;</code> and become
      searchable through <code>/search</code> and the MCP endpoint.</li>
</ol>

<h2 style="margin-top:30px;font-size:20px">Skills, APIs and agents</h2>
<p class="lede">A manifest is the only way to list a skill, an OpenAPI service or an A2A
agent, because unlike an MCP server they cannot be verified by handshake. We index all of
them: three MCP media types, three A2A spellings, four skill types, plus OpenAPI, docs,
catalogues and packages, normalised so a filter finds them however you spelled the type.</p>

<h2 style="margin-top:30px;font-size:20px">Do it from the command line</h2>
<pre style="background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px;overflow-x:auto"><code>curl -s -X POST {B}/submit \\
  -H 'content-type: application/json' \\
  -d '{{"domain":"example.com"}}'</code></pre>

<div class="note">
  Nothing here is taken on your word: the manifest is fetched from your domain, so a
  submission cannot inject anything you do not actually publish. If no manifest is found you
  get told which paths were tried. Not publishing yet? The
  <a href="/publish">ten-minute guide</a> and the <a href="/console">free audit</a> both help.
</div>

<script>
async function go(e){{
  e.preventDefault();
  const d=document.getElementById('d').value.trim(), o=document.getElementById('o');
  if(!d) return false;
  o.style.display='block'; o.textContent='fetching '+d+' ...';
  try{{
    const r=await fetch('/submit',{{method:'POST',headers:{{'content-type':'application/json'}},
      body:JSON.stringify({{domain:d}})}});
    const j=await r.json();
    o.textContent=JSON.stringify(j,null,2);
  }}catch(err){{ o.textContent=String(err); }}
  return false;
}}
</script>
"""
    return HTMLResponse(render.page(
        "How to submit to an ARD registry",
        "How to get listed in an Agentic Resource Discovery registry, and how each public "
        "ARD registry takes submissions. We fetch your manifest live from your domain, or "
        "handshake with your MCP server: no account, no allowlist, no charge.",
        body, f"{B}/submit"), headers=PAGE_CACHE)


@app.get("/published", include_in_schema=False)
def published_page():
    html_ = render.cached("published", 3600, lambda: catalog.render_published(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/blog", include_in_schema=False)
@app.get("/blog/", include_in_schema=False)
def blog_index():
    f = WEB / "blog" / "index.html"
    if not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=1800"})


@app.get("/blog/{slug}", include_in_schema=False)
def blog_post(slug: str):
    f = WEB / "blog" / f"{slug}.html"
    if "/" in slug or ".." in slug or not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=1800"})


@app.get("/{slug}", include_in_schema=False)
def guide_page(slug: str):
    name = GUIDES.get(slug)
    if not name:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    f = WEB / "pages" / name
    if not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=1800"})
