"""The dense matrix is one shared file, and it is never built on the event loop.

Two defects are pinned here, both of which took `federation=auto` down in
production on 2026-09-21.

Building the matrix is a multi-second read of every vector in the database. It
used to run inline inside `dense_ranking`, wrapped in an `asyncio.wait_for`
that could not interrupt it, because a coroutine that never awaits cannot be
cancelled: one search froze its whole worker for as long as the read took.

And the matrix was held per worker. Several workers times half a gigabyte each
is more memory than a search service should spend on one index, so workers thrashed, and a leg that looks
enabled silently contributed nothing on most requests. It is a mapped file now,
so the workers share one copy.
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DIR = tempfile.mkdtemp(prefix="dense-test-")
os.environ["NEURONTO_DENSE_DIR"] = _DIR

import numpy as np
from app import embed

_p = _f = 0


def check(name, ok):
    global _p, _f
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if ok: _p += 1
    else:  _f += 1


VEC, KEYS, LOCK = embed._dense_paths()


def write_build(n=4, dim=8, tag=1.0, age_s=0.0):
    """Stand in for a real build: n rows of dim floats, plus their keys."""
    m = (np.arange(n * dim, dtype="<f4").reshape(n, dim) + tag)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    m.tofile(VEC)
    open(KEYS, "w").write("\n".join(f"k{i}" for i in range(n)))
    if age_s:
        t = time.time() - age_s
        os.utime(VEC, (t, t))


def clear():
    for p in (VEC, KEYS, LOCK):
        try: os.unlink(p)
        except OSError: pass
    embed._snap = (None, [], 0.0)
    embed._refreshing = False
    embed._refresh_task = None


async def run():
    builds = {"n": 0}
    slow_s = 1.5

    def slow_rebuild():
        time.sleep(slow_s)          # stands in for the real multi-second read
        builds["n"] += 1
        write_build(tag=99.0)

    real_rebuild = embed._rebuild

    # ── 1. no build yet: answer immediately, without the dense leg ──────────
    clear()
    embed._rebuild = slow_rebuild
    t0 = time.perf_counter()
    m, keys = await embed.ensure_matrix()
    check("with no build the caller is not made to wait", time.perf_counter() - t0 < 0.25)
    check("and is told there is none, so the leg is skipped", m is None and keys == [])

    ticks = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < slow_s:
        await asyncio.sleep(0.01)
        ticks += 1
    check("the event loop keeps running while the build happens", ticks > 50)
    await asyncio.sleep(0.4)
    check("the build ran behind the request", builds["n"] == 1)

    # ── 2. a fresh build is mapped and served ──────────────────────────────
    clear()
    write_build(n=4, dim=8, tag=1.0)
    t0 = time.perf_counter()
    m, keys = await embed.ensure_matrix()
    check("a fresh build is served", m is not None and list(keys) == ["k0","k1","k2","k3"])
    check("mapping it is fast", time.perf_counter() - t0 < 0.25)
    check("its shape comes from the file, not a guess", m.shape == (4, 8))
    check("it is mapped, not copied into the process", isinstance(m, np.memmap))

    # ── 3. a stale build is still served, and refreshed behind the request ──
    builds["n"] = 0
    clear()
    write_build(n=4, dim=8, tag=1.0, age_s=embed.config.DENSE_REBUILD_S + 60)
    t0 = time.perf_counter()
    m, keys = await embed.ensure_matrix()
    check("a stale build is served rather than withheld", m is not None)
    check("and serving it does not wait for the rebuild", time.perf_counter() - t0 < 0.25)
    await asyncio.sleep(slow_s + 0.5)
    check("a rebuild ran behind it", builds["n"] == 1)

    # ── 4. a new build is picked up ────────────────────────────────────────
    m, keys = await embed.ensure_matrix()
    check("the worker re-maps once the file changes", m is not None and float(m[0][0]) != 0.0)

    # ── 5. one build between many workers ──────────────────────────────────
    builds["n"] = 0
    clear()
    write_build(age_s=embed.config.DENSE_REBUILD_S + 60)
    await asyncio.gather(*[embed.ensure_matrix() for _ in range(8)])
    await asyncio.sleep(slow_s + 0.6)
    check("eight concurrent searches produce one build, not eight", builds["n"] == 1)

    # ── 6. the lock is reclaimed, never waited on forever ──────────────────
    embed._rebuild = real_rebuild
    clear()
    open(LOCK, "w").write("99999")
    old = time.time() - embed._LOCK_STALE_S - 10
    os.utime(LOCK, (old, old))
    embed._rebuild()
    check("a stale lock from a killed build is reclaimed", not os.path.exists(LOCK))

    # ── 7. invalidate marks stale, it does not delete ──────────────────────
    clear()
    write_build()
    embed.invalidate()
    check("invalidate keeps the build", os.path.exists(VEC))
    check("invalidate marks it stale", time.time() - os.path.getmtime(VEC) > embed.config.DENSE_REBUILD_S)
    clear()

    # ── 8. the streamed build is the same matrix the old one was ──────────
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE vectors(key TEXT, model TEXT, dim INT, vec BLOB, ts INT)")
    db.execute("CREATE TABLE tool_vectors(key TEXT, model TEXT, dim INT, vec BLOB, n INT, ts INT)")
    rng = np.random.default_rng(3)
    prose = rng.normal(size=(5000, 8)).astype("<f4")
    tools = rng.normal(size=(7, 8)).astype("<f4")
    db.executemany("INSERT INTO vectors VALUES(?,?,8,?,1)",
                   [(f"e{i}", "m", v.tobytes()) for i, v in enumerate(prose)])
    db.executemany("INSERT INTO tool_vectors VALUES(?,?,8,?,1,1)",
                   [(f"e{i}", "m", v.tobytes()) for i, v in enumerate(tools)])
    db.execute("INSERT INTO vectors VALUES('bad','m',4,?,1)", (b"x" * 16,))
    db.commit()
    clear()
    check("a streamed build succeeds", embed.load(db) is True)
    want = np.vstack([prose, tools])
    want = want / np.linalg.norm(want, axis=1, keepdims=True)
    got = np.fromfile(VEC, dtype="<f4").reshape(-1, 8)
    keys = open(KEYS).read().split("\n")
    check("every row of both tables, in order, normalised",
          got.shape == want.shape and np.allclose(got, want, atol=1e-6))
    check("a vector of the wrong size is skipped", "bad" not in keys and len(keys) == 5007)
    check("no temporary file is left behind", not os.path.exists(VEC + ".tmp"))

    # ── 9. the external builder: skips when nothing changed, workers only map ──
    clear()
    r1 = embed.build_if_changed(db)
    r2 = embed.build_if_changed(db)
    check("the external builder builds once", r1["built"] and not r2["built"])
    db.execute("INSERT INTO vectors VALUES('new','m',8,?,2)", (prose[0].tobytes(),))
    db.commit()
    check("and again once a vector is written", embed.build_if_changed(db)["built"])
    builds["n"] = 0
    embed._rebuild = slow_rebuild
    embed.config.DENSE_BUILDER = "external"
    t = time.time() - embed.config.DENSE_REBUILD_S - 60
    os.utime(VEC, (t, t))
    m, keys = await embed.ensure_matrix()
    embed.invalidate()
    await asyncio.sleep(slow_s + 0.3)
    check("with an external builder a stale file is mapped, never rebuilt by a worker",
          m is not None and builds["n"] == 0)
    check("and invalidate leaves the file alone", abs(os.path.getmtime(VEC) - t) < 1)
    embed.config.DENSE_BUILDER = "inprocess"
    embed._rebuild = real_rebuild
    for p in (VEC + ".stamp",):
        try: os.unlink(p)
        except OSError: pass
    clear()


asyncio.run(run())
print(f"  {_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
