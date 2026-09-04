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
import contextvars
import json
import os
import re
import secrets
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

from . import (a2a, adoption, audit, badge, bench, catalog, config, embed, events,
               federation, ingest, liveness, limits, publisher, reliability,
               render, resolve, safety, search, state, store, submissions, tools_index)
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


def db() -> sqlite3.Connection:
    """A connection for this thread. See store.tls_conn for why not one shared."""
    return store.tls_conn()


# The expensive pages, with the TTL each is served at. Warmed on startup and
# refreshed on a timer, so the first visitor after a deploy is never the one who
# pays for a 22 second build.
def _warmable():
    return [
        ("home",        600,  _render_home),
        ("tools-index", 1800, lambda: catalog.render_index(db())),
        ("pubs-index",  1800, lambda: catalog.render_publishers_index(db())),
        ("published",   3600, lambda: catalog.render_published(db())),
        ("adoption-html", 3600, lambda: catalog.render_adoption(adoption.report(db()))),
    ] + [(f"cat-{slug}", 1800, (lambda sl: lambda: catalog.render_category(db(), sl))(slug))
         # Largest categories first: they are the slowest to build and the most
         # likely to be opened, so they should be the first to stop being stale.
         for slug, _n in sorted(catalog.published(db()).items(),
                                key=lambda kv: -kv[1])]


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
        # The category map first: every category page and the sitemap read it,
        # and it is the single most expensive thing the request path can touch.
        try:
            if render.warm_value("published-map", 1800,
                                 lambda: catalog._compute_published(db())):
                built += 1
                catalog.published(db(), refresh=False)
        except Exception:
            pass
        for key, ttl, build in _warmable():
            try:
                if render.warm(key, ttl, build):
                    built += 1
                    time.sleep(0.12)
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

    async def _retrier():
        # Pending submissions come back here until they index or give up.
        # Both workers run this; the row claim in `submissions.due` makes
        # that safe, and a worker dying mid-attempt only delays the row.
        await asyncio.sleep(20)
        while True:
            try:
                await retry_due_submissions()
            except Exception:
                pass
            await asyncio.sleep(SUBMIT_RETRY_EVERY_S)

    asyncio.ensure_future(_retrier())


# Set here and nowhere else. The policy allows exactly what the pages use:
# their own inline scripts and styles, Google Fonts, and the CDN the API
# documentation page loads its viewer from. Nothing may frame these pages.
# Set here and nowhere else. It allows exactly what these pages actually load:
# their own inline scripts and styles, Google Fonts, the CDN the API reference
# loads its viewer from, and the tag manager running at the edge together with
# the analytics tool it injects. The first version omitted the last of those and
# silently broke it; a policy that blocks a tool the site is meant to be using
# is a bug in the policy.
_CSP = ("default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
        "https://static.cloudflareinsights.com https://*.hotjar.com https://*.hotjar.io; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net "
        "https://*.hotjar.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://*.hotjar.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://*.hotjar.com https://*.hotjar.io wss://*.hotjar.com "
        "https://static.cloudflareinsights.com https://cloudflareinsights.com; "
        "frame-src https://*.hotjar.com; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
_SECURITY = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": _CSP,
}


@app.middleware("http")
async def _timing(request: Request, call_next):
    """Latency is the product claim, so every response carries its own measurement."""
    t0 = time.perf_counter()
    resp = await call_next(request)
    resp.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    for k, v in _SECURITY.items():
        resp.headers.setdefault(k, v)
    return resp


@app.exception_handler(RecursionError)
async def _too_deep(request: Request, exc: RecursionError) -> JSONResponse:
    # A body nested thousands of levels deep exhausts the parser's stack. That
    # is the caller's problem, and it was being reported as ours.
    return JSONResponse(status_code=400, content={"error": "invalid_request",
                                                  "detail": "request body is nested too deeply"})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Every unhandled error is answered, recorded and alerted on.

    Three distinct 500s went to the journal on 2026-09-01 and nobody read
    them; two public pages were down for hours. The traceback still goes to
    the journal (the server middleware re-raises after this returns), but the
    fact now also goes to the event sink, which is where someone is looking.
    """
    ref = secrets.token_hex(4)
    try:
        events.emit("error", a=request.url.path[:80], b=type(exc).__name__,
                    ok=False, detail=f"{ref} {str(exc)[:140]}")
    except Exception:
        pass
    return JSONResponse(status_code=500, headers={"Cache-Control": "no-store"}, content={
        "error": "internal",
        "ref": ref,
        "detail": ("this is a fault on our side, not in your request, and it has been "
                   "recorded. Quote the ref if you write to us."),
    })


# ─────────────────────────── Registry REST API (§5.3) ───────────────────────

_SEARCH_SCHEMA = {
    "requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "query": {"type": "object", "required": ["text"],
                      "properties": {"text": {"type": "string"}}},
            "limit": {"type": "integer", "default": 10},
            "federation": {"type": "string", "enum": ["auto", "none", "referrals"], "default": "auto",
                           "description": "auto (the spec default) fans out to every public ARD registry and fuses; none answers from this index alone in ~60 ms."}},
        "required": ["query"],
        "example": {"query": {"text": "charge a credit card"}, "limit": 10}}}}}}


@app.post("/search", openapi_extra=_SEARCH_SCHEMA)
async def search_endpoint(body: dict, request: Request) -> JSONResponse:
    """POST /search - the one endpoint the spec mandates.

    Every result carries `identifier`, `score` (0-100) and `source`, which are
    the three fields the conformance tool requires of a SearchResultItem.
    """
    request_key = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    q = body.get("query") or {}
    # `query` as a bare string is the commonest thing a hand-written client
    # sends, and it used to raise AttributeError and answer 500. A 500 tells a
    # caller our service is broken, so they retry rather than fix the payload;
    # the same confusion in the transport layer cost this project two days. Take
    # the obvious meaning, and say what the spec wanted, rather than crashing.
    if isinstance(q, str):
        q = {"text": q}
    elif not isinstance(q, dict):
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "query must be an object with a `text` field (spec 5.3.2), "
                      "for example {\"query\": {\"text\": \"read a pdf\"}}."})
    text = (q.get("text") or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "query.text is required for Search (spec 5.3.2)."})
    if len(text) > 2000:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "query.text is longer than 2000 characters. A query is a request "
                      "in a sentence or two, not a document."})
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
    # The last sync read on the event loop in this handler. search.search
    # already threads its local leg; log_search is a queue. This one is a
    # single indexed lookup, but a lookup that waits on a writer lock stalls
    # every request on the worker, so it goes to a thread with its own conn.
    owner = await asyncio.to_thread(
        lambda: publisher.domain_for_key(store.tls_conn(), auth))
    out = await search.search(conn, text, flt, page_size, mode, owner_domain=owner)
    took = int((time.perf_counter() - t0) * 1000)
    fed_ok = sum(1 for f in (out.get("_federated") or []) if f.get("ok"))
    # The homepage asks twice for one search: this index first, because it
    # answers in about 200ms and gives the reader something to look at, then
    # every registry, which is bounded by the slowest of them. The first call
    # is a preview of the second, not a second search, so it is not counted and
    # records no impressions. A header rather than a body field: the request
    # schema is the spec's, and a client hint has no business in it. Suppressing
    # it only affects our own numbers, so there is nothing to abuse here.
    preview = request.headers.get("x-neuronto-preview") == "1"
    if not preview:
        store.log_search(conn, text, mode, len(out["results"]), took, fed_ok,
                         entries=out["results"], authenticated=bool(owner),
                         probe=_is_probe(request))
    cleaned = search.clean(out["results"])
    payload: dict[str, Any] = {"results": cleaned,
                               "queryMatch": search.query_match(text, cleaned)}
    if not preview:
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
                            # Say when an answer came from cache rather than
                            # from the registry just now. A federating index
                            # that presents a stored answer as a live one is
                            # making a claim it cannot support.
                            **({"cached": True, "age_s": f.get("age_s", 0)}
                               if f.get("cached") else {}),
                            **({"error": f["error"]} if f.get("error") else {})}
                           for f in fed],
        }
    return JSONResponse(payload)


# Paths that real callers guessed in the outside-failure log, each pointed at
# the thing they were looking for. Aliases, not new surfaces: every target here
# already existed. 301 for documents; 307/308 where the method must survive.
#
#   an agent that read /openapi.json then tried three other conventional names
#   for it, and /api/about after finding /about; a client that POSTed /api/mcp
#   after succeeding at /mcp; a curl user who guessed /api/ard/v1/search before
#   finding POST /search; and a person in Chrome, twice in twenty minutes,
#   asking for /pricing, /login and /signup on a site with no accounts.
_ALIASES = {
    "/api/openapi.json": ("/openapi.json", 301),
    "/api/v1/openapi.json": ("/openapi.json", 301),
    "/swagger.json": ("/openapi.json", 301),
    "/api/about": ("/about", 301),
    "/.well-known/mcp.json": ("/.well-known/mcp/server-card.json", 301),
    "/mcp/.well-known/mcp": ("/.well-known/mcp/server-card.json", 301),
    "/api/ard/v1/search": ("/search", 308),
    "/login": ("/connect", 302),
    "/signup": ("/connect", 302),
    "/register": ("/connect", 302),
}
for _src, (_dst, _code) in _ALIASES.items():
    def _mk(dst=_dst, code=_code):
        def _alias() -> RedirectResponse:
            return RedirectResponse(dst, status_code=code)
        return _alias
    app.add_api_route(_src, _mk(), methods=["GET"], include_in_schema=False)


@app.post("/api/mcp", include_in_schema=False)
def api_mcp_alias() -> RedirectResponse:
    """A client that had just succeeded at /mcp tried /api/mcp next. 307 keeps
    the POST and the body; the /api/{host} vendor route had been answering 405."""
    return RedirectResponse("/mcp", status_code=307)


@app.get("/search", include_in_schema=False)
@app.delete("/search", include_in_schema=False)
@app.put("/search", include_in_schema=False)
def search_wrong_verb(request: Request) -> Response:
    """405, never 404. Same repair as GET /mcp, for the same reason.

    A person in a browser is the exception: the outside-failure monitor saw a
    visitor click through fourteen pages of the site and then get a JSON 405
    at /search. They wanted the search box, which is on the homepage.

    The outside-failure monitor's first run found four sessions that GET this
    route and were told it does not exist. All four had succeeded at something
    else first, so they were real callers, not scanners. A 404 tells a client
    the endpoint is gone; a 405 with Allow tells it the verb was wrong, which
    is the truth and is fixable in one line on their side.
    """
    if request.method == "GET" and _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return JSONResponse(status_code=405, headers={"Allow": "POST", "Cache-Control": "no-store"},
                        content={"error": "method_not_allowed",
                                 "detail": "search is POST. Send {\"query\": {\"text\": \"...\"}} "
                                           "as JSON. Full contract: /api-docs"})


_EXPLORE_SCHEMA = {
    "requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "query": {"type": "object", "properties": {"text": {"type": "string"}},
                      "description": "Optional. Restricts the counts to entries matching this text."},
            "resultType": {"type": "object", "properties": {"facets": {
                "type": "array", "items": {"oneOf": [
                    {"type": "string"},
                    {"type": "object", "properties": {"field": {"type": "string"},
                                                      "limit": {"type": "integer"}},
                     "required": ["field"]}]},
                "description": "Which facets to count. Omit to receive every supported facet."}}}},
        "example": {"query": {"text": "payments"},
                    "resultType": {"facets": [{"field": "type", "limit": 20}, "publisher"]}}}}}}}


@app.post("/explore", openapi_extra=_EXPLORE_SCHEMA)
def explore_endpoint(body: dict) -> JSONResponse:
    """POST /explore - optional introspection over facets (§5.3.3).

    Explore does not federate; it is scoped to this registry's own index.

    A bare body, or one without `resultType.facets`, returns every supported
    facet rather than a 400. The outside-failure monitor watched one agent read
    /openapi.json, find a request schema that said only "object", and then send
    seven plausible bodies here and be refused each time. Introspection that has
    to be introspected first is a contradiction; asked for nothing specific, it
    now answers with everything it has.
    """
    rt = body.get("resultType") or {}
    facets = rt.get("facets") or body.get("facets")
    if not facets:
        facets = ["type", "type_family", "publisher", "source", "tags", "capabilities"]
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
            # What this resource is genuinely a good ANSWER to, which is not the
            # same as what gets searched most. "find an MCP server for a task"
            # is high demand and the wrong claim: the right answer to it is an
            # actual server, not a registry. Our own /audit caught us returning
            # ourselves for 1 of 5 of these when they were written for demand,
            # while two other registries returned us for 4 of 5. A representative
            # query is a promise about fitness, not a keyword slot.
            "representativeQueries": [
                "which ARD registries exist",
                "a registry that federates other MCP registries",
                "search every MCP and ARD registry at once",
                "where can I search for MCP servers, skills and agents",
                "how do AI agents discover MCP servers",
            ],
            "tags": ["registry", "discovery", "ard", "federation", "index", "mcp",
                     "mcp-server", "search", "tool-discovery", "agents"],
            "trustManifest": {"identity": "did:web:neuronto.com"},
        }, {
            "identifier": "urn:air:neuronto.com:tool:ard-publish",
            "displayName": "ard-publish - build and verify an ARD manifest",
            "type": "application/ai-catalog+json",
            "url": f"{B}/publish",
            "description": ("Open source tool that builds, validates and verifies an Agentic "
                            "Resource Discovery manifest, then checks which registries actually "
                            "return your domain. pip install ard-publish."),
            # Five, not six: the conformance tool recommends 2 to 5 per entry for
            # vector index embedding, and six earned a warning we had not had before.
            "representativeQueries": [
                "how to make my MCP server discoverable",
                "how do I publish an ARD manifest",
                "make my API discoverable by AI agents",
                "which registries actually return my domain",
                "validate my ai-catalog.json",
            ],
            "tags": ["ard", "sdk", "publishing", "validation", "python", "open-source",
                     "mcp-server", "discoverability", "manifest"],
            "trustManifest": {"identity": "did:web:neuronto.com"},
        }, {
            "identifier": "urn:air:neuronto.com:mcp:discovery",
            "displayName": "Neuronto ARD Registry: ARD and MCP discovery (MCP server)",
            "type": "application/mcp-server-card+json",
            "url": f"{B}/.well-known/mcp/server-card.json",
            "description": ("The same federated discovery as an MCP server, so an agent can "
                            "search every ARD registry from the tool interface it already speaks."),
            "capabilities": ["find_resource", "registry_stats"],
            # This entry IS a callable tool, so its queries describe the tool a
            # client is looking for, not the resources it will go and find.
            "representativeQueries": [
                "an MCP server that finds other MCP servers",
                "a tool to search every ARD registry at once",
                "give my agent the ability to discover tools at runtime",
                "search the agent-readable web from one MCP tool",
            ],
            "tags": ["mcp", "discovery", "search", "ard", "mcp-server",
                     "tool-discovery", "registry", "agents"],
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
            "/badge", "/connect", "/privacy", "/state-of-mcp", "/console", "/blog",
            # The capability pages and the two measurement pages. These carry
            # the verified tool surface, which exists on no other site, so they
            # are the pages most worth discovering.
            "/tools/", "/bench", "/adoption", "/submit", "/published"]
    urls += [f"/connect/{s_}" for s_ in catalog.CLIENTS] + ["/connect/frameworks"]
    urls += [f"/tools/{slug}" for slug in catalog.published(db())]
    urls += ["/api/"] + [f"/api/{v['host']}" for v in catalog.vendor_hosts(db())]
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

@app.get("/agents.md", include_in_schema=False)
def agents_md():
    """Operating instructions for an agent that has landed here.

    llms.txt describes what this site *is*, at length, for a reader assembling
    context. This is the other question: an agent is already here and wants to
    know what to call and how. Short on purpose. It is also the third of the
    three artifacts every preflight and audit tool in this ecosystem checks
    (ard.json, llms.txt, agents.md), and we were failing that check on our own
    domain while telling other people to pass it.
    """
    B = config.PUBLIC_BASE
    c = store.counts(db())
    return PlainTextResponse(f"""# Neuronto ARD Registry, for agents

Neuronto is an Agentic Resource Discovery (ARD) registry and index. Ask it what
can do a task and it answers from its own index and every other public ARD
registry at once, fused into one ranking.

{c["entries"]} resources from {c["publishers"]} publishers. No key, no signup.

## Call it

Three interfaces, same index. Pick whichever you already speak.

- MCP (streamable HTTP): POST {B}/mcp
  Tools: find_resource, find_tool, registry_stats, publish_resource.
  GET on this endpoint answers 405: there is no server-initiated stream,
  every tool answers inside the request that asked.
- A2A (JSON-RPC): POST {B}/a2a, card at {B}/.well-known/agent-card.json
  Method message/send, returns a Message. No tasks, no streaming.
- REST (ARD v0.91): POST {B}/search with {{"query": {{"text": "..."}}}}

## Publish yourself

- Build a manifest from what your domain already serves: POST {B}/manifest/build
- Get indexed: POST {B}/submit with an MCP endpoint or a domain.
  Verified, not trusted: your endpoint has to complete a handshake, or your
  domain has to serve a manifest that parses. A busy index answers 202 with a
  queue id and retries on its own, so a failure costs you nothing.
- Or pip install ard-publish and run it yourself.

## Rules of the road

- Anonymous: 60 requests an hour. With a domain key: 300. Prove a domain with
  a DNS TXT record at {B}/claim to get one.
- `score` is semantic relevance only. It is not a trust, safety or quality
  rating, and must not be presented to a user as one.
- `verified` reports what we fetched: whether the endpoint answered and what
  its own tools/list returned. Nothing here is inferred from a name.
- Results carry `found_in` so you can tell our index from a federated one.

## Full documentation

{B}/api-docs, {B}/llms.txt, {B}/.well-known/ard.json
""", headers={"Cache-Control": "public, max-age=900"})


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

## How to be listed (no key, no signup, no allowlist)

Serving a manifest is not being indexed: a registry has to fetch it. Registries
crawl domain lists they chose, so a domain outside one is invisible to it
indefinitely. To be indexed here, send the domain or the endpoint.

- POST https://neuronto.com/submit   {{"domain":"example.com"}}
  Both well-known paths are fetched from that domain and whatever parses is
  indexed in the same request.
- POST https://neuronto.com/submit   {{"endpoint":"https://example.com/mcp"}}
  For an MCP server with no manifest at all: the endpoint must complete an
  initialize handshake and its own tools/list is read back.
- MCP tool publish_resource on https://neuronto.com/mcp does the same thing
  from inside a conversation.
- A submission that does not verify at that moment is not dropped: the answer is
  202 with status "pending", a submission id and the evidence of what the endpoint
  returned, and it is retried automatically for about two and a half days.
  GET https://neuronto.com/submit/status/<id> shows where it stands. One call is
  enough; there is no need to resubmit.
- POST https://neuronto.com/audit    {{"domain":"example.com"}}
  Reports which registries return you, and whether this one can index you now.

Nothing is taken on the submitter's word: everything listed was fetched from
the domain that claims it. Publishing guide: https://neuronto.com/publish

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
            # This worker's view; each of the four learns independently.
            "circuits": federation.breaker_snapshot(),
            "federation_budget_ms": config.FEDERATION_BUDGET_MS,
            "series": store.daily_series(conn, days),
            "manifests": store.manifest_adoption(conn),
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
def home():
    # Built from live counts, so it is cached like the other aggregate pages
    # rather than rendered per request; crawlers reach the origin for it.
    return HTMLResponse(render.cached("home", 600, _render_home), headers=PAGE_CACHE)

@app.get("/about", include_in_schema=False)
@app.get("/registry", include_in_schema=False)
def _pages(): return home()


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
    tok = _PROBE.set(_is_probe(request))
    try:
        status, payload = await handle(db(), body)
    finally:
        _PROBE.reset(tok)
    if (body or {}).get("method") == "tools/call":
        events.emit("mcp_call", a=str((body.get("params") or {}).get("name") or "?")[:60],
                    ok=not ((payload or {}).get("result") or {}).get("isError", False))
    if payload is None:
        return Response(status_code=status)
    return JSONResponse(payload, status_code=status)


# The transport lets a client open an SSE stream with GET so the server can push
# messages it did not solicit. We have nothing to push: every tool answers inside
# the POST that asked. The spec is explicit that a server which does not offer
# that stream MUST answer 405, and 405 is the answer the client SDKs treat as
# "fine, carry on". We were answering 404, which means "no such endpoint", and
# 88 requests from `node` clients took it at its word before this was noticed.
_MCP_NO_STREAM = {
    "jsonrpc": "2.0", "id": None,
    "error": {"code": -32000,
              "message": "this endpoint does not offer a server-initiated SSE stream",
              "data": {"use": "POST /mcp with a JSON-RPC message",
                       "reason": "every tool answers inside the request that asked"}}}


@app.get("/mcp", include_in_schema=False)
@app.delete("/mcp", include_in_schema=False)
def mcp_no_stream() -> Response:
    return JSONResponse(_MCP_NO_STREAM, status_code=405,
                        headers={"Allow": "POST", "Cache-Control": "no-store"})


# ─────────────────────────────── A2A binding ────────────────────────────────
# The other half of the discovery world asks for an Agent Card by convention and
# reads a 404 as "not an agent". Trust and reputation crawlers asked 108 times
# before this existed. The card is only honest because this endpoint answers.

@app.get("/sse", include_in_schema=False)
@app.post("/sse", include_in_schema=False)
@app.get("/mcp/sse", include_in_schema=False)
@app.post("/mcp/sse", include_in_schema=False)
def sse_transport_not_served() -> Response:
    """Clients on the older HTTP+SSE transport look here. We serve streamable
    HTTP at /mcp only, which is the current transport and the one every SDK
    tries first. Tell them exactly that, rather than a bare 404 that reads as
    "no MCP here at all". The monitor found real python clients doing this."""
    return JSONResponse(status_code=404, headers={"Cache-Control": "no-store"}, content={
        "error": "transport_not_served",
        "detail": ("this registry speaks MCP over streamable HTTP, not the older HTTP+SSE "
                   "transport. Connect to /mcp with POST (JSON-RPC) and Accept: "
                   "application/json, text/event-stream. Setup for every client: /connect"),
        "endpoint": f"{config.PUBLIC_BASE}/mcp", "transport": "streamable-http"})


@app.get("/.well-known/agent-card.json", include_in_schema=False)
@app.get("/.well-known/agent.json", include_in_schema=False)
def agent_card():
    return JSONResponse(a2a.card(), headers=CACHE)


@app.post("/a2a", include_in_schema=False)
async def a2a_endpoint(body: dict, request: Request) -> Response:
    tok = _PROBE.set(_is_probe(request))
    try:
        status, payload = await a2a.handle(db(), body)
    finally:
        _PROBE.reset(tok)
    if (body or {}).get("method") in ("message/send", "SendMessage"):
        events.emit("a2a_call", a="message/send",
                    ok=not (payload or {}).get("error"))
    if payload is None:
        return Response(status_code=status)
    return JSONResponse(payload, status_code=status)


@app.get("/a2a", include_in_schema=False)
@app.delete("/a2a", include_in_schema=False)
def a2a_no_stream() -> Response:
    return JSONResponse({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32000,
                                   "message": "POST a JSON-RPC message/send here",
                                   "data": {"card": "/.well-known/agent-card.json"}}},
                        status_code=405,
                        headers={"Allow": "POST", "Cache-Control": "no-store"})


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
        man_ = report.get("_manifest") or None
        qs_ = audit.queries_for(man_, report["domain"])
        comp = audit.competition(conn, report["domain"], man_, queries=qs_)
        report["competition"] = comp
        # Our own row in `coverage` was a presence flag (0 or 1) sitting in a
        # column where every other registry reports how many of the same queries
        # returned the domain. That understated the home index against every
        # upstream, in the one report a publisher reads to decide who is worth
        # publishing to. Measured on the same queries, it is comparable now.
        appears = int((comp.get("summary") or {}).get("you_appear_in") or 0)
        for row in report.get("coverage") or []:
            if row.get("registry") == "Neuronto":
                row["queries"] = int((comp.get("summary") or {}).get("tested") or len(qs_))
                row["returned_for"] = appears
                row["indexed"] = bool(hits)
                break
        report["coverage_note"] = (
            "Every row is scored over the same five queries, taken from this "
            "domain's own representativeQueries. The matching rule is not "
            "identical on both sides and the difference favours the upstreams: "
            "an upstream counts as returning you if your domain appears anywhere "
            "in its response, which includes a mention inside another entry, "
            "while this index counts only your own entry appearing in the top "
            "ten. Read a low number here against a high one there as a question "
            "to check rather than a verdict.")
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
    # The audit fetched and validated the manifest, which is everything a
    # submission does. Say so in a form a client can act on, rather than making
    # the reader re-derive it from prose. Never indexes as a side effect: being
    # audited is not consent to be listed.
    man = report.get("_manifest")
    if man and not hits:
        report["indexable"] = {
            "ready": True,
            "entries": len(man.get("entries") or []),
            "how": {"http": f"POST {config.PUBLIC_BASE}/submit "
                            f'{{"domain": "{report["domain"]}"}}',
                    "cli": f"ard-publish submit {report['domain']}",
                    "mcp": "publish_resource"},
            "note": ("your manifest parsed here, so indexing it needs no further work "
                     "from you. It is fetched from your domain again at that moment, so "
                     "nothing is taken on this audit's word."),
        }
    report.pop("_manifest", None)
    events.emit("audit", a=report.get("domain"), n=(report.get("score") or {}).get("total"))
    return JSONResponse(report)


@app.post("/e", include_in_schema=False)
def client_event(body: dict, request: Request) -> Response:
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
        # The queue a submission sits in until it indexes. Published because
        # "we never drop a submission" is a claim, and a claim gets a number.
        "submissions": submissions.stats(),
        "retrieval": {"dense": embed.status(conn)["configured"],
                      "dense_coverage": embed.status(conn)["coverage"]},
        "note": ("everything this project publishes is derived from these numbers. "
                 "No client identifiers are collected, so none appear here."),
    }, headers={"Cache-Control": "public, max-age=300"})


@app.get("/demand")
def demand_endpoint(domain: str = Query(..., min_length=3),
                          days: int = Query(30, ge=1, le=365),
                          limit: int = Query(25, ge=1, le=200),
                          include_probe: int = Query(0, ge=0, le=1)) -> JSONResponse:
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
    d = store.demand_for(db(), host, days, limit, include_probe=bool(include_probe))
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
    # Off the loop, own connection. On 2026-09-01 this INSERT ran on the
    # request connection and met the crawl's lock twice in one minute: two
    # 500s for a caller who had done nothing wrong. The manifest itself is
    # computed already, so a busy index costs only the hosted copy.
    hosted = True
    try:
        await _index_write(lambda c: c.execute(
            """INSERT INTO stats(k,v) VALUES(?,?)
               ON CONFLICT(k) DO UPDATE SET v=excluded.v""",
            (f"manifest:{host}", json.dumps(man, ensure_ascii=False))))
    except Exception as e:
        if not _busy(e):
            raise
        hosted = False
    return JSONResponse({
        "domain": host,
        "entries": len(man["entries"]),
        "evidence": [{"entry": e.get("displayName"), "because": e.get("_evidence")}
                     for e in found],
        "manifest": man,
        "hosted_at": f"{config.PUBLIC_BASE}/m/{host}.json" if hosted else None,
        **({} if hosted else {"hosted_note": (
            "the index was busy and the hosted copy was not stored; the manifest "
            "above is complete, save it or call again in 30 seconds")}),
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
    try:
        key = await _index_write(lambda c: publisher.issue_key(c, host))
    except Exception as e:
        if not _busy(e):
            raise
        return _busy_response("your key", domain=host, verified=True,
                              note="the DNS proof is verified; no change needed on your side")
    # Ask for the badge here, at the one moment it is unambiguously earned. This
    # response used to hand back a key, a scope note and a curl line, and stop.
    # Our first verified third-party publisher published a DNS TXT record to
    # prove they owned their domain, which is close to the highest-friction
    # thing anyone can be asked for short of payment, and we never asked them
    # for an img tag. The badge is also the only distribution mechanism in this
    # ecosystem with evidence behind it, so not asking was the expensive half.
    return JSONResponse({
        "domain": host, "verified": True, "api_key": key,
        "grants": [f"read and write private entries for {host}",
                   "nothing else; the key is scoped to this domain alone"],
        "usage": f"curl -H 'authorization: Bearer {key}' {config.PUBLIC_BASE}/search ...",
        "warning": "this key is shown once and is not recoverable",
        "badge": {
            **badge.snippet(host),
            "says": ("only what we observed: whether your endpoint answers and how "
                     "many tools it listed. It is not an endorsement or a rating, and "
                     "it changes on its own if your endpoint stops answering."),
            "links_to": ("your own page on this index, so it sends your reader "
                         "somewhere about you rather than to us"),
        },
    })


@app.post("/private/entries")
async def private_add(body: dict, request: Request) -> JSONResponse:
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
    try:
        k = await _index_write(lambda c: publisher.add_private(c, owner, ent))
    except Exception as e:
        if not _busy(e):
            raise
        return _busy_response("the entry", owner=owner)
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
async def private_delete(body: dict, request: Request) -> JSONResponse:
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
    try:
        gone = await _index_write(lambda c: store.delete_private(c, owner, ident))
    except Exception as e:
        if not _busy(e):
            raise
        return _busy_response("the deletion", owner=owner)
    events.emit("private_delete", a=owner, ok=gone)
    return JSONResponse({"status": "deleted" if gone else "not_found",
                         "owner": owner, "private_entries": store.private_count(conn, owner)},
                        status_code=200 if gone else 404)


# The commercial layer, if this deployment has one. It is a separate package that
# is never published: the open source project is a complete conformant registry
# and simply has no /doctor, /plan, /me or /pricing. Nothing in app/ imports it.
COMMERCIAL: list[str] = []
try:
    import commercial as _commercial
except ImportError:
    pass
else:
    try:
        COMMERCIAL = _commercial.register(app, {
            "db": db,
            "host_arg": _host_arg,
            "page_cache": PAGE_CACHE,
            "limited": _LIMITED,
        })
        print(f"commercial layer: {', '.join(COMMERCIAL)}", flush=True)
    except Exception as _e:                      # never take the registry down with it
        print(f"commercial layer failed to load: {type(_e).__name__}: {_e}", flush=True)


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
def tools_endpoint(body: dict) -> JSONResponse:
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
        return tools_endpoint({"query": {"text": q}, "limit": limit,
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


@app.get("/api", include_in_schema=False)
@app.get("/api/", include_in_schema=False)
def vendor_index():
    """Every API vendor with a page. Registered above the /{slug} catch-all so
    the bare /api is ours and not a guide lookup for a page called "api"."""
    html_ = render.cached("api-index", 1800, lambda: catalog.render_vendor_index(db()))
    return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=1800"})


@app.get("/api/{host}", include_in_schema=False)
def vendor_page(host: str):
    """One vendor's indexed API surface.

    The URL a stranger guessed five ways before this existed. Gated on
    `catalog.MIN_OPS` so it is a page about something, never a doorway, and it
    carries only what the vendor wrote or a probe observed.
    """
    h = _host_arg(host)
    if not h or not catalog._vendor_ok(db(), h):
        return JSONResponse(status_code=404, content={
            "error": "not_found",
            "detail": f"no API page for {host!r}. Pages exist for vendors with at least "
                      f"{catalog.MIN_OPS} documented operations indexed; the list is at /api/. "
                      "To search every indexed operation: POST /search."})
    html_ = render.cached(f"api-{h}", 1800, lambda: catalog.render_vendor(db(), h))
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

@app.get("/ard-adoption", include_in_schema=False)
def adoption_alias():
    # Guessed by an assistant walking /ard-publishers and /ard-registries.
    # A model that guesses a pattern once will guess it again.
    return Response(status_code=301, headers={"Location": "/adoption"})


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


@app.get("/connect/{slug}", include_in_schema=False)
def connect_client_page(slug: str, request: Request):
    """One page per client, because that is the query people actually type."""
    # Handled inside the slug route rather than as its own path, so it cannot
    # depend on which route FastAPI matches first.
    if slug == "frameworks":
        if _wants_html(request):
            html_ = render.cached("connect-frameworks-html", 3600,
                                  catalog.render_frameworks_page)
            return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600",
                                                "Vary": "Accept"})
        B = config.PUBLIC_BASE
        return JSONResponse({
            "endpoint": f"{B}/mcp",
            "verified_snippets_for": ["langchain", "openai-agents-sdk", "vercel-ai-sdk"],
            "also_speak_mcp": ["llamaindex", "crewai", "mastra", "pydantic-ai",
                               "google-adk", "microsoft-agent-framework", "strands"],
            "without_mcp": {"npm": "neuronto", "rest": f"{B}/search"},
            "html": f"{B}/connect/frameworks",
        }, headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept"})
    if slug not in catalog.CLIENTS:
        return Response(status_code=404)
    if _wants_html(request):
        html_ = render.cached(f"connect-{slug}-html", 3600,
                              lambda: catalog.render_client_page(slug))
        return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600",
                                            "Vary": "Accept"})
    c = catalog.CLIENTS[slug]
    B = config.PUBLIC_BASE
    return JSONResponse({
        "client": c["name"], "kind": c["kind"],
        "endpoint": f"{B}/mcp",
        "config_location": c["label"],
        "docs": c["doc"],
        "all_clients": f"{B}/connect",
    }, headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept"})


@app.get("/state-of-mcp")
def state_of_mcp(request: Request):
    """What share of the agentic web actually answers, measured not asserted.

    Published because nobody else can: a directory knows what it lists, and only
    something that probes knows what responds. JSON to a machine, HTML to a
    browser, and the limitations travel inside the payload rather than in a
    footnote, so a reader who quotes the number also gets its caveats.
    """
    rep = render.cached("state-of-mcp", 900, lambda: state.report(db()))
    if _wants_html(request):
        html_ = render.cached("state-of-mcp-html", 900,
                              lambda: catalog.render_state_page(rep))
        return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=900",
                                            "Vary": "Accept"})
    return JSONResponse(rep, headers={"Cache-Control": "public, max-age=900",
                                      "Vary": "Accept"})


@app.get("/reliability")
def reliability_report(request: Request, entry: str = Query("", max_length=400)):
    """How reliably endpoints answer, over time rather than at one instant.

    `/state-of-mcp` says what share of the index answers right now. This says
    what share of *probes* an endpoint has answered, which is the question a
    consumer actually has, and it is only answerable by something that has been
    probing on a timer and counting.

    Rates are withheld below a minimum number of probes. A single successful
    probe is not 100% uptime, and publishing it as one would be the most quotable
    wrong number on the site.
    """
    if entry:
        r = reliability.for_entry(db(), entry)
        if r is None:
            return JSONResponse(status_code=404, content={
                "error": "not_found",
                "detail": "no entry with that key. Keys come from /search results."})
        return JSONResponse({"entry": entry, **r})
    rep = render.cached("reliability", 900, lambda: reliability.corpus(db()))
    return JSONResponse(rep, headers={"Cache-Control": "public, max-age=900"})


@app.get("/tool-safety")
def tool_safety(request: Request, entry: str = Query("", max_length=400)):
    """What tool descriptions in the index tell the model to do.

    A tool description is text that enters the model's context and is read as
    instruction, so it is the one part of a third-party server that acts on an
    agent before the agent calls anything.

    The measurement is published because the answer is a surprise: the corpus is
    close to clean. It is deliberately NOT a score, a badge or a ranking signal.
    Findings are counts and excerpts for a human to read, and the classes the
    detectors refuse to match are published alongside the ones they do, because
    an earlier draft of this scan was wrong about four legitimate publishers.
    """
    if entry:
        return JSONResponse(safety.for_entry(db(), entry))
    rep = render.cached("tool-safety", 3600, lambda: safety.scan_corpus(db()))
    return JSONResponse(rep, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/liveness")
def liveness_feed(dead: int = Query(0, ge=0, le=1),
                  since: int = Query(0, ge=0),
                  limit: int = Query(500, ge=1, le=5000),
                  cursor: int = Query(0, ge=0)):
    """Which endpoints answer, as a feed anyone may consume, including rivals.

    We probe every endpoint we index and keep the transitions. Other registries
    do not, which means they list servers that stopped answering months ago and
    have no way to know. Publishing this costs us nothing we would otherwise
    sell and makes the whole ecosystem less wrong, which is worth more to us
    than the small advantage of being the only ones who can tell.

    No key, no attribution required, no rate limit beyond the shared one.
    `dead=1` is the useful half: the entries worth re-checking on your side.
    """
    conn = db()
    where = ["url <> ''"]
    args: list = []
    if dead:
        where.append("live = 0")
    else:
        where.append("live IS NOT NULL")
    if since:
        where.append("live_checked >= ?")
        args.append(since)
    if cursor:
        where.append("rowid > ?")
        args.append(cursor)
    sql = ("SELECT rowid, identifier, url, live, live_status, live_ms, live_checked "
           "FROM entries WHERE " + " AND ".join(where) + " ORDER BY rowid LIMIT ?")
    rows = conn.execute(sql, (*args, limit)).fetchall()
    items = [{
        "identifier": r["identifier"],
        "url": r["url"],
        "answering": bool(r["live"]),
        "http_status": r["live_status"],
        "ms": r["live_ms"],
        "checked": r["live_checked"],
    } for r in rows]
    body = {
        "items": items,
        "count": len(items),
        "what": ("liveness observations from probing each endpoint. `answering` means it "
                 "responded, not that it is correct, safe or good"),
        "licence": "free to use, redistribute and build on. No attribution required.",
        "params": {"dead": "1 for endpoints that stopped answering",
                   "since": "unix seconds, only entries checked since then",
                   "cursor": "pass the last nextCursor to continue"},
    }
    if len(items) == limit and rows:
        body["nextCursor"] = rows[-1]["rowid"]
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=600"})


@app.get("/privacy", include_in_schema=False)
@app.get("/privacy-policy", include_in_schema=False)
def privacy_page(request: Request):
    """What we receive, keep and forward. See catalog.render_privacy_page."""
    if _wants_html(request):
        html_ = render.cached("privacy-html", 3600, catalog.render_privacy_page)
        return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600",
                                            "Vary": "Accept"})
    return JSONResponse({
        "accounts": "none. no signup, no payment, no personal data requested",
        "ip_addresses": "not stored. the analytics table has no column for one",
        "session_id": ("truncated sha256 of a daily-rotating salt, address and user agent; "
                       "unlinkable across days. Sec-GPC or DNT yields no session at all"),
        "search_queries": {
            "anonymous": ("query text truncated to 200 chars, mode, result count, duration "
                          "and the entries returned with their rank"),
            "with_api_key": "not recorded. the query is replaced with a placeholder",
            "why": f"powers {config.PUBLIC_BASE}/demand, scoped to a publisher's own domain",
        },
        "query_leaves_the_machine": [
            "to the other public ARD registries when federating, cached 30 minutes then deleted",
            "to an embedding provider, to vectorise the query for semantic retrieval",
        ],
        "retention": "raw analytics rows 90 days; daily aggregates indefinitely, with no identifier",
        "cookies": "none for tracking. no advertising, no third-party analytics",
        "html": f"{config.PUBLIC_BASE}/privacy",
    }, headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept"})


@app.get("/connect", include_in_schema=False)
def connect_page(request: Request):
    """Copy-paste setup for every client. See catalog.render_connect_page."""
    if _wants_html(request):
        html_ = render.cached("connect-html", 3600, catalog.render_connect_page)
        return HTMLResponse(html_, headers={"Cache-Control": "public, max-age=3600",
                                            "Vary": "Accept"})
    B = config.PUBLIC_BASE
    return JSONResponse({
        "endpoint": f"{B}/mcp",
        "transport": "streamable-http, POST only; GET and DELETE answer 405",
        "a2a": {"endpoint": f"{B}/a2a", "card": f"{B}/.well-known/agent-card.json"},
        "rest": f"{B}/search",
        "claude_code": ["/plugin marketplace add neuronto/ard-connectors",
                        "/plugin install neuronto-agent-finder@neuronto"],
        "clients": {
            "claude_desktop": {"mcpServers": {"neuronto": {
                "command": "npx", "args": ["-y", "mcp-remote", f"{B}/mcp"]}}},
            "cursor": {"mcpServers": {"neuronto": {"url": f"{B}/mcp"}}},
            "vscode": {"servers": {"neuronto": {"type": "http", "url": f"{B}/mcp"}}},
            "gemini": {"mcpServers": {"neuronto": {"httpUrl": f"{B}/mcp"}}},
        },
        "auth": "none. 60 requests an hour anonymous, 300 with a verified domain key",
    }, headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept"})


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

@app.post("/submit", responses={
    200: {"description": "verified and indexed; `submission.id` is the receipt"},
    202: {"description": "kept and retried: not verified at this moment, `evidence` says "
                         "what the endpoint returned, `retry.status_url` shows progress"},
    400: {"description": "malformed request, nothing queued"},
    404: {"description": "every retry attempt failed; the last `evidence` is included"},
})
async def submit_endpoint(body: dict, request: Request = None) -> JSONResponse:
    """Index an MCP endpoint (`{"endpoint": url}`) or a manifest-serving domain
    (`{"domain": host}`). A submission that does not verify right now is kept
    and retried on a fixed schedule for about two and a half days."""
    b = body or {}
    probe = _is_probe(request)
    resp = await _submit(b, source="http" if request is not None else "mcp", probe=probe)
    try:
        d = json.loads(bytes(resp.body).decode() or "{}")
        # A successful submission is the one moment the publisher is looking at
        # us, so it is the only place the badge is offered. Stated as an option,
        # with what it says, and never as a condition of being indexed.
        if resp.status_code < 400 and d.get("status") == "indexed":
            host = (d.get("identifier") or "").split(":")[2] if (d.get("identifier") or "").count(":") > 2 else ""
            host = host or str(b.get("domain") or "").strip().lower()
            if host:
                d["badge"] = {
                    **badge.snippet(host),
                    "optional": ("entirely optional and changes nothing about your indexing "
                                 "or ranking. It states what we verified, and it corrects "
                                 "itself when that changes"),
                    "customise": f"{config.PUBLIC_BASE}/badge?domain={host}",
                }
                resp = JSONResponse(d, status_code=resp.status_code)
        _emit_submit(b, resp.status_code, d, probe=probe)
    except Exception:
        pass
    return resp


_PROBE: contextvars.ContextVar[bool] = contextvars.ContextVar("nb_probe", default=False)


def _is_probe(request) -> bool:
    """Our own suites, so their queue rows and alerts are told apart from a
    publisher's. Never affects what the request is allowed to do."""
    if request is None:
        return _PROBE.get()
    try:
        h = request.headers
        return h.get("x-neuronto-probe") == "1" or "neuronto-e2e" in (h.get("user-agent") or "")
    except Exception:
        return False


def _emit_submit(b: dict, code: int, d: dict, probe: bool = False,
                 source: str = "http") -> None:
    """Record WHY, not just that it failed. Without this a publisher who
    cannot get in leaves an `ok=0` and nothing else, and answering "was that
    us or them" means correlating request timings against the systemd
    journal. It cost a full investigation on 2026-09-01 and the answer was
    still unknowable."""
    if b.get("dry_run") or d.get("dry_run"):
        return          # nothing happened, so nothing to record or alert on
    target = str(b.get("endpoint") or b.get("mcp") or b.get("domain") or b.get("url") or "")[:120]
    ok = code < 400 and d.get("status") == "indexed"
    sub = d.get("submission") or {}
    events.emit("submit", a="endpoint" if (b.get("endpoint") or b.get("mcp")) else "domain",
                b=target, n=d.get("verified_tools"), ok=ok, probe=probe, source=source,
                attempt=sub.get("attempts"), submission=sub.get("id"),
                final=(d.get("status") == "gave_up"),
                status=d.get("status"),
                detail=None if ok and (sub.get("attempts") or 1) == 1 else
                f"{code} {d.get('status') or '?'} {d.get('reason') or ''}: "
                f"{_clip(str(d.get('detail') or ''), 160)}")


def _clip(text: str, n: int) -> str:
    """Cut at a word boundary. A hard slice produced an alert reading
    "Nothing was indexed yet. `evi." which reads as a broken service rather
    than a truncated sentence, and these alerts are how we find out we are
    broken."""
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    cut = t[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip(" .,;:`") + "..."


SUBMIT_RETRY_EVERY_S = int(os.getenv("NEURONTO_SUBMIT_RETRY_EVERY", "60"))


async def retry_due_submissions(limit: int = 5) -> list[dict]:
    """One pass of the retrier. Called on a timer by the app, and directly by
    the resilience test. Returns the rows it attempted."""
    done = []
    for row in submissions.due(limit):
        body = {row["kind"]: row["target"]}
        try:
            resp = await _submit(body, source="retry", probe=bool(row.get("probe")))
            d = json.loads(bytes(resp.body).decode() or "{}")
            _emit_submit(body, resp.status_code, d, probe=bool(row.get("probe")),
                         source="retry")
            done.append(d)
        except Exception as e:
            # The attempt itself failed on our side; release the row so the
            # next pass retries rather than leaving it claimed for CLAIM_S.
            submissions.record(row["id"], indexed=False, reason="error:internal",
                               detail=f"{type(e).__name__}: {str(e)[:120]}",
                               evidence={"exception": f"{type(e).__name__}: {str(e)[:160]}"})
    return done


async def _index_write(fn):
    """Run an index write in a thread on its own connection. Returns what
    `fn(conn)` returns.

    Two things wrong with doing it inline. The connection on the event loop
    has a 45 second busy timeout, so a write that meets the crawl's lock
    freezes every request this worker is serving for up to 45 seconds; that
    is what the one "successful" submission on 2026-09-01 looked like. And a
    freeze that long is indistinguishable from an outage to the publisher.
    Off the loop, with a short timeout: five seconds of patience, then it is
    a `busy` and the queue takes it back thirty seconds later.
    """
    def run():
        c = store.connect()
        c.execute("PRAGMA busy_timeout=5000")
        try:
            out = fn(c)
            c.commit()
            return out
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
        finally:
            c.close()
    return await asyncio.to_thread(run)


def _busy_response(what: str, retry_s: int = 30, **extra) -> JSONResponse:
    """The index takes one writer. When a request-side write meets the crawl,
    the honest answer is a 503 with a time, not a 45 second freeze and not a
    500. Nothing the caller did was wrong and the answer says so."""
    return JSONResponse(status_code=503, headers={"Retry-After": str(retry_s)}, content={
        "status": "busy", **extra,
        "detail": (f"the index was busy writing and could not record {what} right now. "
                   f"Nothing is wrong on your side; call again in {retry_s} seconds."),
    })


def _busy(e: Exception) -> bool:
    return isinstance(e, sqlite3.OperationalError) and "locked" in str(e).lower()


class _Busy(Exception):
    """The index lock was held through both quick tries; the queue retries."""


async def _index_verified(url: str, res: dict, dry: bool = False) -> tuple[dict, str | None]:
    """Write one handshaken MCP endpoint into the index.

    Shared by both submit doors, and by every endpoint the resolver finds,
    so a server discovered through a server card is recorded exactly as one
    a publisher named directly: same entry shape, same tools, same liveness
    mark. `dry` builds the entry and writes nothing.
    """
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    name = res.get("server_name") or host
    entry = {
        "identifier": f"urn:air:{host}:mcp:{re.sub(r'[^a-z0-9-]+', '-', name.lower())[:60]}",
        "displayName": name,
        "type": "application/mcp-server-card+json",
        "url": url,
        "description": (f"MCP server at {host}, verified by introspection: "
                        f"{len(res['tools'])} tools exposed."
                        if res["tools"] else
                        f"MCP server at {host}. Requires credentials before listing tools."
                        if res["auth"] else f"MCP server at {host}."),
    }
    if dry:
        return entry, None
    key_box: dict = {}

    def write(c):
        key = store.upsert_entry(c, entry, "submitted")
        if res["tools"]:
            store.replace_tools(c, key, res["tools"])
        store.mark_introspection(c, key, res["status"], len(res["tools"]),
                                 res["auth"], res.get("server_name"))
        store.mark_liveness(c, key, True, 200, None)
        key_box["key"] = key

    # A publisher joining the index is the rarest and most valuable write we
    # take. Two quick tries; if the crawl holds the lock through both, the
    # queue brings it back in thirty seconds rather than asking the publisher.
    for attempt in range(2):
        try:
            await _index_write(write)
            return entry, key_box.get("key")
        except Exception as e:
            if not _busy(e):
                raise
            if attempt == 1:
                raise _Busy() from e
            await asyncio.sleep(1.0)
    raise _Busy()


async def _submit(body: dict, source: str = "http", probe: bool = False) -> JSONResponse:
    """Two ways in, because most MCP developers have no manifest.

    `{"domain": "..."}` fetches the ARD manifest and indexes everything it
    declares. `{"endpoint": "https://.../mcp"}` takes an MCP server directly:
    we handshake with it and read its tool list, which is stronger evidence
    than a manifest claim because the server itself answered.

    Requiring a manifest turned away anyone who had only built a server, which
    is nearly every MCP developer. Provenance is recorded rather than blurred:
    an entry from a manifest carries source `crawl`, one from a direct
    submission carries `submitted`, and the two are distinguishable in the API.

    Every call past input validation is a submission row first and an outcome
    second (see `submissions`). A failed attempt answers 202 with the reason,
    the evidence and when we will try again; only a request that could never
    verify (a malformed one) is answered with a plain refusal.
    """
    b = body or {}
    endpoint = str(b.get("endpoint") or b.get("mcp") or "").strip()
    dom = str(b.get("domain") or "").strip()
    dry = bool(b.get("dry_run"))

    # One field for people who do not know, and should not need to know, our
    # distinction between "an endpoint" and "a domain". `url` takes anything:
    # a bare host, a homepage, a manifest, a server card, an endpoint. The
    # resolver works out what is behind it. The two older fields stay, because
    # clients already send them.
    if not endpoint and not dom:
        u = str(b.get("url") or "").strip()
        if u:
            endpoint = u if "://" in u else "https://" + u

    # ---- an MCP endpoint, or anything that leads to one ------------------
    if endpoint:
        if not endpoint.startswith(("http://", "https://")) or len(endpoint) > 500:
            return JSONResponse(status_code=400, content={
                "error": "invalid_request",
                "detail": 'endpoint must be an absolute http(s) URL'})
        # Resolved, in a thread: getaddrinfo blocks, and this runs on the loop.
        if not await asyncio.to_thread(resolve.is_safe_target,
                                       resolve.host_of(endpoint) or ""):
            return JSONResponse(status_code=400, content={
                "error": "invalid_request",
                "detail": "/submit connects to the host you name, from our network, and indexes what answers, so it only accepts publicly routable addresses. This name does not resolve to one."})
        # `dry_run` writes nothing and alerts nobody: no submission row, no
        # index write, no event. The probes still run, because the point is to
        # see what they would find. `submissions.record` and `_not_indexed`
        # both accept a missing id, so a None here threads through cleanly.
        sid = None if dry else submissions.open("endpoint", endpoint, source, probe)
        import httpx as _httpx

        # The URL itself is only worth a handshake if it names something below
        # the host. POSTing an initialize to a homepage gets a 405 from every
        # web server on earth, and we already know that; skip straight to
        # finding out what the host actually runs.
        direct: dict | None = None
        bare = not resolve._path_of(endpoint).strip("/")
        async with _httpx.AsyncClient(follow_redirects=True) as c:
            async with limits.outbound():
                if not bare:
                    try:
                        direct = await tools_index.introspect_one(c, endpoint, retries=1)
                    except Exception as e:
                        direct = {"status": f"error:{type(e).__name__}", "tools": [],
                                  "auth": False, "server_name": None,
                                  "evidence": {"exception": f"{type(e).__name__}: {str(e)[:160]}",
                                               "stage": "before the request was sent"}}
                direct_ok = bool(direct) and (direct["status"].startswith("ok")
                                              or direct["status"] == "auth")
                # The submitted URL answered: index it, and do not go looking
                # for more. A publisher who named an endpoint gets that endpoint.
                if direct_ok:
                    found = {"submitted": endpoint, "host": resolve.host_of(endpoint),
                             "working": [{"url": endpoint, "source": "submitted",
                                          "status": direct["status"],
                                          "tools": len(direct["tools"]),
                                          "auth": bool(direct["auth"]),
                                          "server_name": direct.get("server_name"),
                                          "evidence": direct.get("evidence"),
                                          "_raw": direct}],
                             "candidates": [], "checked": []}
                else:
                    found = await resolve.resolve(
                        db(), endpoint, c,
                        lambda cl, u: tools_index.introspect_one(cl, u, retries=0),
                        direct_result=direct)

        working = found.get("working") or []
        if not working:
            # Before refusing: this host may publish a manifest and run no MCP
            # server at all. A publisher of skills, APIs or agents is a
            # publisher, and the domain door already falls back the other way
            # when a host has a server and no manifest. Same resolution, same
            # index, whichever door they came through.
            mhost = found.get("host") or resolve.host_of(endpoint)
            mdata, mpath = None, None
            if mhost:
                async with limits.outbound():
                    mdata, mpath = await ingest.fetch_manifest(mhost)
            if mdata and (mdata.get("entries") or []):
                n = len(mdata["entries"])
                if not dry:
                    try:
                        n = await _index_write(
                            lambda c: ingest.index_manifest(c, mhost, mdata, mpath,
                                                            strict=True,
                                                            source="submitted"))
                    except Exception as e:
                        if not _busy(e):
                            raise
                        detail = ("your manifest was read, and the index was busy "
                                  "writing so it is not searchable yet. Nothing is "
                                  "wrong on your side; it will be indexed "
                                  "automatically within a minute.")
                        row = submissions.record(sid, indexed=False, reason="busy",
                                                 detail=detail, evidence=None, busy=True)
                        return _not_indexed("busy", row, domain=mhost, reason="busy",
                                            detail=detail, evidence=None)
                    catalog.invalidate_publishers(); render.invalidate()
                row = submissions.record(sid, indexed=True, reason="indexed", tools=0)
                out = {
                    "status": "indexed",
                    "domain": mhost,
                    "manifest_path": mpath,
                    "resources_indexed": n,
                    "page": f"{config.PUBLIC_BASE}/ard-publishers/{mhost}",
                    "submission": submissions.public(row),
                    "note": ("no MCP endpoint answered on this host, and it publishes "
                             "an ARD manifest, so the manifest was indexed instead. "
                             "Entries of every type are indexed, not only MCP servers."),
                }
                if dry:
                    out["dry_run"] = True
                    out["note"] = "dry run: nothing was written. " + out["note"]
                return JSONResponse(out)

            # Nothing on this host answered a handshake, anywhere we know to
            # look. Say exactly what was tried. Whether that is worth retrying
            # depends on the kind of failure, judged on the submitted URL when
            # there was one and on the best-looking candidate otherwise: an
            # unreachable host is transient and stays queued; a host that is up
            # and simply runs no MCP server is a settled answer.
            judge = direct if direct is not None else (
                (found.get("candidates") or [{}])[0].get("evidence") and
                {"status": (found.get("candidates") or [{}])[0].get("status"),
                 "evidence": (found.get("candidates") or [{}])[0].get("evidence")} or
                {"status": "error:unreachable", "evidence": {}})
            reason = str(judge.get("status") or "error:unknown")
            tried = [{k: v for k, v in cnd.items() if k in ("url", "source", "status", "tools")}
                     for cnd in (found.get("candidates") or [])]
            detail = ("no MCP endpoint answered a handshake on "
                      f"{found.get('host') or endpoint}, and it publishes no ARD "
                      "manifest at /.well-known/ard.json. " + resolve.explain(found)
                      + ". `evidence` is what the submitted URL returned; `tried` is "
                        "every location checked and what each answered.")
            row = submissions.record(sid, indexed=False, reason=reason, detail=detail,
                                     evidence=judge.get("evidence"),
                                     scheduled=(source == "retry"))
            return _not_indexed("not_an_mcp_server", row, endpoint=endpoint,
                                reason=reason, detail=detail,
                                evidence=judge.get("evidence"), tried=tried,
                                discovery_checked=found.get("checked") or [],
                                **({"dry_run": True} if dry else {}))

        # Index every distinct server that answered. Usually one; sometimes a
        # host runs several, and a discovery document names each of them.
        indexed: list[dict] = []
        for w in working:
            res = w["_raw"]
            try:
                ent, key = await _index_verified(w["url"], res, dry=dry)
            except _Busy as e:
                detail = ("your server verified, and the index was busy writing so it "
                          "is not searchable yet. Nothing is wrong with your server; "
                          "it will be indexed automatically within a minute.")
                row = submissions.record(sid, indexed=False, reason="busy", detail=detail,
                                         evidence={"verified": True, "tools": w["tools"]},
                                         busy=True)
                return _not_indexed("busy", row, endpoint=endpoint, reason="busy",
                                    detail=detail, evidence=None, verified=True)
            indexed.append({"endpoint": w["url"], "found_via": w["source"],
                            "server_name": res.get("server_name"),
                            "verified_tools": len(res["tools"]),
                            "tools": [t.get("name") for t in res["tools"]][:40],
                            "auth_required": bool(res["auth"]),
                            "identifier": ent["identifier"], "_key": key})
        if not dry:
            catalog.invalidate_publishers(); render.invalidate()
        first = indexed[0]
        row = submissions.record(sid, indexed=True, reason="indexed",
                                 entry_key=first.get("_key"),
                                 tools=sum(i["verified_tools"] for i in indexed))
        body_out: dict = {
            "status": "indexed",
            "endpoint": first["endpoint"],
            "server_name": first["server_name"],
            "verified_tools": first["verified_tools"],
            "tools": first["tools"],
            "auth_required": first["auth_required"],
            "identifier": first["identifier"],
            "submission": submissions.public(row),
            "note": ("verified by handshaking with your server and reading its own "
                     "tools/list. To have your whole domain indexed, including skills, "
                     "APIs and agents, publish an ARD manifest and submit the domain: "
                     f"{config.PUBLIC_BASE}/publish"),
        }
        # Honest about how it was found. If the caller named the endpoint, the
        # block is absent. If we had to find it, say from where, so the caller
        # learns the URL and the mechanism rather than just getting lucky.
        if first["found_via"] != "submitted" or len(indexed) > 1:
            body_out["resolved"] = {
                "submitted": endpoint,
                "found_via": first["found_via"],
                "endpoints": [{k: v for k, v in i.items() if k != "_key"} for i in indexed],
                "note": ("the submitted URL did not answer an MCP handshake itself, so "
                         "the host was resolved through its own discovery documents, "
                         "this index, and the conventional paths. Everything listed "
                         "here answered."),
            }
        if dry:
            body_out["dry_run"] = True
            body_out["note"] = "dry run: nothing was written, nothing was recorded. " + body_out["note"]
        return JSONResponse(body_out)

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
    if not await asyncio.to_thread(resolve.is_safe_target, host):
        return JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "detail": "/submit connects to the host you name, from our network, and indexes what answers, so it only accepts publicly routable addresses. This name does not resolve to one."})

    sid = None if dry else submissions.open("domain", host, source, probe)
    conn = db()
    before = conn.execute("SELECT COUNT(*) FROM entries WHERE lower(publisher)=?",
                          (host,)).fetchone()[0]
    # Network first, with nothing held; then the write, in a thread on its own
    # connection. The 2026-09-01 loss was a write on the loop connection meeting
    # the crawl's lock; the domain path had the same shape until 2026-09-02.
    async with limits.outbound():
        data, path = await ingest.fetch_manifest(host)
    got = {"considered": 1, "crawled": 1, "manifest": path, "entries": 0}
    if data and dry:
        got["entries"] = len(data.get("entries") or [])
    elif data:
        try:
            got["entries"] = await _index_write(
                lambda c: ingest.index_manifest(c, host, data, path, strict=True,
                                                source="submitted"))
        except Exception as e:
            if not _busy(e):
                raise
            detail = ("the index was busy writing and could not take this domain right "
                      "now. Nothing is wrong on your side; it will be retried "
                      "automatically within a minute.")
            row = submissions.record(sid, indexed=False, reason="busy", detail=detail,
                                     evidence=None, busy=True)
            return _not_indexed("busy", row, domain=host, reason="busy", detail=detail,
                                evidence=None)
    elif not dry:
        # Nothing to write but the fact that we looked, which the publisher
        # page and `verified_manifests` read. Best effort: a busy index must
        # not turn "no manifest" into a 500.
        try:
            await _index_write(lambda c: ingest.index_manifest(c, host, {"entries": []},
                                                               None, strict=True))
        except Exception as e:
            if not _busy(e):
                raise
    after = conn.execute("SELECT COUNT(*) FROM entries WHERE lower(publisher)=?",
                         (host,)).fetchone()[0]
    catalog.invalidate_publishers(); render.invalidate()

    if not path and after == 0:
        # No manifest is not the same as nothing to index. Most MCP developers
        # have a server and no manifest, and this door used to send them away
        # to guess the endpoint and come back through the other one. Look for
        # the server here instead, the same way the endpoint door does.
        import httpx as _httpx
        async with _httpx.AsyncClient(follow_redirects=True) as c:
            async with limits.outbound():
                found = await resolve.resolve(
                    conn, host, c,
                    lambda cl, u: tools_index.introspect_one(cl, u, retries=0))
        working = found.get("working") or []
        if working:
            indexed = []
            for w in working:
                try:
                    ent, key = await _index_verified(w["url"], w["_raw"], dry=dry)
                except _Busy:
                    detail = ("your server verified, and the index was busy writing so it "
                              "is not searchable yet. It will be indexed automatically "
                              "within a minute.")
                    row = submissions.record(sid, indexed=False, reason="busy",
                                             detail=detail, busy=True,
                                             evidence={"verified": True})
                    return _not_indexed("busy", row, domain=host, reason="busy",
                                        detail=detail, evidence=None, verified=True)
                indexed.append({"endpoint": w["url"], "found_via": w["source"],
                                "server_name": w["_raw"].get("server_name"),
                                "verified_tools": len(w["_raw"]["tools"]),
                                "identifier": ent["identifier"]})
            if not dry:
                catalog.invalidate_publishers(); render.invalidate()
            srow = submissions.record(sid, indexed=True, reason="indexed",
                                      tools=sum(i["verified_tools"] for i in indexed))
            return JSONResponse({
                "status": "indexed",
                "domain": host,
                "manifest_path": None,
                "resources_indexed": len(indexed),
                "page": f"{config.PUBLIC_BASE}/ard-publishers/{host}",
                "resolved": {
                    "endpoints": indexed,
                    "note": ("no ARD manifest on this domain, but it runs an MCP server, "
                             "found through the host's own discovery documents, this "
                             "index, or the conventional paths, and verified by "
                             "handshake. Publishing a manifest lists everything else: "
                             f"{config.PUBLIC_BASE}/publish"),
                },
                "submission": submissions.public(srow),
                **({"dry_run": True} if dry else {}),
            })
        detail = ("no ARD manifest at either well-known path, and no MCP endpoint "
                  "answered a handshake anywhere we know to look on this host. "
                  + resolve.explain(found) + ". To be listed, run an MCP server at "
                  "/mcp, or publish a manifest: " + config.PUBLIC_BASE + "/publish")
        ev = {"checked": ingest.PATHS, "crawl": _small(got),
              "tried": [{k: v for k, v in cnd.items() if k in ("url", "source", "status")}
                        for cnd in (found.get("candidates") or [])]}
        srow = submissions.record(sid, indexed=False, reason="no_manifest", detail=detail,
                                  evidence=ev, scheduled=(source == "retry"))
        return _not_indexed("no_manifest", srow, domain=host, reason="no_manifest",
                            detail=detail, evidence=ev, checked=ingest.PATHS,
                            tried=ev["tried"], **({"dry_run": True} if dry else {}))

    srow = submissions.record(sid, indexed=True, reason="indexed", tools=after)
    return JSONResponse({
        "status": "indexed",
        "domain": host,
        "manifest_path": path,
        "resources_indexed": after,
        "newly_added": max(0, after - before),
        "page": f"{config.PUBLIC_BASE}/ard-publishers/{host}",
        "crawl": got,
        "submission": submissions.public(srow),
        "note": ("fetched live from your domain, not taken from this form. Endpoint "
                 "reachability is probed separately and is not a trust or safety rating"),
    })


def _small(x, n: int = 600):
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
        return json.loads(s) if len(s) <= n else s[:n]
    except Exception:
        return str(x)[:n]


def _not_indexed(why: str, row: dict | None, **fields) -> JSONResponse:
    """The answer to an attempt that did not index.

    202 while we are still going to retry, 404 once we have given up. The
    body always carries the machine readable reason, the evidence and the
    submission so a caller retrying blind never has to guess whose side the
    problem is on. A `busy` never counts against the publisher.
    """
    pub = submissions.public(row)
    still = bool(pub and pub.get("status") == "pending")
    # Report the queue's own status rather than assuming anything that is not
    # pending was given up on. A `rejected` row was never retried at all: the
    # endpoint gave a settled answer, and calling that "gave_up" would tell a
    # publisher we had tried for days when we had tried once and stopped.
    body = {
        "status": "pending" if still else ((pub.get("status") if pub else None) or why),
        "indexed": False,
        "refusal": why,
        **{k: v for k, v in fields.items() if v is not None or k in ("evidence",)},
        "submission": pub,
    }
    if still and pub:
        body["detail"] = (fields.get("detail") or "") + " " + pub["note"]
        body["retry"] = {"next_attempt_in_s": pub["next_attempt_in_s"],
                         "attempts_left": pub["attempts_left"],
                         "status_url": pub["status_url"]}
    return JSONResponse(status_code=202 if still else 404, content=body,
                        headers={"Cache-Control": "no-store"})


@app.get("/submit/status/{sid}")
def submit_status(sid: str) -> JSONResponse:
    """Where a submission stands. Public, because the id is the only
    credential a publisher has for it and there is nothing sensitive in it."""
    if not re.fullmatch(r"[0-9a-f]{12}", sid or ""):
        return JSONResponse(status_code=400, content={"error": "invalid_request",
                                                      "detail": "not a submission id"})
    row = submissions.get(sid)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found",
                                                      "detail": "no such submission"})
    return JSONResponse(submissions.public(row), headers={"Cache-Control": "no-store"})


@app.get("/submit/status")
def submit_status_for(endpoint: str = Query(None), domain: str = Query(None)) -> JSONResponse:
    """The latest submission for a target, for a publisher who lost the id."""
    if endpoint:
        row = submissions.latest_for("endpoint", endpoint.strip())
    elif domain:
        host = (domain.replace("https://", "").replace("http://", "")
                      .strip("/").split("/")[0].lower())
        row = submissions.latest_for("domain", host)
    else:
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "give endpoint= or domain="})
    if not row:
        return JSONResponse(status_code=404, content={
            "error": "not_found", "detail": "nothing has been submitted for that target"})
    return JSONResponse(submissions.public(row), headers={"Cache-Control": "no-store"})


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

<h2 style="margin-top:30px;font-size:20px">What happens when it does not verify</h2>
<p class="lede">It is kept. A submission that cannot be verified at that moment, for any
reason, answers <code>202</code> with <code>"status": "pending"</code>, a submission id and
<code>evidence</code>: the HTTP status, content type and first bytes your endpoint actually
returned, or the JSON-RPC error it sent, so you can see exactly what we saw. We then retry
it ourselves on a fixed schedule (1 minute, 5, 15, 1 hour, 4, 12, 24, 24) until it verifies
or the attempts run out, and <code>GET {B}/submit/status/&lt;id&gt;</code> shows where it
stands at any time. A refusal caused by us rather than by you, such as the index being busy,
costs none of those attempts. Submitting again is harmless and joins the same queue. So a
server that was mid-deploy or a DNS record that had not propagated still ends up indexed
with no second submission from you, and if every attempt fails you are told that too, with
the last evidence, rather than left guessing.</p>

<div class="note">
  Nothing here is taken on your word: the manifest is fetched from your domain, so a
  submission cannot inject anything you do not actually publish. If no manifest is found you
  get told which paths were tried, and the submission is retried. Not publishing yet? The
  <a href="/publish">ten-minute guide</a> and the <a href="/console">free audit</a> both help.
</div>

<script>
async function go(e){{
  e.preventDefault();
  const d=document.getElementById('d').value.trim(), o=document.getElementById('o');
  if(!d) return false;
  o.style.display='block'; o.textContent='fetching '+d+' ...';
  try{{
    const body=(d.startsWith('http://')||d.startsWith('https://'))?{{endpoint:d}}:{{domain:d}};
    const r=await fetch('/submit',{{method:'POST',headers:{{'content-type':'application/json'}},
      body:JSON.stringify(body)}});
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
