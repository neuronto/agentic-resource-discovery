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
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)

from . import (adoption, audit, badge, bench, catalog, config, embed,
               federation, ingest, liveness, render, search, store,
               tools_index)
from .normalize import media_family

app = FastAPI(title="Neuronto ARD Registry", version="1.0.0",
              docs_url="/api-docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["X-Response-Time-Ms"])

_conn: sqlite3.Connection | None = None
WEB = Path(__file__).resolve().parent.parent / "web"


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = store.connect()
        store.init(_conn)
    return _conn


@app.on_event("startup")
async def _startup() -> None:
    db()


@app.middleware("http")
async def _timing(request: Request, call_next):
    """Latency is the product claim, so every response carries its own measurement."""
    t0 = time.perf_counter()
    resp = await call_next(request)
    resp.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    return resp


# ─────────────────────────── Registry REST API (§5.3) ───────────────────────

@app.post("/search")
async def search_endpoint(body: dict) -> JSONResponse:
    """POST /search - the one endpoint the spec mandates.

    Every result carries `identifier`, `score` (0-100) and `source`, which are
    the three fields the conformance tool requires of a SearchResultItem.
    """
    q = body.get("query") or {}
    text = (q.get("text") or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "query.text is required for Search (spec 5.3.2)."})
    flt = q.get("filter") if isinstance(q.get("filter"), dict) else None
    page_size = body.get("pageSize") or config.PAGE_SIZE_DEFAULT
    try:
        page_size = max(1, min(int(page_size), config.PAGE_SIZE_MAX))
    except (TypeError, ValueError):
        page_size = config.PAGE_SIZE_DEFAULT
    mode = str(body.get("federation") or "auto")

    t0 = time.perf_counter()
    conn = db()
    out = await search.search(conn, text, flt, page_size, mode)
    took = int((time.perf_counter() - t0) * 1000)
    fed_ok = sum(1 for f in (out.get("_federated") or []) if f.get("ok"))
    store.log_search(conn, text, mode, len(out["results"]), took, fed_ok,
                     entries=out["results"])
    payload: dict[str, Any] = {"results": search.clean(out["results"])}
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
async def explore_endpoint(body: dict) -> JSONResponse:
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
async def agents_endpoint(
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
        "host": {"displayName": "Neuronto Agentic Resource Discovery (ARD) index",
                 "identifier": "did:web:neuronto.com",
                 "documentationUrl": f"{B}/about"},
        "entries": [{
            # §5.3: a registry's base URL is discovered by finding an entry of
            # this type. This is how Neuronto becomes findable AS a registry.
            "identifier": "urn:air:neuronto.com:registry:neuronto",
            "displayName": "Neuronto Agentic Resource Discovery (ARD) registry",
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
            "displayName": "Neuronto - ARD & MCP discovery (MCP server)",
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
async def ard_json(): return JSONResponse(_manifest(), headers=CACHE)

@app.get("/.well-known/ai-catalog.json", include_in_schema=False)
async def ai_catalog(): return JSONResponse(_manifest(), headers=CACHE)

@app.get("/.well-known/did.json", include_in_schema=False)
async def did_json():
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
async def mcp_card():
    from .mcp import server_card
    return JSONResponse(server_card(), headers=CACHE)

@app.get("/robots.txt", include_in_schema=False)
async def robots():
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
async def sitemap():
    B = config.PUBLIC_BASE
    urls = ["/", "/what-is-ard", "/publish", "/submit-mcp-server",
            "/registries", "/console", "/blog",
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
async def llms_txt():
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
offer at /.well-known/ard.json. Neuronto is both a registry and a publisher.

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
async def health():
    c = store.counts(db())
    t = store.tool_counts(db())
    return {"status": "ok", "entries": c["entries"], "publishers": c["publishers"],
            "live": c["live"], "dead": c["dead"],
            "verified_tools": t["tools"], "servers_introspected": t["introspected"]}

@app.get("/stats")
async def stats(days: int = Query(30, ge=7, le=90)):
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


@app.get("/", include_in_schema=False)
async def home():
    return HTMLResponse(_render_home())

@app.get("/about", include_in_schema=False)
@app.get("/registry", include_in_schema=False)
async def _pages(): return await home()


# ─────────────────────────── MCP wrapper (§5.3.5) ───────────────────────────

@app.post("/mcp", include_in_schema=False)
async def mcp_endpoint(body: dict) -> Response:
    from .mcp import handle
    status, payload = await handle(db(), body)
    if payload is None:
        return Response(status_code=status)
    return JSONResponse(payload, status_code=status)


# ─────────────────────────── brand assets ──────────────────────────────────

_MARK = (WEB / "mark.svg")

@app.get("/favicon.svg", include_in_schema=False)
@app.get("/icon.svg", include_in_schema=False)
async def favicon_svg():
    if _MARK.exists():
        return Response(_MARK.read_text(encoding="utf-8"), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})
    return Response(status_code=404)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
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
    report = await audit.run(dom, local_hits=1 if hits else 0)
    if "error" in report:
        return JSONResponse(status_code=400, content=report)
    report["indexed_here"] = hits
    return JSONResponse(report)


@app.get("/console", include_in_schema=False)
async def console_page():
    f = WEB / "console.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    return HTMLResponse(_render_home())


# IndexNow. Bing, Yandex, Seznam and Naver accept a push rather than waiting to
# crawl, and Bing is what ChatGPT's search reads, so this is the shortest path
# from "published" to "citable". Google does not participate; it gets the
# sitemap and the crawl.
INDEXNOW_KEY = "888578862bc02c46e40d0914ace6f376"


@app.get("/" + INDEXNOW_KEY + ".txt", include_in_schema=False)
async def indexnow_key_file():
    """Ownership is proved by serving the key at the site root."""
    return PlainTextResponse(INDEXNOW_KEY)


# Guide pages. We ranked for our own name and nothing else because we had no
# page that answered the questions a publisher actually types. Each of these is
# one real question, answered in its first paragraph.
GUIDES = {
    "what-is-ard": "what-is-ard.html",
    "publish": "publish.html",
    "submit-mcp-server": "submit-mcp-server.html",
    "registries": "registries.html",
}


@app.get("/img/{name}", include_in_schema=False)
async def image(name: str):
    f = WEB / "img" / name
    if "/" in name or ".." in name or not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return Response(f.read_bytes(), media_type="image/jpeg",
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
async def tools_index_slash():
    html_ = render.cached("tools-index", 1800, lambda: catalog.render_index(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


@app.get("/tools/{slug}", include_in_schema=False)
async def tools_category(slug: str):
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

def _wants_html(request: Request) -> bool:
    """Content negotiation, biased to JSON.

    A browser or crawler sends `text/html` and gets the page; anything else,
    including a bare request with no Accept header, gets JSON. Biasing to JSON
    keeps the published harness link and every existing API client working
    exactly as before.
    """
    a = (request.headers.get("accept") or "").lower()
    return "text/html" in a and "application/json" not in a


@app.get("/bench")
async def bench_endpoint(request: Request):
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
async def adoption_endpoint(request: Request):
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
async def badge_svg(publisher: str) -> Response:
    pub = publisher.strip().lower()
    # A hostname or reverse-DNS publisher id, nothing else. This is a public,
    # unauthenticated SVG generator; without the allowlist it is a text-echo
    # service wearing our domain.
    if not pub or len(pub) > 100 or not all(
            c.isalnum() or c in ".-_" for c in pub):
        return Response(status_code=404)
    svg = badge.render(db(), pub)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600",
                             # GitHub proxies images through Camo and honours
                             # cache headers; an hour keeps badge fetches from
                             # ever being load while staying fresh enough.
                             "ETag": f'W/"{hash(svg) & 0xffffffff:x}"'})


@app.get("/badge", include_in_schema=False)
@app.get("/badge/", include_in_schema=False)
async def badge_help():
    return JSONResponse({
        "what": "README badge showing your server's verified tool count",
        "url": f"{config.PUBLIC_BASE}/badge/<your-publisher-id>.svg",
        "publisher_id": "the publisher segment of your URN, or your domain",
        "markdown": badge.snippet("your.domain"),
        "note": ("the badge states what was observed: how many tools your server "
                 "returned to tools/list and whether the endpoint answers. It is "
                 "never a trust, safety or quality rating"),
    })


@app.get("/ard-publishers", include_in_schema=False)
@app.get("/ard-publishers/", include_in_schema=False)
async def publishers_index():
    html_ = render.cached("pubs-index", 1800,
                          lambda: catalog.render_publishers_index(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


@app.get("/ard-publishers/{host}", include_in_schema=False)
async def publisher_page(host: str):
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
async def publishers_moved():
    return RedirectResponse("/ard-publishers", status_code=301)


@app.get("/publishers/{host}", include_in_schema=False)
async def publisher_moved(host: str):
    return RedirectResponse(f"/ard-publishers/{host}", status_code=301)


@app.get("/feed.xml", include_in_schema=False)
async def feed():
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
    dom = str((body or {}).get("domain") or "").strip()
    host = (dom.replace("https://", "").replace("http://", "")
               .strip("/").split("/")[0].lower())
    # A hostname, strictly. `../etc/passwd` reduces to `..` after the path split,
    # which passed a naive "contains a dot" check and reached the fetcher.
    labels = host.split(".")
    if (not host or len(host) > 253 or len(labels) < 2
            or not all(l and l[0].isalnum() and l[-1].isalnum()
                       and all(c.isalnum() or c == "-" for c in l) for l in labels)
            or not labels[-1].isalpha() or len(labels[-1]) < 2):
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "a hostname is required, for example {\"domain\": \"example.com\"}"})

    conn = db()
    before = conn.execute("SELECT COUNT(*) FROM entries WHERE lower(publisher)=?",
                          (host,)).fetchone()[0]
    # Live fetch and ingest, both well-known paths, exactly as the crawler does.
    # A long maintenance job can hold the write lock; that is our problem, not the
    # submitter's, so it gets an honest 503 with a retry rather than a 500.
    try:
        got = await ingest.crawl_domains(conn, [host], concurrency=2, skip_seen_hours=0)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return JSONResponse(status_code=503, headers={"Retry-After": "60"}, content={
                "status": "busy",
                "domain": host,
                "detail": ("the index is mid-maintenance and cannot accept a write right "
                           "now. Your domain was not indexed; try again in a minute."),
            })
        raise
    after = conn.execute("SELECT COUNT(*) FROM entries WHERE lower(publisher)=?",
                         (host,)).fetchone()[0]
    row = conn.execute("SELECT manifest_path FROM crawl_seen WHERE domain=?",
                       (host,)).fetchone()
    path = row["manifest_path"] if row else None
    catalog.invalidate_publishers()
    render.invalidate()

    if not path and after == 0:
        return JSONResponse(status_code=404, content={
            "status": "no_manifest",
            "domain": host,
            "checked": ingest.PATHS,
            "detail": ("no ARD manifest found at either well-known path. Serve one and "
                       "submit again; see " + config.PUBLIC_BASE + "/publish"),
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
  <h1>Submit your domain to the ARD index</h1>
  <p class="lede">If you serve an Agentic Resource Discovery manifest, this indexes it now
  rather than whenever a crawler reaches you. We fetch it live from your domain, so there is
  nothing to fill in beyond the hostname. No account, no allowlist, no charge, no paid
  ranking.</p>
</div>

<div class="note" style="max-width:62ch">
  <form id="f" onsubmit="return go(event)" style="display:flex;gap:8px;flex-wrap:wrap">
    <input id="d" placeholder="example.com" aria-label="Your domain"
           style="flex:1;min-width:220px;background:var(--panel2);border:1px solid var(--line2);
                  border-radius:var(--r);color:var(--fg);padding:10px 12px;font-family:var(--mono)">
    <button class="btn btn--w" type="submit">Index my domain</button>
  </form>
  <pre id="o" style="margin-top:14px;display:none;white-space:pre-wrap"></pre>
</div>

<h2 style="margin-top:34px;font-size:20px">What happens when you submit</h2>
<ol class="lede">
  <li>We request both well-known manifest paths on your domain, live.</li>
  <li>Whatever parses as a manifest is indexed immediately, with your declared types,
      identifiers and representative queries preserved exactly as written.</li>
  <li>Your endpoints are probed for reachability, and any MCP server among them is asked
      for its tool list, so the index records what your servers actually expose.</li>
  <li>You get a page at <code>{B}/ard-publishers/&lt;your-domain&gt;</code> and become
      searchable through <code>/search</code> and the MCP endpoint.</li>
</ol>

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
        "Submit your domain to the ARD index",
        "Index your Agentic Resource Discovery manifest now. We fetch it live from your "
        "domain: no account, no allowlist, no charge.",
        body, f"{B}/submit"))


@app.get("/published", include_in_schema=False)
async def published_page():
    html_ = render.cached("published", 3600, lambda: catalog.render_published(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/blog", include_in_schema=False)
@app.get("/blog/", include_in_schema=False)
async def blog_index():
    f = WEB / "blog" / "index.html"
    if not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=1800"})


@app.get("/blog/{slug}", include_in_schema=False)
async def blog_post(slug: str):
    f = WEB / "blog" / f"{slug}.html"
    if "/" in slug or ".." in slug or not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=1800"})


@app.get("/{slug}", include_in_schema=False)
async def guide_page(slug: str):
    name = GUIDES.get(slug)
    if not name:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    f = WEB / "pages" / name
    if not f.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=1800"})
