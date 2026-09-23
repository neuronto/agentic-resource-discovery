#!/usr/bin/env python3
"""Retrieval regression gate: the frozen eval sets against a pinned, accepted baseline.

WHY. On 2026-09-02 the tool leg began running in every search mode, two days after the
last benchmark. Nothing measured retrieval again until 2026-09-23, when a publisher's
own verbatim query was found to return its entry 21% of the time, down from 98%. The
change was reasonable, the tests passed, and three weeks of search ran degraded because
no instrument was pointed at ranking quality. This is that instrument, on a schedule.

WHAT IT MEASURES, both in `federation: none` (deterministic, no network, so a change
in the number is a change in our ranking and never an upstream having a bad hour):

  * the `holdout` half of the frozen declared set (bench/declared-v1.json):
    recall@10 and MRR on queries publishers wrote about their own resources;
  * the capability battery (scripts/battery.py): hit@5 on ordinary requests whose
    obvious answer is a named vendor;
  * known-item queries: each holdout target's own display name, recall@10 and MRR.

And one structural property: `federation: auto` never scores below `none` on the
holdout. Federated legs are additive by design (rank.py), so auto falling below local
means that design broke, which is exactly the criticism a published review made of the
old fusion. That leg touches the network, so it is compared to local measured in the
same run, never to a pinned number.

Each is a guard on the other. Tuning for publisher queries alone would switch the tool
leg off and lose "charge a credit card"; tuning for the battery alone is how the 21%
happened.

THE RULE. Fail when holdout recall@10 or MRR falls more than TOLERANCE below the
baseline, or battery hit@5 loses a case. Fail too when the instrument is broken: a set
whose digest does not match, or searches that raised. A gate that cannot fail tests
nothing, so `scripts/test_provenance.py` plants the old defect and checks it is caught.

    python -m scripts.evalgate               # measure, compare, exit 1 on regression
    python -m scripts.evalgate --accept      # measure and pin as the new baseline

`--accept` is a deliberate human act after a change that was meant to move a number.
It never runs from the timer.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from app import bench, rank, search, store
from scripts.battery import CASES, hit_rank
from app.evalset import load

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SET = os.path.join(ROOT, "bench", "declared-v1.json")
BASELINE = os.path.join(ROOT, "bench", "baseline.json")
TOLERANCE = 0.02     # two points of recall or MRR; below that is noise at n=106


async def measure() -> dict:
    ev = load(SET)
    tasks = [t for t in ev["tasks"] if t["split"] == "holdout"]
    conn = store.tls_conn()
    hits, rr, errors, lat = 0, 0.0, 0, []
    for t in tasks:
        t0 = time.perf_counter()
        try:
            out = await search.search(conn, t["query"], None, 10, "none")
        except Exception:
            errors += 1
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        res = [bench._norm(x.get("identifier")) for x in out.get("results") or []]
        want = bench._norm(t["expect"])
        if want in res:
            hits += 1
            rr += 1.0 / (res.index(want) + 1)
    # Known-item queries: each holdout target's own display name. Built from the
    # frozen tasks at run time, so it needs no second file and cannot drift from them.
    n_hits, n_rr, n_n = 0, 0.0, 0
    for t in tasks:
        r = conn.execute("SELECT display_name FROM entries WHERE key=?", (t["key"],)).fetchone()
        if not r or not r[0] or len(r[0]) < 4:
            continue
        n_n += 1
        try:
            out = await search.search(conn, r[0], None, 10, "none")
        except Exception:
            errors += 1
            continue
        res = [bench._norm(x.get("identifier")) for x in out.get("results") or []]
        want = bench._norm(t["expect"])
        if want in res:
            n_hits += 1
            n_rr += 1.0 / (res.index(want) + 1)
    # auto on the same tasks: may not fall below local, measured in the same run.
    a_hits, a_rr, a_fed = 0, 0.0, 0
    for t in tasks:
        try:
            out = await search.search(conn, t["query"], None, 10, "auto")
        except Exception:
            errors += 1
            continue
        a_fed += bool((out.get("_gate") or {}).get("federated"))
        res = [bench._norm(x.get("identifier")) for x in out.get("results") or []]
        want = bench._norm(t["expect"])
        if want in res:
            a_hits += 1
            a_rr += 1.0 / (res.index(want) + 1)
    b5 = b10 = 0
    for q, targets in CASES:
        try:
            out = await search.search(conn, q, None, 10, "none")
        except Exception:
            errors += 1
            continue
        r = hit_rank(out.get("results") or [], targets)
        if r:
            b10 += 1
            b5 += r <= 5
    n = len(tasks)
    lat.sort()
    return {
        "set": os.path.basename(SET), "set_sha256": ev["sha256"],
        "holdout_n": n,
        "recall@10": round(hits / n, 4) if n else 0.0,
        "mrr": round(rr / n, 4) if n else 0.0,
        "name_n": n_n,
        "name_recall@10": round(n_hits / n_n, 4) if n_n else 0.0,
        "name_mrr": round(n_rr / n_n, 4) if n_n else 0.0,
        "auto_recall@10": round(a_hits / n, 4) if n else 0.0,
        "auto_mrr": round(a_rr / n, 4) if n else 0.0,
        "auto_federated": a_fed,
        "battery_n": len(CASES), "battery_hit@5": b5, "battery_hit@10": b10,
        "errors": errors,
        "p50_ms": int(lat[len(lat) // 2]) if lat else None,
        "weights": {"fts": list(rank.FTS_WEIGHTS), "tool_strict": rank.TOOL_LEG_WEIGHT,
                    "tool_loose": rank.TOOL_LEG_LOOSE_WEIGHT},
        "ts": int(time.time()),
    }


def verdict(cur: dict, base: dict) -> list[str]:
    fails = []
    if cur["errors"]:
        fails.append(f"instrument: {cur['errors']} searches raised")
    if cur["set_sha256"] != base.get("set_sha256"):
        fails.append("instrument: eval set differs from the one the baseline was pinned on")
    for m in ("recall@10", "mrr", "name_recall@10", "name_mrr"):
        if m in base and cur[m] < base[m] - TOLERANCE:
            fails.append(f"{m} {base[m]:.4f} -> {cur[m]:.4f}")
    for m in ("recall@10", "mrr"):
        if cur[f"auto_{m}"] < cur[m] - 0.01:
            fails.append(f"auto below local on {m}: {cur['auto_' + m]:.4f} < {cur[m]:.4f}")
    if cur["battery_hit@5"] < base["battery_hit@5"]:
        fails.append(f"battery hit@5 {base['battery_hit@5']} -> {cur['battery_hit@5']}")
    return fails


def main(argv: list[str]) -> int:
    cur = asyncio.run(measure())
    line = (f"holdout recall@10 {cur['recall@10']:.4f} mrr {cur['mrr']:.4f} "
            f"(n={cur['holdout_n']}) | names {cur['name_recall@10']:.4f}/{cur['name_mrr']:.4f} "
            f"| auto {cur['auto_recall@10']:.4f}/{cur['auto_mrr']:.4f} "
            f"federated {cur['auto_federated']} | battery hit@5 {cur['battery_hit@5']}/{cur['battery_n']} "
            f"| p50 {cur['p50_ms']} ms")
    if "--accept" in argv:
        if cur["errors"]:
            print(f"  refusing to pin a baseline measured with errors: {line}")
            return 1
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
            f.write("\n")
        print(f"  baseline pinned: {line}")
        return 0
    try:
        with open(BASELINE, encoding="utf-8") as f:
            base = json.load(f)
    except FileNotFoundError:
        print("  no baseline pinned; run with --accept once")
        return 1
    fails = verdict(cur, base)
    print(f"  {'REGRESSION' if fails else 'ok'}: {line}")
    for x in fails:
        print(f"    - {x}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
