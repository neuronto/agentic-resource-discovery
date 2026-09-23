"""One-time: give every existing entry a provenance map for its representativeQueries.

Rows written before the split carry one unioned `rep_queries` column, so their
provenance has to be inferred once. From then on `store.rq_merge` records it at write
time, and each row's `legacy` slot retires as soon as every source carrying the row has
restated its own queries, which the scheduled ingest does on its next pass. The
inference below therefore only has to be conservative, not perfect: an error in it is
temporary, and it errs towards under-claiming what a publisher wrote.

  * every source is one that synthesises queries  -> all inferred
  * no source synthesises queries                 -> declared, unless the query
                                                     carries a synthesis signature
  * both                                          -> all inferred, until the declared
                                                     source restates its own

`rep_queries` itself is not modified, so matching, the FTS index and the vectors are
unchanged by this migration; it only fills `rq_sources` and `rep_queries_declared`.

  python -m scripts.migrate_rq_provenance --dry-run
  python -m scripts.migrate_rq_provenance   # holding the index write lock
"""
from __future__ import annotations

import json
import random
import sys

from app import store

# Sources whose ingest synthesises representativeQueries (they write `_inferredQueries`
# since the split). Every other source relays what a publisher or a registry stated.
SYNTH = {"glama", "mcpso", "smithery", "dockerhub", "mcp-registry", "apis.guru"}


def synth_signature(q: str, desc: str) -> bool:
    """True when a query has the shape our synthesisers produce.

    They lowercase everything, and build from the description or the title: the first
    sentence, "<name> for this task", "use the <title>", "<title> api". A publisher's
    real query is almost never a verbatim fragment of its own description, and the
    indexing filter already drops one that repeats the description whole.
    """
    low = q.strip().lower().rstrip(".")
    if "for this task" in low:
        return True
    if q != q.lower():
        return False
    if low.startswith("use the ") or low.endswith(" api"):
        return True
    d = (desc or "").lower()
    return bool(low) and low in d


def plan(row) -> dict:
    srcs = set(json.loads(row["sources"] or "[]"))
    rq = store._qlist(json.loads(row["rep_queries"] or "[]"))
    if not rq:
        return {}
    if srcs and srcs <= SYNTH:
        d, i = [], rq
    elif not (srcs & SYNTH):
        d = [q for q in rq if not synth_signature(q, row["description"])]
        i = [q for q in rq if q not in d]
    else:
        # Measured on 2026-09-23: what survived the signature test on these rows was
        # mostly our own synthesis built from ANOTHER source's description, which no
        # test on the stored description can see. So nothing is claimed for them
        # here; their declared sources restate their own queries on the next harvest.
        d, i = [], rq
    return {store.LEGACY: {"d": d, "i": i}}


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    conn = store.connect()           # a write connection; tls_conn is query_only
    store.init(conn)                 # adds the two columns
    rows = conn.execute("""SELECT key, sources, rep_queries, description
                           FROM entries WHERE rq_sources IS NULL""").fetchall()
    stats = {"rows": len(rows), "with_queries": 0, "declared": 0, "inferred": 0,
             "mixed_rows": 0, "mixed_declared": 0, "mixed_inferred": 0}
    samples: dict[str, list] = {"kept_declared": [], "judged_inferred": []}
    updates = []
    for r in rows:
        slots = plan(r)
        allq, decl = store.rq_derive(slots)
        if allq:
            stats["with_queries"] += 1
            stats["declared"] += len(decl)
            stats["inferred"] += len(allq) - len(decl)
            srcs = set(json.loads(r["sources"] or "[]"))
            if srcs & SYNTH and srcs - SYNTH:
                stats["mixed_rows"] += 1
                stats["mixed_declared"] += len(decl)
                stats["mixed_inferred"] += len(allq) - len(decl)
                samples["kept_declared"] += [(q, r["key"][:40]) for q in decl]
                samples["judged_inferred"] += [(q, r["key"][:40])
                                               for q in allq if q not in decl]
        updates.append((json.dumps(slots), json.dumps(decl), r["key"]))
    print(json.dumps(stats))
    rnd = random.Random(1)
    for k, v in samples.items():
        print(f"\n  sample {k}:")
        for q, key in rnd.sample(v, min(12, len(v))):
            print(f"    {q[:80]!r:84} {key}")
    if dry:
        print("\n  dry run: nothing written")
        return 0
    done = 0
    for i in range(0, len(updates), 2000):
        chunk = updates[i:i + 2000]
        conn.executemany("""UPDATE entries SET rq_sources=?, rep_queries_declared=?
                            WHERE key=? AND rq_sources IS NULL""", chunk)
        conn.commit()
        done += len(chunk)
    left = conn.execute("SELECT COUNT(*) FROM entries WHERE rq_sources IS NULL").fetchone()[0]
    print(f"\n  migrated {done} rows; {left} rows still without a provenance map")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
