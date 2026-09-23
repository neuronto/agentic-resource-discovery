"""Freeze a declared-only retrieval eval set.

WHY THIS EXISTS. ARD-Bench samples its tasks from our own index, and the index's
`representativeQueries` column is a union: what a publisher wrote, plus what we
synthesised for resources that came from aggregators and never had any (the first
sentence of the description, "<name> for this task"). A census on 2026-09-23 found
that of 7,898 queries on entries that also carried a publisher-declared source, only
368 survived removing our own synthesis patterns. A benchmark drawn from that column
grades our ranking against text our ingest wrote, and tuning against it would tune the
ranker to agree with itself.

WHAT COUNTS AS DECLARED. Only a query read from the publisher's own manifest, fetched
from the publisher's own domain at a well-known path. Not an upstream registry's copy
(it may synthesise too, and we cannot tell), not a submission we built from tool names.
Each task records the manifest URL and the sha256 of the body it was read from, so
anyone can check that the query is the publisher's, verbatim.

WHY IT IS FROZEN. The set is written once to `bench/declared-v<N>.json` and never
regenerated in place. A ranking change is measured against the same tasks as the
change before it; a new set is a new version, reported as such. Tasks are split by a
stable hash of the publisher into `tune` (the only half a weight may be chosen on) and
`holdout` (what is reported and what the regression gate reads), so a tuned number is
never presented as a measured one.

    python -m scripts.build_eval bench/declared-v1.json     # build, refuses to overwrite
    python -m scripts.build_eval --check bench/declared-v1.json
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sys
import time
import zlib

import httpx

from app import config, ingest, store
from app.evalset import digest as _digest, load  # noqa: F401  (load is re-exported)
from app.normalize import dedupe_key, normalize_identifier, publisher_of

MIN_LEN, MAX_LEN = 12, 160   # the same bounds ARD-Bench has always used
SEED = 7


def split_of(publisher: str) -> str:
    # crc32, never hash(): str hashing is randomised per process.
    return "holdout" if zlib.crc32(publisher.encode()) % 2 else "tune"


async def _fetch_all(domains: list[str]) -> dict[str, tuple[dict, str, str]]:
    out: dict[str, tuple[dict, str, str]] = {}
    sem = asyncio.Semaphore(16)
    async with httpx.AsyncClient(headers=ingest.HEADERS, timeout=config.CRAWL_TIMEOUT_S,
                                 follow_redirects=True) as client:
        async def one(dom: str):
            base = f"https://{dom}"
            async with sem:
                for path in ingest.PATHS:
                    try:
                        r = await client.get(base + path)
                    except Exception:
                        continue
                    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                        continue
                    try:
                        d = r.json()
                    except Exception:
                        continue
                    if isinstance(d, dict) and isinstance(d.get("entries"), list):
                        out[dom] = (d, base + path, hashlib.sha256(r.content).hexdigest())
                        return
        await asyncio.gather(*(one(d) for d in domains))
    return out


def build(conn) -> dict:
    domains = [r[0] for r in conn.execute(
        "SELECT domain FROM crawl_seen WHERE manifest_path IS NOT NULL AND entries > 0 "
        "ORDER BY domain")]
    fetched = asyncio.run(_fetch_all(domains))

    candidates: list[dict] = []
    for dom in sorted(fetched):
        data, murl, sha = fetched[dom]
        for e in data.get("entries") or []:
            if not isinstance(e, dict):
                continue
            # Exactly the filter indexing applies, so a query we drop on ingest is
            # never a task we then fail to find.
            e = ingest._without_placeholder_queries(e)
            ident_raw = e.get("identifier") or e.get("@id") or e.get("url")
            ident = normalize_identifier(ident_raw) or ident_raw
            url = e.get("url")
            if not ident:
                continue
            pub = publisher_of(ident, url)
            if not pub or pub == config.PUBLISHER:
                continue
            key = dedupe_key(ident, url)
            indexed = conn.execute("SELECT 1 FROM entries WHERE key=?", (key,)).fetchone()
            for q in e.get("representativeQueries") or []:
                q = str(q).strip()
                if MIN_LEN <= len(q) <= MAX_LEN:
                    candidates.append({"query": q, "expect": ident, "key": key,
                                       "publisher": pub, "manifest": murl,
                                       "manifest_sha256": sha,
                                       "indexed": bool(indexed)})

    # One task per publisher, chosen reproducibly, so one prolific catalogue cannot
    # carry the score. Only indexed targets are tasks: a target we never indexed
    # measures the crawl, not the ranking, and is counted separately.
    rnd = random.Random(SEED)
    rnd.shuffle(candidates)
    by_pub: dict[str, dict] = {}
    for c in candidates:
        if c["indexed"] and c["publisher"] not in by_pub:
            by_pub[c["publisher"]] = c
    tasks = []
    for pub in sorted(by_pub):
        c = dict(by_pub[pub])
        c.pop("indexed")
        c["split"] = split_of(pub)
        tasks.append(c)

    return {
        "name": "neuronto-declared",
        "created": time.strftime("%Y-%m-%d", time.gmtime()),
        "ground_truth": ("a publisher-written representativeQuery, read verbatim from the "
                         "publisher's own manifest at a well-known path on its own domain; "
                         "never from an upstream registry's copy and never synthesised"),
        "selection": (f"one task per publisher, seed {SEED}, query length {MIN_LEN}-{MAX_LEN}, "
                      "the same placeholder filter indexing applies, entries published by "
                      "this registry excluded"),
        "splits": ("`tune` is the only half a ranking parameter may be chosen on; "
                   "`holdout` is what is reported and what the regression gate reads. "
                   "Split by crc32 of the publisher."),
        "counts": {
            "manifests_listed": len(domains),
            "manifests_fetched": len(fetched),
            "declared_queries": len(candidates),
            "publishers_with_a_query": len({c["publisher"] for c in candidates}),
            "publishers_not_indexed": len({c["publisher"] for c in candidates}
                                          - {c["publisher"] for c in candidates if c["indexed"]}),
            "tasks": len(tasks),
            "tune": sum(t["split"] == "tune" for t in tasks),
            "holdout": sum(t["split"] == "holdout" for t in tasks),
        },
        "sha256": _digest(tasks),
        "tasks": tasks,
    }


def main(argv: list[str]) -> int:
    if argv[:1] == ["--check"]:
        d = load(argv[1])
        print(f"  {argv[1]}: {len(d['tasks'])} tasks, digest ok")
        return 0
    path = argv[0]
    if os.path.exists(path):
        print(f"  {path} exists; a frozen set is never rebuilt in place", file=sys.stderr)
        return 1
    conn = store.connect()
    d = build(conn)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(json.dumps(d["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
