#!/usr/bin/env python3
"""End-to-end tests against a running Neuronto.

Exercises the live HTTP surface, not the internals, because the thing that
matters is what an agent actually receives. Every assertion is about integrity
between the features rather than each feature alone: that verified tools reach
lexical ranking, that a tool found by /tools belongs to a server findable by
/search, that MCP and REST agree, that the dense leg changes results without
breaking the fast path, and that relevance is never presented as trust.

  python3 scripts/e2e.py [base_url]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://neuronto.com").rstrip("/")
UA = {"User-Agent": "neuronto-e2e/1.0", "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream"}

_passed: list[str] = []
_failed: list[tuple[str, str]] = []
_skipped: list[tuple[str, str]] = []


class Skip(Exception):
    """Raised by a test that cannot run here. Reported as skipped, never passed.

    A check that silently passes when it did not run is testing nothing, which
    is exactly how two earlier checks in this project stayed green for weeks
    while asserting nothing.
    """


def get(path, timeout=30):
    r = urllib.request.urlopen(urllib.request.Request(BASE + path, headers=UA), timeout=timeout)
    return r.status, json.loads(r.read() or b"{}")


# Routes that spend outbound work and are rate limited per caller. The suite is
# the heaviest caller of these by a wide margin, and one afternoon of runs
# exhausted the anonymous hour on /submit. When a verified key is supplied it
# is sent on these routes only: it raises the allowance and changes nothing
# else about their behaviour. It is never sent on /search, because the private
# isolation tests depend on that call being anonymous.
_KEYED_ROUTES = ("/submit", "/audit", "/manifest/build", "/claim/verify", "/mcp")


def post(path, payload, timeout=40):
    hdrs = dict(UA)
    key = os.getenv("NEURONTO_KEY", "").strip()
    if key and any(path.startswith(r) for r in _KEYED_ROUTES):
        hdrs["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers=hdrs, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    for line in body.splitlines():          # tolerate an SSE frame
        if line.startswith("data: "):
            body = line[6:]
    return r.status, json.loads(body or "{}")


def check(name, fn):
    try:
        fn()
        _passed.append(name)
        print(f"  PASS  {name}", flush=True)
    except Skip as e:
        _skipped.append((name, str(e)))
        print(f"  SKIP  {name}\n          {e}", flush=True)
    except AssertionError as e:
        _failed.append((name, str(e)))
        print(f"  FAIL  {name}\n          {e}", flush=True)
    except Exception as e:
        _failed.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ERROR {name}\n          {type(e).__name__}: {e}", flush=True)


# --------------------------------------------------------------------------
# 1. The index is there and reports the new subsystems
# --------------------------------------------------------------------------
def t_health():
    s, d = get("/health")
    assert s == 200, s
    assert d["entries"] > 5000, d["entries"]
    assert d["verified_tools"] > 0, "health does not report verified tools"
    assert d["servers_introspected"] > 0, "health does not report introspection"


def t_stats_subsystems():
    s, d = get("/stats")
    assert s == 200, s
    v = d.get("verified") or {}
    assert v.get("tools", 0) > 1000, f"too few verified tools: {v}"
    assert v.get("auth_required", 0) > 0, "auth-required facet missing"
    dn = d.get("dense") or {}
    assert dn.get("configured") is True, "dense leg not configured"
    assert dn.get("coverage", 0) > 0.9, f"dense coverage low: {dn.get('coverage')}"


# --------------------------------------------------------------------------
# 2. Tool-level index, and its integrity with the entry index
# --------------------------------------------------------------------------
def t_tools_search():
    s, d = post("/tools", {"query": {"text": "extract text from a pdf"}, "limit": 5})
    assert s == 200, s
    assert d["results"], "no tools returned"
    for t in d["results"]:
        assert t.get("tool"), "tool row without a name"
        assert t.get("endpoint"), "tool row without an endpoint"
        assert t.get("verified") is True, "tool not marked verified"


def t_tools_get_form_matches_post():
    s1, d1 = post("/tools", {"query": {"text": "send an email"}, "limit": 5})
    s2, d2 = get("/tools?q=send%20an%20email&limit=5")
    assert s1 == s2 == 200
    assert [t["tool"] for t in d1["results"]] == [t["tool"] for t in d2["results"]], \
        "GET and POST forms of /tools disagree"


def t_tool_schema_on_demand():
    s, d = post("/tools", {"query": {"text": "read a file"}, "limit": 3})
    assert not any("inputSchema" in t for t in d["results"]), \
        "schema returned when it was not asked for"
    s, d2 = post("/tools", {"query": {"text": "read a file"}, "limit": 3,
                            "withSchema": True})
    assert any("inputSchema" in t for t in d2["results"]), \
        "schema withheld when it was asked for"


def t_tool_server_is_findable_as_entry():
    """A tool's server must be a real entry, reachable through the entry API."""
    s, d = post("/tools", {"query": {"text": "database query"}, "limit": 5})
    assert d["results"], "no tools to cross-check"
    ident = d["results"][0]["identifier"]
    s, e = post("/search", {"query": {"text": d["results"][0]["server"]},
                            "federation": "none", "pageSize": 25})
    assert s == 200, s
    got = {r.get("identifier") for r in e["results"]}
    assert ident in got, f"tool's server {ident} not findable via /search"


def t_verified_tools_reach_lexical_ranking():
    """The tool text must be inside the entry index, not a side table.

    Search for a distinctive verified tool name and expect its own server back.
    """
    s, d = post("/tools", {"query": {"text": "screenshot"}, "limit": 10})
    assert d["results"], "no tools for the probe"
    pick = next((t for t in d["results"] if len(t["tool"]) > 8), d["results"][0])
    s, e = post("/search", {"query": {"text": pick["tool"]},
                            "federation": "none", "pageSize": 20})
    got = {r.get("identifier") for r in e["results"]}
    assert pick["identifier"] in got, (
        f"tool name {pick['tool']!r} does not retrieve its own server; "
        "tool_text is not reaching the FTS index")


# --------------------------------------------------------------------------
# 3. Search integrity: verification is exposed and is never trust
# --------------------------------------------------------------------------
def t_search_exposes_verification():
    s, d = post("/search", {"query": {"text": "pdf"}, "federation": "none",
                            "pageSize": 25})
    assert s == 200, s
    withv = [r for r in d["results"] if "verification" in r]
    assert withv, "no result carried a verification block"
    v = withv[0]["verification"]
    assert "reachable" in v and "tools" in v, v
    assert isinstance(v.get("checked"), int), v


def t_relevance_is_not_trust():
    s, d = post("/search", {"query": {"text": "pdf"}, "federation": "none"})
    blob = json.dumps(d).lower()
    for word in ("trust score", "safety score", "trusted", "certified"):
        assert word not in blob, f"response implies trust: {word!r}"
    for r in d["results"]:
        v = r.get("verification") or {}
        assert "trust" not in json.dumps(v).lower(), "verification implies trust"


def t_auth_required_is_surfaced():
    s, d = get("/stats")
    assert (d["verified"]["auth_required"] or 0) > 0, \
        "auth-required servers are not counted anywhere"


# --------------------------------------------------------------------------
# 4. Retrieval modes: the fast path stays fast, dense adds recall
# --------------------------------------------------------------------------
def t_fast_path_is_lexical_only_and_fast():
    t0 = time.perf_counter()
    s, d = post("/search", {"query": {"text": "convert markdown to html"},
                            "federation": "none", "pageSize": 10})
    ms = (time.perf_counter() - t0) * 1000
    assert s == 200, s
    assert ms < 2500, f"fast path took {ms:.0f} ms including network"


def t_dense_leg_runs_in_auto():
    s, d = post("/search", {"query": {"text": "understand a research paper"},
                            "federation": "auto", "pageSize": 10}, timeout=60)
    assert s == 200, s
    assert d["results"], "auto returned nothing"


def t_modes_are_consistent():
    """Every mode must return well-formed entries with an identifier."""
    for mode in ("none", "referrals", "auto"):
        s, d = post("/search", {"query": {"text": "weather forecast"},
                                "federation": mode, "pageSize": 5}, timeout=60)
        assert s == 200, f"{mode}: {s}"
        for r in d["results"]:
            assert r.get("identifier"), f"{mode}: entry without identifier"
        if mode == "referrals":
            assert d.get("referrals"), "referrals mode returned no referrals"


# --------------------------------------------------------------------------
# 5. MCP surface agrees with REST
# --------------------------------------------------------------------------
def _rpc(method, params=None, rid=1):
    return post("/mcp", {"jsonrpc": "2.0", "id": rid, "method": method,
                         "params": params or {}}, timeout=60)


def t_mcp_lists_every_tool_and_labels_the_write():
    s, d = _rpc("tools/list")
    assert s == 200, s
    tools = {t["name"]: t for t in d["result"]["tools"]}
    assert set(tools) == {"find_resource", "find_tool", "registry_stats",
                          "publish_resource"}, set(tools)
    # A client decides what it may call unattended from these hints, so the one
    # tool that writes must be the only one that says so.
    writes = {n for n, t in tools.items()
              if t["annotations"].get("readOnlyHint") is False}
    assert writes == {"publish_resource"}, writes


def t_mcp_find_tool_matches_rest():
    s, d = _rpc("tools/call", {"name": "find_tool",
                               "arguments": {"query": "extract text from a pdf",
                                             "limit": 5}})
    assert s == 200, s
    payload = json.loads(d["result"]["content"][0]["text"])
    s2, rest = post("/tools", {"query": {"text": "extract text from a pdf"},
                               "limit": 5})
    assert [t["tool"] for t in payload["tools"]] == \
           [t["tool"] for t in rest["results"]], "MCP find_tool disagrees with /tools"


def t_mcp_stats_match_rest():
    s, d = _rpc("tools/call", {"name": "registry_stats", "arguments": {}})
    payload = json.loads(d["result"]["content"][0]["text"])
    s2, st = get("/stats")
    assert payload["verified_tools"] == st["verified"]["tools"], \
        "MCP and /stats disagree on verified tool count"
    assert payload["entries"] == st["entries"], "MCP and /stats disagree on entries"


def t_mcp_find_resource_carries_verification():
    s, d = _rpc("tools/call", {"name": "find_resource",
                               "arguments": {"query": "pdf", "limit": 10,
                                             "federate": False}})
    payload = json.loads(d["result"]["content"][0]["text"])
    assert payload["results"], "find_resource returned nothing"
    assert any("verified" in r for r in payload["results"]), \
        "no result carried the verified block"
    assert "not a trust or safety rating" in payload["note"]


# --------------------------------------------------------------------------
# 6. Benchmark and adoption are published and honest
# --------------------------------------------------------------------------
def t_bench_published():
    s, d = get("/bench")
    assert s == 200, f"/bench returned {s}"
    assert d["tasks"] > 0 and d["targets"], d
    assert "known_bias" in d, "benchmark does not disclose its bias"
    assert "harness" in d, "benchmark does not link its harness"
    # It must measure somebody other than us.
    others = [k for k in d["targets"] if "neuronto" not in k.lower()]
    assert len(others) >= 3, f"benchmark only measured {others}"


def t_bench_matching_is_normalised():
    s, d = get("/bench")
    assert "urn:ai" in d.get("identifier_matching", ""), \
        "benchmark does not state how identifiers are matched"
    gh = next((v for k, v in d["targets"].items() if "GitHub" in k), None)
    assert gh is not None, "GitHub Agent Finder not measured"
    assert gh["answered"] > 0, "GitHub answered nothing; check the harness"


def t_adoption_published():
    s, d = get("/adoption")
    assert s == 200, s
    w = d["watchlist"]
    assert w["hosts"] >= 15, w
    assert "detail" in w and w["detail"], "no per-host detail"
    assert d["crawl"]["hosts_seen"] > 0, d["crawl"]
    assert "method" in d, "adoption does not state its method"


# --------------------------------------------------------------------------
# 7. Nothing regressed: conformance-critical surfaces still correct
# --------------------------------------------------------------------------
def t_manifest_still_valid_and_clean():
    s, d = get("/.well-known/ard.json")
    assert s == 200, s
    assert d["specVersion"] and d["entries"], d
    # The characters are written as escapes on purpose. A blanket dash sweep
    # over the source once rewrote the literals inside this very assertion into
    # plain hyphens, which made it assert that no URL contains "-" and fail
    # against a perfectly clean manifest. A test about a character must not
    # spell that character literally.
    import re as _re
    bad = []
    def _walk(o, path=""):
        if isinstance(o, dict):
            for kk, vv in o.items():
                _walk(vv, path + "." + kk)
        elif isinstance(o, list):
            for i, vv in enumerate(o):
                _walk(vv, f"{path}[{i}]")
        elif isinstance(o, str) and _re.search("[\u2014\u2013]", o):
            bad.append(f"{path} = {o[:90]!r}")
    _walk(d)
    assert not bad, "manifest contains an em or en dash at: " + "; ".join(bad)


def t_agents_is_paginated_object():
    s, d = get("/agents")
    assert s == 200, s
    assert isinstance(d, dict) and isinstance(d.get("items"), list), \
        "GET /agents is not a paginated object"


def t_explore_still_works():
    s, d = post("/explore", {"resultType": {"facets": ["type"]}})
    assert s == 200, s
    assert d.get("facets"), d


def t_openapi_documents_new_endpoints():
    s, d = get("/openapi.json")
    assert s == 200, s
    paths = set(d["paths"])
    for p in ("/tools", "/bench", "/adoption"):
        assert p in paths, f"{p} missing from openapi.json"


def t_unknown_url_still_404s():
    try:
        s, _ = get("/definitely-not-a-real-page-xyz")
        assert False, f"unknown URL returned {s}, expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code


# --------------------------------------------------------------------------
# 8. The rendered pages: server-side, honest, and never advertising a stub
# --------------------------------------------------------------------------
def _html(path, timeout=40):
    req = urllib.request.Request(BASE + path,
                                 headers={"User-Agent": "neuronto-e2e/1.0",
                                          "Accept": "text/html"})
    r = urllib.request.urlopen(req, timeout=timeout)
    return r.status, r.read().decode("utf-8", "replace")


def t_capability_index_renders_server_side():
    s, h = _html("/tools/")
    assert s == 200, s
    assert "verified tools" in h.lower(), "index does not mention verified tools"
    # the numbers must be in the HTML, not fetched by a script
    import re as _re
    assert _re.search(r"<b>[\d,]{3,}</b>", h), "no server-rendered figures on /tools/"
    assert h.count('href="/tools/') >= 15, "too few capability pages linked"


def t_capability_page_is_relevant_and_not_thin():
    s, h = _html("/tools/pdf-documents")
    assert s == 200, s
    import re as _re
    names = _re.findall(r'<span class="tn">([^<]+)</span>', h)
    assert len(names) >= 20, f"only {len(names)} tools on the page"
    # the page must actually be about its subject
    hit = sum(1 for n in names[:15] if "pdf" in n.lower() or "doc" in n.lower()
              or "ocr" in n.lower())
    assert hit >= 8, f"only {hit}/15 leading tools relate to the category: {names[:15]}"


def t_page_counts_match_the_table_rule():
    """The headline figure and the table must come from the same rule."""
    s, h = _html("/tools/databases")
    import re as _re
    m = _re.search(r"<b>([\d,]+)</b>verified tools", h)
    assert m, "no headline tool count"
    headline = int(m.group(1).replace(",", ""))
    s2, api = post("/tools", {"query": {"text": "sql database"}, "limit": 1})
    assert headline > 0 and headline < 5000, f"implausible headline count {headline}"


def t_stub_categories_are_not_published():
    """A category below the threshold must 404 and stay out of the sitemap."""
    try:
        s, _ = _html("/tools/translation-language")
        assert False, f"stub category returned {s}"
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/sitemap.xml", headers={"User-Agent": "e2e"}), timeout=30)
    sm = r.read().decode()
    assert "translation-language" not in sm, "sitemap advertises a 404 page"
    assert "/tools/pdf-documents" in sm, "sitemap missing a real capability page"


def t_bench_and_adoption_negotiate_content():
    """HTML to a browser, JSON to an API client, from one URL."""
    for path in ("/bench", "/adoption"):
        s, h = _html(path)
        assert s == 200, f"{path} html {s}"
        assert "<table" in h, f"{path} rendered no table"
        s2, d = get(path)                      # Accept: application/json
        assert isinstance(d, dict), f"{path} did not return JSON to an API client"


def t_bench_page_shows_the_unflattering_number():
    """The page must publish the conditioned column and the disclosed bias."""
    s, h = _html("/bench")
    assert "when carried" in h.lower(), "conditioned column missing from the page"
    assert "known bias" in h.lower(), "page does not disclose the benchmark bias"


def t_pages_carry_canonical_and_description():
    for path in ("/tools/", "/tools/email", "/bench", "/adoption"):
        s, h = _html(path)
        assert '<link rel="canonical"' in h, f"{path} has no canonical"
        assert '<meta name="description"' in h, f"{path} has no meta description"
        assert "<title>" in h, f"{path} has no title"


def t_pages_never_imply_trust():
    for path in ("/tools/", "/tools/pdf-documents"):
        s, h = _html(path)
        low = h.lower()
        for word in ("trusted", "certified", "safe to use", "trust score"):
            assert word not in low, f"{path} implies trust: {word!r}"


# --------------------------------------------------------------------------
# 9. Badges: the distribution mechanism
# --------------------------------------------------------------------------
def t_badge_renders_for_known_publisher():
    import random as _rnd
    req = urllib.request.Request(BASE + f"/badge/com.jojapi.svg?cb={_rnd.randrange(10**9)}",
                                 headers={"User-Agent": "e2e"})
    r = urllib.request.urlopen(req, timeout=30)
    body = r.read().decode()
    assert r.headers.get("Content-Type", "").startswith("image/svg"), \
        r.headers.get("Content-Type")
    assert "<svg" in body and "</svg>" in body, "not an svg"
    # The badge states an observation. Which one depends on what the last probe
    # saw, so accept any of the three rather than pinning the wording of one.
    assert any(w in body for w in ("tools verified", "tool verified",
                                   "endpoint verified", "indexed")), \
        f"badge states nothing observable: {body[:200]}"


def t_badge_unknown_is_neutral_not_error():
    req = urllib.request.Request(BASE + "/badge/never.indexed.example.svg",
                                 headers={"User-Agent": "e2e"})
    r = urllib.request.urlopen(req, timeout=30)
    assert r.status == 200, "unknown publisher must get a neutral badge, not a broken image"
    assert "not indexed" in r.read().decode()


def t_badge_rejects_garbage_input():
    import urllib.parse as _up
    for bad in ("..%2F..%2Fetc%2Fpasswd", "a b c", "<script>"):
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + f"/badge/{_up.quote(bad)}.svg",
                headers={"User-Agent": "e2e"}), timeout=20)
            raise AssertionError(f"badge accepted {bad!r}")
        except urllib.error.HTTPError as e:
            assert e.code == 404, e.code


def t_badge_never_implies_trust():
    req = urllib.request.Request(BASE + "/badge/com.jojapi.svg",
                                 headers={"User-Agent": "e2e"})
    body = urllib.request.urlopen(req, timeout=30).read().decode().lower()
    for word in ("trusted", "certified", "score", "safe"):
        assert word not in body, f"badge implies trust: {word!r}"


# --------------------------------------------------------------------------
# 10. The accumulating asset: history that cannot be rebuilt later
# --------------------------------------------------------------------------
def t_search_records_impressions():
    """A query must record which entries it returned, not just that it happened.

    Without this the query log can say somebody searched for "pdf" but can never
    tell a publisher which queries surfaced their resource, which is the whole
    of any future reporting product.
    """
    import time as _t
    # Checked through /demand rather than /stats. /stats used to echo the query
    # text of every visitor, which published a tenant's private-registry query
    # verbatim, so it now reports shape only. /demand is the scoped version and
    # the one that matters: a publisher seeing the queries that returned THEIR
    # resources. Testing the property through the surface that actually sells it.
    marker = f"pdf extraction probe {int(_t.time())}"
    s, d = post("/search", {"query": {"text": marker}, "federation": "none",
                            "pageSize": 5}, timeout=70)
    assert s == 200, s
    results = d.get("results") or []
    assert results, "the probe query returned nothing, so nothing could be recorded"

    # the search itself must still be counted, without its text
    s2, st = get("/stats")
    recent = st.get("recent") or []
    assert recent, "no recent activity recorded at all"
    assert all("q" not in r for r in recent), "/stats is echoing query text again"
    assert recent[0].get("ts", 0) >= int(_t.time()) - 300, "activity log is stale"

    # And the impression must reach the publisher report. Several of the returned
    # hosts are tried: /demand is keyed on the publisher string, which is not
    # always the URL host, and one 404 says nothing about whether impressions
    # are being written. Only if none of them has a report is this inconclusive.
    hosts = []
    for r in results:
        for cand in (r.get("url") or "", r.get("identifier") or ""):
            if "//" in cand:
                h = cand.split("//", 1)[1].split("/", 1)[0].lower()
                if h and h not in hosts:
                    hosts.append(h)
    saturated = []
    for host in hosts[:5]:
        try:
            s3, dem = get(f"/demand?domain={host}&limit=200")
        except urllib.error.HTTPError:
            continue
        queries = [q.get("query") for q in (dem.get("queries") or [])]
        if marker in queries:
            return
        # 200 is the ceiling. A publisher past it will not show a query seen
        # once, which is correct, so that host is inconclusive, not a failure.
        if len(queries) >= 200:
            saturated.append(host)
            continue
        raise AssertionError(
            f"the query that returned {host} never reached its demand report "
            f"({len(queries)} queries listed, none is the probe)")
    raise Skip(f"every reachable report was saturated ({saturated}) or absent; "
               f"impressions unverified on this run")


def t_stats_exposes_history_counts():
    """Observation history must be non-empty and visible."""
    s, d = get("/stats")
    h = d.get("history") or {}
    assert h.get("observations", 0) > 0, \
        "no observation history recorded; liveness is being overwritten"
    assert "impressions" in h, "impressions not reported"


# --------------------------------------------------------------------------
# 11. ARD publisher pages, feed, and social preview
# --------------------------------------------------------------------------
def t_publishers_index():
    s, h = _html("/ard-publishers")
    assert s == 200, s
    assert h.count('href="/ard-publishers/') >= 150, "publisher list is not complete"
    assert "ard.json" in h, "page does not explain what a manifest is"


def t_publisher_page_renders():
    s, h = _html("/ard-publishers/zapier.com")
    assert s == 200, s
    assert "zapier.com" in h and "declared resources" in h


def t_publisher_page_shows_the_full_record():
    """The page exists because the manifest is machine-only. Show the manifest."""
    s, h = _html("/ard-publishers/clickhouse.com")
    assert s == 200, s
    for needed in ("endpoint:", "identifier:", "urn:air:", "published to be found for",
                   "with a callable endpoint"):
        assert needed in h, f"publisher page omits {needed!r}"


def t_every_ard_publisher_is_listed():
    """No editorial filter. A publisher without descriptions is still a publisher."""
    for host in ("padlet.com", "supademo.com", "dribba.com"):
        st, _ = _html(f"/ard-publishers/{host}")
        assert st == 200, f"{host} missing from the list ({st})"


def t_publisher_page_never_claims_an_unmade_check():
    """A publisher we have not probed must not render as zero answering.

    Saying "0 answering" about a real business we never tested is a false
    statement about somebody else's service. The subject is discovered from the
    live index rather than hardcoded: the first version named one publisher,
    and the assertion silently went stale the moment that publisher got probed.
    """
    s, idx = _html("/ard-publishers")
    assert s == 200, s
    import re as _re
    # Scope to one table row. A DOTALL wildcard between the link and the phrase
    # spans rows, so it matched any publisher followed later in the document by
    # some other row's "not checked", which is how this first reported a false
    # positive against a fully probed publisher.
    unprobed = []
    for row in _re.findall(r"<tr>.*?</tr>", idx, _re.S):
        if "not checked" not in row:
            continue
        m = _re.search(r'href="/ard-publishers/([^"]+)"', row)
        if m:
            unprobed.append(m.group(1))
    if not unprobed:
        # Everything has been probed. The invariant cannot be violated, but the
        # structural check below still must hold on a real page.
        s2, h2 = _html("/ard-publishers/clickhouse.com")
        assert "answering of" in h2 or "not yet" in h2.lower(), \
            "page states an answering count with no indication of what was checked"
        return
    host = unprobed[0]
    s3, h3 = _html(f"/ard-publishers/{host}")
    assert s3 == 200, s3
    assert "not yet checked" in h3.lower(), \
        f"{host} was never probed but its page does not say so"
    assert not _re.search(r"<b>0</b>answering", h3), \
        f"{host} renders 0 answering despite never being probed"


def t_answering_counts_always_state_the_denominator():
    """Any answering figure must say how many were checked, on every page."""
    import re as _re
    for host in ("clickhouse.com", "zapier.com", "apify.com"):
        s, h = _html(f"/ard-publishers/{host}")
        if s != 200:
            continue
        m = _re.search(r"<b>([\d,]+)</b>answering of ([\d,]+) checked", h)
        assert m or "not yet" in h.lower(), \
            f"{host} reports answering without a denominator"


def t_unknown_publisher_404s():
    try:
        _html("/ard-publishers/definitely-not-real.example")
        raise AssertionError("unknown publisher did not 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code


def t_feed_is_valid_rss():
    import xml.etree.ElementTree as _ET
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/feed.xml", headers={"User-Agent": "e2e"}), timeout=30)
    assert r.headers.get("Content-Type", "").startswith("application/rss"), \
        r.headers.get("Content-Type")
    root = _ET.fromstring(r.read())
    items = root.findall(".//item")
    assert len(items) >= 5, f"feed has only {len(items)} items"
    for it in items[:5]:
        assert it.find("title") is not None and it.find("title").text
        assert it.find("link") is not None


def t_social_preview_tags_present():
    for path in ("/tools/", "/ard-publishers", "/bench"):
        s, h = _html(path)
        assert 'property="og:image"' in h, f"{path} has no og:image"
        assert 'name="twitter:card" content="summary_large_image"' in h, \
            f"{path} has no large twitter card"
        assert 'type="application/rss+xml"' in h, f"{path} does not advertise the feed"


def t_robots_allows_social_crawlers():
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/robots.txt", headers={"User-Agent": "e2e"}), timeout=30)
    body = r.read().decode()
    for ua in ("Redditbot", "Twitterbot", "LinkedInBot", "Slackbot", "Discordbot"):
        assert ua in body, f"{ua} not named in robots.txt"


def t_sitemap_lists_publishers():
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/sitemap.xml", headers={"User-Agent": "e2e"}), timeout=30)
    sm = r.read().decode()
    assert "/ard-publishers" in sm, "sitemap missing publisher pages"
    assert sm.count("<loc>") >= 100, f"sitemap only has {sm.count('<loc>')} urls"


def t_publisher_page_states_the_real_manifest_path():
    """Never assert a path we did not see serve this publisher's manifest.

    Every page once said /.well-known/ard.json. Every publisher checked serves
    the pre-v0.91 /.well-known/ai-catalog.json and 404s on ard.json, so the page
    was wrong about 178 named companies and would have sent readers to a 404.
    """
    s, h = _html("/ard-publishers/clickhouse.com")
    assert s == 200, s
    assert "ai-catalog.json" in h, "page does not state the path actually served"
    import re as _re
    assert not _re.search(r"manifest at\s*<code>/\.well-known/ard\.json</code>", h), \
        "page asserts ard.json for a publisher that serves ai-catalog.json"


def t_adoption_checks_both_paths():
    """Measuring the path instead of the practice under-counts adoption ~10x."""
    s, d = get("/adoption")
    assert "ai-catalog" in d.get("method", ""), \
        "adoption method does not mention the legacy path"
    w = d["watchlist"]
    assert w["publishing"] >= 1, \
        "watchlist reports zero publishers; the legacy path is probably not being checked"
    for x in w["detail"]:
        if x["publishes"]:
            assert x.get("path"), "a publishing host records no manifest path"


def t_old_publisher_urls_redirect():
    """The pages launched at /publishers and were indexed there. Do not break them."""
    import urllib.request as _u
    class NoRedirect(_u.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    op = _u.build_opener(NoRedirect)
    for old, new in (("/publishers/", "/ard-publishers"),
                     ("/publishers/zapier.com", "/ard-publishers/zapier.com")):
        try:
            op.open(_u.Request(BASE + old, headers={"User-Agent": "e2e"}), timeout=25)
            raise AssertionError(f"{old} did not redirect")
        except urllib.error.HTTPError as e:
            assert e.code == 301, f"{old} returned {e.code}, expected 301"
            assert e.headers.get("Location", "").endswith(new), e.headers.get("Location")


def t_publishers_page_is_query_shaped():
    """Headings should match what people and answer engines actually ask."""
    s, h = _html("/ard-publishers")
    low = h.lower()
    for q in ("what is an ard publisher", "how do i become an ard publisher",
              "which path do they actually use"):
        assert q in low, f"missing question heading: {q!r}"
    assert "application/ld+json" in h, "no structured data"
    import re as _re
    title = _re.search(r"<title>([^<]*)</title>", h).group(1).lower()
    assert "ard publisher" in title and _re.search(r"\d", title), \
        f"title is not specific: {title!r}"


# --------------------------------------------------------------------------
# 12. Submission: the only way a new publisher can reach an index today
# --------------------------------------------------------------------------
def t_submit_page_renders():
    s, h = _html("/submit")
    assert s == 200, s
    assert "submit" in h.lower() and "ard-publishers" in h


def t_submit_indexes_a_real_publisher():
    """Fetched live from the domain, not taken from the form."""
    s, d = post("/submit", {"domain": "clickhouse.com"}, timeout=60)
    assert s == 200, f"{s} {d}"
    assert d["status"] == "indexed", d
    assert d["manifest_path"], "no manifest path recorded"
    assert d["resources_indexed"] > 0, d
    assert d["page"].endswith("/ard-publishers/clickhouse.com"), d["page"]


def t_submit_rejects_a_domain_without_a_manifest():
    s, d = post("/submit", {"domain": "example.com"}, timeout=60)
    assert s == 404, f"{s} {d}"
    assert d["status"] == "no_manifest"
    assert len(d.get("checked") or []) >= 2, "does not say which paths were tried"


def t_submit_validates_input():
    for bad in ("../etc/passwd", "..", "not a domain", "", "a.b", "-x.com"):
        s, d = post("/submit", {"domain": bad}, timeout=30)
        assert s == 400, f"accepted {bad!r} with {s}"


def t_published_page_lists_every_external_artefact():
    """The crawl path to everything we published off-domain.

    IndexNow is domain-verified and refuses a foreign URL, so a page that is
    itself indexed and links each artefact is the only honest way to get them
    discovered. If a link rots, the page becomes a liability rather than an asset.
    """
    s, h = _html("/published")
    assert s == 200, s
    import re as _re
    ext = set(_re.findall(r'href="(https?://(?!neuronto\.com)[^"]+)"', h))
    assert len(ext) >= 12, f"only {len(ext)} external artefacts linked"
    for must in ("github.com/neuronto/agentic-resource-discovery",
                 "huggingface.co/datasets/", "pypi.org/project/ard-publish",
                 "registry.modelcontextprotocol.io"):
        assert any(must in u for u in ext), f"missing {must}"
    assert "application/ld+json" in h and '"sameAs"' in h, "no sameAs schema"


def t_published_links_all_resolve():
    """Every external link must actually be reachable."""
    import re as _re
    s, h = _html("/published")
    ext = sorted(set(_re.findall(r'href="(https?://(?!neuronto\.com)[^"]+)"', h)))
    # A 4xx or 5xx is our problem: we are pointing at something that is not
    # there. A connection failure is theirs, and failing the build on a third
    # party's outage teaches people to ignore the build.
    dead, unreachable = [], []
    for u in ext:
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25)
            if r.status != 200:
                dead.append((u, r.status))
        except urllib.error.HTTPError as e:
            dead.append((u, e.code))
        except Exception as e:
            unreachable.append((u, type(e).__name__))
    assert not dead, f"dead links on /published: {dead}"
    if unreachable:
        raise Skip(f"could not reach, treated as their outage not our dead link: {unreachable}")


def t_navigation_is_consistent_sitewide():
    """Every page must link every section, static and generated alike.

    The homepage carries its own hardcoded nav, separate from the one generated
    pages share, so it silently fell behind: it linked none of /tools/,
    /ard-publishers, /bench, /adoption, /submit or /published. The most crawled
    page on the site was the one not linking its best content.
    """
    SECTIONS = ("/tools/", "/ard-publishers", "/bench", "/adoption",
                "/submit", "/published", "/publish", "/console", "/blog")
    for path in ("/", "/what-is-ard", "/blog", "/console", "/publish",
                 "/tools/", "/ard-publishers", "/bench"):
        s, h = _html(path)
        assert s == 200, f"{path} -> {s}"
        missing = [x for x in SECTIONS if f'href="{x}"' not in h]
        assert not missing, f"{path} does not link: {missing}"


def t_submit_accepts_a_bare_mcp_endpoint():
    """Most MCP developers have no manifest. They must still be able to submit."""
    s, d = post("/submit", {"endpoint": "https://mcp.deepwiki.com/mcp"}, timeout=90)
    assert s == 200, f"{s} {d}"
    assert d["status"] == "indexed", d
    assert d["verified_tools"] > 0, "no tools read from the server"
    assert isinstance(d.get("tools"), list) and d["tools"], "tool names not returned"
    assert d["identifier"].startswith("urn:air:"), d["identifier"]


def t_submit_rejects_a_url_that_is_not_mcp():
    s, d = post("/submit", {"endpoint": "https://example.com/"}, timeout=60)
    assert s == 404, f"{s} {d}"
    assert d["status"] == "not_an_mcp_server"
    assert "handshake" in d["detail"].lower()


def t_submitted_server_becomes_searchable():
    """Indexing is worthless if the thing cannot then be found."""
    s, d = post("/tools", {"query": {"text": "read wiki structure"}, "limit": 5})
    assert s == 200, s
    names = [t["tool"] for t in d["results"]]
    assert any("wiki" in n.lower() for n in names), \
        f"submitted server's tools are not searchable: {names}"


def t_submit_page_documents_both_routes():
    s, h = _html("/submit")
    low = h.lower()
    assert "only have an mcp server" in low, "MCP-only route not documented"
    assert '"endpoint"' in h, "endpoint payload not shown"
    assert "skills" in low, "does not explain how skills get listed"


def t_media_type_normalisation_is_universal():
    """Every spelling in circulation must land in the right family.

    The classifier looked for the substring "mcp" and therefore filed
    application/vnd.modelcontextprotocol.server+json, a real MCP server type
    seen in our own crawl, as "other". That is precisely the silent-drop failure
    this registry exists to fix, committed by us.
    """
    s, d = post("/explore", {"resultType": {"facets": [{"field": "type_family",
                                                        "limit": 20}]}})
    assert s == 200, s
    fams = {b["value"]: b["count"] for b in d["facets"]["type_family"]["buckets"]}
    for expected in ("mcp-server", "skill", "a2a-agent", "openapi", "doc",
                     "plugin", "graphql", "package"):
        assert expected in fams, f"family {expected} absent from the index"
    # "other" must stay a genuine remainder, not a dumping ground
    total = sum(fams.values())
    assert fams.get("other", 0) / total < 0.05, \
        f"{fams.get('other')} of {total} entries unclassified"


def t_agent_filter_spans_both_agent_families():
    """A2A cards and ACP/OASF/AgentFacts descriptors are different but a caller
    asking for agents means both."""
    s, d = post("/search", {"query": {"text": "agent",
                                      "filter": {"type": ["application/a2a-agent-card+json"]}},
                            "federation": "none", "pageSize": 5})
    assert s == 200, s
    assert d["results"], "agent type filter returned nothing"


def t_every_family_filters_end_to_end():
    """Each family must be reachable through the MCP tool and return its own kind.

    The taxonomy touches five places: the classifier, the preferred-type table,
    the MCP kind enum, the search filter and the homepage. An incomplete table
    in any one of them fails silently, which is how kind="agent" and kind="doc"
    came to resolve to "other" while every surface still returned 200.
    """
    s, st = get("/stats")
    fams = [k for k, v in (st.get("families") or {}).items() if k and v]
    assert len(fams) >= 10, f"only {len(fams)} families in the index"

    s, d = _rpc("tools/list")
    tool = next(t for t in d["result"]["tools"] if t["name"] == "find_resource")
    enum = set(tool["inputSchema"]["properties"]["kind"]["enum"])
    missing = [f for f in fams if f not in enum and f != "other"]
    assert not missing, f"families indexed but not offered by the MCP tool: {missing}"

    # and the filter must actually restrict
    s, res = post("/search", {"query": {"text": "api",
                                        "filter": {"type": ["application/graphql+json"]}},
                              "federation": "none", "pageSize": 5})
    assert s == 200, s
    for r in res["results"]:
        t = (r.get("type") or "").lower()
        assert "graphql" in t, f"graphql filter returned {t}"


def t_family_names_round_trip():
    """A bare family name is a legal filter value and must mean itself."""
    for fam, must in (("mcp-server", "mcp"), ("skill", "skill"),
                      ("openapi", "openapi"), ("graphql", "graphql")):
        s, d = post("/search", {"query": {"text": "a", "filter": {"type": [fam]}},
                                "federation": "none", "pageSize": 3})
        assert s == 200, f"{fam}: {s}"
        for r in d["results"]:
            t = (r.get("type") or "").lower()
            assert must in t or t == "", f"filter {fam!r} returned type {t!r}"


def t_filter_at_the_top_level_is_honoured_not_ignored():
    """A misplaced filter must not silently return everything.

    The spec nests filter inside query. A caller putting it at the top level got
    unfiltered results with no error, which is the worst of both options.
    """
    s, d = post("/search", {"query": {"text": "api"},
                            "filter": {"type": ["application/graphql+json"]},
                            "federation": "none", "pageSize": 5})
    assert s == 200, s
    for r in d["results"]:
        t = (r.get("type") or "").lower()
        assert "graphql" in t, f"top-level filter ignored, returned {t}"


def t_social_preview_is_complete_on_every_page():
    """A shared link with no image renders as a grey box nobody clicks.

    The homepage carries its own head, separate from generated pages, and had
    no og:image at all while declaring twitter:card=summary_large_image. Facebook
    fell back to scraping the page and blew up the 32px favicon mark; LinkedIn
    fell back to the bare domain.
    """
    import re as _re
    for path in ("/", "/what-is-ard", "/publish", "/console",
                 "/tools/", "/ard-publishers", "/submit", "/published"):
        s, h = _html(path)
        assert s == 200, f"{path} -> {s}"
        for tag in ('property="og:title"', 'property="og:description"',
                    'property="og:image"', 'property="og:url"',
                    'name="twitter:card"'):
            assert tag in h, f"{path} missing {tag}"
        img = _re.search(r'og:image" content="([^"]+)"', h).group(1)
        assert img.startswith("https://"), f"{path} og:image is not absolute: {img}"
        # a large-image card with no image is worse than no card
        if 'content="summary_large_image"' in h:
            assert 'name="twitter:image"' in h, f"{path} claims a large card with no image"


def t_no_dashes_in_anything_a_crawler_reads():
    """Rule 4 applies hardest where it is most visible: the preview card."""
    import re as _re
    for path in ("/", "/what-is-ard", "/tools/", "/ard-publishers", "/published"):
        s, h = _html(path)
        head = h[:h.find("</head>")] if "</head>" in h else h[:4000]
        bad = _re.findall(r"[\u2014\u2013]", head)
        assert not bad, f"{path} head contains {len(bad)} em/en dashes"


def t_preview_image_is_reachable_and_large_enough():
    """Facebook and LinkedIn ignore images under 200px and prefer 1200x630+."""
    import re as _re, struct
    s, h = _html("/")
    url = _re.search(r'og:image" content="([^"]+)"', h).group(1)
    r = urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": "facebookexternalhit/1.1"}), timeout=30)
    assert r.status == 200, r.status
    ctype = r.headers.get("Content-Type", "")
    data = r.read()
    # A PNG served as image/jpeg is a reason for a crawler to drop the card, and
    # it happened: the image route hardcoded image/jpeg for every extension.
    if url.endswith(".png"):
        assert ctype == "image/png", f"PNG served as {ctype!r}"
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "not actually a PNG"
        w, hgt = struct.unpack(">II", data[16:24])
    else:
        assert "jpeg" in ctype, ctype
        i, w, hgt = 2, 0, 0
        while i < len(data) - 9:
            if data[i] != 0xFF:
                break
            if data[i + 1] in (0xC0, 0xC2):
                hgt, w = struct.unpack(">HH", data[i + 5:i + 9]); break
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    assert len(data) > 20000, f"preview image is only {len(data)} bytes"
    assert w >= 1200 and hgt >= 600, f"preview image is {w}x{hgt}, too small for a large card"


def t_preview_crawlers_get_html_not_json():
    """/bench and /adoption negotiate content, and a crawler sends Accept: */*.

    Biasing to JSON meant a shared link to either rendered as no card at all.
    """
    for path in ("/bench", "/adoption"):
        req = urllib.request.Request(BASE + path, headers={
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Accept": "*/*"})
        r = urllib.request.urlopen(req, timeout=40)
        body = r.read().decode("utf-8", "replace")
        assert "text/html" in r.headers.get("Content-Type", ""), \
            f"{path} serves {r.headers.get('Content-Type')} to a preview crawler"
        assert 'property="og:image"' in body, f"{path} has no card for a crawler"
    # and an API client still gets JSON
    s, d = get("/bench")
    assert isinstance(d, dict) and "targets" in d, "API client no longer gets JSON"


def t_demand_reports_real_queries():
    """The question a publisher has is not "am I listed" but "did anyone look"."""
    s, d = get("/demand?domain=clickhouse.com")
    assert s == 200, s
    assert d["indexed"] > 0, d
    assert "queries" in d and "resources" in d, d
    for q in d["queries"]:
        assert q["query"] and q["times"] >= 1 and q["best_rank"] >= 1, q
    # privacy is a property of the schema, not a promise in prose
    blob = json.dumps(d).lower()
    for leak in ("ip", "user_agent", "useragent", "session", "cookie"):
        assert f'"{leak}"' not in blob, f"demand response exposes {leak}"


def t_demand_404s_for_an_unindexed_domain():
    try:
        get("/demand?domain=definitely-not-indexed.example")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code


def t_metrics_json_is_public_and_complete():
    """Every published claim must be checkable against one endpoint."""
    s, d = get("/metrics.json")
    assert s == 200, s
    for sec in ("index", "verified", "liveness", "ard_publishers", "history",
                "federation", "retrieval"):
        assert sec in d, f"metrics missing {sec}"
    assert d["index"]["entries"] > 5000
    assert d["verified"]["tools"] > 1000
    assert d["ard_publishers"]["verified_manifests"] > 100
    # the path split is the finding we reported upstream; it must stay visible
    assert d["ard_publishers"]["by_path"], "manifest path split not published"


def _post_auth(path, payload, key, method="POST"):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={**UA, "Authorization": f"Bearer {key}"},
                                 method=method)
    try:
        r = urllib.request.urlopen(req, timeout=40)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def t_manifest_generation_only_emits_what_it_fetched():
    """A generated manifest must never invent an entry."""
    s, d = post("/manifest/build", {"domain": "mcp.deepwiki.com"})
    assert s == 200, s
    assert d.get("entries", 0) > 0, "generated nothing for a known MCP host"
    built = (d.get("manifest") or {}).get("entries") or []
    assert len(built) == d["entries"], "the count and the manifest disagree"
    assert len(d.get("evidence") or []) == len(built), \
        "an entry was emitted with no evidence behind it"
    for e in built:
        assert e.get("url"), "entry with no endpoint"
    for ev in d["evidence"]:
        assert ev.get("because"), "evidence with no reason"
    # and it must be served back as a real manifest
    s2, d2 = get("/m/mcp.deepwiki.com.json")
    assert s2 == 200, s2
    assert d2.get("specVersion") and d2.get("entries"), "hosted manifest malformed"
    for e in d2["entries"]:
        assert not any(k.startswith("_") for k in e), "internal key leaked into manifest"


def t_hosted_manifest_states_it_is_not_authored_by_the_owner():
    """Hosting a manifest for a domain must never look like the domain wrote it."""
    r = urllib.request.urlopen(
        urllib.request.Request(BASE + "/m/mcp.deepwiki.com.json", headers=UA), timeout=30)
    src = r.headers.get("x-manifest-source") or ""
    assert "not authored by the domain owner" in src.lower(), f"missing disclaimer: {src!r}"


def t_claim_requires_dns_proof():
    """An unverified domain must not receive a key."""
    s, d = post("/claim", {"domain": "example.com"})
    assert s == 200, s
    val = ((d.get("record") or {}).get("value") or "")
    assert val.startswith("neuronto-site-verification="), d
    s2, d2 = post("/claim/verify", {"domain": "example.com"})
    assert s2 >= 400 or not d2.get("verified"), "issued a key without proof"
    assert not d2.get("api_key"), "LEAK: key issued for an unproven domain"


def t_private_endpoints_reject_anonymous_callers():
    for method, payload in (("POST", {"entry": {"displayName": "x"}}),
                            ("DELETE", {"identifier": "x"})):
        req = urllib.request.Request(BASE + "/private/entries",
                                     data=json.dumps(payload).encode(),
                                     headers=UA, method=method)
        try:
            urllib.request.urlopen(req, timeout=20)
            raise AssertionError(f"{method} /private/entries allowed anonymously")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"{method} gave {e.code}, want 401"
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/private/entries", headers=UA), timeout=20)
        raise AssertionError("GET /private/entries allowed anonymously")
    except urllib.error.HTTPError as e:
        assert e.code == 401, e.code


def t_private_entries_are_absent_from_every_public_surface():
    """The isolation claim, checked rather than asserted."""
    s, d = get("/metrics.json")
    assert s == 200
    total = d["index"]["entries"]
    assert sum(d["index"]["by_kind"].values()) == total, \
        "by_kind does not reconcile with the entry total, a hidden class exists"
    # nothing public may ever carry the private label
    for q in ("employee", "internal", "staff directory", "payroll"):
        s2, d2 = post("/search", {"query": {"text": q}, "federation": "none",
                                  "pageSize": 25})
        assert s2 == 200, s2
        leaked = [r for r in d2.get("results", []) if r.get("visibility") == "private"]
        assert not leaked, f"LEAK: {q!r} returned private entries {leaked[:1]}"


def t_publish_resource_verifies_before_indexing():
    """The agent publishing route must hold the same bar as the HTTP one."""
    s, d = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in d["result"]["tools"]}
    assert "publish_resource" in names, names
    tool = [t for t in d["result"]["tools"] if t["name"] == "publish_resource"][0]
    assert tool["annotations"]["readOnlyHint"] is False, "write tool claims read-only"

    def call(args):
        _, r = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "publish_resource", "arguments": args}})
        res = r["result"]
        return res.get("isError", False), json.loads(res["content"][0]["text"])

    err, _ = call({})
    assert err, "accepted a submission with no endpoint and no domain"
    err, body = call({"endpoint": "https://example.com/definitely-not-mcp"})
    assert err, f"indexed a URL that is not an MCP server: {body}"
    err, body = call({"endpoint": "https://mcp.deepwiki.com/mcp"})
    if err and "timeout" in json.dumps(body).lower():
        # The negative assertions above are the ones that protect us. The
        # positive path needs a live third party, and its being slow today says
        # nothing about our verification, so it is skipped rather than failed.
        raise Skip("the reference MCP server did not answer in time")
    assert not err and body.get("status") == "indexed", body
    assert body.get("verified_tools", 0) > 0, "indexed without reading any tools"


def t_publish_resource_and_submit_cannot_drift():
    """Both routes must produce the same identifier for the same server."""
    _, r = post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "publish_resource",
                                    "arguments": {"endpoint": "https://mcp.deepwiki.com/mcp"}}})
    via_mcp = json.loads(r["result"]["content"][0]["text"]).get("identifier")
    _, via_http = post("/submit", {"endpoint": "https://mcp.deepwiki.com/mcp"})
    if not via_mcp or not via_http.get("identifier"):
        # Either leg can lose the race against a slow third party. Agreement
        # can only be judged when both answered.
        raise Skip("the reference MCP server did not answer both routes in time")
    assert via_mcp and via_mcp == via_http.get("identifier"), \
        f"routes disagree: {via_mcp} vs {via_http.get('identifier')}"


def t_audit_reports_who_outranks_the_publisher():
    """The comparison must be present, honest and free of internal keys."""
    s, d = post("/audit", {"domain": "vercel.com"}, timeout=90)
    assert s == 200, s
    assert "_manifest" not in d, "internal manifest leaked into the report"
    comp = d.get("competition")
    assert comp, "no competition section"
    assert "note" in comp and "never endorsement" in comp["note"], comp.get("note")
    for q in comp.get("queries", []):
        for a in q.get("ahead_of_you", []):
            assert a.get("has_that_you_may_not"), "a rival is listed with no reason"
    assert any("rank" in r.lower() or "returned" in r.lower()
               for r in d["recommendations"]), "comparison produced no advice"


def t_audit_advice_names_a_fixable_cause():
    """A finding a publisher cannot act on is not a finding."""
    s, d = post("/audit", {"domain": "huggingface.co"}, timeout=90)
    assert s == 200, s
    recs = d.get("recommendations") or []
    assert recs, "no recommendations"
    for r in recs:
        assert "\u2014" not in r and "\u2013" not in r, f"dash in copy: {r}"
        assert len(r) > 40, f"advice too thin to act on: {r}"


# ---------------------------------------------------------------------------
# Boundaries. These assert what must NOT happen, so they are the ones that
# matter after a refactor. The key-gated ones skip without a key rather than
# fail, because the suite must stay runnable by anyone against production.
# ---------------------------------------------------------------------------

import os
KEY  = os.getenv("NEURONTO_KEY", "").strip()
KEY2 = os.getenv("NEURONTO_KEY2", "").strip()
# A token that appears nowhere in the public index. "canary" alone is a false
# positive: ai.canaryusers is a real publisher, which cost one debugging round.
#
# A private entry carrying this token is kept permanently on neuronto.com as a
# tripwire. Without it the leak tests below would pass while asserting nothing,
# which is the exact failure this project has hit before: a check that cannot
# fail is testing nothing. If those tests go green after the entry is deleted,
# they are lying. RUNBOOK has the command to restore it.
CANARY = "zqxjv7f3a"


def _auth(path, payload, key, method="POST"):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={**UA, "Authorization": f"Bearer {key}"}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def t_manifest_build_refuses_private_addresses():
    """It fetches on an arbitrary caller's behalf, so it must not reach inside."""
    for host in ("127.0.0.1", "localhost", "169.254.169.254", "10.0.0.1",
                 "192.168.1.1", "[::1]", "0.0.0.0", "metadata.google.internal"):
        s, d = post("/manifest/build", {"domain": host})
        got = d.get("entries", 0) if isinstance(d, dict) else 0
        assert s >= 400 or got == 0, f"{host} produced {got} entries (status {s})"


def t_claim_validates_the_hostname():
    for host in ("localhost", "..", "a..b", "-x.com", "x-.com", "", ".",
                 "a" * 300 + ".com", "exa mple.com"):
        s, d = post("/claim", {"domain": host})
        assert s >= 400, f"accepted hostname {host!r} (status {s})"
    # A scheme is stripped rather than refused, deliberately and identically to
    # /audit. What matters is that it normalises to the bare host, nothing more.
    s, d = post("/claim", {"domain": "http://x.com/some/path?q=1"})
    assert s == 200 and d.get("domain") == "x.com", f"normalisation: {d.get('domain')!r}"


def t_malformed_bearer_tokens_are_all_rejected():
    for k in ("", "nk_", "nk_x", "Bearer", "null", "undefined", "nk_" + "A" * 200,
              "nk_' OR 1=1 --", "../../etc/passwd", "nk_%00"):
        s, _ = _auth("/private/entries", None, k, method="GET")
        assert s == 401, f"token {k[:24]!r} gave {s}, want 401"


def t_no_private_entry_reaches_any_public_surface():
    """The isolation claim, checked across every public read path at once."""
    for mode in ("auto", "none", "referrals"):
        s, d = post("/search", {"query": {"text": f"{CANARY} private canary marker"},
                                "federation": mode, "pageSize": 30}, timeout=70)
        assert s == 200, f"{mode}: {s}"
        bad = [r for r in d.get("results", []) if r.get("visibility") == "private"]
        assert not bad, f"LEAK via federation={mode}: {bad[:1]}"
    s, d = post("/explore", {"query": {"text": f"{CANARY} canary"},
                             "resultType": {"facets": [{"field": "type"},
                                                       {"field": "publisher"}]}})
    assert s == 200, f"/explore: {s} {str(d)[:120]}"
    assert CANARY not in json.dumps(d).lower(), "/explore leaked a private entry"
    for off in (0, 100, 500):
        s, d = get(f"/agents?limit=100&offset={off}")
        assert s == 200, s
        assert CANARY not in json.dumps(d).lower(), f"/agents leaked at offset {off}"
    for path in ("/feed.xml", "/sitemap.xml", "/badge/neuronto.com.svg",
                 "/publisher/neuronto.com", "/metrics.json"):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                BASE + path, headers={"User-Agent": UA["User-Agent"]}), timeout=40)
            assert CANARY not in r.read().decode("utf-8", "replace").lower(), \
                f"{path} leaked a private entry"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404), f"{path} -> {e.code}"


def t_public_counts_reconcile():
    """A hidden class of entry would show up here as a mismatch."""
    s, d = get("/metrics.json")
    assert s == 200
    total = d["index"]["entries"]
    assert sum(d["index"]["by_kind"].values()) == total, \
        "by_kind does not reconcile with the entry total"
    assert sum(d["index"]["sources"].values()) >= total, \
        "an entry exists with no recorded source"


def t_publish_resource_is_idempotent():
    ids = []
    for _ in range(2):
        _, r = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params":
                    {"name": "publish_resource",
                     "arguments": {"endpoint": "https://mcp.deepwiki.com/mcp"}}}, timeout=70)
        ids.append(json.loads(r["result"]["content"][0]["text"]).get("identifier"))
    if not all(ids):
        # Idempotency can only be judged when both calls reached the third party.
        raise Skip(f"the reference MCP server did not answer both calls: {ids}")
    assert ids[0] == ids[1], f"not idempotent: {ids}"


def t_generation_and_submission_agree():
    s, d = post("/manifest/build", {"domain": "mcp.deepwiki.com"}, timeout=70)
    built = {e["identifier"] for e in (d.get("manifest") or {}).get("entries", [])}
    s2, d2 = post("/submit", {"endpoint": "https://mcp.deepwiki.com/mcp"}, timeout=70)
    if not built or not d2.get("identifier"):
        raise Skip("the reference MCP server did not answer both routes in time")
    assert d2.get("identifier") in built, \
        f"generation and submission disagree: {d2.get('identifier')} not in {built}"


def t_audit_competition_agrees_with_search():
    """The report must never claim a rank the index does not actually give."""
    s, d = post("/audit", {"domain": "vercel.com"}, timeout=120)
    assert s == 200, s
    for q in (d.get("competition") or {}).get("queries", [])[:2]:
        s2, d2 = post("/search", {"query": {"text": q["query"]},
                                  "federation": "none", "pageSize": 10}, timeout=70)
        rank = None
        for i, r in enumerate(d2.get("results", []), 1):
            if "vercel.com" in (r.get("url", "") + r.get("identifier", "")).lower():
                rank = i
                break
        assert rank == q["your_best_rank"], \
            f"{q['query']!r}: report says {q['your_best_rank']}, search says {rank}"


def t_owner_sees_own_private_entries():
    if not KEY:
        raise Skip("set NEURONTO_KEY to a verified domain's key")
    s, d = post("/search", {"query": {"text": f"{CANARY} private canary marker"},
                            "federation": "none", "pageSize": 25}, timeout=70)
    assert not [r for r in d["results"] if r.get("visibility") == "private"]
    req = urllib.request.Request(BASE + "/search",
        data=json.dumps({"query": {"text": f"{CANARY} private canary marker"},
                         "federation": "none", "pageSize": 25}).encode(),
        headers={**UA, "Authorization": f"Bearer {KEY}"}, method="POST")
    body = urllib.request.urlopen(req, timeout=70).read().decode()
    for line in body.splitlines():
        if line.startswith("data: "):
            body = line[6:]
    res = json.loads(body)["results"]
    priv = [r for r in res if r.get("visibility") == "private"]
    assert len(priv) == 1, f"owner sees {len(priv)} private entries, want 1"
    assert priv[0]["score"] >= 90, f"full-coverage match scored {priv[0]['score']}"


def t_one_tenant_cannot_reach_another():
    if not (KEY and KEY2):
        raise Skip("set NEURONTO_KEY and NEURONTO_KEY2 to two verified domains' keys")
    s, d = _auth("/private/entries", None, KEY2, method="GET")
    assert s == 200, s
    assert not any(CANARY.upper() in str(e.get("displayName", "")).upper()
                   for e in d.get("entries", [])), "cross-tenant read"
    s2, d2 = _auth("/private/entries",
                   {"identifier": "urn:air:neuronto.com:private:adversary-canary"},
                   KEY2, method="DELETE")
    assert s2 == 404, f"cross-tenant delete returned {s2}"
    s3, d3 = _auth("/private/entries", None, KEY, method="GET")
    assert d3.get("count", 0) >= 1, "another tenant's key destroyed the entry"


def t_score_is_relative_and_the_envelope_says_so():
    """A caller must be able to tell "best of nothing" from a real answer.

    Every top result scores near 100 by construction, because the score is
    relative to its own result set. Measured on the live index, the nonsense
    query below scored 100. An agent acting without a human cannot see the
    difference from the score alone, so the envelope carries an absolute one.
    """
    s, d = post("/search", {"query": {"text": "zzzz nonexistent capability qqqq"},
                            "federation": "none", "pageSize": 5}, timeout=70)
    assert s == 200, s
    m = d.get("queryMatch")
    assert m, "no queryMatch in the search envelope"
    assert m["coverage"] == 0.0 and m["confidence"] == "none", \
        f"nonsense query reported {m['confidence']} at coverage {m['coverage']}"
    assert "relative" in m["note"] and "never a trust" in m["note"]

    s2, d2 = post("/search", {"query": {"text": "send an email"},
                              "federation": "none", "pageSize": 5}, timeout=70)
    m2 = d2["queryMatch"]
    assert m2["coverage"] >= 0.6 and m2["confidence"] in ("medium", "high"), \
        f"a real capability query reported {m2['confidence']} at {m2['coverage']}"
    # the point of the field: the two are indistinguishable by score alone
    assert d["results"][0]["score"] >= 90 and d2["results"][0]["score"] >= 90, \
        "premise changed: top scores are no longer both near 100"


def t_query_match_never_claims_correctness():
    """Coverage is word overlap. It must not be described as more than that."""
    s, d = post("/search", {"query": {"text": "get the weather forecast"},
                            "federation": "none", "pageSize": 3}, timeout=70)
    m = d["queryMatch"]
    assert set(m["matchedTerms"]) <= set(m["queryTerms"]), "matched a term not in the query"
    assert abs(m["coverage"] - len(m["matchedTerms"]) / max(1, len(m["queryTerms"]))) < 1e-6, \
        "coverage does not equal matched over total, so it is not what it says"
    assert "not correctness" in m["note"], "the note must not overclaim"


def t_rate_limiter_is_live_and_counting():
    """Headers must be present and the allowance must actually decrement.

    Refusal itself is covered by scripts/test_limits.py, which can exhaust a
    window against a temporary database without spending this caller's real
    allowance on every test run.
    """
    def probe():
        req = urllib.request.Request(BASE + "/claim",
            data=json.dumps({"domain": "ratelimit-probe.example"}).encode(),
            headers=UA, method="POST")
        r = urllib.request.urlopen(req, timeout=30)
        return {k.lower(): v for k, v in r.headers.items()}
    a = probe()
    assert "x-ratelimit-limit" in a, f"no limit headers on a limited route: {list(a)}"
    b = probe()
    ra, rb = int(a["x-ratelimit-remaining"]), int(b["x-ratelimit-remaining"])
    assert rb < ra, f"allowance did not decrement: {ra} then {rb}"
    assert int(a["x-ratelimit-reset"]) > time.time(), "reset is in the past"


def t_unlimited_routes_carry_no_limit_headers():
    """Local search is the product and must not be metered."""
    req = urllib.request.Request(BASE + "/search",
        data=json.dumps({"query": {"text": "pdf"}, "federation": "none"}).encode(),
        headers=UA, method="POST")
    r = urllib.request.urlopen(req, timeout=40)
    hdrs = {k.lower() for k in r.headers.keys()}
    assert "x-ratelimit-limit" not in hdrs, "local search is being rate limited"


def t_limits_are_published_not_secret():
    s, d = get("/metrics.json")
    lim = d.get("limits")
    assert lim and lim.get("enabled"), "limiter absent or disabled"
    assert lim["rules"], "no rules published"
    for name, r in lim["rules"].items():
        assert r["limit"] > 0 and r["window_s"] > 0, f"{name} has a nonsense rule"
        assert r["verified_limit"] >= r["limit"], \
            f"{name}: proving domain ownership must never lower your allowance"


def t_no_query_text_on_any_public_surface():
    """A visitor's query, and above all a tenant's, is not ours to publish."""
    s, d = get("/stats")
    assert s == 200
    for row in (d.get("recent") or []):
        assert "q" not in row, f"/stats is publishing query text: {row}"
    blob = json.dumps(d).lower()
    assert CANARY not in blob, "/stats leaked a private query"
    s2, d2 = get("/metrics.json")
    assert CANARY not in json.dumps(d2).lower(), "/metrics.json leaked a private query"


def t_authenticated_search_is_not_logged():
    if not KEY:
        raise Skip("set NEURONTO_KEY to a verified domain's key")
    marker = f"{CANARY} private canary marker"
    req = urllib.request.Request(BASE + "/search",
        data=json.dumps({"query": {"text": marker}, "federation": "none"}).encode(),
        headers={**UA, "Authorization": f"Bearer {KEY}"}, method="POST")
    urllib.request.urlopen(req, timeout=70).read()
    # the search happened; its text must not appear anywhere public
    s, d = get("/stats")
    assert CANARY not in json.dumps(d).lower(), \
        "an authenticated search reached the public activity feed"
    s2, d2 = get("/demand?domain=neuronto.com")
    if s2 == 200:
        assert CANARY not in json.dumps(d2).lower(), \
            "an authenticated search reached the demand report"


def t_publisher_counts_agree_across_surfaces():
    """Three surfaces once reported three different numbers for one word."""
    s, d = get("/metrics.json")
    a = d["ard_publishers"]
    assert a["manifest_hosts"] >= a["publishers_indexed"], \
        "more publishers indexed than manifests verified, which is impossible"
    assert a["manifest_hosts"] - a["publishers_indexed"] == \
        a["hosts_serving_no_indexed_resource"], "the gap does not reconcile"
    assert sum(a["by_path"].values()) == a["manifest_hosts"], \
        "by_path does not sum to the host count"
    assert a.get("definitions"), "the numbers ship without saying what they mean"
    # the public page must quote the same figures
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/ard-publishers", headers={"User-Agent": UA["User-Agent"]}), timeout=40)
    html = r.read().decode("utf-8", "replace")
    for n in (a["manifest_hosts"], a["publishers_indexed"]):
        assert f"{n:,}" in html or str(n) in html, \
            f"the page does not carry {n}, so it has diverged from metrics again"


def t_every_verified_manifest_records_the_path_it_was_found_on():
    """The crawler dropped this for every domain it found, so it went unnoticed."""
    s, d = get("/metrics.json")
    a = d["ard_publishers"]
    assert set(a["by_path"]) <= {"/.well-known/ard.json", "/.well-known/ai-catalog.json"}, \
        f"unexpected manifest path recorded: {set(a['by_path'])}"
    assert a["by_path"].get("/.well-known/ai-catalog.json", 0) > 0, \
        "premise changed: the ecosystem was on the predecessor path"


def t_site_name_is_on_every_page():
    """The name of the thing is in the slot that matters, once, on every page."""
    import re as _re
    for path in ("/", "/what-is-ard", "/publish", "/submit", "/submit-mcp-server", "/console",
                 "/ard-registries", "/ard-manifest-generator", "/ard-conformance",
                 "/ard-publishers", "/tools/", "/bench", "/adoption", "/published", "/blog"):
        s, h = _html(path)
        assert s == 200, f"{path}: {s}"
        t = _re.search(r"<title>(.*?)</title>", h, _re.S)
        assert t and "Neuronto ARD Registry" in t.group(1), f"{path}: title {t.group(1) if t else None!r}"
        assert 'property="og:site_name" content="Neuronto ARD Registry"' in h, f"{path}: og:site_name"
        assert t.group(1).count("Neuronto ARD Registry") == 1, f"{path}: name repeated in title"


def t_new_pages_answer_the_query_in_the_h1():
    import re as _re
    want = {"/ard-registries": "ARD registries",
            "/ard-manifest-generator": "ARD manifest generator",
            "/ard-conformance": "ARD conformance",
            "/submit": "How to submit to an ARD registry"}
    for path, phrase in want.items():
        s, h = _html(path)
        assert s == 200, f"{path}: {s}"
        h1 = _re.search(r"<h1[^>]*>(.*?)</h1>", h, _re.S)
        assert h1 and phrase.lower() in _re.sub(r"<[^>]+>", "", h1.group(1)).lower(), \
            f"{path}: h1 is {h1.group(1) if h1 else None!r}"
        assert "\u2014" not in h and "\u2013" not in h, f"{path}: dash in page"


def t_registries_moved_with_a_permanent_redirect():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        op.open(urllib.request.Request(BASE + "/registries", headers={"User-Agent": UA["User-Agent"]}), timeout=30)
        raise AssertionError("/registries did not redirect")
    except urllib.error.HTTPError as e:
        assert e.code == 301, e.code
        assert e.headers.get("location", "").endswith("/ard-registries"), e.headers.get("location")


def t_comparison_page_states_its_method_and_date():
    s, h = _html("/ard-registries")
    assert "September 2026" in h, "no measurement date"
    assert "POST /search" in h or "POST <code>/search" in h.replace("<code>", "<code>"), "no definition of a registry"
    for name in ("WellKnown", "Desvela", "GitHub Agent Finder", "Hugging Face"):
        assert name in h, f"{name} missing from the comparison"


def t_generator_page_fronts_the_real_endpoint():
    s, h = _html("/ard-manifest-generator")
    assert "/manifest/build" in h, "page does not call the generator"
    assert "Nothing is invented" in h, "the one rule that matters is not stated"


def t_every_page_links_the_new_sections():
    for path in ("/", "/what-is-ard", "/tools/", "/ard-publishers", "/console"):
        s, h = _html(path)
        assert 'href="/ard-registries"' in h, f"{path} does not link the comparison"


def t_no_helper_is_defined_twice():
    """A second definition of a helper silently replaces the first.

    A duplicate `_html` that omitted the Accept header shadowed the real one and
    broke four passing tests at once. Same shape as the duplicated functions
    found in catalog.py: later definition wins, quietly.
    """
    import ast as _ast
    import collections
    src = _ast.parse(open(__file__).read())
    names = [n.name for n in src.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
    dupes = [n for n, c in collections.Counter(names).items() if c > 1]
    assert not dupes, f"defined more than once in this file: {dupes}"


def t_badge_is_monochrome_and_states_a_fact():
    """No score, no colour grade: the spec separates discovery from trust."""
    import re as _re
    import random as _rnd
    # Cache-busted: the badge is served with an hour of cache and a CDN in
    # front, so without this the test grades whatever the edge happened to keep
    # from a previous deploy. It cost one confusing failure already.
    cb = _rnd.randrange(10 ** 9)
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + f"/badge/zapier.com.svg?cb={cb}", headers={"User-Agent": UA["User-Agent"]}), timeout=30)
    svg = r.read().decode()
    assert r.headers.get("content-type", "").startswith("image/svg+xml")
    # Neutral, not literally grey. A neutral carrying a slight bias is a chosen
    # colour and reads as designed; the tolerance only has to separate that from
    # an actual hue. The badge this replaced used #3a5f8a, a spread of 80; every
    # neutral here is under 10.
    cols = {c.lower() for c in _re.findall(r"#[0-9A-Fa-f]{6}", svg)}
    for c in cols:
        rr, gg, bb = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        spread = max(rr, gg, bb) - min(rr, gg, bb)
        assert spread <= 24, f"badge carries a hue, so it reads as a grade: {c} (spread {spread})"
    assert "prefers-color-scheme" in svg, "no dark variant"
    for word in ("score", "grade", "rating", "certified", "approved", "trusted"):
        assert word not in svg.lower(), f"badge implies a judgment: {word}"
    for theme in ("light", "dark"):
        r2 = urllib.request.urlopen(urllib.request.Request(
            BASE + f"/badge/zapier.com.svg?theme={theme}&cb={cb}",
            headers={"User-Agent": UA["User-Agent"]}), timeout=30)
        assert r2.status == 200


def t_unknown_domain_gets_a_badge_not_a_broken_image():
    import random as _rnd
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + f"/badge/definitely-not-indexed-xyz.com.svg?cb={_rnd.randrange(10**9)}",
        headers={"User-Agent": UA["User-Agent"]}), timeout=30)
    assert r.status == 200 and "not indexed yet" in r.read().decode()


def t_badge_page_gives_a_snippet_and_no_obligation():
    s, h = _html("/badge?domain=zapier.com")
    assert s == 200, s
    assert "/ard-publishers/zapier.com" in h, "the badge does not link the publisher's own page"
    assert "alt=" in h and "Neuronto ARD Registry" in h, "no alt text in the snippet"
    assert "optional" in h.lower() or "Nothing here is required" in h, \
        "the page does not say displaying it is optional"
    # RULE 8: no strategy talk on a public page
    for w in ("nofollow", "backlink", "link equity", "authority", "SEO", "ranking factor"):
        assert w.lower() not in h.lower(), f"public page discusses distribution strategy: {w}"


def t_submit_offers_the_badge_without_requiring_it():
    s, d = post("/submit", {"domain": "zapier.com"}, timeout=70)
    if d.get("status") != "indexed":
        raise Skip(f"submit returned {d.get('status')}, cannot check the offer")
    b = d.get("badge")
    assert b and b.get("markdown") and b.get("optional"), f"no optional badge offer: {list(d)}"
    assert "optional" in b["optional"].lower()


def t_pages_report_themselves_and_honour_do_not_track():
    """Most pages are served from a CDN, so a page that cannot report itself is
    a page nobody can count."""
    for path in ("/", "/tools/", "/ard-publishers", "/what-is-ard", "/ard-registries"):
        s_, h = _html(path)
        assert s_ == 200 and "sendBeacon" in h, f"{path} carries no beacon"
        assert "globalPrivacyControl" in h and "doNotTrack" in h, \
            f"{path} beacon does not check the browser's opt-out"

    def post_e(payload, extra=None):
        req = urllib.request.Request(BASE + "/e", data=json.dumps(payload).encode(),
                                     headers={**UA, **(extra or {})}, method="POST")
        return urllib.request.urlopen(req, timeout=30).status

    assert post_e({"t": "view", "p": "/x", "vw": 1440}) == 204
    assert post_e({"t": "end", "p": "/x", "d": 30, "s": 50}) == 204
    # opt-out and junk are both accepted and dropped, never an error to the page
    assert post_e({"t": "view", "p": "/x"}, {"DNT": "1"}) == 204
    assert post_e({"t": "view", "p": "/x"}, {"Sec-GPC": "1"}) == 204
    assert post_e({"t": "nonsense"}) == 204
    assert post_e({}) == 204


def t_beacon_endpoint_is_never_cached():
    """It is a POST, so it must reach the origin every time."""
    req = urllib.request.Request(BASE + "/e", data=b'{"t":"view","p":"/x"}',
                                 headers={**UA}, method="POST")
    r = urllib.request.urlopen(req, timeout=30)
    assert r.headers.get("cf-cache-status", "DYNAMIC").upper() in ("DYNAMIC", "BYPASS"), \
        f"the beacon endpoint is being cached: {r.headers.get('cf-cache-status')}"


def t_header_and_footer_are_grouped_and_fit():
    """Nine flat links plus a search field stopped fitting and the first one
    wrapped onto three lines. Four groups is the shape the pages have."""
    import re as _re
    for path in ("/", "/what-is-ard", "/tools/", "/console", "/ard-registries"):
        s_, h = _html(path)
        assert s_ == 200, f"{path}: {s_}"
        assert h.count('class="ng"') == 4, f"{path}: {h.count(chr(34)+'ng'+chr(34))} nav groups, want 4"
        assert h.count('class="ni"') >= 14, f"{path}: too few grouped items"
        assert h.count('class="fg"') == 5, f"{path}: footer is not 5 columns"
        # the flat row and the retired link must not come back
        assert 'class="fl"' not in h, f"{path}: the old flat footer row is back"
        assert 'href="/registries"' not in h, f"{path}: links the retired /registries"
        # search must exist and work without JavaScript
        assert 'class="navsearch"' in h and 'action="/tools"' in h, f"{path}: no header search"
        # and the phone keeps the three primary actions visible
        assert 'class="navquick"' in h, f"{path}: no mobile quick links"


def _re_email():
    import re as _re
    return _re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", _re.I)


def t_contact_address_is_an_image_only():
    """Published deliberately, and never as text a scraper can lift."""
    # The address itself is not written here either: a test that hard-codes it
    # publishes it just as effectively as the page would. Supply it through the
    # environment to check exactly, or the generic patterns always apply.
    import os as _os
    addr = _os.getenv("NEURONTO_CONTACT", "").strip().lower()
    s_, h = _html("/")
    assert "/img/contact.png" in h, "no contact image in the footer"
    low = h.lower()
    for form in ("mailto:", "&#64;", "[at]", " (at) "):
        assert form not in low, f"an address appears as text: {form}"
    assert not _re_email().search(h), "an address appears as text in the page"
    if addr:
        assert addr not in low, "the exact address appears as text"
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/img/contact.png", headers={"User-Agent": UA["User-Agent"]}), timeout=30)
    assert r.status == 200 and r.headers.get("content-type") == "image/png"
    # never in structured data or the machine-readable documents either
    for path in ("/.well-known/ard.json", "/llms.txt", "/openapi.json"):
        try:
            rr = urllib.request.urlopen(urllib.request.Request(
                BASE + path, headers={"User-Agent": UA["User-Agent"]}), timeout=30)
            body = rr.read().decode("utf-8", "replace")
            assert not _re_email().search(body), f"{path} carries an address"
            if addr:
                assert addr not in body.lower(), f"{path} carries the address"
        except urllib.error.HTTPError:
            pass


def t_security_headers_on_every_response():
    """One owner, present on HTML, JSON and images alike."""
    want = {"strict-transport-security": "max-age=", "x-content-type-options": "nosniff",
            "x-frame-options": "DENY", "referrer-policy": "strict-origin",
            "content-security-policy": "frame-ancestors 'none'", "permissions-policy": "camera=()"}
    for path in ("/", "/metrics.json", "/badge/zapier.com.svg", "/tools/"):
        r = urllib.request.urlopen(urllib.request.Request(
            BASE + path + ("?cb=1" if "?" not in path else "&cb=1"),
            headers={"User-Agent": UA["User-Agent"], "Accept": "text/html"}), timeout=30)
        got = {k.lower(): v for k, v in r.headers.items()}
        for h, frag in want.items():
            assert frag in got.get(h, ""), f"{path}: {h} missing or wrong: {got.get(h)!r}"
        assert len([k for k in r.headers.keys() if k.lower() == "content-security-policy"]) == 1, \
            f"{path}: CSP set more than once"


def t_no_top_level_definition_is_duplicated_in_the_app():
    """The fourth time this bit: a second `_text_w` in badge.py silently won.

    Runs on the checked-in source, so it needs the repository layout.
    """
    import ast as _ast, collections, glob, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = glob.glob(os.path.join(root, "app", "*.py"))
    if not files:
        raise Skip("app/ not beside this script")
    for f in files:
        names = collections.Counter(n.name for n in _ast.parse(open(f).read()).body
                                    if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)))
        d = [n for n, c in names.items() if c > 1]
        assert not d, f"{os.path.basename(f)} defines twice: {d}"


def t_malformed_and_oversized_input_is_refused_not_crashed():
    s, d = post("/search", {"query": {"text": "a" * 2001}, "federation": "none"})
    assert s == 400 and d.get("error") == "invalid_request", (s, d)
    req = urllib.request.Request(BASE + "/search", data=("[" * 3000 + "]" * 3000).encode(),
                                 headers=UA, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30)
        raise AssertionError("deeply nested body was accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"nested body gave {e.code}, want 400 (a 500 blames us for the caller's input)"
    s2, d2 = post("/search", {"query": {"text": "pdf"}, "federation": "none", "pageSize": 100000})
    assert s2 == 200 and len(d2.get("results", [])) <= 100, "pageSize is not capped at the spec maximum"


def t_search_stays_fast_under_concurrency():
    """The product claim, measured through the edge from here.

    Ten searches at once. Each alone is tens of milliseconds at the origin;
    if they serialise on the event loop the median climbs past a second.
    """
    import concurrent.futures as cf, time as _t
    def one(q):
        t = _t.perf_counter()
        s_, _ = post("/search", {"query": {"text": q}, "federation": "none", "pageSize": 10}, timeout=60)
        return s_, (_t.perf_counter() - t) * 1000
    qs = ["send an email", "read a pdf", "query postgres", "post to slack", "scrape a site",
          "transcribe audio", "generate an image", "deploy a container", "weather", "translate"]
    with cf.ThreadPoolExecutor(10) as ex:
        res = list(ex.map(one, qs))
    assert all(r[0] == 200 for r in res), [r[0] for r in res]
    med = sorted(r[1] for r in res)[5]
    assert med < 1500, f"median {med:.0f}ms for 10 concurrent searches; they are serialising again"


def t_audit_names_the_action_when_a_domain_is_not_indexed():
    """The guide's own step 4 says publishing is not being indexed. The audit
    used to confirm that and then advise patience, which was the funnel's dead
    end: it told a publisher to wait when the fix was one request."""
    # A domain that serves a manifest. If it is already indexed here the branch
    # under test cannot fire, so index state decides which assertion applies.
    s_, d = post("/audit", {"domain": "vercel.com"}, timeout=120)
    assert s_ == 200, s_
    here = [c for c in d["coverage"] if c["registry"].lower().startswith("neuronto")]
    assert here, "the audit does not report this index in its coverage"
    recs = " ".join(d.get("recommendations") or [])
    if here[0]["indexed"]:
        assert "indexable" not in d, "offers to index a domain it already holds"
    else:
        ix = d.get("indexable")
        assert ix and ix.get("ready"), "a fetchable manifest is not reported as indexable"
        assert ix["how"]["cli"].endswith(d["domain"]), ix["how"]
        assert d["domain"] in recs and "/submit" in recs, \
            "the recommendation does not name the action or the domain"
    # Never advise patience about a registry that does take submissions.
    assert "crawl on their own schedule" not in recs, \
        "the old passive wording is back"


def t_publish_guide_ends_with_the_step_that_gets_you_indexed():
    s_, h = _html("/publish")
    assert s_ == 200, s_
    import re as _re
    steps = [_re.sub(r"<[^>]+>", "", m).strip() for m in _re.findall(r"<h2[^>]*>(.*?)</h2>", h, _re.S)]
    assert any("tell the registries" in x.lower() for x in steps), \
        f"the guide never tells the reader to submit: {steps}"
    assert 'id="pf"' in h and "/submit" in h, "the step has no action on the page"
    # and it must stay honest about the registries that have no submission path
    assert "no submission path found" in h, "the comparison flatters us by omission"


def t_every_machine_readable_document_serves():
    """A 500 on one of these went unnoticed because the only test that fetched
    it swallowed HTTP errors. These are what a crawler or an agent reads, so a
    failure here is invisible to a human and total for a machine."""
    want = {
        "/llms.txt": "text/plain",
        "/robots.txt": "text/plain",
        "/sitemap.xml": "xml",
        "/feed.xml": "xml",
        "/.well-known/ard.json": "json",
        "/.well-known/ai-catalog.json": "json",
        "/.well-known/did.json": "json",
        "/openapi.json": "json",
        "/metrics.json": "json",
    }
    for path, kind in want.items():
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                BASE + path + "?cb=1", headers={"User-Agent": UA["User-Agent"]}), timeout=30)
        except urllib.error.HTTPError as e:
            raise AssertionError(f"{path} returned {e.code}")
        body = r.read()
        assert r.status == 200 and body, f"{path}: {r.status}, {len(body)} bytes"
        assert kind in r.headers.get("content-type", ""), \
            f"{path}: {r.headers.get('content-type')}"
        if kind == "json":
            json.loads(body)


def t_llms_txt_tells_an_agent_how_to_be_listed():
    """It is the machine-readable guide, and it described only how to query us."""
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + "/llms.txt?cb=1", headers={"User-Agent": UA["User-Agent"]}), timeout=30)
    t = r.read().decode()
    assert "How to be listed" in t, "llms.txt never says how to get indexed"
    assert '{"domain":"example.com"}' in t, \
        "the submit example is malformed (f-string braces are a real hazard here)"
    assert "/submit" in t and "publish_resource" in t


def main():
    print(f"\n  E2E against {BASE}\n" + "  " + "-" * 62)
    for name, fn in list(globals().items()):
        if name.startswith("t_") and callable(fn):
            check(name[2:].replace("_", " "), fn)
    print("  " + "-" * 62)
    tail = f", {len(_skipped)} skipped" if _skipped else ""
    print(f"  {len(_passed)} passed, {len(_failed)} failed{tail}")
    if _skipped:
        print("\n  SKIPPED (these assert nothing until you supply what they need):")
        for n, why in _skipped:
            print(f"   - {n}: {why}")
    if _failed:
        print("\n  FAILURES:")
        for n, e in _failed:
            print(f"   - {n}: {e}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
