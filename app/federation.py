"""Live fan-out to upstream registries.

Spec §5.4 defines three federation modes and makes `auto` the default: query
upstream registries, merge their results with your own, return one set. We
benchmarked all four registries that exist on 2026-08-31 and none of them
implements it, `auto` returned byte-identical results to `none` on every one.

Two properties matter here. The fan-out is concurrent and hard-bounded, because
GitHub's finder answers in ~2.0 s and Hugging Face in ~1.7 s, and no client
should wait on the slowest upstream. And a failing upstream is never fatal: we
return what we have, and say in the response which registries answered.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from . import config, fedcache
from .normalize import dedupe_key, media_family, normalize_identifier

_HEADERS = {"content-type": "application/json", "user-agent": config.USER_AGENT}


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
    limits = httpx.Limits(max_connections=len(config.UPSTREAMS) + 2,
                          max_keepalive_connections=len(config.UPSTREAMS) + 2)
    # Not `async with`: stragglers keep running after the budget, so the client
    # has to outlive this function and is closed by whoever finishes last.
    client = httpx.AsyncClient(limits=limits, follow_redirects=True)
    tasks = [asyncio.create_task(_one(client, up, text, page_size))
             for up in config.UPSTREAMS]
    done, pending = await asyncio.wait(tasks, timeout=budget)

    out = []
    for d in done:
        try:
            r = d.result()
            out.append(r)
            if r.get("ok") and r.get("results"):
                fedcache.put(r["id"], text, r["results"])
        except Exception:
            pass

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
