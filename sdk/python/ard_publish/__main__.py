"""ard-publish: make your API, MCP server or AI agent discoverable by AI agents.

  ard-publish init <domain>              scaffold a valid manifest (stdout)
  ard-publish validate <file>            check a manifest locally
  ard-publish check <domain>             audit: discovery, conformance, coverage, competition
  ard-publish generate <domain>          have a manifest generated from what the domain serves
  ard-publish submit <endpoint|domain>   index an MCP endpoint or a manifest-serving domain
  ard-publish status <id|endpoint|domain>  where a submission stands (it is retried for days)
  ard-publish claim <domain>             get the DNS TXT record that proves you own it
  ard-publish verify <domain>            verify the record and receive a key

Set ARD_REGISTRY to use a different ARD registry (default https://neuronto.com).
Set ARD_KEY to a key from `verify` to raise your rate limit on the registry.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from . import Entry, Manifest, validate

REGISTRY = os.getenv("ARD_REGISTRY", "https://neuronto.com").rstrip("/")


def _call(path: str, payload: dict) -> dict:
    """POST to the registry. A refusal is returned as data, never raised."""
    headers = {"content-type": "application/json", "user-agent": "ard-publish/1.2"}
    key = os.getenv("ARD_KEY", "").strip()
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = urllib.request.Request(REGISTRY + path, data=json.dumps(payload).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        # A registry refusal carries `status` (not_an_mcp_server, no_manifest) or
        # `error` (rate_limited, invalid_request). Only invent a label when it
        # gave neither, so the caller sees the registry's own word for it.
        if "error" not in body and "status" not in body:
            body["error"] = f"http_{e.code}"
        body["_status"] = e.code
        return body


def _refused(d: dict) -> bool:
    if d.get("error") == "rate_limited":
        print(f"rate limited: {d.get('detail', '')}", file=sys.stderr)
        if d.get("raiseTheLimit"):
            print(f"  {d['raiseTheLimit']}", file=sys.stderr)
        return True
    if d.get("_status", 200) >= 400 and d.get("error"):
        print(f"{d['error']}: {d.get('detail', '')}", file=sys.stderr)
        return True
    return False       # a `status` refusal is the command's own to report


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
    print(f"#\n# Then: ard-publish validate .well-known/ard.json", file=sys.stderr)
    print(f"# And once it is live:  ard-publish submit {domain}", file=sys.stderr)
    return 0


def _validate(path: str) -> int:
    problems = validate(json.load(open(path)))
    if not problems:
        print("valid, publishable")
        print("\nnext: serve it, then `ard-publish submit <your-domain>` so a registry "
              "has actually seen it. Publishing and being indexed are different things.",
              file=sys.stderr)
        return 0
    print(f"{len(problems)} problem(s):")
    for p in problems:
        print("  -", p)
    return 1


def _check(domain: str) -> int:
    """Ask a live registry whether the whole setup actually worked."""
    d = _call("/audit", {"domain": domain})
    if _refused(d):
        return 2
    s = d["score"]
    print(f"{d['domain']}  grade {s['grade']}  {s['total']}/100\n")
    for b in s["breakdown"]:
        print(f"  {b['points']:>3}/{b['max']:<3}  {b['check']:<32} {b['detail']}")
    print("\n  registries returning you:")
    for c in d["coverage"]:
        print(f"    {'yes' if c['indexed'] else ' no'}  {c['registry']}")
    comp = d.get("competition") or {}
    if comp.get("queries"):
        print("\n  for your own representative queries, who is returned instead of you:")
        for q in comp["queries"]:
            rank = q.get("your_best_rank")
            print(f"    {q['query']!r}: " + (f"you rank {rank}" if rank else "you are not returned"))
            for a in q.get("ahead_of_you", [])[:3]:
                print(f"       ahead: {a['name']}  ({a['has_that_you_may_not']})")
    ix = d.get("indexable")
    if ix and ix.get("ready"):
        print(f"\n  not in this index yet, and your manifest already parsed here "
              f"({ix['entries']} entr{'y' if ix['entries'] == 1 else 'ies'}).")
        print(f"    {ix['how']['cli']}")
    print("\n  next:")
    for i, r_ in enumerate(d["recommendations"], 1):
        print(f"    {i}. {r_}")
    return 0 if s["total"] >= 75 else 1


def _generate(domain: str) -> int:
    """A manifest built from what the domain already exposes, nothing invented."""
    d = _call("/manifest/build", {"domain": domain})
    if _refused(d):
        return 2
    if not d.get("entries"):
        print(f"nothing found on {domain}: no MCP server answered a handshake, and no "
              f"OpenAPI document, agent card or llms.txt was served.", file=sys.stderr)
        return 1
    print(json.dumps(d["manifest"], indent=2))
    print(f"# {d['entries']} entr{'y' if d['entries'] == 1 else 'ies'}, each from something "
          f"that actually answered:", file=sys.stderr)
    for ev in d.get("evidence", []):
        print(f"#   {ev['entry']}: {ev['because']}", file=sys.stderr)
    if d.get("hosted_at"):
        print(f"# also hosted at {d['hosted_at']} until you serve your own", file=sys.stderr)
    print(f"#\n# Then: ard-publish submit {domain}", file=sys.stderr)
    return 0


def _submit(target: str) -> int:
    """An MCP endpoint is verified by handshake; a domain by fetching its manifest."""
    body = {"endpoint": target} if target.startswith("http") else {"domain": target}
    d = _call("/submit", body)
    if _refused(d):
        return 2
    st = d.get("status", "")
    if st == "indexed":
        print(f"indexed: {d.get('identifier') or d.get('page') or target}")
        if d.get("verified_tools") is not None:
            print(f"  verified tools: {d['verified_tools']}  "
                  f"{', '.join(d.get('tools', [])[:8])}")
        host = target.split("//")[-1].split("/")[0] if "//" in target else target
        print(f"\nnext: ard-publish check {host}   (which registries return you, and who "
              f"outranks you)", file=sys.stderr)
        return 0
    sub = d.get("submission") or {}
    if st == "pending" and sub:
        # Not indexed yet, and not lost either: the registry keeps the submission
        # and retries it on its own schedule. Say what it saw and where to look.
        print(f"pending: {d.get('refusal') or d.get('reason') or 'not verified yet'}. "
              f"{d.get('detail', '')}")
        ev = d.get("evidence")
        if ev:
            print(f"  your endpoint answered: {json.dumps(ev)[:300]}")
        print(f"  the registry will retry {sub.get('attempts_left', '?')} more times, "
              f"next in {sub.get('next_attempt_in_s', '?')}s; nothing to resend")
        print(f"  watch it: ard-publish status {sub.get('id', '')}", file=sys.stderr)
        return 3
    print(f"{st or 'not indexed'}: {d.get('detail', '')}")
    return 1


def _status(target: str) -> int:
    """A submission id from `submit`, or the endpoint or domain that was submitted."""
    import re
    import urllib.parse
    if re.fullmatch(r"[0-9a-f]{12}", target):
        path = f"/submit/status/{target}"
    elif target.startswith("http"):
        path = "/submit/status?endpoint=" + urllib.parse.quote(target, safe="")
    else:
        path = "/submit/status?domain=" + urllib.parse.quote(target, safe="")
    req = urllib.request.Request(REGISTRY + path, headers={"user-agent": "ard-publish/1.2"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        print(f"{'no such submission' if e.code == 404 else 'http_' + str(e.code)}", file=sys.stderr)
        return 2
    print(f"{d.get('status')}: {d.get('target')}  (attempt {d.get('attempts')})")
    if d.get("note"):
        print(f"  {d['note']}")
    if d.get("reason") and d.get("status") != "indexed":
        print(f"  last reason: {d['reason']}")
    if d.get("evidence"):
        print(f"  last answer from your side: {json.dumps(d['evidence'])[:300]}")
    return 0 if d.get("status") == "indexed" else 3


def _claim(domain: str) -> int:
    d = _call("/claim", {"domain": domain})
    if _refused(d):
        return 2
    r = d["record"]
    print(f"publish this DNS record at {r['host']}:")
    print(f"  type   {r['type']}")
    print(f"  name   {r['name']}")
    print(f"  value  {r['value']}")
    print(f"then run: ard-publish verify {domain}")
    return 0


def _verify(domain: str) -> int:
    d = _call("/claim/verify", {"domain": domain})
    if d.get("verified"):
        print(f"verified {domain}")
        print(f"key: {d['api_key']}")
        print("store it; it is shown once. Export it as ARD_KEY to raise your rate limit "
              "and to register private entries.", file=sys.stderr)
        return 0
    print(f"not verified. expected TXT: {d.get('expect', '')}", file=sys.stderr)
    found = d.get("found") or []
    if found:
        print(f"found: {found}", file=sys.stderr)
    return 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 2
    cmd, arg = sys.argv[1], sys.argv[2]
    cmds = {"init": _init, "validate": _validate, "check": _check,
            "generate": _generate, "submit": _submit, "status": _status,
            "claim": _claim, "verify": _verify}
    fn = cmds.get(cmd)
    if fn is None:
        print(f"unknown command {cmd!r}\n\n" + __doc__.strip())
        return 2
    return fn(arg)


if __name__ == "__main__":
    raise SystemExit(main())
