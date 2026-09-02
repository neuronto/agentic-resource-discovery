"""neuronto on the command line: python -m neuronto "read a PDF"."""
from __future__ import annotations

import argparse
import json
import sys

from . import (NeurontoError, find_resource, find_tool, liveness, registry_stats,
               __version__)

KINDS = {
    "mcp": "application/mcp-server+json",
    "api": "application/vnd.oai.openapi+json",
    "skill": "application/ai-skill+json",
    "agent": "application/ai-agent+json",
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="neuronto",
        description="Find the MCP servers, skills, agents and APIs that can do a task, "
                    "across every public ARD registry at once.")
    p.add_argument("command", nargs="?", default="find",
                   help="find (default) | tools | stats | dead")
    p.add_argument("query", nargs="*", help="what you need it to do")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--kind", choices=sorted(KINDS))
    p.add_argument("--local", action="store_true",
                   help="this index only, do not federate to other ARD registries")
    p.add_argument("--json", action="store_true", help="raw JSON, for piping")
    p.add_argument("--key", help="a verified-domain key; keyed searches are not logged")
    p.add_argument("--version", action="version", version=f"neuronto {__version__}")
    a = p.parse_args(argv)

    cmd, words = a.command, list(a.query)
    if cmd not in ("find", "tools", "stats", "dead"):
        words.insert(0, cmd)
        cmd = "find"
    query = " ".join(words).strip()

    try:
        if cmd == "stats":
            s = registry_stats()
            if a.json:
                print(json.dumps(s, indent=2)); return 0
            print(f"{s['index']['entries']:,} resources from "
                  f"{s['index']['publishers']:,} publishers")
            print(f"{s['reachability']['share_answering_pct']}% of probed endpoints answer "
                  f"({s['reachability']['not_answering']:,} do not)")
            print(f"{s['tools']['verified_tools_total']:,} verified tools, "
                  f"median {s['tools']['median_tools_per_server']} per server")
            print(f"\nmeasured over {s['window']['days']} days, "
                  f"{s['window']['observations']:,} recorded changes")
            return 0

        if cmd == "dead":
            d = liveness(dead=True, limit=a.limit)
            if a.json:
                print(json.dumps(d, indent=2)); return 0
            print(f"{d['count']} endpoint(s) that stopped answering:\n")
            for i in d["items"]:
                print(f"  {i['http_status'] or '---'}  {i['url']}")
            print("\nfree to reuse, no attribution required")
            return 0

        if not query:
            p.print_help(); return 2

        if cmd == "tools":
            tools = find_tool(query, limit=a.limit, api_key=a.key)
            if a.json:
                print(json.dumps(tools, indent=2)); return 0
            if not tools:
                print(f'No verified tool matches "{query}".'); return 1
            print(f'{len(tools)} tool(s) for "{query}":\n')
            for i, t in enumerate(tools, 1):
                print(f"{i:>2}. {t.get('tool') or 'unnamed'}")
                if t.get("server"):
                    print(f"    on {t['server']}  {t.get('endpoint', '')}".rstrip())
                if t.get("description"):
                    print(f"    {str(t['description'])[:150]}")
                print()
            print("every tool above was read from that server's own tools/list")
            return 0

        out = find_resource(query, limit=a.limit, kind=KINDS.get(a.kind or ""),
                            federate=not a.local, api_key=a.key)
        if a.json:
            print(json.dumps(out, indent=2)); return 0
        results = out["results"]
        if not results:
            print(f'Nothing matched "{query}" in this index or the registries it federates.')
            return 1
        fed = out.get("federation") or {}
        regs = [r for r in (fed.get("registries") or []) if r.get("ok")]
        extra = f", {len(regs)} registries answered" if regs else ""
        print(f'{len(results)} match(es) for "{query}"{extra}:\n')
        for i, r in enumerate(results, 1):
            bits = []
            if r.get("type"):
                bits.append(str(r["type"]).replace("application/", ""))
            if isinstance(r.get("score"), (int, float)):
                bits.append(f"relevance {r['score']}")
            print(f"{i:>2}. {r.get('displayName') or r.get('identifier') or 'unnamed'}")
            if bits:
                print("    " + "  ·  ".join(bits))
            if r.get("url"):
                print(f"    {r['url']}")
            if r.get("description"):
                print(f"    {str(r['description'])[:150]}")
            print()
        print("score is relevance only, never a trust or safety rating")
        return 0

    except NeurontoError as e:
        print(f"neuronto: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
