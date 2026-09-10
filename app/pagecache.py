"""The page cache: shared by every worker, durable across restarts, and never empty.

Anything the index derives that is too expensive to compute per request lives
here, keyed by name and by a stamp of the code that produced it: generated
pages when a deployment renders them, and JSON values such as the published
capability map. Stale entries are served while they rebuild, and invalidation
marks entries stale rather than removing them.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

_cache: dict[str, tuple[float, str]] = {}


# The page cache is on disk, shared by every worker, and survives a restart.
#
# It began as a dict per worker, and the failure was measured rather than
# theorised: the capability index took **22.4 seconds** to build and 2ms to
# serve, the service runs several workers so each cold miss happened once per
# worker, every deploy emptied all of them, and the reverse proxy gives up at 30
# seconds. Under crawl load that became a 504 on a page that is fast in the warm
# case, which is the worst possible shape of slow.
#
# Three properties fix it, and the third is the one that matters:
#   * shared, so one worker's work serves the others
#   * durable, so a restart does not send the next visitor to the back of a
#     22 second queue
#   * **stale while revalidate**, so an expired entry is served immediately and
#     rebuilt behind the request. After the very first build of a key, nobody
#     ever waits for a rebuild again.
_CACHE_DB = Path(os.getenv("NEURONTO_PAGECACHE_DB",
                           str(Path(os.getenv("NEURONTO_DB", "./data/neuronto.db")).parent
                               / "pagecache.db")))
# A durable page cache means a template change does not reach anybody until the
# cache is cleared, and forgetting is silent: the site keeps serving correct
# looking HTML built by the previous deploy. The beacon shipped and appeared on
# no cached page for exactly this reason. So the key carries a stamp derived
# from the code that renders the page; when that changes, every entry is a miss
# and rebuilds itself. No deploy step to remember.
_STAMP_INPUTS: list[Path] = []
_stamp: str | None = None


def add_stamp_inputs(paths) -> None:
    """Files outside the engine whose changes must invalidate cached pages.

    A deployment that renders pages registers its templates and page code here.
    A cache key that misses an input invalidates nothing, and the failure is
    silent: the page deploys, restarts, passes /health and serves the old build.
    """
    global _stamp
    _STAMP_INPUTS.extend(Path(p) for p in paths)
    _stamp = None


def _build_stamp() -> str:
    h = hashlib.sha1()
    here = Path(__file__).resolve().parent
    # main.py builds several documents inline (/llms.txt, /agents.md), so it is an
    # input as much as the data modules are.
    files = [here / n for n in ("pagecache.py", "directory.py", "badge.py", "main.py")]
    files += _STAMP_INPUTS
    for f in files:
        try:
            st = f.stat()
            h.update(f"{f}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            pass
    return h.hexdigest()[:8]


def stamp() -> str:
    global _stamp
    if _stamp is None:
        _stamp = _build_stamp()
    return _stamp
_local = threading.local()
_building: set[str] = set()
_build_lock = threading.Lock()


def _cdb() -> sqlite3.Connection | None:
    """One connection per thread. SQLite objects are not shareable across them."""
    c = getattr(_local, "conn", None)
    if c is not None:
        return c
    try:
        _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_CACHE_DB), timeout=5)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=OFF")      # a cache; losing it costs a rebuild
        c.execute("PRAGMA busy_timeout=4000")
        # The log had grown to 384 MB beside a 37 MB cache: pages are rewritten
        # whole, readers are always present, and nothing capped it.
        c.execute("PRAGMA journal_size_limit=67108864")
        c.execute("""CREATE TABLE IF NOT EXISTS pages(
                       key TEXT PRIMARY KEY, built REAL NOT NULL, html TEXT NOT NULL
                     ) WITHOUT ROWID""")
        c.commit()
        _local.conn = c
        return c
    except Exception:
        return None


def _k(key: str) -> str:
    """The stored key, tied to the rendering code that produced it."""
    return f"{stamp()}:{key}"


def _read(key: str):
    """This build's copy, or the previous build's if there is not one yet.

    A stamp change used to make every page cold at once, so the first visitor
    after a deploy paid a full rebuild: measured at fifteen seconds on a
    capability page, and the CDN then cached that slow response for everyone.
    A new build should make pages *stale*, not absent. The old copy is returned
    with a timestamp of zero, which reads as expired everywhere, so the caller
    serves it immediately and rebuilds behind the request exactly as it does for
    any other stale entry. Content from one deploy ago for a few seconds is a
    far better answer than a fifteen second wait.
    """
    stamped = _k(key)
    c = _cdb()
    if c is None:
        return _cache.get(stamped)
    try:
        r = c.execute("SELECT built, html FROM pages WHERE key=?", (stamped,)).fetchone()
        if r:
            return (r[0], r[1])
        r = c.execute("SELECT html FROM pages WHERE key LIKE ? AND key != '__warmlock__' "
                      "ORDER BY built DESC LIMIT 1", ("%:" + key,)).fetchone()
        return (0.0, r[0]) if r else None
    except Exception:
        return _cache.get(stamped)


def _write(key: str, html_: str) -> None:
    key = _k(key)
    _cache[key] = (time.time(), html_)          # in-process copy, avoids a read per hit
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("INSERT INTO pages(key,built,html) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET built=excluded.built, html=excluded.html",
                  (key, time.time(), html_))
        # Drop only the older copies of the page just written: the rest are still
        # serving as the fallback for pages this build has not rebuilt yet.
        bare = key.split(":", 1)[1] if ":" in key else key
        c.execute("DELETE FROM pages WHERE key LIKE ? AND key != ? AND key != '__warmlock__'",
                  ("%:" + bare, key))
        c.commit()
    except Exception:
        pass


def _rebuild(key: str, build, claim: bool = False) -> None:
    claimed = False
    try:
        if claim:
            claimed = _claim_build(key)
            if not claimed:
                return
        _write(key, build())
    except Exception:
        pass
    finally:
        with _build_lock:
            _building.discard(key)
        if claimed:
            _release_build(key)


# ── staleness shared by every worker ────────────────────────────────────────
# `invalidate()` used to DELETE every page, and every successful submission
# called it. The next visitor to /tools or any capability page then found
# nothing at all and waited for a synchronous rebuild: fifteen seconds for
# /tools, measured 2026-09-10, and the CDN kept that slow miss for everyone.
# Now it records the moment it happened, in the cache itself so every worker
# sees it, and anything built before that moment is stale: served at once and
# rebuilt behind the request, exactly like a page past its TTL. Bookkeeping
# rows carry no colon, and every page is stored as `STAMP:key`, so the two can
# never be confused, including by the previous-build fallback in `_read`.
_GEN_KEY = "__gen__"
_gen = {"t": 0.0, "read": 0.0}
_claim_miss: dict[str, float] = {}


def _generation() -> float:
    """When any worker last invalidated the cache. Read at most once a second."""
    now = time.time()
    if now - _gen["read"] < 1.0:
        return _gen["t"]
    c = _cdb()
    if c is not None:
        try:
            r = c.execute("SELECT built FROM pages WHERE key=?", (_GEN_KEY,)).fetchone()
            if r:
                _gen["t"] = max(_gen["t"], float(r[0]))
        except Exception:
            pass
    _gen["read"] = now
    return _gen["t"]


def _fresh(got, ttl: int) -> bool:
    return got[0] > _generation() and time.time() - got[0] < ttl


def _claim_build(key: str, ttl: int = 180) -> bool:
    """One rebuild of a page at a time across every worker.

    Four workers each finding /tools stale each started the same fifteen second
    build. The claim is a row that expires on its own, so a worker that dies
    mid-build holds nobody up for longer than `ttl`.
    """
    now = time.time()
    c = _cdb()
    if c is None:
        return True
    try:
        cur = c.execute(
            "INSERT INTO pages(key,built,html) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET built=excluded.built, html=excluded.html "
            "WHERE pages.built < ?",
            ("__build__|" + key, now, f"pid{os.getpid()}", now - ttl))
        c.commit()
        if cur.rowcount > 0:
            return True
    except Exception:
        return True
    _claim_miss[key] = now
    return False


def _release_build(key: str) -> None:
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("DELETE FROM pages WHERE key=?", ("__build__|" + key,))
        c.commit()
    except Exception:
        pass


def _stale_rebuild(key: str, build) -> None:
    """Refresh a stale page off the request path, at most once at a time.

    The claim is taken inside the thread, never on the request, because it is a
    write and a write can wait on the lock.
    """
    if time.time() - _claim_miss.get(key, 0.0) < 10:
        return
    with _build_lock:
        if key in _building:
            return
        _building.add(key)
    threading.Thread(target=_rebuild, args=(key, build, True),
                     name=f"rebuild:{key}", daemon=True).start()


def _lookup(key: str, ttl: int):
    """This worker's copy while it is fresh, otherwise whatever is newest."""
    stamped = _k(key)
    got = _cache.get(stamped)
    if got and _fresh(got, ttl):
        return got
    shared = _read(key)
    if shared is None and got is not None and got[0] <= _generation():
        # Dropped by an invalidation, possibly in another worker: absent, so the
        # caller builds it now rather than serving what was deliberately removed.
        _cache.pop(stamped, None)
        return None
    if shared and (got is None or shared[0] > got[0]):
        if shared[0] > 0:
            _cache[stamped] = shared
        return shared
    return got or shared


def cached(key: str, ttl: int, build) -> str:
    """Serve a generated page, rebuilding behind the request when it is stale.

    These pages aggregate tens of thousands of rows. Rebuilding one per request
    would spend the whole latency budget on work whose inputs change a few times
    a day, and rebuilding one *during* a request is how a fast page becomes a
    gateway timeout. Stale means past its TTL or built before the last
    invalidation by any worker; either way the old copy is served immediately.
    """
    got = _lookup(key, ttl)
    if got:
        if not _fresh(got, ttl):
            _stale_rebuild(key, build)
        return got[1]
    # Nothing at all: this is the only path that waits, and warm_pages() exists
    # so that a visitor is not the one who pays for it.
    html_ = build()
    _write(key, html_)
    return html_


def claim_warm(holder: str, ttl: int = 300) -> bool:
    """Claim the right to warm the cache, across processes.

    Every worker starts at the same moment and each was warming the same 40-odd
    expensive pages independently. On a two core box that saturated both for a
    minute after every deploy, which is exactly when traffic arrives, and it was
    entirely duplicated work: the cache is shared, so one worker building it
    serves all of them. The claim lives in the cache itself, so it needs no
    extra coordination and expires on its own if a worker dies mid-warm.
    """
    c = _cdb()
    if c is None:
        return True                              # no shared store, warm locally
    now = time.time()
    try:
        r = c.execute("SELECT built, html FROM pages WHERE key='__warmlock__'").fetchone()
        if r and now - r[0] < ttl:
            return False                         # somebody else is warming
        c.execute("INSERT INTO pages(key,built,html) VALUES('__warmlock__',?,?) "
                  "ON CONFLICT(key) DO UPDATE SET built=excluded.built, html=excluded.html",
                  (now, holder))
        c.commit()
        return True
    except Exception:
        return True


def release_warm() -> None:
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("DELETE FROM pages WHERE key='__warmlock__'")
        c.commit()
    except Exception:
        pass


def cached_value(key: str, ttl: int, build):
    """The same cache, for a small JSON value instead of a page.

    Exists because `catalog.published()` was a per-process dict computed on the
    first request that needed it: twenty-five aggregate queries, ten seconds on
    an idle box, more under load, once per worker, in front of a visitor. The
    page cache made every page instant and this map, checked before the page
    cache was even consulted, put the whole cost back. Anything derived from
    the index that a request path needs belongs here, warmed like the pages,
    served stale like the pages, and never computed in front of anyone after
    the first build.
    """
    import json as _json
    got = _lookup(key, ttl)
    if got:
        if not _fresh(got, ttl):
            _stale_rebuild(key, lambda: _json.dumps(build()))
        try:
            return _json.loads(got[1])
        except Exception:
            pass
    val = build()
    _write(key, _json.dumps(val))
    return val


def warm_value(key: str, ttl: int, build) -> bool:
    import json as _json
    got = _read(key)
    if got and _fresh(got, ttl):
        return False
    if not _claim_build(key):
        return False
    try:
        _write(key, _json.dumps(build()))
    finally:
        _release_build(key)
    return True


def warm(key: str, ttl: int, build) -> bool:
    """Build a page if it is missing or stale. Returns True if it built."""
    got = _read(key)
    if got and _fresh(got, ttl):
        return False
    if not _claim_build(key):
        return False
    try:
        _write(key, build())
    finally:
        _release_build(key)
    return True


def invalidate(keys=()) -> None:
    """Make every page stale, and remove only the pages named in `keys`.

    Stale is enough for aggregate pages: the next request serves the old copy
    and rebuilds it. A page that must show the change on its very next view,
    such as the submitting publisher's own page, is named and removed.
    """
    now = time.time()
    _cache.clear()
    _gen["t"], _gen["read"] = now, now
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("INSERT INTO pages(key,built,html) VALUES(?,?,'') "
                  "ON CONFLICT(key) DO UPDATE SET built=excluded.built", (_GEN_KEY, now))
        for k in keys:
            c.execute("DELETE FROM pages WHERE key=?", (_k(k),))
        c.commit()
    except Exception:
        pass


def checkpoint() -> None:
    """Fold the log back into the cache file without waiting on anyone.

    Passive, so it never blocks a writer; `journal_size_limit` then trims the
    file once the log resets.
    """
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
    except Exception:
        pass


def cache_stats() -> dict:
    c = _cdb()
    out = {"backend": "sqlite" if c else "memory", "in_process": len(_cache)}
    if c:
        try:
            out["stored"] = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            out["bytes"] = c.execute("SELECT COALESCE(SUM(LENGTH(html)),0) FROM pages").fetchone()[0]
            out["oldest_s"] = int(time.time() - (c.execute(
                "SELECT MIN(built) FROM pages").fetchone()[0] or time.time()))
        except Exception:
            pass
    return out
