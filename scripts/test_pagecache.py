#!/usr/bin/env python3
"""The page cache serves a stale page rather than no page, and rebuilds once.

No network and no index: a temporary cache file and build functions whose calls
the test can see.

    python scripts/test_pagecache.py
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp(prefix="neuronto-pagecache-")
os.environ["NEURONTO_PAGECACHE_DB"] = os.path.join(_tmp, "pagecache.db")
os.environ.setdefault("NEURONTO_DB", os.path.join(_tmp, "neuronto.db"))
os.environ["NEURONTO_EVENT_SINK"] = ""
from app import pagecache as R  # noqa: E402

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS += bool(cond); FAIL += (not cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")

def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False

def slow(value, calls):
    def build():
        calls.append(value)
        time.sleep(0.3)
        return f"<p>{value}</p>"
    return build

def test_invalidate_serves_stale_and_rebuilds_behind():
    calls = []
    check("a missing page is built and served", R.cached("page", 600, slow("one", calls)) == "<p>one</p>")
    check("a fresh page is not rebuilt",
          R.cached("page", 600, slow("x", calls)) == "<p>one</p>" and calls == ["one"])
    time.sleep(0.01)
    R.invalidate()
    t0 = time.time()
    got = R.cached("page", 600, slow("two", calls))
    check("after invalidate the previous page is served without waiting",
          got == "<p>one</p>" and time.time() - t0 < 0.2)
    check("and the page is rebuilt behind the request",
          wait_for(lambda: R.cached("page", 600, slow("three", calls)) == "<p>two</p>"))
    check("exactly one rebuild ran", calls.count("two") == 1 and "three" not in calls)

def test_named_keys_are_removed():
    R.cached("pub-example.com", 600, lambda: "<p>old</p>")
    R.invalidate(["pub-example.com"])
    check("a named page is rebuilt before it is served again",
          R.cached("pub-example.com", 600, lambda: "<p>new</p>") == "<p>new</p>")

def test_one_rebuild_claim_at_a_time():
    R._claim_miss.clear()
    check("the first claim wins", R._claim_build("k1"))
    check("a second claim loses while the first is held", not R._claim_build("k1"))
    R._release_build("k1")
    check("a released claim can be taken again", R._claim_build("k1"))
    R._release_build("k1")

def test_another_workers_invalidation_counts_here():
    R.cached("shared", 600, lambda: "<p>a</p>")
    time.sleep(0.01)
    c = R._cdb()
    c.execute("INSERT INTO pages(key,built,html) VALUES(?,?,'') "
              "ON CONFLICT(key) DO UPDATE SET built=excluded.built", (R._GEN_KEY, time.time()))
    c.commit()
    R._gen["read"] = 0.0          # as if a second had passed since this worker last looked
    time.sleep(0.01)
    got = R.cached("shared", 600, lambda: "<p>b</p>")
    check("a page built before another worker invalidated is stale here too",
          got == "<p>a</p>" and wait_for(lambda: R.cached("shared", 600, lambda: "<p>c</p>") == "<p>b</p>"))

def test_warm_skips_fresh_and_builds_stale():
    check("warm builds a missing page", R.warm("w", 600, lambda: "<p>w1</p>"))
    check("warm leaves a fresh page alone", not R.warm("w", 600, lambda: "<p>w2</p>"))
    time.sleep(0.01)
    R.invalidate()
    check("warm rebuilds a page made stale by invalidate", R.warm("w", 600, lambda: "<p>w3</p>"))
    check("and the rebuilt page is what is served", R.cached("w", 600, lambda: "<p>w4</p>") == "<p>w3</p>")

if __name__ == "__main__":
    test_invalidate_serves_stale_and_rebuilds_behind()
    test_named_keys_are_removed()
    test_one_rebuild_claim_at_a_time()
    test_another_workers_invalidation_counts_here()
    test_warm_skips_fresh_and_builds_stale()
    print(f"  {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
