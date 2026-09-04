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
import sqlite3
import json
import time

import httpx

from . import config, store

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
                    # Commit per query, not per upstream. SQLite takes one
                    # writer, and an open transaction here spanned every
                    # remaining POST in the battery: the lock was held for the
                    # eight minutes this stage runs, and a publisher submitting
                    # in that window was refused with "index is mid-maintenance"
                    # after waiting out the busy timeout. Happened to a real
                    # publisher on 2026-09-01, three times in five minutes.
                    conn.commit()
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



def _flush(conn, pending: list[tuple], tries: int = 6) -> bool:
    """Write the crawl_seen batch, waiting out a busy writer.

    An unhandled `database is locked` here used to end the whole crawl. With a
    supervised service that means a restart loop that never advances, because
    the next run hits the same contended moment. Retry with backoff, and on
    persistent failure keep the batch rather than dropping the record of work
    already done.
    """
    if not pending:
        return True
    delay = 0.5
    for _ in range(tries):
        try:
            conn.executemany("""INSERT INTO crawl_seen(domain,last_crawl,status,entries,manifest_path)
                VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET
                last_crawl=excluded.last_crawl, status=excluded.status,
                entries=excluded.entries,
                -- Never overwrite an observed path with NULL: a later crawl
                -- that times out must not erase what an earlier one saw.
                manifest_path=COALESCE(excluded.manifest_path, crawl_seen.manifest_path)""",
                pending)
            conn.commit()
            pending.clear()
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            time.sleep(delay)
            delay = min(delay * 2, 20)
    print("    warning: could not commit crawl batch, will retry next flush", flush=True)
    return False


async def fetch_manifest(dom: str, client: httpx.AsyncClient | None = None
                         ) -> tuple[dict | None, str | None]:
    """The network half of a crawl: the first well-known path that answers with
    a manifest, and which path it was. No database, so it can run on the event
    loop with nothing held."""
    base = dom if dom.startswith("http") else f"https://{dom}"
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=HEADERS, timeout=config.CRAWL_TIMEOUT_S,
                                   follow_redirects=True)
    try:
        for path in PATHS:
            try:
                r = await client.get(base + path)
                if r.status_code == 200 and r.headers.get("content-type", "").find("json") >= 0:
                    d = r.json()
                    if isinstance(d, dict) and isinstance(d.get("entries"), list):
                        return d, path
            except Exception:
                continue
    finally:
        if own:
            await client.aclose()
    return None, None


def index_manifest(conn, dom: str, data: dict, hit_path: str | None,
                   strict: bool = True, source: str = "crawl") -> int:
    """The write half: upsert every entry of a fetched manifest in one
    transaction and record the crawl. Pure SQLite, so a request can run it in
    a thread on its own connection (`main._index_write`) and never on the loop.

    `source` is who caused this, not who wrote it. It defaults to `crawl` because
    the bulk crawler is the usual caller, and the submit doors pass `submitted`
    so a deliberate submission is not filed as something we happened to find.
    Getting this wrong is invisible until someone counts submissions from the
    entries table and publishes the answer.

    `strict` is for the submit path: a locked index must surface as the
    exception so the caller can answer `busy` and queue the submission. The
    bulk crawler passes False and rides through contention entry by entry, as
    it always did, because one dropped entry there is cheaper than one dropped
    domain."""
    got = 0
    for e in data.get("entries") or []:
        if not isinstance(e, dict):
            continue
        try:
            if store.upsert_entry(conn, e, source):
                got += 1
        except sqlite3.OperationalError:
            if strict:
                raise
            # Contended writer: give it a moment rather than dropping
            # the publisher we just successfully fetched.
            time.sleep(1.0)
            try:
                if store.upsert_entry(conn, e, source):
                    got += 1
            except Exception:
                continue
        except Exception:
            # A malformed entry from a third party is expected, not
            # exceptional. Skip it and keep crawling.
            continue
    if strict:
        conn.execute("""INSERT INTO crawl_seen(domain,last_crawl,status,entries,manifest_path)
            VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET
            last_crawl=excluded.last_crawl, status=excluded.status,
            entries=excluded.entries,
            manifest_path=COALESCE(excluded.manifest_path, crawl_seen.manifest_path)""",
            (dom, int(time.time()), "found" if got else "none", got, hit_path))
        conn.commit()
    else:
        # Bound the transaction to one domain. These writes run inside a
        # gather of up to 2000 coroutines, so without this the lock is held
        # for the whole chunk while every one of them is still fetching.
        try:
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return got


async def crawl_domains(conn, domains: list[str], concurrency: int | None = None,
                        skip_seen_hours: int = 168) -> dict:
    """Fetch the well-known paths across a domain list.

    This is where an index is actually won. The only other general crawler in
    the ecosystem covers the Tranco top 100K, so every publisher below that rank
    is invisible everywhere, and those are precisely the ones who need finding.

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
        async with sem:
            data, hit_path = await fetch_manifest(dom, client)
        checked += 1
        got = 0
        if data:
            got = index_manifest(conn, dom, data, hit_path, strict=False)
            if got:
                found += 1; entries += got
        # Record WHICH path answered. This used to be dropped here, so every
        # publisher the crawler found had a manifest and no record of where,
        # and `verified_manifests` (which counts a non-null path) undercounted
        # by every domain crawled since the column was added. The value was
        # known three lines up the whole time.
        pending.append((dom, int(time.time()), "found" if got else "none", got, hit_path))
        if len(pending) >= 400:
            _flush(conn, pending)

    try:
        # Chunked so a very large seed list does not build hundreds of thousands
        # of coroutine objects before a single request goes out.
        CH = 2000
        for i in range(0, len(todo), CH):
            batch = todo[i:i + CH]
            await asyncio.gather(*(one(d, clients[j % len(clients)])
                                   for j, d in enumerate(batch)))
            _flush(conn, pending)
            print(f"    crawled {min(i+CH, len(todo))}/{len(todo)}  publishers={found}  entries={entries}",
                  flush=True)
    finally:
        for c_ in clients:
            await c_.aclose()
    return {"considered": len(domains), "crawled": checked,
            "skipped_recent": len(domains) - len(todo),
            "publishers_found": found, "entries": entries}
