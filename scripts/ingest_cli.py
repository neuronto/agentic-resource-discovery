#!/usr/bin/env python3
"""Fill and maintain the index.

  python -m scripts.ingest_cli mcp          # the official MCP Registry
  python -m scripts.ingest_cli upstreams    # harvest the four ARD registries
  python -m scripts.ingest_cli smithery     # the Smithery aggregator
  python -m scripts.ingest_cli glama        # the Glama aggregator (needs a key)
  python -m scripts.ingest_cli dockerhub    # Docker's mcp/ namespace
  python -m scripts.ingest_cli mcpso [n]    # mcp.so via its sitemap
  python -m scripts.ingest_cli crawl f.txt  # well-known paths on a domain list
  python -m scripts.ingest_cli liveness [n] # probe a batch of endpoints
  python -m scripts.ingest_cli introspect [n]  # tools/list on MCP endpoints
  python -m scripts.ingest_cli embed [n]    # build dense vectors (prose + tools)
  python -m scripts.ingest_cli adoption     # re-probe the adoption watchlist
  python -m scripts.ingest_cli bench [k] [n]# run ARD-Bench
  python -m scripts.ingest_cli reindex      # rebuild the FTS table
  python -m scripts.ingest_cli payments [n] # re-read servers that answered 402, probe stated prices
  python -m scripts.ingest_cli all

`all` is what the timer runs. Order matters: introspection must precede
embedding, because a verified tool list is part of the text we embed, and
reindexing folds those tools into the lexical index.
"""
import asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import (adoption, bench, dockerhub, embed, glama, ingest, liveness,  # noqa: E402
                 mcpso,
                 smithery,
                 store,
                 tools_index)


def _num(i: int, default: int) -> int:
    return int(sys.argv[i]) if len(sys.argv) > i and sys.argv[i].isdigit() else default


async def main() -> None:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    conn = store.connect(); store.init(conn)
    t0 = time.time()

    if cmd in ("mcp", "all"):
        print("mcp-registry:", await ingest.from_mcp_registry(conn), flush=True)
    if cmd in ("upstreams", "all"):
        print("upstreams:", await ingest.from_upstreams(conn), flush=True)
    if cmd in ("glama", "all"):
        # Highest miss rate of any source evaluated: 72% of a 3,000 server sample
        # was absent from the index. Needs GLAMA_API_KEY; skips cleanly without one.
        # The backfill (400 pages, 40,000 servers) was a one-off; run
        # `ingest_cli glama 400` by hand to extend it. Glama's cursor is ordered
        # newest first (verified: endCursor.createdAt descends page over page), so
        # a small budget per scheduled run catches everything published since the
        # last one, without re-walking the whole catalogue four times a day.
        print("glama:", await glama.from_glama(conn, max_pages=_num(2, 40)), flush=True)
    if cmd == "mcpso":
        # Not in `all`: it reads ~18,700 pages, so it runs on its own schedule
        # rather than inside a six-hourly job that also walks four registries.
        print("mcpso:", await mcpso.from_mcpso(conn, max_servers=_num(2, 25000)),
              flush=True)
    if cmd in ("dockerhub", "all"):
        # Docker's own mcp/ namespace: containerised servers, a different artifact
        # from a repo or an endpoint, so some appear nowhere else.
        print("dockerhub:", await dockerhub.from_dockerhub(conn), flush=True)
    if cmd in ("smithery", "all"):
        # An aggregator, not an ARD registry, so it cannot go through upstreams.
        # Carries community servers never published to the official registry:
        # 35% of a sample was absent from our index when this was added.
        print("smithery:", await smithery.from_smithery(conn), flush=True)
    if cmd == "crawl":
        src = Path(sys.argv[2])
        conc = int(sys.argv[3]) if len(sys.argv) > 3 else None
        # Optional time box in seconds, so a scheduled crawl gives the index lock back.
        secs = float(sys.argv[4]) if len(sys.argv) > 4 else None
        doms = [l.strip() for l in src.read_text().splitlines() if l.strip()
                and not l.startswith("#")]
        print(f"crawling {len(doms)} domains", flush=True)
        print("crawl:", await ingest.crawl_domains(conn, doms, concurrency=conc, max_seconds=secs),
              flush=True)
    if cmd in ("liveness", "all"):
        print("liveness:", await liveness.sweep(conn, limit=_num(2, 400)), flush=True)
    if cmd in ("introspect", "all"):
        print("introspect:", await tools_index.sweep(conn, limit=_num(2, 400)), flush=True)
    if cmd in ("embed", "all"):
        # 2000 a run fell behind the index's growth and coverage sat at 0.85 for days;
        # 6000 every six hours clears a backlog and keeps up with a registry walk.
        print("embed:", await embed.build(conn, limit=_num(2, 6000)), flush=True)
        # The tool surface gets its own vector. Runs after introspection for the
        # same reason the prose vector does: there is nothing to embed until the
        # tools have been read.
        print("embed-tools:", await embed.build_tools(conn, limit=_num(2, 2000)),
              flush=True)
    if cmd in ("adoption", "all"):
        print("adoption:", await adoption.refresh_watchlist(conn), flush=True)
    if cmd == "bench":
        out = await bench.run(conn, k=_num(2, 0) or None, n=_num(3, 0) or None)
        for name, m in (out.get("targets") or {}).items():
            print(f"  {name:34s} {m}", flush=True)
    if cmd == "payments":
        # Servers recorded as broken because they answered 402, read again now
        # that a 402 is understood, then a live check of what manifests state.
        rows = conn.execute("""SELECT key, url FROM entries WHERE mcp_status='error:http402'
                               AND url LIKE 'http%'""").fetchall()
        print(f"re-introspecting {len(rows)} servers that answered 402", flush=True)
        print("introspect:", await tools_index.sweep(conn, rows=list(rows)), flush=True)
        rows = conn.execute("""SELECT key, url FROM entries WHERE pay_declared IS NOT NULL
                               AND pay_live IS NULL AND url LIKE 'http%' LIMIT ?""",
                            (_num(2, 400),)).fetchall()
        print(f"probing {len(rows)} endpoints that state a price", flush=True)
        print("liveness:", await liveness.sweep(conn, rows=list(rows)), flush=True)
        print("payments:", store.payment_counts(conn), flush=True)
    if cmd == "reindex":
        print("reindexed:", store.rebuild_fts(conn), flush=True)

    print("counts:", store.counts(conn), flush=True)
    if cmd in ("introspect", "all", "reindex"):
        print("verified:", store.tool_counts(conn), flush=True)
    print(f"done in {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
