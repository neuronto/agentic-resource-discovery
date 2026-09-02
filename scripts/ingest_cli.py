#!/usr/bin/env python3
"""Fill and maintain the index.

  python -m scripts.ingest_cli mcp          # the official MCP Registry
  python -m scripts.ingest_cli upstreams    # harvest the four ARD registries
  python -m scripts.ingest_cli crawl f.txt  # well-known paths on a domain list
  python -m scripts.ingest_cli liveness [n] # probe a batch of endpoints
  python -m scripts.ingest_cli introspect [n]  # tools/list on MCP endpoints
  python -m scripts.ingest_cli embed [n]    # build dense vectors (prose + tools)
  python -m scripts.ingest_cli adoption     # re-probe the adoption watchlist
  python -m scripts.ingest_cli bench [k] [n]# run ARD-Bench
  python -m scripts.ingest_cli reindex      # rebuild the FTS table
  python -m scripts.ingest_cli all

`all` is what the timer runs. Order matters: introspection must precede
embedding, because a verified tool list is part of the text we embed, and
reindexing folds those tools into the lexical index.
"""
import asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import adoption, bench, embed, ingest, liveness, store, tools_index  # noqa: E402


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
    if cmd == "crawl":
        src = Path(sys.argv[2])
        conc = int(sys.argv[3]) if len(sys.argv) > 3 else None
        doms = [l.strip() for l in src.read_text().splitlines() if l.strip()
                and not l.startswith("#")]
        print(f"crawling {len(doms)} domains", flush=True)
        print("crawl:", await ingest.crawl_domains(conn, doms, concurrency=conc), flush=True)
    if cmd in ("liveness", "all"):
        print("liveness:", await liveness.sweep(conn, limit=_num(2, 400)), flush=True)
    if cmd in ("introspect", "all"):
        print("introspect:", await tools_index.sweep(conn, limit=_num(2, 400)), flush=True)
    if cmd in ("embed", "all"):
        print("embed:", await embed.build(conn, limit=_num(2, 2000)), flush=True)
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
    if cmd == "reindex":
        print("reindexed:", store.rebuild_fts(conn), flush=True)

    print("counts:", store.counts(conn), flush=True)
    if cmd in ("introspect", "all", "reindex"):
        print("verified:", store.tool_counts(conn), flush=True)
    print(f"done in {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
