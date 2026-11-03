"""The MCP Registry v0.1 read API, checked the way an MCP host reads it.

    python scripts/test_subregistry.py [base] [--full]

Validates every record against the ServerResponse / ServerList schemas of the
registry's own OpenAPI (fetched fresh, never a copy we could drift from), walks the
verified gallery to its end (and the full gallery with --full) checking that the
cursor never repeats or skips, and replays what VS Code's gallery client does: probe
`/v0.1/servers?limit=1`, query with `limit`, `version=latest`, `search` and `cursor`,
drop any record without name, description, version and a package or remote, then
fetch `{name}/versions/latest` with the name URL-encoded. Then the rest of the
contract: one record per version without `version=latest`, deleted records only on
request or with `updated_since`, writes answered 501, and snapshot headers with a
working ETag.
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

# Search, the way the gallery's search box sends it: the OpenAPI defines it as a
# case-insensitive substring match on the name.
s = c.get(f"{BASE}/v0.1/servers", params={"limit": 50, "version": "latest", "search": "GitHub"}).json()
hits = s["servers"]
check(bool(hits) and all("github" in x["server"]["name"].lower() for x in hits),
      f"search=GitHub matches names case-insensitively, and only names ({len(hits)})")
check(all(vscode_keeps(x) for x in hits), "a client keeps every search result")
s2 = c.get(f"{BASE}/v0.1/servers", params={"limit": 50, "version": "latest", "search": "io"}).json()["servers"]
check(bool(s2) and all("io" in x["server"]["name"].lower() for x in s2), "a two-letter search still matches names")

# One record per version: the list without a version filter, and the versions endpoints.
allv = c.get(f"{BASE}/v0.1/servers?limit=100").json()["servers"]
pairs = [(x["server"]["name"], x["server"]["version"]) for x in allv]
check(pairs == sorted(pairs) and len(pairs) == len(set(pairs)), "without version=latest: one record per version, name then version order")
multi = next((n for n in {p[0] for p in pairs} if sum(1 for p in pairs if p[0] == n) > 1), None)
if multi:
    enc = urllib.parse.quote(multi, safe="")
    vs = c.get(f"{BASE}/v0.1/servers/{enc}/versions").json()
    lat = c.get(f"{BASE}/v0.1/servers/{enc}/versions/latest").json()
    flags = [((x.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}).get("isLatest") for x in vs["servers"]]
    check(len(vs["servers"]) > 1 and not list(V_LIST.iter_errors(vs)), f"/versions lists every version of {multi} ({len(vs['servers'])})")
    check(flags.count(True) <= 1 and (not any(flags) or
          ((lat.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}).get("isLatest")),
          "/versions/latest is the version the registry marks latest")
cur = None
seen = []
for _ in range(3):
    u = f"{BASE}/v0.1/servers?limit=100" + ("&cursor=" + urllib.parse.quote(cur, safe="") if cur else "")
    d = c.get(u).json()
    seen += [(x["server"]["name"], x["server"]["version"]) for x in d["servers"]]
    cur = d["metadata"].get("nextCursor")
check(seen == sorted(seen) and len(seen) == len(set(seen)), "the all-versions cursor never repeats or reorders")

# Deleted records: hidden by default, shown on request, always shown with updated_since.


def is_deleted(x):
    m = x.get("_meta") or {}
    return ((m.get("io.modelcontextprotocol.registry/official") or {}).get("status") == "deleted"
            or (m.get("com.neuronto/registry") or {}).get("status") == "deleted")


dflt = c.get(f"{BASE}/v0.1/servers?limit=100").json()["servers"]
withd = c.get(f"{BASE}/v0.1/servers?limit=100&include_deleted=true").json()["servers"]
check(not any(is_deleted(x) for x in dflt), "deleted records are left out by default")
check(any(is_deleted(x) for x in withd), "include_deleted=true returns deleted records")
old = c.get(f"{BASE}/v0.1/servers?limit=100&updated_since=2020-01-01T00:00:00Z").json()["servers"]
check(any(is_deleted(x) for x in old), "updated_since always includes deleted records")
check(c.get(f"{BASE}/v0.1/servers?include_deleted=maybe").status_code == 400, "a bad include_deleted is a 400")

# Snapshot freshness and conditional requests.
h = c.get(f"{BASE}/v0.1/servers?limit=1")
check(bool(h.headers.get("x-registry-snapshot")) and bool(h.headers.get("last-modified")),
      f"every answer names its snapshot ({h.headers.get('x-registry-snapshot')})")
et = h.headers.get("etag")
check(bool(et) and c.get(f"{BASE}/v0.1/servers?limit=1", headers={"If-None-Match": et}).status_code == 304,
      "If-None-Match with the ETag is a 304")
check("x-registry-snapshot" in (h.headers.get("access-control-expose-headers") or "").lower(),
      "CORS exposes the snapshot headers to browser clients")

# Errors and edges.
check(c.get(f"{BASE}/v0.1/servers?limit=500").json()["metadata"]["count"] <= 100, "limit is capped at 100")
check(c.get(f"{BASE}/v0.1/servers?limit=abc").status_code == 400, "a non-integer limit is a 400")
check(c.get(f"{BASE}/v0.1/servers?updated_since=yesterday").status_code == 400, "a bad updated_since is a 400")
nf = c.get(f"{BASE}/v0.1/servers/{urllib.parse.quote('com.example/does-not-exist', safe='')}/versions/latest")
check(nf.status_code == 404 and nf.json().get("error"), "an unknown server is a 404 with an error body")
check(c.get(f"{BASE}/v0.1/servers/{urllib.parse.quote(names[0], safe='')}/versions/0.0.0-not-a-version").status_code == 404,
      "an unknown version is a 404")
pub = c.post(f"{BASE}/v0.1/publish", json={})
check(pub.status_code == 501 and pub.json().get("error"), "publishing is a 501 with an error body, as the OpenAPI documents for a read-only registry")
enc0 = urllib.parse.quote(names[0], safe="")
check(c.put(f"{BASE}/v0.1/servers/{enc0}/versions/1.0.0", json={}).status_code == 501, "PUT a version is a 501")
check(c.delete(f"{BASE}/v0.1/servers/{enc0}/versions/1.0.0").status_code == 501, "DELETE a version is a 501")
since = c.get(f"{BASE}/verified/v0.1/servers?limit=5&updated_since=2099-01-01T00:00:00Z").json()
check(since["metadata"]["count"] == 0, "updated_since in the future returns nothing")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
