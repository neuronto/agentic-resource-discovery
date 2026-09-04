#!/usr/bin/env python3
"""Circuit breaker and in-flight cap for the federation fan-out. No network:
`_one` is replaced with a fake upstream whose behaviour the test controls.

    python scripts/test_federation.py
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NEURONTO_DB", "/tmp/neuronto-test.db")
# Never let a unit test's fake upstreams ("x", "y", "d") land in the live
# analytics: the first run of this file put eight of them there.
os.environ["NEURONTO_EVENT_SINK"] = ""
from app import config, federation as F  # noqa: E402

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS += cond; FAIL += (not cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")

def test_breaker_opens_after_n_failures():
    F._breaker.clear()
    for _ in range(config.FED_BREAKER_FAILS - 1):
        F.breaker_record("x", False)
    check("still allowed below the threshold", F.breaker_allows("x"))
    F.breaker_record("x", False)
    check("open after N consecutive failures", not F.breaker_allows("x"))
    check("snapshot reports it open", F.breaker_snapshot()["x"]["open"])

def test_breaker_half_open_probe_then_close():
    F._breaker.clear()
    now = time.time()
    for _ in range(config.FED_BREAKER_FAILS):
        F.breaker_record("y", False, now=now)
    later = now + config.FED_BREAKER_COOLDOWN_S + 1
    check("one probe allowed after cooldown", F.breaker_allows("y", now=later))
    check("a second concurrent probe is not", not F.breaker_allows("y", now=later))
    F.breaker_record("y", True, now=later)
    check("success closes the circuit", F.breaker_allows("y", now=later + 1))
    check("failure count reset", F._breaker["y"]["fails"] == 0)

def test_success_resets_partial_failures():
    F._breaker.clear()
    F.breaker_record("z", False); F.breaker_record("z", False); F.breaker_record("z", True)
    for _ in range(config.FED_BREAKER_FAILS - 1):
        F.breaker_record("z", False)
    check("a success in between resets the streak", F.breaker_allows("z"))

async def _fake_one(client, up, text, page_size, delay=0.3):
    await asyncio.sleep(delay)
    return {"id": up[0], "name": up[1], "source": up[3], "ok": True, "ms": 300,
            "results": [{"key": f"k-{up[0]}", "identifier": f"urn:{up[0]}", "url": None,
                         "displayName": "t", "type": None, "type_family": None,
                         "description": None, "tags": None, "capabilities": None,
                         "representativeQueries": None, "source": up[3]}]}

async def _run_semaphore_test():
    F._breaker.clear(); F._sem = None
    orig_one, orig_ups = F._one, config.UPSTREAMS
    F._one = _fake_one
    config.UPSTREAMS = [("a", "A", "http://a", "http://a")]
    try:
        n = config.FED_MAX_INFLIGHT + 3
        outs = await asyncio.gather(*[F.fan_out("q", 5) for _ in range(n)])
    finally:
        F._one, config.UPSTREAMS = orig_one, orig_ups
    shed = sum(1 for o in outs if o and o[0].get("error") == "capacity")
    served = sum(1 for o in outs if o and o[0].get("ok"))
    check(f"cap holds: {served} served, {shed} shed of {n}", served == config.FED_MAX_INFLIGHT and shed == 3)
    check("shed answer still lists every upstream", all(len(o) == 1 for o in outs))

async def _run_breaker_skips_dead_upstream():
    F._breaker.clear(); F._sem = None
    async def dead(client, up, text, page_size):
        return {"id": up[0], "name": up[1], "source": up[3], "ok": False, "ms": 1,
                "error": "ReadTimeout", "results": []}
    orig_one, orig_ups = F._one, config.UPSTREAMS
    F._one = dead
    config.UPSTREAMS = [("d", "Dead", "http://d", "http://d")]
    try:
        for _ in range(config.FED_BREAKER_FAILS):
            await F.fan_out("q", 5)
        out = await F.fan_out("q", 5)
    finally:
        F._one, config.UPSTREAMS = orig_one, orig_ups
    check("dead upstream is skipped and labelled", out[0]["error"] == "circuit open")

if __name__ == "__main__":
    test_breaker_opens_after_n_failures()
    test_breaker_half_open_probe_then_close()
    test_success_resets_partial_failures()
    asyncio.run(_run_semaphore_test())
    asyncio.run(_run_breaker_skips_dead_upstream())
    print(f"  {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
