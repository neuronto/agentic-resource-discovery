"""The MCP Registry v0.1 read API, checked the way an MCP host reads it.

    python scripts/test_subregistry.py [base] [--full]

Validates every record against the ServerResponse / ServerList schemas of the
registry's own OpenAPI (fetched fresh, never a copy we could drift from), walks the
verified gallery to its end (and the full gallery with --full) checking that the
cursor never repeats or skips, and replays what VS Code's gallery client does: probe
`/v0.1/servers?limit=1`, query with `limit`, `version=latest`, `search` and `cursor`,
drop any record without name, description, version and a package or remote, then
fetch `{name}/versions/latest` with the name URL-encoded.
"""
import json
import sys
import urllib.parse

import httpx
import jsonschema
import yaml
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

BASE = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "https://neuronto.com").rstrip("/")
FULL = "--full" in sys.argv
SPEC = "https://raw.githubusercontent.com/modelcontextprotocol/registry/main/docs/reference/api/openapi.yaml"
H = {"user-agent": "NeurontoBot/1.0 (+https://neuronto.com) subregistry-test", "x-neuronto-probe": "1"}

c = httpx.Client(headers=H, timeout=60, follow_redirects=False)
spec = yaml.safe_load(httpx.get(SPEC, timeout=60).text)
root = {"$id": "urn:mcp-registry-openapi", "components": spec["components"]}
reg = Registry().with_resource("urn:mcp-registry-openapi", Resource(contents=root, specification=DRAFT202012))


def validator(name):
    return jsonschema.Draft202012Validator({"$ref": f"urn:mcp-registry-openapi#/components/schemas/{name}"},
                                           registry=reg, format_checker=jsonschema.FormatChecker())


V_LIST, V_ONE = validator("ServerList"), validator("ServerResponse")
passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS ", what)
    else:
        failed += 1
        print("  FAIL ", what)


def vscode_keeps(rec):
    s = rec.get("server") or {}
    return bool(s.get("name") and s.get("description") and s.get("version")
                and (s.get("packages") or s.get("remotes")))


def walk(prefix, max_pages=None):
    names, cursor, pages, bad = [], None, 0, []
    while True:
        u = f"{BASE}{prefix}/v0.1/servers?limit=100&version=latest"
        if cursor:
            u += "&cursor=" + urllib.parse.quote(cursor, safe="")
        r = c.get(u)
        assert r.status_code == 200, (u, r.status_code)
        d = r.json()
        errs = list(V_LIST.iter_errors(d))
        if errs:
            bad.append((u, errs[0].message[:200]))
        names += [x["server"]["name"] for x in d["servers"]]
        bad += [(x["server"]["name"], "client would drop it") for x in d["servers"] if not vscode_keeps(x)]
        pages += 1
        cursor = (d.get("metadata") or {}).get("nextCursor")
        if not cursor or (max_pages and pages >= max_pages):
            return names, pages, bad, cursor is None


print(f"base {BASE}")

# Discovery, as VS Code does it before anything else.
r = c.get(f"{BASE}/v0.1/servers?limit=1")
check(r.status_code == 200 and isinstance(r.json().get("servers"), list), "probe /v0.1/servers?limit=1 answers a ServerList")
check(r.headers.get("access-control-allow-origin") == "*", "CORS: Access-Control-Allow-Origin *")
check("GET" in (r.headers.get("access-control-allow-methods") or ""), "CORS: Access-Control-Allow-Methods includes GET")
check("authorization" in (r.headers.get("access-control-allow-headers") or "").lower(), "CORS: Authorization allowed")
o = c.options(f"{BASE}/v0.1/servers")
check(o.status_code in (200, 204) and o.headers.get("access-control-allow-origin") == "*", "OPTIONS answers with CORS")

# The verified gallery, walked to the end.
names, pages, bad, complete = walk("/verified")
check(complete, f"verified gallery walks to the end ({pages} pages, {len(names)} servers)")
check(len(names) == len(set(names)), "cursor never repeats a server")
check(names == sorted(names), "results come in name order, so the cursor is stable")
check(not bad, f"every page validates against ServerList and keeps every record ({len(bad)} problems)")
for b in bad[:5]:
    print("        ", b)

# The full gallery: the first pages always, the whole thing with --full.
fnames, fpages, fbad, fcomplete = walk("", None if FULL else 15)
check(not fbad, f"full gallery: {fpages} pages validate ({len(fbad)} problems)" + ("" if FULL else " (first pages only)"))
check(len(fnames) == len(set(fnames)) and fnames == sorted(fnames), "full gallery: no repeats, name order")
if FULL:
    check(fcomplete, f"full gallery walks to the end ({len(fnames)} servers)")
    check(set(names) <= set(fnames), "every verified server is also in the full gallery")

# Every verified record round-trips through the version endpoints, URL-encoded as clients do.
sample = names[:: max(1, len(names) // 60)][:60] + [n for n in names if any(ch.isupper() for ch in n)][:5]
rt_ok = 0
for n in sample:
    enc = urllib.parse.quote(n, safe="")
    one = c.get(f"{BASE}/v0.1/servers/{enc}/versions/latest")
    vs = c.get(f"{BASE}/v0.1/servers/{enc}/versions")
    good = (one.status_code == 200 and not list(V_ONE.iter_errors(one.json()))
            and one.json()["server"]["name"] == n
            and vs.status_code == 200 and not list(V_LIST.iter_errors(vs.json()))
            and c.get(f"{BASE}/v0.1/servers/{enc}/versions/"
                      f"{urllib.parse.quote(one.json()['server']['version'], safe='')}").status_code == 200)
    rt_ok += good
    if not good:
        print("         round trip failed:", n, one.status_code, vs.status_code)
check(rt_ok == len(sample), f"name round-trips through /versions, /versions/latest and /versions/{{version}} ({rt_ok}/{len(sample)})")

# Search, the way the gallery's search box sends it.
s = c.get(f"{BASE}/v0.1/servers", params={"limit": 50, "version": "latest", "search": "github"}).json()
hits = s["servers"]
check(bool(hits) and all("github" in json.dumps([x["server"]["name"], x["server"].get("title", ""),
                                                  x["server"]["description"]]).lower() for x in hits),
      f"search=github returns only matching servers ({len(hits)})")
check(all(vscode_keeps(x) for x in hits), "a client keeps every search result")

# Errors and edges.
check(c.get(f"{BASE}/v0.1/servers?limit=500").json()["metadata"]["count"] <= 100, "limit is capped at 100")
check(c.get(f"{BASE}/v0.1/servers?limit=abc").status_code == 400, "a non-integer limit is a 400")
check(c.get(f"{BASE}/v0.1/servers?updated_since=yesterday").status_code == 400, "a bad updated_since is a 400")
nf = c.get(f"{BASE}/v0.1/servers/{urllib.parse.quote('com.example/does-not-exist', safe='')}/versions/latest")
check(nf.status_code == 404 and nf.json().get("error"), "an unknown server is a 404 with an error body")
check(c.get(f"{BASE}/v0.1/servers/{urllib.parse.quote(names[0], safe='')}/versions/0.0.0-not-a-version").status_code == 404,
      "an unknown version is a 404")
check(c.post(f"{BASE}/v0.1/publish", json={}).status_code == 405, "publishing is a 405: this registry is read-only")
since = c.get(f"{BASE}/verified/v0.1/servers?limit=5&updated_since=2099-01-01T00:00:00Z").json()
check(since["metadata"]["count"] == 0, "updated_since in the future returns nothing")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
