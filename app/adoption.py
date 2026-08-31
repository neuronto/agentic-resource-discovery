"""ARD adoption tracking.

The spec's own adoption is unmeasured, and the measurement is unflattering in a
way that is worth publishing: as of this writing not one of the organisations
associated with the working group serves `/.well-known/ard.json`. GitHub and
Hugging Face run ARD registries and publish no ARD manifest themselves.

Our crawl already knows the other half: hosts that ship a callable agentic
resource but describe it nowhere a registry can find. That gap, publishable and
re-measurable, is the reason a publisher would use us rather than wait.

Two populations, kept separate because they answer different questions:

  * **watchlist** - named organisations, checked because who has and has not
    adopted is the news.
  * **crawled**   - every host our crawler has seen, aggregated, which gives the
    adoption *rate* rather than an anecdote.

We ask for one well-known path per host and nothing else. A 200 that is not
JSON is not a manifest, and is recorded as absent.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from . import config

_PATH = "/.well-known/ard.json"


async def _check(client: httpx.AsyncClient, host: str) -> dict:
    url = f"https://{host}{_PATH}"
    try:
        r = await client.get(url, timeout=config.ADOPTION_TIMEOUT_S,
                             headers={"user-agent": config.USER_AGENT,
                                      "accept": "application/json"},
                             follow_redirects=True)
    except Exception:
        return {"host": host, "has": 0, "status": None}
    if r.status_code != 200:
        return {"host": host, "has": 0, "status": r.status_code}
    # A 200 of HTML is a soft-404 or a SPA shell, not a manifest.
    try:
        body = r.json()
    except Exception:
        return {"host": host, "has": 0, "status": r.status_code}
    ok = isinstance(body, dict) and bool(
        body.get("entries") is not None or body.get("specVersion") or body.get("host"))
    return {"host": host, "has": 1 if ok else 0, "status": r.status_code,
            "entries": len(body.get("entries") or []) if ok else 0}


async def refresh_watchlist(conn) -> dict:
    """Re-probe the named organisations."""
    hosts = list(dict.fromkeys(config.ADOPTION_WATCHLIST))
    sem = asyncio.Semaphore(config.ADOPTION_CONCURRENCY)
    now = int(time.time())
    out = []
    async with httpx.AsyncClient() as client:
        async def one(h):
            async with sem:
                return await _check(client, h)
        out = await asyncio.gather(*(one(h) for h in hosts))
    for d in out:
        conn.execute("""INSERT INTO adoption(host,has_manifest,status,entries,notable,checked)
                        VALUES(?,?,?,?,1,?)
                        ON CONFLICT(host) DO UPDATE SET
                          has_manifest=excluded.has_manifest, status=excluded.status,
                          entries=excluded.entries, notable=1, checked=excluded.checked""",
                     (d["host"], d["has"], d.get("status"), d.get("entries") or 0, now))
    conn.commit()
    have = sum(d["has"] for d in out)
    return {"checked": len(out), "publishing": have, "absent": len(out) - have}


def report(conn) -> dict:
    """The publishable picture: the watchlist, plus the crawl-wide rate."""
    rows = conn.execute(
        """SELECT host, has_manifest, status, entries, checked
           FROM adoption WHERE notable=1 ORDER BY has_manifest DESC, host""").fetchall()
    watch = [{"host": r["host"],
              "publishes": bool(r["has_manifest"]),
              "status": r["status"],
              "entries": r["entries"] or 0,
              "checked": r["checked"]} for r in rows]

    # Crawl-wide: hosts we visited, and how many carried a manifest. crawl_seen
    # records every domain the crawler resolved; `entries > 0` means we parsed a
    # manifest there.
    tot = conn.execute("SELECT COUNT(*) FROM crawl_seen").fetchone()[0]
    with_m = conn.execute(
        "SELECT COUNT(*) FROM crawl_seen WHERE entries > 0").fetchone()[0]

    # Publishers we hold entries for, by whether the resource is callable.
    callable_hosts = conn.execute(
        """SELECT COUNT(DISTINCT publisher) FROM entries
           WHERE publisher IS NOT NULL AND url LIKE 'http%'""").fetchone()[0]

    return {
        "watchlist": {
            "hosts": len(watch),
            "publishing": sum(1 for w in watch if w["publishes"]),
            "detail": watch,
        },
        "crawl": {
            "hosts_seen": tot,
            "hosts_with_manifest": with_m,
            "rate": round(with_m / tot, 5) if tot else 0.0,
        },
        "index": {"publishers_with_callable_resource": callable_hosts},
        "method": (f"one GET of {_PATH} per host; a 200 that does not parse as an "
                   "ARD manifest object is recorded as absent"),
    }
