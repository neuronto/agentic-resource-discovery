"""Live fan-out to upstream registries.

Spec §5.4 defines three federation modes and makes `auto` the default: query
upstream registries, merge their results with your own, return one set. We
benchmarked all four registries that exist on 2026-08-31 and none of them
implements it, `auto` returned byte-identical results to `none` on every one.

Two properties matter here. The fan-out is concurrent and hard-bounded, because
the slowest live upstream answers in ~500 ms and one is dead, and no client
should wait on the slowest upstream. And a failing upstream is never fatal: we
return what we have, and say in the response which registries answered.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from . import config, events, fedcache
from .normalize import dedupe_key, media_family, normalize_identifier

_HEADERS = {"content-type": "application/json", "user-agent": config.USER_AGENT}


# ── circuit breaker ─────────────────────────────────────────────────────────
# Per upstream, per worker process: {"fails": consecutive failures,
# "open_until": epoch seconds, "probing": one half-open request in flight}.
# In memory on purpose. Four workers each learn within a few requests, and a
# breaker that survived restarts would keep a recovered upstream dark.
_breaker: dict[str, dict] = {}


def _state(uid: str) -> dict:
    return _breaker.setdefault(uid, {"fails": 0, "open_until": 0.0, "probing": False})


def breaker_allows(uid: str, now: float | None = None) -> bool:
    """True if this upstream should be queried now.

    Closed: always. Open and cooling: never. Open and cooled: exactly one
    probe at a time, so a dead upstream costs one request per cooldown rather
    than one per search.
    """
    st = _state(uid)
    now = time.time() if now is None else now
    if st["open_until"] <= now and st["fails"] < config.FED_BREAKER_FAILS:
        return True
    if st["open_until"] <= now and not st["probing"]:
        st["probing"] = True
        return True
    return False


def breaker_record(uid: str, ok: bool, now: float | None = None) -> None:
    st = _state(uid)
    now = time.time() if now is None else now
    st["probing"] = False
    if ok:
        if st["fails"] >= config.FED_BREAKER_FAILS:
            events.emit("fed_circuit", a=uid, b="closed")
        st["fails"] = 0
        st["open_until"] = 0.0
        return
    st["fails"] += 1
    if st["fails"] >= config.FED_BREAKER_FAILS:
        was_open = st["open_until"] > now
        st["open_until"] = now + config.FED_BREAKER_COOLDOWN_S
        if not was_open:
            events.emit("fed_circuit", a=uid, b="open")


def breaker_snapshot() -> dict:
    """For /stats and the tests: which circuits are open right now."""
    now = time.time()
    return {uid: {"fails": st["fails"],
                  "open": st["open_until"] > now or st["fails"] >= config.FED_BREAKER_FAILS,
                  "cooldown_s": max(0, int(st["open_until"] - now))}
            for uid, st in _breaker.items()}


# ── in-flight cap ───────────────────────────────────────────────────────────
_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    # Created lazily inside the running loop; a Semaphore made at import time
    # binds to whatever loop existed then, which under uvicorn is not this one.
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.FED_MAX_INFLIGHT)
    return _sem


async def _one(client: httpx.AsyncClient, up: tuple, text: str, page_size: int) -> dict:
    uid, name, url, source = up
    t0 = time.perf_counter()
    try:
        r = await client.post(url, json={"query": {"text": text}, "pageSize": page_size},
                              headers=_HEADERS, timeout=config.UPSTREAM_TIMEOUT_S)
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return {"id": uid, "name": name, "source": source, "ok": False,
                    "ms": ms, "error": f"http {r.status_code}", "results": []}
        data = r.json()
        results = data.get("results") or []
        clean = []
        for e in results:
            if not isinstance(e, dict):
                continue
            ident = normalize_identifier(e.get("identifier") or e.get("@id") or e.get("url"))
            u = e.get("url")
            if not ident and not u:
                continue
            clean.append({
                "key": dedupe_key(ident, u),
                "identifier": ident or u,
                "displayName": e.get("displayName"),
                "type": e.get("type") or e.get("mediaType"),
                "type_family": media_family(e.get("type") or e.get("mediaType")),
                "url": u,
                "description": e.get("description"),
                "tags": e.get("tags"),
                "capabilities": e.get("capabilities"),
                "representativeQueries": e.get("representativeQueries"),
                "source": source,
            })
        return {"id": uid, "name": name, "source": source, "ok": True,
                "ms": ms, "results": clean}
    except Exception as exc:
        return {"id": uid, "name": name, "source": source, "ok": False,
                "ms": int((time.perf_counter() - t0) * 1000),
                "error": type(exc).__name__, "results": []}


async def fan_out(text: str, page_size: int = 20,
                  budget_ms: int | None = None) -> list[dict]:
    """Query every upstream at once and return whatever arrives in budget.

    `asyncio.wait` with a deadline rather than gather, so one hung upstream
    costs us the budget and nothing more. Pending calls are cancelled; we do not
    wait on them to unwind.
    """
    if not config.FEDERATION_ENABLED or not config.UPSTREAMS:
        return []
    budget = (budget_ms or config.FEDERATION_BUDGET_MS) / 1000.0

    # Capacity first. Under a spike the honest answer to "search every
    # registry" is sometimes "not right now": the caller gets the local result
    # promptly and the federation block says so, instead of every caller
    # queueing behind every other one and all of them timing out together.
    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=config.FED_SHED_WAIT_S)
    except asyncio.TimeoutError:
        events.emit("fed_shed")
        return [{"id": up[0], "name": up[1], "source": up[3], "ok": False,
                 "ms": 0, "error": "capacity", "results": []}
                for up in config.UPSTREAMS]

    try:
        skipped = [up for up in config.UPSTREAMS if not breaker_allows(up[0])]
        live = [up for up in config.UPSTREAMS if up not in skipped]
        limits = httpx.Limits(max_connections=len(live) + 2,
                              max_keepalive_connections=len(live) + 2)
        # Not `async with`: stragglers keep running after the budget, so the
        # client has to outlive this function and is closed by whoever finishes
        # last.
        client = httpx.AsyncClient(limits=limits, follow_redirects=True)
        tasks = [asyncio.create_task(_one(client, up, text, page_size)) for up in live]
        done, pending = await asyncio.wait(tasks, timeout=budget) if tasks else (set(), set())
    finally:
        # Released as soon as the budget window closes. Stragglers finishing in
        # the background are not a fan-out the caller is waiting on.
        sem.release()

    out = []
    for d in done:
        try:
            r = d.result()
            out.append(r)
            breaker_record(r["id"], bool(r.get("ok")))
            if r.get("ok") and r.get("results"):
                fedcache.put(r["id"], text, r["results"])
        except Exception:
            pass
    for up in skipped:
        # Listed, not hidden. A federating registry that quietly drops an
        # upstream is serving a smaller index than it claims.
        out.append({"id": up[0], "name": up[1], "source": up[3], "ok": False,
                    "ms": 0, "error": "circuit open", "results": []})

    for up in config.UPSTREAMS:
        if any(o["id"] == up[0] for o in out):
            continue
        # An upstream that missed the budget may have answered this same query
        # recently. Serving that is far better than dropping a whole registry,
        # but it is labelled: `cached` with the age, never dressed up as a live
        # reply. Claiming to have queried a registry we did not would make the
        # federation claim untrue, which costs more than the coverage is worth.
        hit = fedcache.get(up[0], text)
        if hit is not None:
            results, age = hit
            out.append({"id": up[0], "name": up[1], "source": up[3], "ok": True,
                        "ms": int(budget * 1000), "cached": True, "age_s": age,
                        "results": results})
        else:
            out.append({"id": up[0], "name": up[1], "source": up[3], "ok": False,
                        "ms": int(budget * 1000), "error": "budget exceeded",
                        "results": []})
            # Deliberately NOT a breaker failure. The budget is OUR deadline;
            # under a burst every leg can miss it because this worker was
            # busy, and on 2026-09-04 that opened all five circuits on one
            # worker, three of them for upstreams that were perfectly healthy.
            # An upstream's own timeout or error status is its failure. Our
            # aggregate deadline is not.

    if pending and len(_running) < _MAX_FINISHERS:
        # The task object itself is held, not a token: asyncio keeps only a
        # weak reference to a running task, so one whose only reference is the
        # create_task return value can be garbage collected mid-flight. That is
        # exactly what happened first time here, and the symptom was a cache
        # that quietly never filled for the one upstream it exists for.
        t = asyncio.create_task(_finish(client, pending, text))
        _running.add(t)
        t.add_done_callback(_running.discard)
    else:
        for p in pending:
            p.cancel()
        await client.aclose()
    return out


# Background completion of upstreams that missed the budget. Bounded, because
# an unbounded number of detached tasks under load is how a small box dies.
_MAX_FINISHERS = int(os.getenv("NEURONTO_FED_FINISHERS", "8"))
_running: set = set()


async def _finish(client: httpx.AsyncClient, pending: set, text: str) -> None:
    """Let the slow ones land, keep what they said, then close the client.

    Nothing here can affect the response that has already been returned. Its
    only job is to make the next caller's answer more complete.
    """
    try:
        done, still = await asyncio.wait(pending, timeout=_FINISH_GRACE_S)
        for d in done:
            try:
                r = d.result()
            except Exception:
                continue
            if r.get("ok"):
                # It answered, just late. That is a live upstream, and the next
                # caller should not find its circuit open because of one slow
                # reply that did in fact arrive.
                breaker_record(r["id"], True)
            if r.get("ok") and r.get("results"):
                fedcache.put(r["id"], text, r["results"])
        for p in still:
            p.cancel()
    except Exception:
        pass
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


_FINISH_GRACE_S = float(os.getenv("NEURONTO_FED_FINISH_GRACE", "8"))


def referral_entries() -> list[dict]:
    """The other registries, as ARD entries, for `federation: referrals`.

    Typed `application/ai-registry+json`, which §5.3 names as how a registry's
    base URL is discovered. Pointing at the others is not generosity: federation
    is reciprocal in practice, and a registry that refuses to refer is one
    nobody has a reason to refer back to.
    """
    return [{
        "identifier": f"urn:air:{src.split('//')[-1].split('/')[0]}:registry:{uid}",
        "displayName": name,
        "type": "application/ai-registry+json",
        "url": src,
        "description": f"{name}, an upstream ARD discovery service federated by Neuronto.",
    } for uid, name, _url, src in config.UPSTREAMS]
