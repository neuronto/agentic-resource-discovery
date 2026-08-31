"""ARD adoption tracking.

The spec's own adoption is unmeasured, and worth measuring carefully, because
the first careless measurement was wrong. Checking only `/.well-known/ard.json`
returned "0 of 20 organisations publish a manifest". The true figure is 3:
Hugging Face, Vercel and Zapier all publish, at the pre-v0.91
`/.well-known/ai-catalog.json` path that v0.91 renamed. Measuring the path
rather than the practice produced a false headline about named companies.

Our crawl already knows the other half: hosts that ship a callable agentic
resource but describe it nowhere a registry can find. That gap, publishable and
re-measurable, is the reason a publisher would use us rather than wait.

Two populations, kept separate because they answer different questions:

  * **watchlist** - named organisations, checked because who has and has not
    adopted is the news.
  * **crawled**   - every host our crawler has seen, aggregated, which gives the
    adoption *rate* rather than an anecdote.

We ask for every known well-known path per host and nothing else. A 200 that is
not JSON is not a manifest, and is recorded as absent.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from . import config

# Both paths, in spec order. v0.91 moved the manifest to `ard.json`, but the
# ecosystem has not: every publisher we checked still serves the pre-v0.91
# `ai-catalog.json` and 404s on `ard.json`. Checking only the new path told us
# "0 of 20 organisations publish a manifest" when the true answer was 3, because
# Hugging Face, Vercel and Zapier all publish at the old one. A tracker that
# measures the path instead of the practice is measuring the wrong thing.
_PATHS = ["/.well-known/ard.json", "/.well-known/ai-catalog.json"]


async def _check(client: httpx.AsyncClient, host: str) -> dict:
    """Try every known manifest path; report the first that actually parses."""
    last = None
    for path in _PATHS:
        try:
            r = await client.get(f"https://{host}{path}",
                                 timeout=config.ADOPTION_TIMEOUT_S,
                                 headers={"user-agent": config.USER_AGENT,
                                          "accept": "application/json"},
                                 follow_redirects=True)
        except Exception:
            last = None
            continue
        last = r.status_code
        if r.status_code != 200:
            continue
        # A 200 of HTML is a soft-404 or a SPA shell, not a manifest.
        try:
            body = r.json()
        except Exception:
            continue
        if isinstance(body, dict) and (body.get("entries") is not None
                                       or body.get("specVersion") or body.get("host")):
            return {"host": host, "has": 1, "status": r.status_code, "path": path,
                    "entries": len(body.get("entries") or [])}
    return {"host": host, "has": 0, "status": last, "path": None}


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
        conn.execute("""INSERT INTO adoption(host,has_manifest,status,entries,notable,checked,path)
                        VALUES(?,?,?,?,1,?,?)
                        ON CONFLICT(host) DO UPDATE SET
                          has_manifest=excluded.has_manifest, status=excluded.status,
                          entries=excluded.entries, notable=1, checked=excluded.checked,
                          path=excluded.path""",
                     (d["host"], d["has"], d.get("status"), d.get("entries") or 0, now,
                      d.get("path")))
    conn.commit()
    have = sum(d["has"] for d in out)
    return {"checked": len(out), "publishing": have, "absent": len(out) - have}


def report(conn) -> dict:
    """The publishable picture: the watchlist, plus the crawl-wide rate."""
    rows = conn.execute(
        """SELECT host, has_manifest, status, entries, checked, path
           FROM adoption WHERE notable=1 ORDER BY has_manifest DESC, host""").fetchall()
    watch = [{"host": r["host"],
              "publishes": bool(r["has_manifest"]),
              "status": r["status"],
              "path": (r["path"] if "path" in r.keys() else None),
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
        "method": ("one GET of each known manifest path per host, "
                   + " then ".join(_PATHS) +
                   "; a 200 that does not parse as an ARD manifest object is recorded as "
                   "absent. Both paths are checked because v0.91 renamed the file to "
                   "ard.json and the ecosystem still serves the older ai-catalog.json"),
    }
