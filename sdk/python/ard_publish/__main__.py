"""CLI:  python -m ard_publish init <domain>  |  check <domain>  |  validate <file>"""
from __future__ import annotations

import json
import sys
import urllib.request

from . import Entry, Manifest, validate

CONSOLE = "https://neuronto.com/audit"


def _init(domain: str) -> int:
    m = Manifest(host=domain, display_name=domain)
    m.add(Entry.mcp_server(
        name="example", display_name="Example MCP server",
        url=f"https://{domain}/.well-known/mcp/server-card.json",
        host=m.host,
        description="Describe what this resource does, in one plain sentence.",
        queries=["a task someone would ask for in their own words",
                 "another way of asking for the same thing"]))
    print(m.to_json())
    print("# Save as .well-known/ard.json, then add to robots.txt:", file=sys.stderr)
    print("#   " + m.robots_line(), file=sys.stderr)
    print("# and in your <head>:", file=sys.stderr)
    for line in m.link_tags().splitlines():
        print("#   " + line, file=sys.stderr)
    return 0


def _validate(path: str) -> int:
    problems = validate(json.load(open(path)))
    if not problems:
        print("valid — publishable"); return 0
    print(f"{len(problems)} problem(s):")
    for p in problems: print("  -", p)
    return 1


def _check(domain: str) -> int:
    """Ask a live registry whether the whole setup actually worked."""
    req = urllib.request.Request(
        CONSOLE, data=json.dumps({"domain": domain}).encode(),
        headers={"content-type": "application/json", "user-agent": "ard-publish"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    s = d["score"]
    print(f"{d['domain']}  grade {s['grade']}  {s['total']}/100\n")
    for b in s["breakdown"]:
        print(f"  {b['points']:>3}/{b['max']:<3}  {b['check']:<32} {b['detail']}")
    print("\n  registries returning you:")
    for c in d["coverage"]:
        print(f"    {'yes' if c['indexed'] else ' no'}  {c['registry']}")
    print("\n  next:")
    for i, r_ in enumerate(d["recommendations"], 1):
        print(f"    {i}. {r_}")
    return 0 if s["total"] >= 75 else 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip()); return 2
    cmd, arg = sys.argv[1], sys.argv[2]
    return {"init": _init, "check": _check, "validate": _validate}.get(
        cmd, lambda _a: (print(f"unknown command {cmd!r}"), 2)[1])(arg)


if __name__ == "__main__":
    raise SystemExit(main())
