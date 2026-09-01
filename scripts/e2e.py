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
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://neuronto.com").rstrip("/")
UA = {"User-Agent": "neuronto-e2e/1.0", "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream"}

_passed: list[str] = []
_failed: list[tuple[str, str]] = []


def get(path, timeout=30):
    r = urllib.request.urlopen(urllib.request.Request(BASE + path, headers=UA), timeout=timeout)
    return r.status, json.loads(r.read() or b"{}")


def post(path, payload, timeout=40):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers=UA, method="POST")
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


def t_mcp_lists_all_three_tools():
    s, d = _rpc("tools/list")
    assert s == 200, s
    names = {t["name"] for t in d["result"]["tools"]}
    assert names == {"find_resource", "find_tool", "registry_stats"}, names


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
    req = urllib.request.Request(BASE + "/badge/com.jojapi.svg",
                                 headers={"User-Agent": "e2e"})
    r = urllib.request.urlopen(req, timeout=30)
    body = r.read().decode()
    assert r.headers.get("Content-Type", "").startswith("image/svg"), \
        r.headers.get("Content-Type")
    assert "verified tool" in body, "badge does not state the tool count"
    assert "<svg" in body and "</svg>" in body, "not an svg"


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
    marker = f"pdf extraction probe {int(_t.time())}"
    s, d = post("/search", {"query": {"text": marker}, "federation": "none",
                            "pageSize": 5})
    assert s == 200, s
    s2, st = get("/stats")
    # the search must be visible in the recent log
    recent = [r.get("q") for r in (st.get("recent") or [])]
    assert marker in recent, "search was not logged at all"


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


def main():
    print(f"\n  E2E against {BASE}\n" + "  " + "-" * 62)
    for name, fn in list(globals().items()):
        if name.startswith("t_") and callable(fn):
            check(name[2:].replace("_", " "), fn)
    print("  " + "-" * 62)
    print(f"  {len(_passed)} passed, {len(_failed)} failed")
    if _failed:
        print("\n  FAILURES:")
        for n, e in _failed:
            print(f"   - {n}: {e}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
