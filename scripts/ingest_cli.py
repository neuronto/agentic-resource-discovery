#!/usr/bin/env python3
"""Fill and maintain the index.

  python -m scripts.ingest_cli mcp        # the official MCP Registry
  python -m scripts.ingest_cli upstreams  # harvest the four ARD registries
  python -m scripts.ingest_cli crawl f.txt# well-known paths on a domain list
  python -m scripts.ingest_cli liveness   # probe a batch of endpoints
  python -m scripts.ingest_cli all
"""
import asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ingest, liveness, store   # noqa: E402


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
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 400
        print("liveness:", await liveness.sweep(conn, limit=n), flush=True)

    print("counts:", store.counts(conn), flush=True)
    print(f"done in {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
