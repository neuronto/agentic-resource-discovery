"""mcp.so, added 2026-09-18 as an index source.

WHY. ~18,700 servers in its sitemap, and **66% of a 3,741 URL sample was absent** from
our index, second only to Glama. Measured before building, as with the others.

NO API, BY DESIGN. `/api/` answers "only HTML requests are supported here" and is
Disallowed in robots.txt anyway. The page is server rendered, so the listing carries no
fetchable JSON and a headless browser found zero server links in the DOM within 14s.

THE CLIENT MATTERS. With the identical User-Agent, urllib gets 200 and httpx gets 403:
Cloudflare is fingerprinting the TLS/HTTP2 handshake, not reading the UA. That is the
same trap some government sites set for non-browser clients. curl_cffi is
the usual answer but is not installed here, and urllib already passes, so requests run
through urllib in a thread pool rather than adding a dependency to a live service.

THE ROUTE IN IS THE SITEMAP, which robots.txt advertises and does not disallow: 19
sections of ~1,000 `/servers/<slug>` URLs. That is the enumeration path the site
publishes for crawlers, so it is the one to use.

A SLUG IS NOT AN ENTRY. The sitemap gives URLs only, and this project's own rule is that
an entry without representative queries is one no semantic index can rank, so dumping
18,700 bare URLs would inflate the count and degrade the index. Each server page is
therefore read once for its meta description and its GitHub repository, which is enough
to write a real entry. Bounded concurrency and a time box keep that polite and keep the
job from running away.
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.request

from app import ingest, store

SITEMAP = "https://mcp.so/sitemap.xml"
LOC = re.compile(r"<loc>([^<]+)</loc>")
SLUG = re.compile(r"/servers/([^/?#]+)")
DESC = re.compile(r'<meta name="description" content="([^"]{6,400})"', re.I)
OGD = re.compile(r'<meta property="og:description" content="([^"]{6,400})"', re.I)
REPO = re.compile(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:\.git|[\"'<\s])")

# mcp.so links its OWN source repository in the chrome of every page, before the
# server's repository. Taking the first match filed 797 unrelated servers under
# that one URL, and liveness then probed mcp.so's repo and credited its uptime to
# each of them. A repository that is site chrome is never a server's.
SITE_REPOS = {"chatmcp/mcp-directory", "chatmcp/mcpso", "chatmcp/mcp.so"}


def server_repo(html: str) -> str | None:
    for m in REPO.finditer(html):
        r = m.group(1)
        if r.lower() not in SITE_REPOS:
            return f"https://github.com/{r}"
    return None


def _get(url: str, timeout: int = 25) -> tuple[int, str]:
    """urllib, because httpx is fingerprinted and refused. Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": ingest.HEADERS["user-agent"],
                                               "Accept": "text/html,application/xml,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception:
        return 0, ""


async def _aget(url: str) -> tuple[int, str]:
    return await asyncio.to_thread(_get, url)


async def _sections() -> list[str]:
    st, body = await _aget(SITEMAP)
    if st != 200:
        raise RuntimeError(f"sitemap HTTP {st}")
    return [u.replace("&amp;", "&") for u in LOC.findall(body)
            if "section=servers" in u]


async def from_mcpso(conn, max_servers: int = 25000, concurrency: int = 6,
                     max_seconds: float = 3600) -> dict:
    t0 = time.time()
    n = fetched = failed = 0
    slugs: list[str] = []

    try:
        sections = await _sections()
    except Exception as e:
        return {"source": "mcpso", "error": str(e)[:120]}

    for sec in sections:
        st, body = await _aget(sec)
        if st != 200:
            continue
        for u in LOC.findall(body):
            m = SLUG.search(u)
            if m:
                slugs.append(m.group(1))
        await asyncio.sleep(0.2)

    if True:
        seen = list(dict.fromkeys(slugs))[:max_servers]
        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()

        async def one(slug: str):
            nonlocal n, fetched, failed
            if time.time() - t0 > max_seconds:
                return
            async with sem:
                st, h = await _aget(f"https://mcp.so/servers/{slug}")
                if st != 200 or not h:
                    failed += 1
                    return
                await asyncio.sleep(0.12)           # a polite client

            fetched += 1
            m = DESC.search(h) or OGD.search(h)
            desc = (m.group(1).strip() if m else "")
            repo = server_repo(h)
            name = slug.replace("-", " ").strip()

            entry = {
                "identifier": f"urn:air:mcp.so:mcp:{slug}",
                "displayName": name,
                "type": "application/mcp-server-card+json",
                "url": repo or f"https://mcp.so/servers/{slug}",
                "description": desc,
                "tags": ["mcp", "mcp.so"],
                "_inferredQueries": ingest._queries_from(desc, name),
            }
            async with lock:
                if store.upsert_entry(conn, entry, "mcpso"):
                    n += 1
                if n and n % 200 == 0:
                    conn.commit()

        await asyncio.gather(*(one(s) for s in seen))
        conn.commit()

    return {"source": "mcpso", "entries": n, "urls_in_sitemap": len(slugs),
            "pages_fetched": fetched, "failed": failed,
            "seconds": round(time.time() - t0),
            "complete": fetched + failed >= len(set(slugs))}
