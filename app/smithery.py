"""Smithery, added 2026-09-18 as an index source.

WHY. The official MCP Registry is complete in our index (330 pages, 33,004 entries) and
is the largest single source, so the bulk was never missing. But aggregators also carry
community servers never published to the official registry: of a 200 server sample
spread across Smithery's listing, 35% were absent from our index.

THE CAP, AND WHY PAGINATION ALONE IS A TRAP. Smithery reports `totalCount: 15470` and
`totalPages: 5` in the same object. The plain listing hands back **500 servers and no
more**: page 6 and beyond return HTTP 200 with an empty array, so a naive walk stops and
reports success having seen 3% of the registry. pageSize above 100 is refused with 400.
This is the same shape as the TED truncation already in CLAUDE.md, where a total was
published and the walk quietly ended early. Always compare what you fetched against the
total the source itself states.

The way past it: `?q=` runs a SEARCH, and each query has its own result window with its
own totalCount. Walking a battery of queries reaches far more of the registry than the
listing exposes. That is the same technique `ingest.from_upstreams` uses, so the battery
is shared rather than a second copy.

URL CHOICE. A server Smithery hosts and has deployed has a real remote MCP endpoint, so
that is used and liveness can probe it honestly. Everything else gets its Smithery page,
a real resolvable document, exactly as from_mcp_registry falls back to a registry search
URL when a server declares no remote. Inventing an endpoint that cannot answer would
write thousands of false "dead" verdicts into the index.

Skipped: unlisted and inactive servers, which Smithery itself marks as not for use.
"""
from __future__ import annotations

import asyncio

import httpx

from app import ingest, store
from app.smithery_battery import EXTRA

BASE = "https://registry.smithery.ai/servers"
PAGE = 100                 # anything larger is refused with 400
LIST_PAGES = 5             # the hard cap on the unfiltered listing


def _entry(s: dict) -> dict | None:
    qn = s.get("qualifiedName") or ""
    if not qn or s.get("unlisted") or s.get("inactive"):
        return None
    ns = s.get("namespace") or qn.partition("/")[0]
    leaf = (qn.partition("/")[2] or qn).replace("/", ".")
    desc = s.get("description") or ""
    name = s.get("displayName") or qn
    url_ = (f"https://server.smithery.ai/{qn}/mcp"
            if s.get("remote") and s.get("isDeployed")
            else f"https://smithery.ai/server/{qn}")
    tags = ["mcp", "smithery"]
    if s.get("verified"):
        tags.append("verified")
    if s.get("remote"):
        tags.append("remote")
    return {
        "identifier": f"urn:air:{ns}:mcp:{leaf}",
        "displayName": name,
        "type": "application/mcp-server-card+json",
        "url": url_,
        "description": desc,
        "tags": tags,
        "representativeQueries": ingest._queries_from(desc, name),
    }


async def from_smithery(conn, queries: list[str] | None = None,
                        max_pages_per_query: int = 5) -> dict:
    # The capability battery reached 34.6%. EXTRA adds vendor names, formats,
    # verticals and single letters, which pull in servers sharing no capability
    # vocabulary with the originals.
    qs = queries or (ingest.PROBE_QUERIES + EXTRA)
    n = skipped = reqs = 0
    total_listed = None
    seen: set[str] = set()

    async with httpx.AsyncClient(headers=ingest.HEADERS, timeout=30) as client:

        async def walk(params: str, max_pages: int):
            nonlocal n, skipped, reqs, total_listed
            for page in range(1, max_pages + 1):
                try:
                    r = await client.get(f"{BASE}?{params}&pageSize={PAGE}&page={page}")
                    reqs += 1
                    if r.status_code != 200:
                        return
                    d = r.json()
                except Exception:
                    return
                servers = d.get("servers") or []
                if not servers:
                    return
                pg = d.get("pagination") or {}
                if total_listed is None:
                    total_listed = pg.get("totalCount")
                for s in servers:
                    qn = s.get("qualifiedName") or ""
                    if qn in seen:
                        continue
                    seen.add(qn)
                    e = _entry(s)
                    if e is None:
                        skipped += 1
                        continue
                    if store.upsert_entry(conn, e, "smithery"):
                        n += 1
                conn.commit()
                await asyncio.sleep(0.25)           # a polite client
                if pg.get("totalPages") and page >= pg["totalPages"]:
                    return

        await walk("", LIST_PAGES)                  # the popular head
        for q in qs:                                # then the long tail
            await walk(f"q={httpx.QueryParams({'q': q})['q']}", max_pages_per_query)

    return {"source": "smithery", "entries": n, "distinct_seen": len(seen),
            "requests": reqs, "skipped_unlisted_or_inactive": skipped,
            "total_listed_by_smithery": total_listed,
            "reach": round(len(seen) / total_listed, 3) if total_listed else None}
