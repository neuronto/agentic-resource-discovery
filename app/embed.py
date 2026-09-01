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
import json
import struct
import time

import httpx

from . import config

_matrix = None          # numpy array (n, dim), L2-normalised
_keys: list[str] = []
_loaded_at = 0.0
_lock = asyncio.Lock()


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


def invalidate() -> None:
    global _matrix, _loaded_at
    _matrix = None
    _loaded_at = 0.0


def load(conn, force: bool = False):
    """Load all vectors into one normalised matrix, cached in process."""
    global _matrix, _keys, _loaded_at
    if _matrix is not None and not force and (time.time() - _loaded_at) < config.EMBED_CACHE_S:
        return _matrix
    try:
        np = _np()
    except Exception:
        return None
    rows = conn.execute("SELECT key, vec, dim FROM vectors").fetchall()
    if not rows:
        _matrix, _keys, _loaded_at = None, [], time.time()
        return None
    dim = rows[0]["dim"]
    keep = [r for r in rows if r["dim"] == dim and len(r["vec"]) == dim * 4]
    if not keep:
        _matrix, _keys, _loaded_at = None, [], time.time()
        return None
    m = np.frombuffer(b"".join(r["vec"] for r in keep),
                      dtype="<f4").reshape(len(keep), dim).astype("float32")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    _matrix = m / norms
    _keys = [r["key"] for r in keep]
    _loaded_at = time.time()
    return _matrix


async def dense_ranking(conn, text: str, limit: int = 50) -> list[str]:
    """Keys for the query, most semantically similar first.

    Returns an empty list whenever the dense leg is unavailable. An empty
    ranking contributes nothing to RRF, so search behaves exactly as it did
    before this module existed.
    """
    m = load(conn)
    if m is None or not _keys:
        return []
    vecs = await embed_texts([text])
    if not vecs:
        return []
    try:
        np = _np()
        q = np.asarray(vecs[0], dtype="float32")
        if q.shape[0] != m.shape[1]:
            return []          # model changed under us; ignore until rebuilt
        n = float(np.linalg.norm(q)) or 1.0
        sims = m @ (q / n)
        k = min(limit, sims.shape[0])
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [_keys[i] for i in idx]
    except Exception:
        return []


def status(conn) -> dict:
    row = conn.execute("SELECT COUNT(*) n, MAX(ts) t FROM vectors").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return {
        "configured": bool(config.EMBED_API_KEY),
        "model": config.EMBED_MODEL if config.EMBED_API_KEY else None,
        "vectors": row["n"] or 0,
        "entries": total,
        "coverage": round((row["n"] or 0) / total, 4) if total else 0.0,
        "last_built": row["t"],
    }
