"""Docker's official MCP catalogue, added 2026-09-18.

Small (245 repositories) but genuinely distinct: these are the containerised MCP servers
Docker publishes under the `mcp/` namespace, and a container image is a different
artifact from a GitHub repository or a hosted endpoint, so several were absent from the
index even where the same project appeared elsewhere.

URL CHOICE. A Docker image has no endpoint to probe. Its Hub page is the resolvable
address that describes it, which is the same reasoning as app/glama.py: a fabricated MCP
endpoint would write false "dead" verdicts. Tagged `container` so a caller can tell that
this is something to run, not something to connect to.
"""
from __future__ import annotations

import asyncio

import httpx

from app import ingest, store

API = "https://hub.docker.com/v2/repositories/mcp/"


async def from_dockerhub(conn, max_pages: int = 20) -> dict:
    n = pages = 0
    total = None
    url = f"{API}?page_size=100"

    async with httpx.AsyncClient(headers=ingest.HEADERS, timeout=30) as client:
        for _ in range(max_pages):
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
                d = r.json()
            except Exception:
                break

            results = d.get("results") or []
            if not results:
                break
            pages += 1
            total = d.get("count", total)

            for it in results:
                name = it.get("name") or ""
                if not name:
                    continue
                desc = (it.get("description") or "").strip()
                entry = {
                    "identifier": f"urn:air:hub.docker.com:mcp:{name}",
                    "displayName": name,
                    "type": "application/mcp-server-card+json",
                    "url": f"https://hub.docker.com/r/mcp/{name}",
                    "description": desc or f"Containerised MCP server published by Docker as mcp/{name}.",
                    "tags": ["mcp", "docker", "container"],
                    "_inferredQueries": ingest._queries_from(desc, name),
                }
                if store.upsert_entry(conn, entry, "dockerhub"):
                    n += 1
            conn.commit()

            url = d.get("next")
            if not url:
                break
            await asyncio.sleep(0.2)

    return {"source": "dockerhub", "entries": n, "pages": pages,
            "total_listed": total, "complete": total is not None and pages * 100 >= total}
