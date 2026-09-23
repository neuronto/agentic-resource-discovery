"""ARD-Bench: head-to-head retrieval measurement across ARD registries.

Nobody has ever published whether any of these registries returns the right
resource. ToolRet (ACL 2025) established that tool retrieval is measurably
hard and that poor retrieval degrades an agent's task pass rate, but it was run
against corpora, not against the live services agents actually query.

**The ground truth.** For an entry E, the publisher of E wrote
`representativeQueries`, read verbatim from the manifest on the publisher's own
domain and frozen, with that document's sha256, in `bench/declared-v1.json`. The spec describes that field as the text a registry
builds its semantic index from: it is the publisher stating, in a user's words,
what they want to be found for. So the task "given query Q from E, does the
registry return E?" is answerable with no hand-labelling and no judgement call
by us, and it is answerable *identically for every registry*, because every
registry indexes the same public manifests.

**Why this is not rigged.** Four deliberate constraints:

  * The task set is frozen and published, so it cannot be redrawn to suit a
    result. Until 2026-09-23 tasks were sampled live from our own index, whose
    query column mixed publisher text with text our ingest synthesised; 7,530 of
    7,898 queries on some entries were ours, so the benchmark partly graded our
    ranking against our own writing.
  * The query text is the publisher's, never ours, and never rewritten.
  * Only the `holdout` half is reported. Our ranking weights are chosen on the
    `tune` half, and grading ourselves on the half we tuned on would flatter us.
  * Our own entries are excluded as targets. We are a registry indexing other
    people's resources; scoring ourselves on our own manifest would be exactly
    the thing that makes a benchmark worthless.

We publish the harness and the losses. A benchmark the author can rig is a
benchmark nobody cites, and being uncitable would defeat the entire purpose.

Reported per target: recall@k, MRR, nDCG@k, median latency, and how many tasks
the target actually answered.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
import time

import httpx

from . import config, federation, search, store


def build_tasks(conn, n: int | None = None, seed: int = 7) -> list[dict]:
    """The frozen declared set's holdout half, with each target's sources.

    `sources` is read from our index at run time, because whether another
    registry carries a target is a fact about today, while the task is not.
    `n` and `seed` are kept for callers; the set itself is fixed.
    """
    from .evalset import load
    ev = load(str(config.BENCH_SET))
    out = []
    for t in ev["tasks"]:
        if t["split"] != "holdout":
            continue
        r = conn.execute("SELECT sources FROM entries WHERE key=?", (t["key"],)).fetchone()
        try:
            srcs = set(json.loads(r["sources"] or "[]")) if r else set()
        except Exception:
            srcs = set()
        out.append({"query": t["query"], "expect": t["expect"], "key": t["key"],
                    "publisher": t["publisher"], "sources": srcs})
    return out[:n] if n else out


def _norm(ident: str | None) -> str:
    """Canonicalise an identifier before comparing it.

    This is not cosmetic and getting it wrong is how a benchmark lies. GitHub's
    Agent Finder emits `urn:ai:` where the spec says `urn:air:`; comparing raw
    strings scores every one of its correct answers as a miss and hands us a
    clean sweep we did not earn. The first run of this benchmark did exactly
    that and reported 0.0 for GitHub across 120 tasks. Both sides go through
    the same normaliser we already apply on ingest.
    """
    from .normalize import normalize_identifier
    return (normalize_identifier(ident) or "").strip().lower().rstrip(":")


def _metrics(hits: list[int | None], k: int, answered: int, total: int,
             lat: list[float]) -> dict:
    """hits[i] is the 1-based rank of the expected entry, or None if absent."""
    found = [h for h in hits if h]
    recall = len(found) / total if total else 0.0
    mrr = sum(1.0 / h for h in found) / total if total else 0.0
    # Single relevant document per task, so DCG is 1/log2(rank+1) and the ideal
    # is 1.0; nDCG is therefore the mean of those terms.
    ndcg = sum(1.0 / math.log2(h + 1) for h in found) / total if total else 0.0
    return {
        "tasks": total,
        "answered": answered,
        f"recall@{k}": round(recall, 4),
        "mrr": round(mrr, 4),
        f"ndcg@{k}": round(ndcg, 4),
        "found": len(found),
        "median_ms": int(statistics.median(lat)) if lat else None,
    }


async def _ask_upstream(client: httpx.AsyncClient, url: str, source: str,
                        query: str, k: int) -> tuple[list[str], float] | None:
    t0 = time.perf_counter()
    try:
        r = await client.post(url, json={"query": {"text": query},
                                         "pageSize": k, "federation": "none"},
                              headers={"content-type": "application/json",
                                       "user-agent": config.USER_AGENT},
                              timeout=config.BENCH_TIMEOUT_S)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    items = data.get("results") or data.get("items") or []
    if not isinstance(items, list):
        return None
    out = []
    for it in items[:k]:
        if isinstance(it, dict):
            out.append(_norm(it.get("identifier") or it.get("id")))
    return out, ms


async def run(conn, k: int | None = None, n: int | None = None,
              include_upstreams: bool = True) -> dict:
    """Run the benchmark. Returns per-target metrics."""
    k = k or config.BENCH_K
    tasks = build_tasks(conn, n)
    if not tasks:
        return {"error": "no eligible tasks", "tasks": 0}

    targets: dict[str, dict] = {}

    # --- us, in each mode we actually ship -------------------------------
    for label, mode, dense in (("neuronto (lexical only)", "none", False),
                               ("neuronto (hybrid + federated)", "auto", True)):
        hits: list[int | None] = []
        lat: list[float] = []
        answered = 0
        for t in tasks:
            t0 = time.perf_counter()
            try:
                out = await search.search(conn, t["query"], None, k, mode,
                                          use_dense=dense)
                res = out["results"]
                answered += 1
            except Exception:
                res = []
            lat.append((time.perf_counter() - t0) * 1000)
            want = _norm(t["expect"])
            rank_ = None
            for i, e in enumerate(res[:k], start=1):
                if _norm(e.get("identifier")) == want:
                    rank_ = i
                    break
            hits.append(rank_)
        m = _metrics(hits, k, answered, len(tasks), lat)
        # Every task target is in our index by construction, so our coverage is
        # 1.0 and the conditioned figure equals the raw one. Stated explicitly
        # rather than left implicit, because that is exactly the advantage a
        # reader needs to see when comparing the columns.
        m["carries_target"] = len(tasks)
        m["coverage"] = 1.0
        m[f"recall@{k}_when_carried"] = m[f"recall@{k}"]
        m["mrr_when_carried"] = m["mrr"]
        targets[label] = m

    # --- every other public ARD registry, same tasks, same k -------------
    if include_upstreams:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for uid, name, url, source in config.UPSTREAMS:
                hits, lat, answered = [], [], 0
                sem = asyncio.Semaphore(config.BENCH_CONCURRENCY)

                async def one(t):
                    nonlocal answered
                    async with sem:
                        got = await _ask_upstream(client, url, source,
                                                  t["query"], k)
                    return t, got

                in_corpus_hits: list[int | None] = []
                for coro in asyncio.as_completed([one(t) for t in tasks]):
                    t, got = await coro
                    # Does this registry carry the target at all? Asking a
                    # registry for something it never indexed measures our
                    # crawl, not its retrieval, so the two are reported apart.
                    carries = uid in t.get("sources", set())
                    if got is None:
                        hits.append(None)
                        if carries:
                            in_corpus_hits.append(None)
                        continue
                    idents, ms = got
                    answered += 1
                    lat.append(ms)
                    want = _norm(t["expect"])
                    rank_ = None
                    for i, ident in enumerate(idents, start=1):
                        if ident == want:
                            rank_ = i
                            break
                    hits.append(rank_)
                    if carries:
                        in_corpus_hits.append(rank_)
                m = _metrics(hits, k, answered, len(tasks), lat)
                m["carries_target"] = len(in_corpus_hits)
                m["coverage"] = round(len(in_corpus_hits) / len(tasks), 4) if tasks else 0.0
                if in_corpus_hits:
                    sub = _metrics(in_corpus_hits, k, len(in_corpus_hits),
                                   len(in_corpus_hits), lat)
                    m[f"recall@{k}_when_carried"] = sub[f"recall@{k}"]
                    m["mrr_when_carried"] = sub["mrr"]
                targets[name] = m

    payload = {
        "k": k,
        "tasks": len(tasks),
        "generated": int(time.time()),
        "ground_truth": ("publisher-written representativeQueries, read verbatim from "
                         "each publisher's own manifest and frozen with its sha256; one "
                         "task per publisher; holdout half only; entries published by "
                         "this registry excluded"),
        "task_set": "https://github.com/neuronto/agentic-resource-discovery/blob/main/bench/declared-v1.json",
        "identifier_matching": ("both sides normalised to the spec's urn:air: form "
                                "before comparison, so a registry emitting urn:ai: "
                                "is not scored as wrong"),
        "reading_the_numbers": (
            "`recall@k` is over all tasks and therefore mixes two things: whether "
            "a registry indexes the target at all, and whether it retrieves it. "
            "`coverage` is the fraction of targets a registry carries according to "
            "our own ingest records, and `recall@k_when_carried` is retrieval "
            "measured only on those. Compare the conditioned column. Our own "
            "coverage is 1.0 by construction, because tasks are built from our "
            "index, and that is an advantage of the task set, not a finding."),
        "known_bias": ("queries are drawn from representativeQueries, and this "
                       "registry weights that field highest in its own ranking "
                       "(bm25 weight 9.0 of 9.0). Every registry indexes the same "
                       "public manifests, so the field is equally available to all, "
                       "but a registry that chooses not to weight it is penalised "
                       "by our choice of ground truth. Read the absolute numbers "
                       "with that in mind; the harness is published so the task set "
                       "can be replaced."),
        "harness": "https://github.com/neuronto/agentic-resource-discovery/blob/main/app/bench.py",
        "targets": targets,
    }
    _save(conn, payload, len(tasks), k)
    return payload


def _save(conn, payload: dict, tasks: int, k: int) -> bool:
    """Persist a run, retrying past a busy writer.

    A benchmark pass takes minutes and talks to five external services. Losing
    all of it to an eight second lock because the web process happened to be
    logging a query is not acceptable, so this retries with backoff and, if the
    database still will not take it, writes the run beside the database rather
    than dropping it on the floor.
    """
    blob = json.dumps(payload, ensure_ascii=False)
    delay = 0.5
    for _ in range(6):
        try:
            conn.execute("INSERT INTO bench_runs(ts,tasks,k,results) VALUES(?,?,?,?)",
                         (payload["generated"], tasks, k, blob))
            conn.commit()
            return True
        except Exception:
            time.sleep(delay)
            delay *= 2
    try:
        from pathlib import Path
        p = Path(config.DB_PATH).parent / f"bench-{payload['generated']}.json"
        p.write_text(blob, encoding="utf-8")
        print(f"  bench: database busy, run written to {p}", flush=True)
    except Exception:
        pass
    return False


def latest(conn) -> dict | None:
    r = conn.execute("SELECT results FROM bench_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    try:
        return json.loads(r["results"])
    except Exception:
        return None
