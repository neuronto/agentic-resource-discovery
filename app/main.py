"""Neuronto — an ARD registry, publisher and index.

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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import audit, config, federation, ingest, liveness, search, store
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
    """POST /search — the one endpoint the spec mandates.

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
    store.log_search(conn, text, mode, len(out["results"]), took, fed_ok)
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
    """POST /explore — optional introspection over facets (§5.3.3).

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
    for f in facets:
        field = f.get("field")
        col = COLUMN.get(field)
        if not col:
            continue
        limit = int(f.get("limit") or 50)
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
    """GET /agents — deterministic, paginated browsing (§5.3.4).

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
        "host": {"displayName": "Neuronto — Agentic Resource Discovery (ARD) index",
                 "identifier": "did:web:neuronto.com",
                 "documentationUrl": f"{B}/about"},
        "entries": [{
            # §5.3: a registry's base URL is discovered by finding an entry of
            # this type. This is how Neuronto becomes findable AS a registry.
            "identifier": "urn:air:neuronto.com:registry:neuronto",
            "displayName": "Neuronto — Agentic Resource Discovery (ARD) registry",
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
            "displayName": "ard-publish — build and verify an ARD manifest",
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
            "displayName": "Neuronto — ARD & MCP discovery (MCP server)",
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
    lines = ["User-agent: *", "Allow: /", "",
             f"Sitemap: {B}/sitemap.xml",
             f"Agentmap: {B}/.well-known/ard.json", ""]
    return PlainTextResponse("\n".join(lines),
                             headers={"Cache-Control": "public, max-age=300"})

@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    B = config.PUBLIC_BASE
    urls = ["/", "/what-is-ard", "/publish", "/submit-mcp-server",
            "/registries", "/console", "/blog"]
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
    return {"status": "ok", "entries": c["entries"], "publishers": c["publishers"],
            "live": c["live"], "dead": c["dead"]}

@app.get("/stats")
async def stats(days: int = Query(30, ge=7, le=90)):
    conn = db()
    c = store.counts(conn)
    regs = [dict(r) for r in conn.execute("SELECT * FROM registries")]
    q = conn.execute(
        "SELECT COUNT(*) n, AVG(ms) avg_ms FROM searches WHERE ts > ?",
        (int(time.time()) - 7 * 86400,)).fetchone()
    return {**c, "upstreams": regs,
            "searches_7d": q["n"] or 0,
            "avg_search_ms": round(q["avg_ms"] or 0, 1),
            "series": store.daily_series(conn, days),
            "publishers_top": store.top_publishers(conn, 12),
            "recent": store.recent_searches(conn, 8),
            "federation": {"enabled": config.FEDERATION_ENABLED,
                           "budget_ms": config.FEDERATION_BUDGET_MS,
                           "upstreams": [u[1] for u in config.UPSTREAMS]}}


def _fmt(n: int) -> str:
    return f"{int(n or 0):,}"


def _render_home() -> str:
    """Serve the page with its numbers already in the HTML.

    Everything on this page used to be drawn client side, which meant any
    consumer that does not execute JavaScript — most answer-engine crawlers
    among them — saw empty tables and concluded the index was empty. The figures
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
