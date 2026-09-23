"""The dense retrieval leg.

BM25 finds documents that share words with the query. It cannot find the PDF
tool when the user asked about "reading a paper". SCOUT, running in production
at PayPal over 2,000+ tools, reports the working configuration as sparse BM25
fused with dense vectors via reciprocal rank fusion, and ToolRet (ACL 2025)
shows why it matters: retrieval models do measurably badly at tool retrieval,
and that failure propagates straight into the agent's task pass rate.

We already had the sparse half and the RRF. This is the missing half.

Two constraints shape the design:

  * The latency claim is the product. Embedding a query is a network call, so
    the dense leg never runs on the fast path. `federation: none` stays pure
    BM25 and stays in the tens of milliseconds. `auto` is already waiting on
    upstreams behind a budget, so the embedding call runs concurrently inside
    that budget and costs no extra wall clock.

  * It must degrade to nothing. If the embedding service is slow, broken or
    unconfigured, search returns exactly what it returned before, and says so
    in the diagnostics rather than failing.

Vectors are float32 blobs in SQLite and load as one matrix. At ~10k entries
that is 40 MB resident and a single matmul per query.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import threading
import time

import httpx

from . import config

# One snapshot, swapped in a single assignment. A reader takes the matrix and
# its keys in one read, so a refresh landing mid-search can never pair one
# build's row indices with another build's keys.
_snap: tuple = (None, [], 0.0)   # (mapped matrix, keys, file mtime)
_LOCK_STALE_S = 900

# A refresh runs in a worker thread, never on the event loop: building the
# matrix is a multi-second synchronous read of every vector in the database,
# and `asyncio.wait_for` cannot interrupt a coroutine that never awaits, so
# doing it inline froze the whole worker for as long as the read took.
_refresh_lock = threading.Lock()
_refreshing = False
_refresh_task = None             # held: asyncio keeps only a weak reference


def _np():
    import numpy  # imported lazily so the service starts without it
    return numpy


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob)//4}f", blob))


def text_for(row) -> str:
    """The document we embed for an entry.

    Representative queries first: the publisher stating, in the user's own
    words, what they want to be found for is the single best sentence we have.
    Verified tool names follow, because they are evidence rather than prose.
    """
    def flat(js):
        try: return " ".join(json.loads(js or "[]"))
        except Exception: return ""
    parts = [
        flat(row["rep_queries"]),
        row["display_name"] or "",
        row["description"] or "",
        flat(row["tags"]),
        flat(row["capabilities"]),
    ]
    if "tool_text" in row.keys():
        parts.append(row["tool_text"] or "")
    return " \n".join(p for p in parts if p)[:4000]


# Query vectors, cached briefly. The same phrasings recur constantly, from
# our own battery, from crawlers and from real callers, and each miss is a
# ~0.6 s call to an external API inside a budget of about one second. Keyed on
# the exact text; bounded so it cannot grow without limit.
_qcache: dict[str, tuple[float, list[float]]] = {}
_QCACHE_MAX = 512


async def embed_query(text: str) -> list[float] | None:
    """One query's vector, from cache when it is warm."""
    now = time.time()
    hit = _qcache.get(text)
    if hit and now - hit[0] < config.EMBED_CACHE_S:
        return hit[1]
    vecs = await embed_texts([text])
    if not vecs:
        return None
    if len(_qcache) >= _QCACHE_MAX:
        for k in sorted(_qcache, key=lambda k: _qcache[k][0])[:_QCACHE_MAX // 4]:
            _qcache.pop(k, None)
    _qcache[text] = (now, vecs[0])
    return vecs[0]


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch. Returns None if embedding is unavailable, never raises."""
    if not config.EMBED_API_KEY or not texts:
        return None
    payload = {"model": config.EMBED_MODEL, "input": texts}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(config.EMBED_URL, json=payload,
                             headers={"Authorization": f"Bearer {config.EMBED_API_KEY}",
                                      "Content-Type": "application/json"},
                             timeout=config.EMBED_TIMEOUT_S)
        if r.status_code != 200:
            return None
        data = r.json().get("data") or []
        out = [d.get("embedding") for d in data]
        return out if all(isinstance(v, list) and v for v in out) else None
    except Exception:
        return None


async def build(conn, limit: int = 2000, only_missing: bool = True) -> dict:
    """Embed entries that have no current vector."""
    if not config.EMBED_API_KEY:
        return {"embedded": 0, "skipped": "no embedding key configured"}
    where = ("LEFT JOIN vectors v ON v.key = e.key WHERE v.key IS NULL"
             if only_missing else "LEFT JOIN vectors v ON v.key = e.key WHERE 1=1")
    rows = conn.execute(
        f"""SELECT e.key, e.display_name, e.description, e.rep_queries, e.tags,
                   e.capabilities,
                   (SELECT GROUP_CONCAT(name || ' ' || COALESCE(description,''), ' ')
                      FROM tools WHERE entry_key = e.key) AS tool_text
            FROM entries e {where} LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return {"embedded": 0}

    # Release the read snapshot before writing. A connection that SELECTs, then
    # goes to the network, then INSERTs is asking SQLite to upgrade a snapshot
    # that is now stale, and WAL answers SQLITE_BUSY *immediately* for that:
    # the busy handler is skipped entirely, because waiting cannot make a stale
    # snapshot fresh. The 45-second timeout on this connection never applies and
    # the batch dies instantly the moment any other writer has committed.
    conn.commit()

    done = 0
    B = config.EMBED_BATCH
    for i in range(0, len(rows), B):
        chunk = rows[i:i + B]
        vecs = await embed_texts([text_for(r) for r in chunk])
        if vecs is None:
            break
        now = int(time.time())
        for r, v in zip(chunk, vecs):
            conn.execute("""INSERT INTO vectors(key,model,dim,vec,ts)
                            VALUES(?,?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET
                              model=excluded.model, dim=excluded.dim,
                              vec=excluded.vec, ts=excluded.ts""",
                         (r["key"], config.EMBED_MODEL, len(v), pack(v), now))
            done += 1
        conn.commit()
    invalidate()
    return {"embedded": done, "model": config.EMBED_MODEL}


# How much tool text one entry contributes. The cap exists because a handful of
# giant catalogues would otherwise dominate the embedding budget, and because
# similarity over a 4,000-character bag of 790 operations is mush: past a point,
# adding operations makes the vector LESS discriminating, not more.
TOOL_TEXT_CAP = 4000


def tool_text_for(rows) -> str:
    """One capability document for an entry, distilled from its tools.

    Deduplicated on purpose. An OpenAPI document with 790 operations repeats
    "Retrieve a Message resource" style phrasing dozens of times, and the
    repetition buys nothing while eating the whole budget. Distinct lines, in
    the order the server listed them, is a better description of what the thing
    can do than a verbatim concatenation.
    """
    seen: set[str] = set()
    out: list[str] = []
    size = 0
    for name, desc in rows:
        line = " ".join(x for x in ((name or "").strip(),
                                    (desc or "").strip()) if x)
        if not line:
            continue
        norm = " ".join(line.lower().split())[:160]
        if norm in seen:
            continue
        seen.add(norm)
        out.append(line[:300])
        size += len(out[-1]) + 1
        if size >= TOOL_TEXT_CAP:
            break
    return "\n".join(out)[:TOOL_TEXT_CAP]


async def build_tools(conn, limit: int = 2000, only_missing: bool = True) -> dict:
    """Embed the tool surface of entries that have one.

    Additive: it creates a second ranking signal and changes no existing vector,
    so if it turns out to help nothing, deleting the table restores exactly the
    previous behaviour.
    """
    if not config.EMBED_API_KEY:
        return {"embedded": 0, "skipped": "no embedding key configured"}
    join = ("LEFT JOIN tool_vectors tv ON tv.key = e.key WHERE tv.key IS NULL"
            if only_missing else "LEFT JOIN tool_vectors tv ON tv.key = e.key WHERE 1=1")
    keys = [r["key"] for r in conn.execute(
        f"""SELECT e.key FROM entries e {join}
              AND EXISTS (SELECT 1 FROM tools t WHERE t.entry_key = e.key)
            LIMIT ?""", (limit,)).fetchall()]
    if not keys:
        return {"embedded": 0}

    # Release the read snapshot before writing. A connection that SELECTs, then
    # goes to the network, then INSERTs is asking SQLite to upgrade a snapshot
    # that is now stale, and WAL answers SQLITE_BUSY *immediately* for that:
    # the busy handler is skipped entirely, because waiting cannot make a stale
    # snapshot fresh. The 45-second timeout on this connection never applies and
    # the batch dies instantly the moment any other writer has committed.
    conn.commit()

    done = 0
    B = config.EMBED_BATCH
    for i in range(0, len(keys), B):
        chunk = keys[i:i + B]
        docs, counts = [], []
        for k in chunk:
            rows = conn.execute(
                "SELECT name, description FROM tools WHERE entry_key=? LIMIT 2000",
                (k,)).fetchall()
            docs.append(tool_text_for([(r["name"], r["description"]) for r in rows]))
            counts.append(len(rows))
        conn.commit()      # same reason: these reads must not be held over the network
        vecs = await embed_texts(docs)
        if vecs is None:
            break
        now = int(time.time())
        for k, v, n in zip(chunk, vecs, counts):
            conn.execute("""INSERT INTO tool_vectors(key,model,dim,vec,n,ts)
                            VALUES(?,?,?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET
                              model=excluded.model, dim=excluded.dim,
                              vec=excluded.vec, n=excluded.n, ts=excluded.ts""",
                         (k, config.EMBED_MODEL, len(v), pack(v), n, now))
            done += 1
        conn.commit()
    invalidate()
    return {"embedded": done, "model": config.EMBED_MODEL}


def invalidate() -> None:
    """Mark the shared build stale so the next search schedules a rebuild.

    It does not delete anything. Dropping the matrix made the next search
    rebuild from scratch on the request path; marking it stale keeps serving
    the previous build, minutes out of date at worst, while the rebuild runs
    behind the request.
    """
    vec_p, _, _ = _dense_paths()
    try:
        old = time.time() - config.DENSE_REBUILD_S - 1
        os.utime(vec_p, (old, old))
    except OSError:
        pass


def _dense_paths() -> tuple[str, str, str]:
    """Where the shared matrix lives: next to the database it is built from."""
    base = os.environ.get("NEURONTO_DENSE_DIR") or os.path.dirname(
        os.path.abspath(config.DB_PATH))
    return (os.path.join(base, "dense.f32"),
            os.path.join(base, "dense.keys"),
            os.path.join(base, "dense.lock"))


def load(conn, force: bool = False):
    """Build the shared matrix file from the database.

    Synchronous and slow, several seconds of reading every vector there is.
    Never call it from the event loop; `ensure_matrix` is the query path's
    entry point. Writes are atomic, so a worker mapping the file never sees a
    half-written build.
    """
    vec_p, key_p, _ = _dense_paths()
    try:
        np = _np()
    except Exception:
        return None
    rows = conn.execute(
        "SELECT key, vec, dim FROM vectors "
        "UNION ALL SELECT key, vec, dim FROM tool_vectors").fetchall()
    if not rows:
        return None
    dim = rows[0]["dim"]
    # Both vectors of every entry, in one matrix. The key list therefore holds an
    # entry key twice, once for its prose and once for its tools, and
    # `dense_ranking` collapses them keeping the better rank. Fusing here rather
    # than running two matmuls keeps the query path a single matrix multiply.
    keep = [r for r in rows if r["dim"] == dim and len(r["vec"]) == dim * 4]
    if not keep:
        return None
    m = np.frombuffer(b"".join(r["vec"] for r in keep),
                      dtype="<f4").reshape(len(keep), dim).astype("float32")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m /= norms
    tmp_v, tmp_k = vec_p + ".tmp", key_p + ".tmp"
    m.tofile(tmp_v)
    with io.open(tmp_k, "w", encoding="utf-8") as fh:
        fh.write("\n".join(r["key"] for r in keep))
    os.replace(tmp_v, vec_p)
    os.replace(tmp_k, key_p)
    del m
    return True


def _rebuild() -> None:
    """Runs on a worker thread, on that thread's own connection.

    Guarded by a lock file so that several workers noticing staleness in the same
    second produce one build between them rather than four.
    """
    _, _, lock_p = _dense_paths()
    try:
        fd = os.open(lock_p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # Stale lock from a build that was killed: reclaim it, never wait forever.
        try:
            if time.time() - os.path.getmtime(lock_p) > _LOCK_STALE_S:
                os.unlink(lock_p)
        except OSError:
            pass
        return
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        from . import store
        load(store.tls_conn(), force=True)
    finally:
        try:
            os.unlink(lock_p)
        except OSError:
            pass


async def _refresh() -> None:
    global _refreshing, _refresh_task
    try:
        await asyncio.to_thread(_rebuild)
    except Exception:
        pass
    finally:
        with _refresh_lock:
            _refreshing = False
        _refresh_task = None


def _schedule_refresh() -> None:
    """At most one rebuild in flight per worker; the lock file handles the
    other workers."""
    global _refreshing, _refresh_task
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        _refresh_task = asyncio.ensure_future(_refresh())
    except RuntimeError:          # no running loop, e.g. a synchronous caller
        with _refresh_lock:
            _refreshing = False


def _map_file():
    """Map the shared matrix. Cheap: the pages are the OS page cache, so every
    worker reads one copy rather than each holding its own half a gigabyte."""
    global _snap
    vec_p, key_p, _ = _dense_paths()
    try:
        np = _np()
        mtime = os.path.getmtime(vec_p)
        if _snap[0] is not None and _snap[2] == mtime:
            return _snap[0], _snap[1]
        keys = io.open(key_p, encoding="utf-8").read().split("\n")
        size = os.path.getsize(vec_p)
        if not keys or size % (len(keys) * 4):
            return None, []
        dim = size // 4 // len(keys)
        m = np.memmap(vec_p, dtype="<f4", mode="r").reshape(len(keys), dim)
        _snap = (m, keys, mtime)
        return m, keys
    except (OSError, ValueError):
        return None, []


async def ensure_matrix():
    """The matrix and its keys, without ever blocking the event loop.

    The matrix is a file that every worker maps, so this is a stat and, when
    the build has changed, a mmap: microseconds either way. A build older than
    EMBED_CACHE_S is still served while a rebuild runs on a thread behind the
    request, because minutes of drift in one ranking leg cost nothing and a
    rebuild on the request path cost a minute of wall clock. No file at all,
    which happens once on a new deployment, means this search runs without the
    dense leg, which contributes nothing to RRF, and a later one has it.
    """
    vec_p, _, _ = _dense_paths()
    try:
        age = time.time() - os.path.getmtime(vec_p)
    except OSError:
        _schedule_refresh()
        return None, []
    if age > config.DENSE_REBUILD_S:
        _schedule_refresh()
    return _map_file()


async def dense_ranking(conn, text: str, limit: int = 50) -> list[str]:
    """Keys for the query, most semantically similar first.

    Returns an empty list whenever the dense leg is unavailable. An empty
    ranking contributes nothing to RRF, so search behaves exactly as it did
    before this module existed.
    """
    m, keys = await ensure_matrix()
    if m is None or not keys:
        return []
    vec = await embed_query(text)
    if not vec:
        return []
    try:
        np = _np()
        q = np.asarray(vec, dtype="float32")
        if q.shape[0] != m.shape[1]:
            return []          # model changed under us; ignore until rebuilt
        n = float(np.linalg.norm(q)) or 1.0
        sims = m @ (q / n)
        # Over-fetch, because an entry now occupies up to two rows and the two
        # may both land in the top k, which would otherwise halve the leg.
        k = min(limit * 2, sims.shape[0])
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        out, seen = [], set()
        for i in idx:
            key = keys[i]
            if key in seen:
                continue      # already ranked, by whichever vector matched better
            seen.add(key)
            out.append(key)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def status(conn) -> dict:
    row = conn.execute("SELECT COUNT(*) n, MAX(ts) t FROM vectors").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    tv = conn.execute("SELECT COUNT(*) n, MAX(ts) t FROM tool_vectors").fetchone()
    with_tools = conn.execute(
        "SELECT COUNT(DISTINCT entry_key) FROM tools").fetchone()[0]
    return {
        "configured": bool(config.EMBED_API_KEY),
        "model": config.EMBED_MODEL if config.EMBED_API_KEY else None,
        "vectors": row["n"] or 0,
        "entries": total,
        "coverage": round((row["n"] or 0) / total, 4) if total else 0.0,
        "last_built": row["t"],
        "tool_vectors": {
            "vectors": tv["n"] or 0,
            "entries_with_tools": with_tools,
            "coverage": (round((tv["n"] or 0) / with_tools, 4) if with_tools else 0.0),
            "last_built": tv["t"],
            "what_it_is": ("a second vector per entry, embedded from its tools alone, "
                           "so a server's verbs are not truncated out of its prose"),
        },
    }
