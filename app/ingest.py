"""Fill the index.

Three sources, in descending order of what they are worth:

  1. The official MCP Registry. 8,000+ servers, the largest catalogue of
     callable resources in existence, and none of the four ARD registries
     ingests it. This alone puts our index an order of magnitude ahead.
  2. The four upstream ARD registries, harvested by probing them across a broad
     query battery. Mirroring them means our fast path (`federation: none`)
     already contains what a live fan-out would find.
  3. Direct crawl of the four discovery paths on seed domains, which is the only
     way a small publisher who is in nobody's index gets found.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from . import config, store
from .normalize import media_family

HEADERS = {"user-agent": config.USER_AGENT}

# A broad battery, used to harvest upstream indexes. Registries return only a
# page at a time, so coverage comes from asking many different things.
PROBE_QUERIES = [
    "web search", "scrape a website", "browser automation", "send an email",
    "query a database", "postgres", "sql", "vector database", "image generation",
    "video", "audio transcription", "text to speech", "pdf", "documents",
    "spreadsheet", "calendar", "crm", "sales", "marketing", "analytics",
    "payments", "invoicing", "accounting", "crypto", "blockchain", "wallet",
    "github", "git", "code review", "testing", "deployment", "kubernetes",
    "docker", "aws", "cloud", "monitoring", "logging", "security", "scanning",
    "authentication", "identity", "slack", "discord", "telegram", "social media",
    "linkedin", "twitter", "youtube", "maps", "geocoding", "weather", "travel",
    "flights", "hotels", "ecommerce", "shopping", "inventory", "shipping",
    "legal", "contracts", "compliance", "healthcare", "medical", "science",
    "research", "papers", "translation", "language", "summarisation",
    "knowledge base", "memory", "rag", "embeddings", "agents", "workflow",
    "automation", "scheduling", "notifications", "sms", "voice", "telephony",
    "hr", "recruiting", "education", "finance", "stocks", "news", "sports",
    "real estate", "food", "music", "gaming", "3d", "design", "figma", "storage",
]


async def from_mcp_registry(conn, max_pages: int = 120) -> dict:
    """Ingest the official MCP Registry.

    Each server becomes an ARD entry typed with the conformant MCP media type.
    We synthesise representativeQueries from the description when the registry
    has none, because an entry without them is one no semantic index can rank.
    """
    n = 0
    cursor = None
    async with httpx.AsyncClient(headers=HEADERS, timeout=25) as client:
        for _ in range(max_pages):
            url = "https://registry.modelcontextprotocol.io/v0/servers?limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
                d = r.json()
            except Exception:
                break
            servers = d.get("servers") or []
            if not servers:
                break
            for s in servers:
                sv = s.get("server") or s
                name = sv.get("name") or ""
                if not name:
                    continue
                remotes = sv.get("remotes") or []
                url_ = (remotes[0].get("url") if remotes else None) or \
                       f"https://registry.modelcontextprotocol.io/v0/servers?search={name}"
                desc = sv.get("description") or ""
                # MCP names are reverse-DNS with a slash: "io.alterlab/mcp-server".
                # The left half is the real publisher; attributing every server to
                # the registry that lists it would collapse thousands of distinct
                # publishers into one and make publisher facets useless.
                ns, _, leaf = name.partition("/")
                pub = ns if "." in ns else "registry.modelcontextprotocol.io"
                leaf = (leaf or ns).replace("/", ".")
                entry = {
                    "identifier": f"urn:air:{pub}:mcp:{leaf}",
                    "displayName": sv.get("title") or name,
                    "type": "application/mcp-server-card+json",
                    "url": url_,
                    "description": desc,
                    "version": sv.get("version"),
                    "tags": ["mcp", "mcp-registry"],
                    "representativeQueries": _queries_from(desc, sv.get("title") or name),
                }
                if store.upsert_entry(conn, entry, "mcp-registry"):
                    n += 1
            conn.commit()
            cursor = (d.get("metadata") or {}).get("nextCursor")
            if not cursor:
                break
    return {"source": "mcp-registry", "entries": n}


def _queries_from(desc: str, name: str) -> list[str]:
    """A usable stand-in when a publisher gave no representative queries.

    Not invention: it restates the publisher's own description as the kind of
    request it answers, so the semantic index has something true to match on.
    """
    d = (desc or "").strip().rstrip(".")
    if not d:
        return [f"use {name}"]
    first = d.split(".")[0].strip()
    out = [first.lower()] if len(first) > 12 else []
    out.append(f"{name} for this task".lower())
    return out[:3]


async def from_upstreams(conn, queries: list[str] | None = None) -> list[dict]:
    """Harvest the upstream registries across the probe battery."""
    qs = queries or PROBE_QUERIES
    stats = []
    async with httpx.AsyncClient(headers={**HEADERS, "content-type": "application/json"},
                                 timeout=20) as client:
        for uid, name, url, source in config.UPSTREAMS:
            n = 0
            for q in qs:
                try:
                    r = await client.post(url, json={"query": {"text": q}, "pageSize": 100})
                    if r.status_code != 200:
                        continue
                    for e in (r.json().get("results") or []):
                        if isinstance(e, dict) and store.upsert_entry(conn, e, uid):
                            n += 1
                except Exception:
                    continue
                await asyncio.sleep(0.05)
            conn.commit()
            conn.execute("""INSERT INTO registries(id,name,search_url,source,entries_seen,last_ok)
                            VALUES(?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET entries_seen=excluded.entries_seen,
                                                          last_ok=excluded.last_ok""",
                         (uid, name, url, source, n, int(time.time())))
            conn.commit()
            stats.append({"source": uid, "entries": n})
    return stats


PATHS = ["/.well-known/ard.json", "/.well-known/ai-catalog.json"]


async def crawl_domains(conn, domains: list[str], concurrency: int | None = None,
                        skip_seen_hours: int = 168) -> dict:
    """Fetch the well-known paths across a domain list.

    This is where an index is actually won. The only other general crawler in
    the ecosystem covers the Tranco top 100K, so every publisher below that rank
    is invisible everywhere — and those are precisely the ones who need finding.

    Resumable by design: a run over hundreds of thousands of domains will be
    interrupted, and re-fetching what we checked yesterday wastes the budget that
    should go on domains we have never seen.
    """
    import itertools, random

    conc = concurrency or config.CRAWL_CONCURRENCY
    cutoff = int(time.time()) - skip_seen_hours * 3600
    already = {r[0] for r in conn.execute(
        "SELECT domain FROM crawl_seen WHERE last_crawl > ?", (cutoff,))}
    todo = [d for d in (x.strip().lower() for x in domains) if d and d not in already]

    proxies = config.CRAWL_PROXIES or [None]
    pool = itertools.cycle(proxies)
    sem = asyncio.Semaphore(conc)
    found = entries = checked = 0
    pending: list[tuple] = []

    clients = [httpx.AsyncClient(headers=HEADERS, timeout=config.CRAWL_TIMEOUT_S,
                                 follow_redirects=True, proxy=p,
                                 limits=httpx.Limits(max_connections=conc))
               for p in proxies]

    async def one(dom: str, client: httpx.AsyncClient):
        nonlocal found, entries, checked
        base = dom if dom.startswith("http") else f"https://{dom}"
        got, data = 0, None
        async with sem:
            for path in PATHS:
                try:
                    r = await client.get(base + path)
                    if r.status_code == 200 and r.headers.get("content-type", "").find("json") >= 0:
                        d = r.json()
                        if isinstance(d, dict) and isinstance(d.get("entries"), list):
                            data = d
                            break
                except Exception:
                    continue
        checked += 1
        if data:
            for e in data["entries"]:
                if not isinstance(e, dict):
                    continue
                try:
                    if store.upsert_entry(conn, e, "crawl"):
                        got += 1
                except Exception:
                    # A malformed entry from a third party is expected, not
                    # exceptional. Skip it and keep crawling.
                    continue
            if got:
                found += 1; entries += got
        pending.append((dom, int(time.time()), "found" if got else "none", got))
        if len(pending) >= 400:
            conn.executemany("""INSERT INTO crawl_seen(domain,last_crawl,status,entries)
                VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE SET
                last_crawl=excluded.last_crawl, status=excluded.status,
                entries=excluded.entries""", pending)
            conn.commit(); pending.clear()

    try:
        # Chunked so a very large seed list does not build hundreds of thousands
        # of coroutine objects before a single request goes out.
        CH = 2000
        for i in range(0, len(todo), CH):
            batch = todo[i:i + CH]
            await asyncio.gather(*(one(d, clients[j % len(clients)])
                                   for j, d in enumerate(batch)))
            if pending:
                conn.executemany("""INSERT INTO crawl_seen(domain,last_crawl,status,entries)
                    VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE SET
                    last_crawl=excluded.last_crawl, status=excluded.status,
                    entries=excluded.entries""", pending)
                conn.commit(); pending.clear()
            print(f"    crawled {min(i+CH, len(todo))}/{len(todo)}  publishers={found}  entries={entries}",
                  flush=True)
    finally:
        for c_ in clients:
            await c_.aclose()
    return {"considered": len(domains), "crawled": checked,
            "skipped_recent": len(domains) - len(todo),
            "publishers_found": found, "entries": entries}
