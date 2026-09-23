"""Glama, added 2026-09-18 as an index source.

WHY. Of a 3,000 server sample walked from its catalogue, **72% were absent from our
index**, the highest miss rate of any source evaluated: Glama indexes GitHub-published
MCP servers that were never submitted to the official registry. Smithery, by comparison,
was 35% new.

AUTH. Needs a free API key, `GLAMA_API_KEY`. Without one the endpoint answers 401 and
this source skips cleanly rather than failing the ingest run.

PAGINATION. Cursor based (`pageInfo.endCursor` / `hasNextPage`), and unlike Smithery the
cursor actually walks the whole catalogue, so `complete` here means the source said there
was no next page. `first` above 100 is not relied on.

URL CHOICE. Most Glama entries are `hosting:local-only`: a stdio server with no endpoint
to probe. Their repository is the honest, resolvable address for such a server, so that
is used when present, falling back to the Glama record page. A fabricated MCP endpoint
would write false "dead" verdicts, which is the same reasoning as in app/smithery.py.
The hosting attribute is carried through as a tag so a caller can tell the two apart.
"""
from __future__ import annotations

import asyncio
import os

import httpx

from app import ingest, store

API = "https://glama.ai/api/mcp/v1/servers"


def _entry(s: dict) -> dict | None:
    slug = s.get("slug") or s.get("name") or ""
    if not slug:
        return None
    ns = s.get("namespace") or "glama.ai"
    leaf = str(slug).replace("/", ".")
    name = s.get("name") or slug
    desc = s.get("description") or ""

    repo = s.get("repository")
    repo_url = repo.get("url") if isinstance(repo, dict) else None
    url_ = repo_url or s.get("url") or f"https://glama.ai/mcp/servers/{s.get('id','')}"

    tags = ["mcp", "glama"]
    for a in (s.get("attributes") or []):
        a = str(a)
        if a.startswith("hosting:"):
            tags.append(a.split(":", 1)[1])          # local-only | remote-capable
    lic = s.get("spdxLicense")
    if isinstance(lic, dict) and lic.get("name"):
        tags.append(str(lic["name"])[:40])

    return {
        "identifier": f"urn:air:{ns}:mcp:{leaf}",
        "displayName": name,
        "type": "application/mcp-server-card+json",
        "url": url_,
        "description": desc,
        "tags": tags,
        "representativeQueries": ingest._queries_from(desc, name),
    }


async def from_glama(conn, max_pages: int = 400) -> dict:
    key = (os.getenv("GLAMA_API_KEY") or "").strip()
    if not key:
        return {"source": "glama", "skipped": "no GLAMA_API_KEY set"}

    n = pages = 0
    seen: set[str] = set()
    cursor = None
    complete = False
    headers = {**ingest.HEADERS, "Authorization": f"Bearer {key}",
               "Accept": "application/json"}

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for _ in range(max_pages):
            url = f"{API}?first=100" + (f"&after={cursor}" if cursor else "")
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
                d = r.json()
            except Exception:
                break

            servers = d.get("servers") or []
            if not servers:
                complete = True
                break
            pages += 1
            for s in servers:
                sid = s.get("id") or s.get("slug")
                if sid in seen:
                    continue
                seen.add(sid)
                e = _entry(s)
                if e and store.upsert_entry(conn, e, "glama"):
                    n += 1
            conn.commit()

            pi = d.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                complete = True
                break
            cursor = pi.get("endCursor")
            await asyncio.sleep(0.2)                 # a polite client

    return {"source": "glama", "entries": n, "pages": pages,
            "distinct_seen": len(seen), "complete": complete}
