"""Bounded in-process page copies, bounded rebuild threads, protected lexical results.

    python -m scripts.test_bounds
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import pagecache, rank, search  # noqa: E402

FAILS = []


def check(name, ok):
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok:
        FAILS.append(name)


# 1. the in-process copy never exceeds its byte budget and keeps the recent pages
lru = pagecache._Lru(1000)
for i in range(50):
    lru[f"k{i}"] = (1.0, "x" * 100)
check("bytes stay within the budget", lru._bytes <= 1000 and len(lru) <= 10)
check("the most recent page is kept", lru.get("k49") is not None and lru.get("k0") is None)
lru["big"] = (1.0, "y" * 600)
check("a page above a quarter of the budget is not kept", lru.get("big") is None)
lru.clear()
check("clear empties it and zeroes the count", len(lru) == 0 and lru._bytes == 0)

# 2. stale rebuilds are capped and a failed thread start never raises
started = []
orig = threading.Thread


class Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


pagecache._claim_miss.clear()
pagecache._building.clear()
threading.Thread = Boom
try:
    pagecache._stale_rebuild("a", lambda: "x")
    check("a thread that cannot start does not raise", True)
    check("and leaves nothing marked as building", "a" not in pagecache._building)
finally:
    threading.Thread = orig
pagecache._building.update(f"b{i}" for i in range(pagecache._MAX_REBUILDS))
n = threading.active_count()
pagecache._stale_rebuild("c", lambda: "x")
check("past the cap no further rebuild thread starts", threading.active_count() == n and "c" not in pagecache._building)
pagecache._building.clear()

# 3. the lexical leg's top results stay on the page
mk = lambda k: {"_key": k, "identifier": k}
local = [mk(f"l{i}") for i in range(5)]
by = {e["_key"]: e for e in local}
page = [mk(f"t{i}") for i in range(10)]
for e in page:
    by[e["_key"]] = e
out = search.protect_lexical(page, local, by)
keys = [e["_key"] for e in out]
check("page length unchanged", len(out) == 10)
check("the lexical top three are on the page", all(f"l{i}" in keys for i in range(3)))
check("the tool-ranked head keeps its order", keys[:7] == [f"t{i}" for i in range(7)])
check("nothing outside the top three is added", "l3" not in keys)
check("already present entries are not duplicated", len(set(keys)) == 10)
print("FAILED" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
