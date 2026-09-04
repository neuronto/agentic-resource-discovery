"""Probe whether an indexed resource actually answers.

Our ERC-8004 work measured 1.7% of one chain's registered agent endpoints
responding at all, and 87% of x402 services scored as spam by the one grader
that checks. Registries built on self-published manifests inherit that rot
unless somebody looks. Serving entries that point at nothing is the fastest way
to become the index nobody trusts.

We demote rather than delete (see rank.apply_liveness), and we treat any HTTP
answer below 500 as alive: 401, 403 and 405 all mean a server is there and
talking, which is the question being asked.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from . import config, store


async def _probe(client: httpx.AsyncClient, url: str) -> tuple[bool, int | None, int]:
    """One endpoint, under a hard deadline.

    The timeout below is httpx's budget for a single request, and this client
    follows redirects, so without the outer deadline one URL with a long chain
    can hold a probe slot for minutes. It did: the first sweep on the live box
    was killed at thirty minutes having committed nothing, while sixty ordinary
    endpoints measured under eight seconds.
    """
    t0 = time.perf_counter()
    try:
        r = await asyncio.wait_for(
            client.get(url, timeout=config.LIVENESS_TIMEOUT_S,
                       headers={"user-agent": config.USER_AGENT},
                       follow_redirects=True),
            timeout=config.LIVENESS_DEADLINE_S)
        ms = int((time.perf_counter() - t0) * 1000)
        return (r.status_code < 500), r.status_code, ms
    except Exception:
        # Includes the deadline. "We asked and nothing usable came back inside
        # the time we allow" is the same answer as any other failed probe.
        return False, None, int((time.perf_counter() - t0) * 1000)


async def sweep(conn, limit: int = 400, only_stale: bool = True) -> dict:
    """Probe a batch, oldest checks first."""
    cutoff = int(time.time()) - config.LIVENESS_MAX_AGE_H * 3600
    if only_stale:
        rows = conn.execute(
            """SELECT key, url FROM entries
               WHERE url IS NOT NULL AND url != ''
                 AND (live_checked IS NULL OR live_checked < ?)
               ORDER BY (live_checked IS NULL) DESC, live_checked ASC
               LIMIT ?""", (cutoff, limit)).fetchall()
    else:
        rows = conn.execute(
            """SELECT key, url FROM entries WHERE url IS NOT NULL AND url != ''
               LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return {"probed": 0, "alive": 0, "dead": 0}

    # Same shape as the embed loop, and the same trap: read, network, write.
    # Ending the read transaction here means the writes below open a fresh one
    # and get the busy handler, instead of an instant BUSY on a stale snapshot.
    conn.commit()

    sem = asyncio.Semaphore(config.LIVENESS_CONCURRENCY)
    alive = dead = probed = 0

    # Chunked, because the unit of lost work is whatever has not been committed.
    # The first run of this on the live box was killed at its timeout having
    # probed most of eight hundred endpoints and written none of them: the whole
    # batch was one transaction's worth of progress. A chunk still obeys the rule
    # below (no write while any probe is in flight), it just makes the rule cost
    # a chunk instead of a sweep.
    # No keep-alive: each host is visited once, so a pooled connection is never
    # reused, and a connection whose request was cancelled by the deadline is
    # never held on to. Both matter for the teardown below.
    client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=config.LIVENESS_CONCURRENCY * 2,
                            max_keepalive_connections=0),
        max_redirects=5)
    try:
        for start in range(0, len(rows), config.LIVENESS_CHUNK):
            chunk = rows[start:start + config.LIVENESS_CHUNK]
            found: list[tuple] = []

            async def one(key: str, url: str):
                async with sem:
                    ok, status, ms = await _probe(client, url)
                found.append((key, ok, status, ms))
            await asyncio.gather(*(one(r["key"], r["url"]) for r in chunk),
                                 return_exceptions=True)

            # Writes go after the network phase, never inside the gather. SQLite
            # takes one writer, and a transaction opened on the first probe used
            # to stay open until the last one returned, holding the lock for the
            # length of the whole sweep and refusing anyone trying to submit
            # during it.
            for key, ok, status, ms in found:
                store.mark_liveness(conn, key, ok, status, ms)
                if ok: alive += 1
                else:  dead += 1
            conn.commit()
            probed += len(chunk)
    finally:
        # Closing waits on connections, and the deadline above cancels requests
        # mid-flight, so this can hang: it did, for sixteen minutes, after a
        # sweep whose every result was already committed. Nothing is at risk by
        # then, so an overrunning teardown is abandoned rather than waited on.
        try:
            await asyncio.wait_for(client.aclose(), timeout=config.LIVENESS_DEADLINE_S)
        except Exception:
            pass

    return {"probed": probed, "alive": alive, "dead": dead}
