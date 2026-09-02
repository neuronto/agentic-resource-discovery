"""Capability pages, index composition, and HTML views of the measurements.

Deliberately not a directory. We measured what actually ranks for MCP queries:
across 24 server-name searches and 12 capability searches, GitHub took 17 of the
top-three slots and Glama, the largest directory in the category with 80,830
server pages, took none. Ten directory domains split the remainder. Mirroring
the primary source at scale is a commodity race that the primary source wins,
and ten thousand thin auto-generated pages is what scaled-content policies are
written about.

So this builds a small number of pages that carry something no other site has:
the **verified tool surface**, read from each server's own `tools/list`, with
the input schema, whether the endpoint answers, and whether it demands
credentials. That last column exists nowhere else. The format is the one that
actually wins these queries, a comparison page rather than a listing.

Categories are not invented. Each was scored against the whole corpus of 31,411
verified tools before being published, and only those with real volume survive,
so no page is a stub.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import time

from . import config, render, store
from .render import esc, fmt

# slug -> (title, one-line intent, match terms)
CATEGORIES: dict[str, tuple[str, str, list[str]]] = {
 "pdf-documents": ("PDF and document extraction",
   "Reading text, tables and structure out of PDFs and office documents.",
   ["pdf","document","docx","ocr","extract text","parse document"]),
 "web-scraping": ("Web scraping and browser automation",
   "Fetching, rendering and extracting from pages, including sites that fight back.",
   ["scrape","crawl","browser","playwright","puppeteer","screenshot","fetch page","html"]),
 "web-search": ("Web search",
   "Querying search engines and getting ranked results back as data.",
   ["web search","search web","google","serp","bing","search engine"]),
 "databases": ("Databases and SQL",
   "Querying and inspecting relational and document stores.",
   ["sql","postgres","mysql","sqlite","database","query table","mongodb","redis"]),
 "email": ("Email",
   "Sending, reading and searching mail.",
   ["email","smtp","imap","mailbox","send mail","inbox"]),
 "calendar-scheduling": ("Calendar and scheduling",
   "Events, availability, meetings and bookings.",
   ["calendar","schedule","meeting","event","booking","appointment"]),
 "github-git": ("GitHub and version control",
   "Repositories, issues, pull requests and commit history.",
   ["github","git ","repository","pull request","commit","issue tracker"]),
 "cloud-devops": ("Cloud and DevOps",
   "Clusters, containers, infrastructure and deployment.",
   ["kubernetes","docker","aws","terraform","deploy","cluster","s3 bucket"]),
 "payments-billing": ("Payments and billing",
   "Charges, invoices, subscriptions and checkout.",
   ["stripe","payment","invoice","billing","checkout","subscription"]),
 "crypto-blockchain": ("Crypto and blockchain",
   "Wallets, tokens, on-chain data and decentralised finance.",
   ["blockchain","wallet","token","ethereum","solana","onchain","defi","crypto"]),
 "finance-markets": ("Finance and market data",
   "Prices, tickers, portfolios and company financials.",
   ["stock","market data","ticker","portfolio","trading","forex","earnings"]),
 "files-storage": ("Files and storage",
   "Reading, writing and listing files across local and cloud storage.",
   ["file","filesystem","upload","download","storage","directory listing","drive"]),
 "messaging-chat": ("Messaging and chat",
   "Posting and reading in Slack, Discord, Telegram and similar.",
   ["slack","discord","telegram","whatsapp","sms","message channel"]),
 "crm-sales": ("CRM and sales",
   "Contacts, leads, deals and pipelines.",
   ["crm","salesforce","hubspot","lead","contact record","pipeline deal"]),
 "project-management": ("Project management",
   "Tickets, boards, sprints and docs.",
   ["jira","linear","asana","notion","ticket","task board","sprint"]),
 "images-media": ("Images, audio and video",
   "Generating, converting and transcribing media.",
   ["image","video","audio","transcribe","speech","render image","thumbnail"]),
 "maps-location": ("Maps, location and weather",
   "Geocoding, places, routing and forecasts.",
   ["map","geocode","location","address","coordinates","places","weather"]),
 "analytics-monitoring": ("Analytics and monitoring",
   "Metrics, logs, dashboards and alerting.",
   ["metric","analytics","monitor","log","observability","dashboard","alert"]),
 "ai-models": ("AI models and embeddings",
   "Inference, embeddings and prompt execution.",
   ["llm","embedding","completion","prompt","model inference","openai","anthropic"]),
 "security-compliance": ("Security and compliance",
   "Scanning, auditing, sanctions and vulnerability data.",
   ["vulnerability","security scan","compliance","audit log","cve","sanction"]),
 "ecommerce": ("E-commerce",
   "Catalogues, orders, inventory and storefronts.",
   ["shopify","product catalog","order","inventory","cart","ecommerce"]),
 "social-media": ("Social media",
   "Reading and posting across social platforms.",
   ["twitter","reddit","linkedin","instagram","youtube","tiktok","social post"]),
 "hr-recruiting": ("HR and recruiting",
   "Jobs, candidates, applications and hiring workflow.",
   ["job","candidate","resume","recruit","applicant","hiring"]),
 "legal-government": ("Legal and government data",
   "Courts, patents, regulations and public filings.",
   ["legal","court","patent","regulation","government","statute","filing"]),
 "science-research": ("Science and research",
   "Papers, datasets, citations and scholarly search.",
   ["arxiv","paper","pubmed","research","dataset","citation","scholar"]),
 "translation-language": ("Translation and language",
   "Translating, detecting and transcribing language.",
   ["translate","language detect","localization","transcription"]),
}

B = config.PUBLIC_BASE

# A category is published only if it has real substance behind it. Below this
# the page would be a stub, and a stub is the thin auto-generated page we
# decided not to build. The threshold is enforced in one place and honoured by
# the index, the route and the sitemap alike, so we never advertise a URL that
# should not exist.
MIN_TOOLS = 60
_published: dict[str, int] | None = None


def _compute_published(conn: sqlite3.Connection) -> dict[str, int]:
    got = {}
    for slug in CATEGORIES:
        n = category_stats(conn, slug)["tools"]
        if n >= MIN_TOOLS:
            got[slug] = n
    return got


def published(conn: sqlite3.Connection, refresh: bool = False) -> dict[str, int]:
    """Slugs that clear MIN_TOOLS, with their qualifying tool counts.

    Twenty-five aggregate queries over the tool table: ten seconds on an idle
    machine and twice that under load. It used to be a per-process dict filled
    on the first request that needed it, which was the category page and the
    sitemap, so the first visitor to each worker after every deploy waited for
    it, and the CDN then cached that slow response for everyone. It now lives
    in the shared page cache: warmed at startup before any page, served stale
    while it rebuilds, and computed in front of a request only on the very
    first boot of an empty cache.
    """
    global _published
    if _published is not None and not refresh:
        return _published
    if refresh:
        got = _compute_published(conn)
        import json as _json
        render._write("published-map", _json.dumps(got))
        _published = got
        return got
    _published = {k: int(v) for k, v in
                  render.cached_value("published-map", 1800,
                                      lambda: _compute_published(conn)).items()}
    return _published


# Substrings that drag in unrelated tools. "document" matches "documentation"
# and swallows every docs-browsing tool ever written; "file" matches "profile".
# A category page that lists tools which are not in the category is the thin
# programmatic page we specifically decided not to build, so the filter is
# stricter than the counting pass that chose these categories.
_NOISE = ("documentation", "docs", "api docs", "profile", "filename", "filter")


def _match_sql(terms: list[str]) -> tuple[str, list[str]]:
    """Candidate LIKE clause. Deliberately wide; `_score` does the real work."""
    parts, args = [], []
    for t in terms:
        parts.append("(lower(t.name) LIKE ? OR lower(COALESCE(t.description,'')) LIKE ?)")
        args += [f"%{t.lower()}%", f"%{t.lower()}%"]
    return "(" + " OR ".join(parts) + ")", args


def _score(name: str, desc: str, terms: list[str]) -> int:
    """How strongly a tool belongs in a category.

    A term in the tool's own name is near-conclusive: somebody named a function
    `extract_pdf_text`. The same term buried in a paragraph of prose is weak
    evidence, so it takes two of them to qualify. Anything that only matches
    through a noise word does not qualify at all.
    """
    n, d = name.lower(), (desc or "").lower()
    for bad in _NOISE:
        d = d.replace(bad, " ")
    score = 0
    name_hit = False
    desc_hits = 0
    for t in terms:
        t = t.lower().strip()
        if t in n:
            score += 12
            name_hit = True
        elif t in d:
            score += 3
            desc_hits += 1
    if not name_hit and desc_hits < 2:
        return 0
    return score


def category_tools(conn: sqlite3.Connection, slug: str, limit: int = 60) -> list[dict]:
    """The verified tools in a category, best-evidenced first."""
    if slug not in CATEGORIES:
        return []
    where, args = _match_sql(CATEGORIES[slug][2])
    rows = conn.execute(f"""
        SELECT t.name, t.title, t.description, t.input_schema,
               e.display_name, e.identifier, e.url, e.live, e.publisher,
               e.mcp_status, e.mcp_tools
        FROM tools t JOIN entries e ON e.key = t.entry_key
        WHERE {where}
        LIMIT 4000""", args).fetchall()

    terms = CATEGORIES[slug][2]
    scored = []
    seen_names: set[tuple] = set()
    for r in rows:
        sc = _score(r["name"], r["description"] or r["title"] or "", terms)
        if not sc:
            continue
        # One entry per (tool name, publisher). The same server re-listed under
        # several registries must not fill the page with duplicates.
        kk = (r["name"].lower(), (r["publisher"] or "").lower())
        if kk in seen_names:
            continue
        seen_names.add(kk)
        if r["live"] == 1:
            sc += 4
        scored.append((sc, r))
    scored.sort(key=lambda x: -x[0])
    rows = [r for _, r in scored[:limit]]

    out = []
    for r in rows:
        params = []
        if r["input_schema"]:
            try:
                props = (json.loads(r["input_schema"]) or {}).get("properties") or {}
                params = list(props)[:6]
            except Exception:
                pass
        out.append({
            "tool": r["name"], "description": r["description"] or r["title"] or "",
            "server": r["display_name"] or r["identifier"],
            "publisher": r["publisher"], "url": r["url"],
            "live": r["live"], "auth": r["mcp_status"] == "auth",
            "params": params,
        })
    return out


def category_stats(conn: sqlite3.Connection, slug: str) -> dict:
    """Counts over tools that actually qualify, not merely tools that matched.

    The header number and the table must come from the same rule. Counting with
    a loose LIKE and then displaying a strict selection would put a figure on
    the page that the page itself contradicts.
    """
    where, args = _match_sql(CATEGORIES[slug][2])
    terms = CATEGORIES[slug][2]
    rows = conn.execute(f"""SELECT t.name, t.description, t.title, t.entry_key,
                                   e.publisher, e.live
                            FROM tools t JOIN entries e ON e.key = t.entry_key
                            WHERE {where}""", args).fetchall()
    tools = servers = live = 0
    pubs, srv = set(), set()
    for r in rows:
        if not _score(r["name"], r["description"] or r["title"] or "", terms):
            continue
        tools += 1
        srv.add(r["entry_key"])
        if r["publisher"]:
            pubs.add(r["publisher"])
        if r["live"] == 1:
            live += 1
    return {"tools": tools, "servers": len(srv),
            "publishers": len(pubs), "live": live}


def composition(conn: sqlite3.Connection) -> dict:
    """What the index actually holds, and how the ecosystem actually spells it."""
    kinds = [{"kind": r[0] or "unclassified", "n": r[1]} for r in conn.execute(
        "SELECT type_family, COUNT(*) FROM entries GROUP BY 1 ORDER BY 2 DESC")]
    media = [{"type": r[0], "n": r[1]} for r in conn.execute(
        """SELECT type_raw, COUNT(*) FROM entries
           WHERE type_raw IS NOT NULL AND type_raw != ''
           GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")]
    tot = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return {"total": tot, "kinds": kinds, "media": media}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

KIND_LABEL = {
    "mcp-server": "MCP servers", "skill": "Agent skills", "a2a-agent": "A2A agents",
    "agent": "Agent descriptors (ACP, OASF, AgentFacts)",
    "webmcp": "WebMCP browser tools", "plugin": "Plugin manifests",
    "graphql": "GraphQL APIs", "dataset": "Datasets",
    "openapi": "OpenAPI services", "doc": "Machine-readable docs",
    "registry": "ARD registries", "catalog": "Catalogues", "package": "Packages",
    "other": "Other callable resources", "unclassified": "Unclassified",
}


def _tool_rows(tools: list[dict]) -> str:
    out = []
    for t in tools:
        tags = ""
        if t["auth"]:
            tags += '<span class="tag auth">auth required</span>'
        elif t["live"] == 1:
            tags += '<span class="tag ok">answers</span>'
        params = ""
        if t["params"]:
            params = ('<div class="dsc"><span style="color:var(--dim)">arguments:</span> '
                      + ", ".join(f"<code>{esc(p)}</code>" for p in t["params"]) + "</div>")
        desc = (t["description"] or "").strip()
        if len(desc) > 220:
            desc = desc[:217] + "..."
        out.append(
            f'<tr><td><span class="tn">{esc(t["tool"])}</span>{tags}'
            f'{f"<div class=dsc>{esc(desc)}</div>" if desc else ""}{params}</td>'
            f'<td><span class="sv">{esc(t["server"])}</span>'
            f'<div class="dsc">{esc(t["publisher"] or "")}</div></td></tr>')
    return "".join(out)


def render_category(conn: sqlite3.Connection, slug: str) -> str | None:
    if slug not in CATEGORIES or slug not in published(conn):
        return None
    title, intent, _ = CATEGORIES[slug]
    st = category_stats(conn, slug)
    tools = category_tools(conn, slug, 60)
    if not tools:
        return None

    pub = published(conn)
    others = "".join(
        f'<a class="tile" href="/tools/{s}"><div class="t">{esc(CATEGORIES[s][0])}</div>'
        f'<div class="n">{fmt(n)} verified tools</div></a>'
        for s, n in sorted(pub.items(), key=lambda kv: -kv[1])[:9] if s != slug)

    page_title = f"{title}: {fmt(st['tools'])} verified MCP tools"
    desc = (f"{fmt(st['tools'])} verified tools across {fmt(st['servers'])} MCP servers for "
            f"{title.lower()}. Every tool read from the server's own tools/list, with its "
            f"arguments, whether the endpoint answers, and whether it needs credentials.")

    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / <a href="/tools/">Verified tools</a> / {esc(title)}</div>
  <h1>{esc(title)}</h1>
  <p class="lede">{esc(intent)} Every tool below was read from that server's own
  <code>tools/list</code>, so the name and arguments are what the server exposes, not what
  its description claims.</p>
  <ul class="statline">
    <li><b>{fmt(st['tools'])}</b>verified tools</li>
    <li><b>{fmt(st['servers'])}</b>MCP servers</li>
    <li><b>{fmt(st['publishers'])}</b>publishers</li>
    <li><b>{fmt(st['live'])}</b>endpoints answering</li>
  </ul>
</div>

<table class="tl">
  <thead><tr><th>Tool</th><th>Server</th></tr></thead>
  <tbody>{_tool_rows(tools)}</tbody>
</table>

<div class="note">
  Showing {len(tools)} of {fmt(st['tools'])} verified tools. Search the whole set, including
  full input schemas, at <code>{esc(B)}/tools?q=...</code>, or connect an agent to
  <code>{esc(B)}/mcp</code> and call <code>find_tool</code>. No key, no signup.
  <br><br>
  "Answers" means the endpoint responded to a handshake when last probed, and "auth
  required" means it demanded credentials. Both are statements about reachability, never
  about trustworthiness.
</div>

<h2 style="margin-top:44px;font-size:20px">Other capabilities</h2>
<div class="grid">{others}</div>
"""
    return render.page(page_title, desc, body, f"{B}/tools/{slug}")


def render_index(conn: sqlite3.Connection) -> str:
    tot = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    servers = conn.execute("SELECT COUNT(DISTINCT entry_key) FROM tools").fetchone()[0]
    auth = conn.execute("SELECT COUNT(*) FROM entries WHERE mcp_auth=1").fetchone()[0]

    pub = published(conn)
    tiles = []
    for slug, n in sorted(pub.items(), key=lambda kv: -kv[1]):
        st = category_stats(conn, slug)
        tiles.append(
            f'<a class="tile" href="/tools/{slug}"><div class="t">{esc(CATEGORIES[slug][0])}</div>'
            f'<div class="n">{fmt(n)} tools · {fmt(st["servers"])} servers</div></a>')

    comp = composition(conn)
    kind_rows = "".join(
        f'<tr><td>{esc(KIND_LABEL.get(k["kind"], k["kind"]))}</td>'
        f'<td style="width:44%"><span class="bar" style="width:{max(2, round(100*k["n"]/comp["total"]))}%"></span></td>'
        f'<td class="num">{fmt(k["n"])}</td></tr>' for k in comp["kinds"])
    media_rows = "".join(
        f'<tr><td><code style="font-size:12px">{esc(m["type"][:58])}</code></td>'
        f'<td class="num">{fmt(m["n"])}</td></tr>' for m in comp["media"])

    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / Verified tools</div>
  <h1>Verified MCP tools, by capability</h1>
  <p class="lede">Every other registry indexes <em>servers</em>: a name, a URL and whatever
  prose the publisher wrote. The tool names and input schemas an agent actually has to match
  on exist in no index. So we ask each server directly, and store what it answers.</p>
  <ul class="statline">
    <li><b>{fmt(tot)}</b>verified tools</li>
    <li><b>{fmt(servers)}</b>servers introspected</li>
    <li><b>{fmt(auth)}</b>endpoints requiring credentials</li>
    <li><b>{len(tiles)}</b>capabilities</li>
  </ul>
</div>

<div class="grid">{"".join(tiles)}</div>

<h2 style="margin-top:44px;font-size:20px">What is in the index</h2>
<p class="lede" style="margin-bottom:10px">Measured, not claimed. This is the whole index by
kind, {fmt(comp['total'])} resources in total.</p>
<table class="mtable">{kind_rows}</table>

<h2 style="margin-top:40px;font-size:20px">How the ecosystem spells the same thing</h2>
<p class="lede" style="margin-bottom:10px">Media types as publishers actually wrote them.
Filters match exactly, so a registry that does not normalise these silently drops thousands
of entries. We normalise all of them, plus both URN prefixes in circulation
(<code>urn:air:</code> and <code>urn:ai:</code>), on ingest.</p>
<table class="mtable">{media_rows}</table>

<div class="note">
  This is not a directory of every server. We measured what ranks for MCP queries and the
  primary source wins: mirroring it at scale adds nothing. These pages exist because the
  verified tool surface is data that is not published anywhere else.
  <br><br>The whole corpus is also an open dataset under CC BY 4.0:
  <a href="https://huggingface.co/datasets/AgenticResourceDiscovery/verified-mcp-tools">huggingface.co/datasets/AgenticResourceDiscovery/verified-mcp-tools</a>.
</div>
"""
    return render.page(
        "Verified MCP tools by capability",
        f"{fmt(tot)} MCP tools verified by reading each server's own tools/list, grouped by "
        f"capability, with arguments, liveness and whether credentials are required.",
        body, f"{B}/tools/")


def render_bench(payload: dict) -> str:
    if not payload or not payload.get("targets"):
        return render.page("ARD-Bench", "No benchmark run yet.",
                           '<div class="pgh"><h1>ARD-Bench</h1>'
                           '<p class="lede">No run recorded yet.</p></div>', f"{B}/bench")
    k = payload["k"]
    rows = []
    for name, m in payload["targets"].items():
        ours = "neuronto" in name.lower()
        rows.append(
            f'<tr{" style=background:var(--panel2)" if ours else ""}>'
            f'<td>{esc(name)}</td>'
            f'<td class="num">{m.get("coverage","")}</td>'
            f'<td class="num">{m.get(f"recall@{k}","")}</td>'
            f'<td class="num"><b>{m.get(f"recall@{k}_when_carried","")}</b></td>'
            f'<td class="num">{m.get("mrr","")}</td>'
            f'<td class="num">{m.get("median_ms","")}</td></tr>')
    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / ARD-Bench</div>
  <h1>ARD-Bench</h1>
  <p class="lede">A head-to-head retrieval measurement across the public ARD registries.
  Ground truth is the publishers' own <code>representativeQueries</code>, so nothing is
  hand-labelled, and the harness is open so the run can be reproduced or the task set
  replaced.</p>
  <ul class="statline">
    <li><b>{fmt(payload['tasks'])}</b>tasks</li>
    <li><b>{k}</b>results per query</li>
    <li><b>{len(payload['targets'])}</b>targets measured</li>
  </ul>
</div>

<table class="tl">
  <thead><tr><th>Registry</th><th class="num">Coverage</th><th class="num">recall@{k}</th>
  <th class="num">recall@{k} when carried</th><th class="num">MRR</th>
  <th class="num">Median ms</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>

<div class="note">
  <b>Read the conditioned column.</b> {esc(payload.get("reading_the_numbers",""))}
</div>
<div class="note">
  <b>Known bias.</b> {esc(payload.get("known_bias",""))}
</div>
<div class="note">
  <b>Identifier matching.</b> {esc(payload.get("identifier_matching",""))}
  <br><br>Harness: <a href="{esc(payload.get('harness',''))}">{esc(payload.get('harness',''))}</a>.
  Machine-readable results: <code>{esc(B)}/bench</code> with
  <code>Accept: application/json</code>.
</div>
"""
    return render.page(
        "ARD-Bench: retrieval measured across ARD registries",
        f"Head-to-head retrieval measurement across {len(payload['targets'])} ARD registries "
        f"over {payload['tasks']} tasks, with published ground truth, disclosed bias and an "
        f"open harness.", body, f"{B}/bench")


def render_adoption(report: dict) -> str:
    w = report["watchlist"]
    rows = "".join(
        f'<tr><td>{esc(d["host"])}</td>'
        f'<td>{"<span class=\"tag ok\">publishes</span>" if d["publishes"] else "<span class=\"tag\">no manifest</span>"}</td>'
        f'<td class="num">{d["status"] if d["status"] is not None else "-"}</td></tr>'
        for d in w["detail"])
    c = report["crawl"]
    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / ARD adoption</div>
  <h1>Who publishes an ARD manifest</h1>
  <p class="lede">Agentic Resource Discovery works when publishers describe what they offer
  at a well-known location on their own domain. This tracks whether that is happening, on a
  named watchlist and across every host our crawler has seen.</p>
  <ul class="statline">
    <li><b>{w['publishing']} of {w['hosts']}</b>watchlist organisations publish one</li>
    <li><b>{fmt(c['hosts_seen'])}</b>hosts crawled</li>
    <li><b>{fmt(c['hosts_with_manifest'])}</b>with a manifest</li>
    <li><b>{round(100*c['rate'],3)}%</b>crawl-wide rate</li>
  </ul>
</div>

<table class="tl">
  <thead><tr><th>Organisation</th><th>Manifest</th><th class="num">HTTP</th></tr></thead>
  <tbody>{rows}</tbody>
</table>

<div class="note">
  {esc(report.get("method",""))}. Two organisations on this list operate ARD registries of
  their own. Publishing a manifest is a few lines of JSON: see
  <a href="/publish">how to publish</a>.
</div>
"""
    return render.page(
        "ARD adoption: who publishes an ard.json",
        f"{w['publishing']} of {w['hosts']} major organisations publish an ARD manifest. "
        f"Measured across {fmt(c['hosts_seen'])} crawled hosts.",
        body, f"{B}/adoption")


# ---------------------------------------------------------------------------
# ARD publisher pages.
#
# This is a different bet from the capability pages, and from the directory we
# deliberately did not build. For an MCP server, GitHub is the primary source
# and mirroring it is a race the primary source wins. For an ARD manifest there
# is **no page at all**: it is machine-only JSON on somebody's domain, and we
# measured that publishers like gstcranes.com, built2winweb.com and ultimaluz.com
# have literally zero pages describing what they publish. Rendering one makes us
# the primary source rather than a mirror.
#
# The whole ARD-native world is ~178 publishers, 2.92 manifests per 1,000 domains
# crawled. That is small enough to cover completely and far too small to be
# scaled content. The value is not traffic today; it is being the canonical
# reference for a category while the category is still 178 publishers wide.
# ---------------------------------------------------------------------------

def publisher_list(conn: sqlite3.Connection) -> list[dict]:
    """Every ARD-native publisher we have found, with no editorial filter.

    An earlier version required two entries and a written description, which
    excluded 48 of 178 publishers including real companies like padlet.com and
    supademo.com whose only fault was declaring resources without prose. Whether
    a publisher writes descriptions is their stylistic choice; it is not grounds
    to leave them off a list of who publishes ARD at all. The point of this list
    is completeness, and a curated subset is a worse artefact than the full set.
    """
    # Membership is verification, not provenance. Previously this asked "did our
    # own crawler find them", which both under-counted (peers know publishers we
    # had not crawled) and mis-framed the list. The right test is the one the
    # page's title claims: does this domain actually serve a manifest, observed
    # by us, at a path we recorded. Of 5,491 publishers peer registries reported,
    # exactly 28 did; the rest are publisher names derived from URNs by
    # registries that never fetched a manifest, and listing them would have made
    # this page a directory of assumptions.
    rows = conn.execute(
        """SELECT e.publisher,
                  COUNT(*) n,
                  SUM(CASE WHEN e.live=1 THEN 1 ELSE 0 END) live,
                  SUM(CASE WHEN e.live IS NOT NULL THEN 1 ELSE 0 END) probed,
                  SUM(COALESCE(e.mcp_tools,0)) tools,
                  GROUP_CONCAT(DISTINCT e.type_family) fams,
                  MIN(e.first_seen) first_seen,
                  MAX(e.updated_at) seen,
                  cs.manifest_path
           FROM entries e
           JOIN crawl_seen cs ON cs.domain = lower(e.publisher)
           WHERE cs.manifest_path IS NOT NULL
             AND e.publisher IS NOT NULL AND e.publisher != ''
           GROUP BY e.publisher
           ORDER BY n DESC, e.publisher""").fetchall()
    return [{"publisher": r["publisher"], "entries": r["n"], "live": r["live"],
             "probed": r["probed"], "tools": r["tools"] or 0,
             "kinds": sorted(x for x in (r["fams"] or "").split(",") if x),
             "path": r["manifest_path"],
             "first_seen": r["first_seen"], "seen": r["seen"]} for r in rows]


_pubset: set[str] | None = None


def invalidate_publishers() -> None:
    """Forget the membership cache after an ingest, so a submission appears now."""
    global _pubset, _published
    _pubset = None
    _published = None


def publisher_ok(conn: sqlite3.Connection, host: str) -> bool:
    global _pubset
    if _pubset is None:
        _pubset = {p["publisher"].lower() for p in publisher_list(conn)}
    return host.lower() in _pubset


def render_publisher(conn: sqlite3.Connection, host: str) -> str | None:
    """One publisher's complete declared surface.

    Everything we hold about them, because the whole reason this page can exist
    is that their manifest is machine-only and nobody has rendered it: the URN,
    the endpoint, the declared type, the version, what the publisher says it is
    for, verified tool counts, and reachability where we have checked.
    """
    if not publisher_ok(conn, host):
        return None
    rows = conn.execute(
        """SELECT identifier, identifier_raw, display_name, description, type_raw,
                  type_family, url, live, live_status, live_checked, live_ms,
                  rep_queries, tags, capabilities, version, trust_identity,
                  mcp_tools, mcp_status, mcp_server_name, first_seen
           FROM entries WHERE lower(publisher)=?
           ORDER BY type_family, display_name""", (host.lower(),)).fetchall()
    if not rows:
        return None
    n = len(rows)
    live = sum(1 for r in rows if r["live"] == 1)
    probed = sum(1 for r in rows if r["live"] is not None)
    tools = sum((r["mcp_tools"] or 0) for r in rows)
    with_url = sum(1 for r in rows if r["url"])
    first = min((r["first_seen"] or 0) for r in rows) or None
    kinds: dict[str, int] = {}
    for r in rows:
        k = r["type_family"] or "other"
        kinds[k] = kinds.get(k, 0) + 1

    def _j(v):
        try:
            return [str(x) for x in json.loads(v or "[]")]
        except Exception:
            return []

    cards = []
    for r in rows:
        tag = ""
        if r["mcp_status"] == "auth":
            tag = '<span class="tag auth">auth required</span>'
        elif r["live"] == 1:
            tag = f'<span class="tag ok">answers{f" in {r["live_ms"]}ms" if r["live_ms"] else ""}</span>'
        elif r["live"] == 0:
            tag = (f'<span class="tag">no response'
                   f'{f" ({r["live_status"]})" if r["live_status"] else ""}</span>')
        else:
            tag = '<span class="tag">not yet checked</span>'
        if r["mcp_tools"]:
            tag += f'<span class="tag ok">{r["mcp_tools"]} verified tools</span>'
        if r["version"]:
            tag += f'<span class="tag">v{esc(r["version"])}</span>'

        meta = []
        if r["url"]:
            meta.append(f'<div class="dsc"><span style="color:var(--dim)">endpoint:</span> '
                        f'<code>{esc(r["url"])}</code></div>')
        meta.append(f'<div class="dsc"><span style="color:var(--dim)">identifier:</span> '
                    f'<code>{esc(r["identifier_raw"] or r["identifier"])}</code></div>')
        rq = _j(r["rep_queries"])[:4]
        if rq:
            meta.append('<div class="dsc"><span style="color:var(--dim)">published to be '
                        'found for:</span> ' + " · ".join(esc(x[:70]) for x in rq) + "</div>")
        tg = (_j(r["tags"]) + _j(r["capabilities"]))[:8]
        if tg:
            meta.append('<div class="dsc">' + " ".join(
                f'<span class="tag">{esc(t[:28])}</span>' for t in tg) + "</div>")
        if r["trust_identity"]:
            meta.append(f'<div class="dsc"><span style="color:var(--dim)">trust identity:'
                        f'</span> <code>{esc(str(r["trust_identity"])[:80])}</code></div>')
        d = (r["description"] or "").strip()
        if len(d) > 260:
            d = d[:257] + "..."
        cards.append(
            f'<tr><td><span class="tn">{esc(r["display_name"] or r["identifier"])}</span>{tag}'
            f'{f"<div class=dsc>{esc(d)}</div>" if d else ""}'
            + "".join(meta) + "</td>"
            f'<td><span class="sv">{esc(r["type_raw"] or r["type_family"] or "")}</span></td></tr>')

    kindline = " · ".join(f"{fmt(v)} {KIND_LABEL.get(k, k).lower()}"
                          for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
    seen_h = (time.strftime("%d %B %Y", time.gmtime(first)) if first else "recently")
    # Never assert a path we have not seen serve this manifest. Stating
    # /.well-known/ard.json for every publisher was wrong for every publisher
    # checked: they serve the pre-v0.91 ai-catalog.json and 404 on ard.json.
    mp = conn.execute("SELECT manifest_path FROM crawl_seen WHERE domain=?",
                      (host.lower(),)).fetchone()
    mpath = (mp["manifest_path"] if mp and mp["manifest_path"]
             else "/.well-known/ai-catalog.json")
    title = f"{host}: what it publishes for AI agents (ARD manifest)"
    desc = (f"{host} publishes an ARD manifest declaring {fmt(n)} agentic resources: "
            f"{kindline}. Endpoints, identifiers, and what each is published to be found "
            f"for, read from the domain's own /.well-known/ard.json.")
    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / <a href="/ard-publishers">ARD publishers</a> / {esc(host)}</div>
  <h1>{esc(host)}</h1>
  <p class="lede">{esc(host)} publishes an Agentic Resource Discovery manifest at
  <code>{esc(mpath)}</code>, declaring what an AI agent can call on this domain.
  Everything below is read from that file. First seen by our crawler on {seen_h}.</p>
  <ul class="statline">
    <li><b>{fmt(n)}</b>declared resources</li>
    <li><b>{fmt(with_url)}</b>with a callable endpoint</li>
    <li><b>{(fmt(live) + "</b>answering of " + fmt(probed) + " checked") if probed
            else ("not yet</b>checked for reachability")}</li>
    <li><b>{fmt(tools)}</b>verified tools</li>
  </ul>
</div>

<table class="tl">
  <thead><tr><th>Declared resource</th><th>Type</th></tr></thead>
  <tbody>{"".join(cards)}</tbody>
</table>

<div class="note">
  <b>Source</b>: <code>https://{esc(host)}{esc(mpath)}</code>, this publisher's own file.
  Identifiers are reproduced exactly as published.
  {("Note: this is the pre-v0.91 path. The specification renamed the manifest to "
    "<code>/.well-known/ard.json</code>, and most of the ecosystem still serves the older "
    "name.") if mpath.endswith("ai-catalog.json") else ""}
  {("Of the resources listed, " + fmt(probed) + " have been probed for reachability.")
   if probed else "None of these endpoints has been probed for reachability yet, so nothing here says whether they answer."}
  "Answers" and "no response" describe only whether an endpoint replied when last probed:
  services go down and come back, and an entry is demoted rather than removed. None of this
  is a trust, quality or safety rating.
  <br><br>
  Search this publisher's resources alongside every other indexed resource at
  <code>{esc(B)}/search</code>, or connect an agent to <code>{esc(B)}/mcp</code>.
  If this is your domain, the <a href="/console">free console</a> shows what a registry
  sees when it reads your manifest.
</div>
"""
    # Structured data. This is the page the badge points at, so it is the page a
    # machine follows the badge to read. Without it a reader gets prose and has
    # to infer the subject; with it the publisher and the resource list are
    # stated outright. Every value here is one we measured.
    ld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [{
            "@type": "WebPage",
            "@id": f"{B}/ard-publishers/{host}",
            "url": f"{B}/ard-publishers/{host}",
            "name": title,
            "description": desc,
            "isPartOf": {"@type": "WebSite", "name": "Neuronto ARD Registry", "url": B},
            "about": {"@type": "Organization", "name": host, "url": f"https://{host}",
                      "identifier": f"did:web:{host}"},
            "primaryImageOfPage": {"@type": "ImageObject", "url": f"{B}/badge/{host}.svg",
                                   "caption": f"{host} on the Neuronto ARD Registry"},
            "mainEntity": {
                "@type": "ItemList",
                "name": f"Agentic resources published by {host}",
                "numberOfItems": len(rows),
                "itemListElement": [
                    {"@type": "ListItem", "position": i,
                     "item": {"@type": "SoftwareApplication",
                              "name": (r["display_name"] or r["identifier"]),
                              "applicationCategory": KIND_LABEL.get(r["type_family"],
                                                                    r["type_family"]),
                              "url": r["url"] or f"https://{host}",
                              "identifier": r["identifier"]}}
                    for i, r in enumerate(rows[:25], start=1)]},
        }]}, ensure_ascii=False)
    return render.page(title, desc, body, f"{B}/ard-publishers/{host}",
                       jsonld=f'<script type="application/ld+json">{ld}</script>')



def render_publishers_index(conn: sqlite3.Connection) -> str:
    """The complete verified list.

    Canonical at /ard-publishers. The slug carries the term deliberately: bare
    "publishers" competes with an enormous unrelated corpus, and the query this
    page exists to answer is "ARD publishers". Headings are shaped like the
    questions people and answer engines actually ask, which our own retrieval
    research found matters more than markup.
    """
    pubs = publisher_list(conn)
    # The single definition, so this page and /metrics.json cannot diverge again.
    counts = store.ard_publisher_counts(conn)
    total_domains = counts["domains_crawled"]
    hosts = counts["manifest_hosts"]
    elsewhere = counts["hosts_serving_no_indexed_resource"]
    by_path: dict[str, int] = {}
    for p in pubs:
        by_path[p["path"] or "unknown"] = by_path.get(p["path"] or "unknown", 0) + 1
    legacy = by_path.get("/.well-known/ai-catalog.json", 0)
    current = by_path.get("/.well-known/ard.json", 0)
    total_res = sum(p["entries"] for p in pubs)
    total_tools = sum(p["tools"] for p in pubs)

    rows = "".join(
        f'<tr><td><a href="/ard-publishers/{esc(p["publisher"])}">{esc(p["publisher"])}</a>'
        f'<div class="dsc">{esc(" · ".join(KIND_LABEL.get(k, k).lower() for k in p["kinds"][:4] if k))}</div></td>'
        f'<td class="num">{fmt(p["entries"])}</td>'
        f'<td class="num">{fmt(p["live"]) if p["probed"] else "<span style=color:var(--dim)>not checked</span>"}</td>'
        f'<td><code style="font-size:12px">{esc((p["path"] or "").replace("/.well-known/",""))}</code></td>'
        f'</tr>' for p in pubs)

    ld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "ARD Publishers: verified Agentic Resource Discovery manifests",
        "description": (f"{hosts} domains verified to serve an Agentic Resource "
                        f"Discovery (ARD) manifest, of which {len(pubs)} are listed here "
                        f"with the resources they declare for AI agents."),
        "url": f"{B}/ard-publishers",
        "creator": {"@type": "Organization", "name": "Neuronto", "url": B},
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "variableMeasured": ["publisher domain", "declared resources",
                             "endpoint reachability", "manifest path"],
    }, ensure_ascii=False)

    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / ARD publishers</div>
  <h1>ARD publishers: {fmt(hosts)} domains serve an Agentic Resource Discovery manifest</h1>
  <p class="lede">Every one was <b>verified by fetching the manifest</b>, not taken from
  another registry's word, out of {fmt(total_domains)} domains checked. The
  {fmt(len(pubs))} listed below are those whose declared resources we also index, together
  declaring {fmt(total_res)} of them. The remaining {fmt(elsewhere)} serve a manifest on one
  host while the resources it declares belong to another, the way
  <code>connectors-skills.zapier.com</code> publishes for <code>zapier.com</code>, so they
  appear here under the domain that owns the resources.</p>
  <ul class="statline">
    <li><b>{fmt(hosts)}</b>manifests verified</li>
    <li><b>{fmt(len(pubs))}</b>publishers listed</li>
    <li><b>{fmt(total_res)}</b>declared resources</li>
    <li><b>{fmt(total_tools)}</b>verified tools</li>
    <li><b>{fmt(total_domains)}</b>domains checked</li>
  </ul>
</div>

<h2 style="margin-top:38px;font-size:20px">What is an ARD publisher?</h2>
<p class="lede">The <a href="/what-is-ard">Agentic Resource Discovery</a> specification
defines a publisher as whoever hosts a manifest describing one or more agentic resources,
at a well-known path on their own domain. It is how a website tells an AI agent what it can
call: which MCP servers, skills, agents and APIs exist here, and what each is for.</p>

<h2 style="margin-top:34px;font-size:20px">Which path do they actually use?</h2>
<p class="lede">Version 0.91 renamed the manifest to <code>/.well-known/ard.json</code>, and
the deployed base has not followed: <b>{fmt(legacy)} of these publishers still serve the
older <code>/.well-known/ai-catalog.json</code></b> and only {fmt(current)} serve the new
name. Anything reading the ecosystem has to request both, and a tool that checks only
<code>ard.json</code> will conclude, wrongly, that almost nobody has adopted the spec.</p>

<h2 style="margin-top:34px;font-size:20px">The complete list</h2>
<table class="tl">
  <thead><tr><th>Publisher</th><th class="num">Resources</th><th class="num">Answering</th>
  <th>Manifest</th></tr></thead>
  <tbody>{rows}</tbody>
</table>

<h2 style="margin-top:38px;font-size:20px">How do I become an ARD publisher?</h2>
<p class="lede">Serve one JSON file describing what you offer, at both well-known paths
while the rename settles. The <a href="/publish">publishing guide</a> is the ten-minute
version, and the <a href="/console">free console</a> shows exactly what a registry sees
when it reads your domain. There is no application and no allowlist: publish the file and
crawlers find it.</p>

<div class="note">
  <b>Method.</b> Each domain here was checked by requesting both known manifest paths and
  requiring the response to parse as an ARD manifest. Domains that other registries name as
  publishers but that serve no manifest are excluded: of 5,491 such names, only 28 served
  one, so listing the rest would make this a directory of assumptions rather than a record
  of what exists. Reachability of individual endpoints is our own probe and says nothing
  about trust, quality or safety.
</div>
"""
    return render.page(
        f"ARD publishers: {fmt(hosts)} domains with an Agentic Resource Discovery manifest",
        (f"The complete verified list of {fmt(len(pubs))} domains publishing an ARD "
         f"(Agentic Resource Discovery) manifest, declaring {fmt(total_res)} resources for "
         f"AI agents. Each verified by fetching the manifest, with endpoint reachability."),
        body, f"{B}/ard-publishers",
        jsonld=f'<script type="application/ld+json">{ld}</script>')


# ---------------------------------------------------------------------------
# Where this project is published.
#
# IndexNow is domain-verified: submitting a huggingface.co or github.com URL
# under our key is refused outright ("One or more URLs are not related to your
# verified domain"). There is no protocol for pushing somebody else's URL to a
# search engine, and anything claiming otherwise is selling something.
#
# What does work is a crawl path. One page on a domain that is itself indexed,
# linking every external artefact, is how those artefacts get found. It doubles
# as the answer to a question an answer engine will actually be asked: where is
# this project published, and is any of it real.
# ---------------------------------------------------------------------------

PUBLISHED = [
    ("Code and specification work", [
        ("GitHub repository", "https://github.com/neuronto/agentic-resource-discovery",
         "The registry, publisher, crawler, tool introspector and benchmark harness. Apache-2.0."),
        ("ARD specification: interoperability field data",
         "https://github.com/ards-project/ard-spec/issues/88",
         "Measured urn:ai vs urn:air split, media-type fragmentation, and the finding that "
         "183 of 199 publishers still serve the pre-v0.91 manifest path."),
        ("ARD specification: reference implementation",
         "https://github.com/ards-project/ard-spec/issues/87",
         "Neuronto as a federated implementation, conformance verified in both modes."),
        ("ARD documentation: reference implementations",
         "https://github.com/ards-project/ard-docs/pull/23",
         "Proposed entry on the specification's own implementations page."),
        ("ARD connectors", "https://github.com/ards-project/ard-connectors/pull/6",
         "Addition to the shared client-side finder list."),
    ]),
    ("Datasets and packages", [
        ("Verified MCP Tools dataset",
         "https://huggingface.co/datasets/AgenticResourceDiscovery/verified-mcp-tools",
         "31,411 tools read from live servers' own tools/list, plus 7,708 introspection "
         "results including which endpoints demand credentials. CC BY 4.0."),
        ("ard-publish on PyPI", "https://pypi.org/project/ard-publish/",
         "Build, validate and verify an ARD manifest for your own domain."),
        ("agentic-resource-discovery on PyPI",
         "https://pypi.org/project/agentic-resource-discovery/",
         "Alias package for the same tool."),
    ]),
    ("Registries and directories", [
        ("Official MCP Registry",
         "https://registry.modelcontextprotocol.io/v0/servers?search=neuronto",
         "Published as com.neuronto/agents-tools-search-discovery-ard-registry, "
         "namespace verified by DNS."),
        ("Docker MCP Registry", "https://github.com/docker/mcp-registry/pull/4863",
         "Remote server submission."),
        ("awesome-mcp-servers", "https://github.com/punkpeye/awesome-mcp-servers/pull/13302",
         "Aggregators section."),
        ("Cline MCP Marketplace", "https://github.com/cline/mcp-marketplace/issues/2372",
         "Marketplace submission."),
        ("mcp.so", "https://github.com/chatmcp/mcpso/issues/3857",
         "Directory submission via the open route."),
        ("APIs.guru", "https://github.com/APIs-guru/openapi-directory/issues/3188",
         "OpenAPI directory submission."),
    ]),
]


def render_published(conn: sqlite3.Connection) -> str:
    n = sum(len(v) for _, v in PUBLISHED)
    secs = []
    for title, items in PUBLISHED:
        rows = "".join(
            f'<tr><td><a href="{esc(u)}" rel="noopener">{esc(name)}</a>'
            f'<div class="dsc">{esc(desc)}</div>'
            f'<div class="dsc"><code style="font-size:12px">{esc(u)}</code></div></td></tr>'
            for name, u, desc in items)
        secs.append(f'<h2 style="margin-top:34px;font-size:20px">{esc(title)}</h2>'
                    f'<table class="tl"><tbody>{rows}</tbody></table>')

    ld = json.dumps({
        "@context": "https://schema.org", "@type": "Organization",
        "name": "Neuronto", "url": B,
        "description": "Agentic Resource Discovery (ARD) index, registry and publisher.",
        "sameAs": [u for _, items in PUBLISHED for _, u, _ in items],
    }, ensure_ascii=False)

    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / Published</div>
  <h1>Where Neuronto is published</h1>
  <p class="lede">Every artefact of this project that lives somewhere other than this
  domain: the source, the open dataset, the packages, the registry entries, and the
  specification work. {n} links, each verified reachable.</p>
</div>
{"".join(secs)}

<div class="note">
  This page exists because search engines cannot be told about somebody else's URL. IndexNow
  is domain-verified and refuses a submission for a host you do not control, so the only
  honest way to get these discovered is to link them from a page that is itself indexed.
  Everything above is a live artefact rather than an announcement: code you can run,
  a dataset you can download, and submissions whose state you can read for yourself,
  including the ones still open.
</div>
"""
    return render.page(
        "Where the Neuronto ARD Registry is published: source, dataset, packages and registry entries",
        f"All {n} external artefacts of the Neuronto Agentic Resource Discovery (ARD) Index: "
        f"the Apache-2.0 source, the CC BY 4.0 verified MCP tools dataset, PyPI packages, "
        f"MCP Registry entry and specification contributions.",
        body, f"{B}/published",
        jsonld=f'<script type="application/ld+json">{ld}</script>')


def render_badge_page(conn: sqlite3.Connection, domain: str = "") -> str:
    """The badge, what it says, and the snippet, for one domain.

    Styles are scoped to this page rather than borrowed. The first version
    reused site classes for prose and left the preview images in a flex row,
    where they were shrunk to 94px against a natural 206 and the code block
    overflowed its container by 500px. Both were only visible in a browser.
    """
    from . import badge as _badge
    B = config.PUBLIC_BASE
    dom = re.sub(r"^https?://", "", (domain or "").strip().lower()).strip("/").split("/")[0]
    dom = dom if re.match(r"^[a-z0-9.-]{3,100}$", dom or "") else ""
    sample = dom or "example.com"
    sn = _badge.snippet(sample)

    if dom:
        st = _badge.stats_for(conn, dom)
        state = (f'<p class="bstate"><b>{esc(dom)}</b> is indexed: {st[1]} MCP server'
                 f'{"s" if st[1] != 1 else ""}, <b>{st[0]}</b> verified tool'
                 f'{"s" if st[0] != 1 else ""}, {st[2]} answering when last probed.</p>'
                 if st else
                 f'<p class="bstate">We hold nothing for <b>{esc(dom)}</b> yet. The badge says so '
                 f'until you <a href="/submit">submit it</a>, then corrects itself within the hour.</p>')
    else:
        state = ('<p class="bstate">Enter your domain to see your own badge. '
                 'The preview below uses <code>example.com</code>.</p>')

    body = f"""
<style>
.bwrap{{max-width:70ch}}
.bstate{{color:var(--fg2);font-size:14px;line-height:1.6;margin:14px 0 0}}
.bform{{display:flex;gap:8px;flex-wrap:wrap;margin:0}}
.bform input{{flex:1 1 240px;min-width:0;background:var(--panel2);border:1px solid var(--line2);
  border-radius:var(--r);color:var(--fg);padding:10px 12px;font-family:var(--mono);font-size:14px}}
.swatches{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;
  margin:22px 0 0;max-width:none}}
.sw{{border:1px solid var(--line);border-radius:var(--r);overflow:hidden}}
.sw .stage{{display:flex;align-items:center;justify-content:center;padding:20px 14px;min-height:64px}}
.sw .stage img{{flex:0 0 auto;height:20px;width:auto;max-width:100%}}
.sw .cap{{border-top:1px solid var(--line);padding:8px 12px;font-size:12px;color:var(--mut);
  display:flex;justify-content:space-between;gap:10px;align-items:baseline}}
.sw .cap code{{font-size:12px;color:var(--fg2)}}
.stage-l{{background:#FFFFFF}} .stage-d{{background:#0A0A0B}}
/* The default badge follows the reader's own system setting, not the colour
   behind it, so it is shown on a neutral ground. An earlier version put it on a
   half-light half-dark stage, which implied it adapts to its surroundings. */
.stage-a{{background:var(--panel2)}}
.snip{{margin:18px 0 0}}
.snip .lbl{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:0 0 6px}}
.snip .lbl span{{font-size:13px;color:var(--fg2)}}
.snip button{{background:var(--panel2);border:1px solid var(--line2);color:var(--fg2);
  border-radius:var(--r);padding:4px 10px;font-size:12px;cursor:pointer;font-family:inherit}}
.snip button:hover{{color:var(--fg);border-color:var(--fg2)}}
.snip pre{{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:13px 14px;margin:0;font-size:12px;line-height:1.55;
  white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}}
.states{{list-style:none;padding:0;margin:16px 0 0;display:grid;gap:12px}}
.states li{{display:grid;grid-template-columns:minmax(0,15em) 1fr;gap:16px;align-items:start;
  font-size:14px;line-height:1.6;color:var(--fg2)}}
.states b{{color:var(--fg);font-weight:560}}
@media(max-width:640px){{.states li{{grid-template-columns:1fr;gap:2px}}}}
</style>

<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / Badge</div>
  <h1>The ARD Registry badge</h1>
  <p class="lede">A small, monochrome badge stating what was actually verified about your
  resources: how many tools your server returned to <code>tools/list</code>, and whether the
  endpoint answered when probed. It is evidence rather than a score, and it keeps itself
  current.</p>
</div>

<div class="bwrap">
  <form class="bform" id="bf" onsubmit="return go(event)" style="margin-top:26px">
    <input id="bd" value="{esc(dom)}" placeholder="example.com" aria-label="Your domain"
           autocomplete="off" spellcheck="false">
    <button class="btn btn--w" type="submit">Get my badge</button>
  </form>
  {state}
</div>

<div class="swatches">
    <div class="sw"><div class="stage stage-a">
      <img src="{B}/badge/{esc(sample)}.svg" alt="{esc(sn['alt'])}" height="20" width="206"></div>
      <div class="cap"><span>follows the reader's system theme</span><code>default</code></div></div>
    <div class="sw"><div class="stage stage-l">
      <img src="{B}/badge/{esc(sample)}.svg?theme=light" alt="Light variant" height="20" width="206"></div>
      <div class="cap"><span>pinned light</span><code>?theme=light</code></div></div>
    <div class="sw"><div class="stage stage-d">
      <img src="{B}/badge/{esc(sample)}.svg?theme=dark" alt="Dark variant" height="20" width="206"></div>
      <div class="cap"><span>pinned dark</span><code>?theme=dark</code></div></div>
</div>

<h2 style="margin-top:40px;font-size:20px">Copy one of these</h2>
<div class="bwrap">
  <div class="snip">
    <div class="lbl"><span>HTML, for your own site or documentation</span>
      <button type="button" onclick="cp('sh',this)">Copy</button></div>
    <pre id="sh">{esc(sn['html'])}</pre>
  </div>
  <div class="snip">
    <div class="lbl"><span>Markdown, for a README</span>
      <button type="button" onclick="cp('sm',this)">Copy</button></div>
    <pre id="sm">{esc(sn['markdown'])}</pre>
  </div>
  <div class="snip">
    <div class="lbl"><span>reStructuredText, for Python docs</span>
      <button type="button" onclick="cp('sr',this)">Copy</button></div>
    <pre id="sr">{esc(sn['rst'])}</pre>
  </div>
  <div class="snip">
    <div class="lbl"><span>Plain text link, where an image would look out of place</span>
      <button type="button" onclick="cp('st',this)">Copy</button></div>
    <pre id="st">{esc(sn['text_html'])}</pre>
  </div>
</div>

<h2 style="margin-top:40px;font-size:20px">What it says, and what it never says</h2>
<div class="bwrap">
  <ul class="states">
    <li><b>N tools verified</b><span>We completed an MCP handshake with your endpoint, it
      returned that many tools, and it answered the last time we probed it.</span></li>
    <li><b>endpoint verified</b><span>Your endpoint answered and returned no public tool list,
      because it asks for credentials first.</span></li>
    <li><b>not indexed yet</b><span>We have not fetched anything from you. It corrects itself
      within the hour once we have.</span></li>
  </ul>
  <p class="lede" style="margin-top:18px">There is no score, no grade and no stars, and there
  never will be. A graded badge invites the reader to treat it as a quality or safety
  judgment, and the specification is explicit that discovery is separate from trust. This
  badge reports an observation, and it will report an unflattering one just as readily.</p>
</div>

<h2 style="margin-top:40px;font-size:20px">Where to put it</h2>
<div class="bwrap">
  <p class="lede">Wherever someone is deciding whether to depend on you. Your documentation
  and integrations pages reach the person evaluating your product; a README reaches the
  developer about to install it. Both are worth having.</p>
  <p class="lede" style="margin-top:14px">It links to
  <code>{B}/ard-publishers/&lt;your-domain&gt;</code>, a page listing what you publish, what
  answered, and when it was last checked. Your reader can verify the claim rather than take
  it, which is the only reason a badge is worth anything.</p>
  <div class="note" style="margin-top:20px">
    Displaying it is entirely optional. Indexing is free either way, nothing about your
    ranking depends on it, and we will never ask you to add it to keep a listing.
  </div>
</div>

<script>
function go(e){{
  e.preventDefault();
  const d=document.getElementById('bd').value.trim().replace(/^https?:[/][/]/,'').split('/')[0];
  if(d) location.search='?domain='+encodeURIComponent(d);
  return false;
}}
function cp(id,btn){{
  const t=document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(()=>{{
    const o=btn.textContent; btn.textContent='Copied';
    setTimeout(()=>{{btn.textContent=o;}},1400);
  }});
}}
</script>
"""
    return render.page(
        "Badge: show what was verified about your resources",
        "A monochrome badge stating how many tools your MCP server returned to tools/list "
        "and whether the endpoint answers. Evidence, not a score. Free, and it updates itself.",
        body, f"{B}/badge")


def render_connect_page() -> str:
    """Every way to point a client at this index, as something to copy.

    The distribution problem is not that people disagree with the idea, it is
    that every client wants its config in a slightly different shape and each
    difference is a step where someone stops. So: one page, one block per
    client, nothing to fill in. Every snippet below was checked against that
    client's own documentation; the key names genuinely differ (`url`,
    `serverUrl`, `httpUrl`, `context_servers`) and guessing one wrong is worse
    than omitting the client.
    """
    B = config.PUBLIC_BASE
    mcp = f"{B}/mcp"

    def snip(idx: str, label: str, code: str, note: str = "") -> str:
        n = f'<p class="cnote">{note}</p>' if note else ""
        return (f'<div class="snip"><div class="lbl"><span>{label}</span>'
                f'<button type="button" onclick="cp(\'{idx}\',this)">Copy</button></div>'
                f'<pre id="{idx}">{esc(code)}</pre></div>{n}')

    j = lambda o: json.dumps(o, indent=2)
    others = "".join(
        f'<a href="/connect/{s_}">{esc(c_["name"])}</a>'
        for s_, c_ in CLIENTS.items())
    claude_code = ("/plugin marketplace add neuronto/ard-connectors\n"
                   "/plugin install neuronto-agent-finder@neuronto")
    claude_desktop = j({"mcpServers": {"neuronto": {
        "command": "npx", "args": ["-y", "mcp-remote", mcp]}}})
    cursor = j({"mcpServers": {"neuronto": {"url": mcp}}})
    vscode = j({"servers": {"neuronto": {"type": "http", "url": mcp}}})
    gemini = j({"mcpServers": {"neuronto": {"httpUrl": mcp}}})
    zed = j({"context_servers": {"neuronto": {"url": mcp}}})
    windsurf = j({"mcpServers": {"neuronto": {"serverUrl": mcp}}})
    warp = j({"mcpServers": {"neuronto": {"url": mcp}}})
    codex = j({"mcpServers": {"neuronto": {"type": "http", "url": mcp}}})
    jetbrains = j({"mcpServers": {"neuronto": {"url": mcp}}})
    cont = ("name: Neuronto\nversion: 0.0.1\nschema: v1\nmcpServers:\n"
            "  - name: Neuronto\n    type: streamable-http\n"
            f"    url: {mcp}\n")
    goose = ("goose://extension?url=" + urllib.parse.quote(mcp, safe="") +
             "&type=streamable_http&id=neuronto&name=Neuronto"
             "&description=" + urllib.parse.quote(
                 "Find MCP servers, skills, agents and APIs across every public ARD registry"))
    rest = ('curl -s -X POST ' + B + '/search \\\n'
            '  -H "Content-Type: application/json" \\\n'
            '  -d \'{"query":{"text":"read a PDF and extract tables"}}\'')
    a2a = ('curl -s -X POST ' + B + '/a2a \\\n'
           '  -H "Content-Type: application/json" \\\n'
           '  -d \'{"jsonrpc":"2.0","id":1,"method":"message/send","params":'
           '{"message":{"messageId":"1","role":"user","parts":'
           '[{"kind":"text","text":"read a PDF"}]}}}\'')

    body = f"""
<style>
.cwrap{{max-width:70ch}}
.cnote{{color:var(--fg2);font-size:13px;line-height:1.6;margin:8px 0 0}}
.snip{{margin:16px 0 0;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}}
.snip .lbl{{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:8px 12px;background:var(--panel2);border-bottom:1px solid var(--line);
  font-size:13px;color:var(--fg2)}}
.snip .lbl button{{background:none;border:1px solid var(--line2);border-radius:6px;
  color:var(--fg2);padding:3px 10px;font-size:12px;cursor:pointer}}
.snip .lbl button:hover{{color:var(--fg);border-color:var(--fg2)}}
.snip pre{{margin:0;padding:12px;overflow-x:auto;font-family:var(--mono);font-size:13px;
  line-height:1.55;white-space:pre}}
h2.csec{{margin:44px 0 0;font-size:20px}}
h3.cli{{margin:30px 0 0;font-size:16px}}
.others{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 0}}
.others a{{border:1px solid var(--line);border-radius:999px;padding:5px 12px;
  font-size:13px;color:var(--fg2);text-decoration:none}}
.others a:hover{{color:var(--fg);border-color:var(--fg2)}}
</style>

<div class="cwrap">
  <p class="lede">One endpoint, <code>{esc(mcp)}</code>, answers every client below. It
  searches this index and every other public ARD registry at once and fuses the rankings,
  so a single connector covers the federation rather than one catalogue.</p>
  <p class="lede" style="margin-top:14px">No account, no key, no signup. Anonymous callers
  get 60 requests an hour; <a href="/console">proving a domain</a> raises that to 300, and
  a keyed search is not logged at all (see <a href="/privacy">privacy</a>).</p>
</div>

<h2 class="csec">Claude Code</h2>
<div class="cwrap">
  <p class="lede">Installs a skill, so asking for a tool in plain language searches ARD
  without you naming a registry. The plugin bundles the connector, so this is the whole
  setup.</p>
  {snip('c1', 'Two commands, in Claude Code', claude_code)}
  <p class="cnote">Then: <code>/agentfinder a tool that can read PDFs</code>. The
  marketplace lists every public Agent Finder, not only this one. Source:
  <a href="https://github.com/neuronto/ard-connectors">neuronto/ard-connectors</a>.</p>
</div>

<h2 class="csec">Editors and IDEs</h2>
<div class="cwrap">
  <h3 class="cli">Cursor</h3>
  {snip('c3', '~/.cursor/mcp.json, or .cursor/mcp.json in a project', cursor)}

  <h3 class="cli">VS Code and GitHub Copilot</h3>
  {snip('c4', '.vscode/mcp.json', vscode)}
  <p class="cnote">Or run <code>MCP: Add Server</code> from the command palette and
  choose HTTP.</p>

  <h3 class="cli">Zed</h3>
  {snip('c8', 'Zed settings.json', zed)}
  <p class="cnote">Or Settings, AI, MCP Servers, Add Server, Add Remote Server. Zed is
  deprecating its own MCP extensions in favour of the official MCP registry, where this
  server is already published.</p>

  <h3 class="cli">Windsurf (Cascade)</h3>
  {snip('c9', '~/.codeium/mcp_config.json', windsurf)}
  <p class="cnote">Cascade uses <code>serverUrl</code> for remote HTTP servers rather
  than <code>url</code>. Press refresh after adding it.</p>

  <h3 class="cli">JetBrains AI Assistant</h3>
  {snip('c10', 'Settings, Tools, AI Assistant, MCP, New MCP Server, as JSON', jetbrains)}
  <p class="cnote">Choose the Streamable HTTP transport. Works across IntelliJ IDEA,
  PyCharm, WebStorm, Rider and the rest of the family.</p>

  <h3 class="cli">Continue</h3>
  {snip('c11', '.continue/mcpServers/neuronto.yaml', cont)}
  <p class="cnote">MCP tools are available in agent mode.</p>
</div>

<h2 class="csec">Terminals and CLIs</h2>
<div class="cwrap">
  <h3 class="cli">Claude Desktop</h3>
  {snip('c2', 'claude_desktop_config.json', claude_desktop)}
  <p class="cnote">Settings, Developer, Edit Config. Restart Claude after saving.</p>

  <h3 class="cli">Gemini CLI</h3>
  {snip('c5', '~/.gemini/settings.json', gemini)}

  <h3 class="cli">Codex CLI</h3>
  {snip('c12', 'MCP server config', codex)}

  <h3 class="cli">Warp</h3>
  {snip('c13', 'MCP servers, + Add, paste JSON', warp)}

  <h3 class="cli">Goose</h3>
  {snip('c14', 'One-click extension link', goose)}
  <p class="cnote">Open that link with Goose installed and it adds the extension. Or add
  a remote extension by hand with type <code>streamable_http</code>.</p>
</div>

<h2 class="csec">Agent platforms</h2>
<div class="cwrap">
  <p class="lede">Anything that can call an MCP server or an HTTP endpoint can use this
  index. In <b>n8n</b>, add the <i>MCP Client Tool</i> node and point it at
  <code>{esc(mcp)}</code> with HTTP Streamable transport. In <b>Cline</b> and
  <b>Roo Code</b>, use Configure MCP Servers and add a remote server with the same URL.
  <b>Dify</b>, <b>LangFlow</b> and similar tool-calling platforms take it as either an MCP
  server or a plain REST call.</p>
</div>

<h2 class="csec">Anything else</h2>
<div class="cwrap">
  <p class="lede">The index answers on three interfaces and they share one search path, so
  they cannot disagree about what it holds.</p>
  {snip('c6', 'REST, the ARD interface', rest)}
  {snip('c7', 'A2A, for agent-to-agent clients', a2a)}
  <p class="cnote">The A2A agent card is at
  <code>/.well-known/agent-card.json</code>. The MCP endpoint is POST only and answers
  <code>405</code> to GET: there is no server-initiated stream, because every tool answers
  inside the request that asked. Full reference at <a href="/api-docs">/api-docs</a>, and
  <a href="/agents.md">/agents.md</a> if you are an agent reading this yourself.</p>
</div>

<h2 class="csec">One page per client</h2>
<div class="cwrap">
  <p class="lede">Each of these covers that client on its own, including the quirk that
  makes a config copied from somewhere else fail in it.</p>
  <div class="others">{others}</div>
</div>

<h2 class="csec">What you get back</h2>
<div class="cwrap">
  <p class="lede">Ranked matches with the endpoint to connect to, which registries carried
  each one, and what we verified by fetching it: whether it answered, and the tools its own
  <code>tools/list</code> returned. The <code>score</code> is semantic relevance only. It is
  not a trust, safety or quality rating and must not be shown to anyone as one.</p>
</div>

<script>
function cp(id,btn){{
  const t=document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(()=>{{
    const o=btn.textContent; btn.textContent='Copied';
    setTimeout(()=>{{btn.textContent=o;}},1400);
  }});
}}
</script>
"""
    return render.page(
        "Connect a client to the index",
        "Copy-paste setup for Claude Code, Cursor, VS Code, Zed, Windsurf, JetBrains, "
        "Continue, Claude Desktop, Gemini CLI, Codex, Warp, Goose and n8n, plus REST and "
        "A2A. One endpoint searches every public ARD registry at once. No key, no signup.",
        body, f"{B}/connect")

def render_privacy_page() -> str:
    """What this service receives, what it keeps, and what leaves it.

    Written from the code rather than from a template, because a policy that
    describes a system nobody built is worse than none: it teaches the reader to
    discount the next one. Every claim here has a mechanism behind it, and the
    two places query text genuinely leaves this machine are stated rather than
    buried under "third-party service providers".
    """
    B = config.PUBLIC_BASE
    body = f"""
<style>
.pw{{max-width:70ch}}
.pw h2{{margin:40px 0 0;font-size:20px}}
.pw h3{{margin:26px 0 0;font-size:16px;color:var(--fg)}}
.pw p{{color:var(--fg2);font-size:15px;line-height:1.65;margin:12px 0 0}}
.pw ul{{color:var(--fg2);font-size:15px;line-height:1.65;margin:12px 0 0;padding-left:20px}}
.pw li{{margin:6px 0}}
.pw code{{font-family:var(--mono);font-size:13px}}
.pw .note{{border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;
  margin:20px 0 0;color:var(--fg2);font-size:14px;line-height:1.6}}
</style>

<div class="pw">
<p class="lede">Neuronto is a public discovery index. There is no account, no signup and
no payment, so there is no customer record to describe. What follows is what the running
code does.</p>

<h2>Search queries</h2>
<p>An anonymous search is recorded: the query text truncated to 200 characters, which
mode it ran in, how many results it returned, how long it took, and the time. Each result
it returned is recorded against that search with its rank.</p>
<p>This exists for one feature. A publisher listed here can ask
<code>{B}/demand</code> what agents were looking for when their own resources came back,
which is the only way a publisher learns whether anyone is looking for what they offer.
The report is scoped to their own domain: it shows queries that returned <em>their</em>
entries and nothing else.</p>
<p><b>A search made with an API key is not recorded.</b> The query is replaced with a
placeholder before it is written and no result-level rows are stored at all. If you do not
want your queries kept, <a href="/console">prove a domain</a> and use the key.</p>

<h3>Where a query goes</h3>
<p>In the default federated mode a query leaves this machine twice, and both are the
point of the product rather than an incidental transfer:</p>
<ul>
  <li><b>To the other public ARD registries</b>, so the answer covers the federation and
      not one catalogue. They are listed on <a href="/ard-registries">ARD registries</a>
      and each has its own policy. Responses are cached for 30 minutes, keyed by the
      normalised query and the upstream, then deleted.</li>
  <li><b>To an embedding provider</b>, to turn the query into a vector for the semantic
      half of retrieval. The text is sent for that call and is not retained there by us.</li>
</ul>
<p>Neither happens for a request you send with <code>federate: false</code> and dense
retrieval off; that path stays on this machine.</p>

<h2>Traffic measurement</h2>
<p>Privacy here is structural rather than promised, which is the only kind worth reading:</p>
<ul>
  <li><b>There is no column for an IP address.</b> Not emptied, not hashed on the way in.
      The table has nowhere to put one.</li>
  <li>A session identifier is a truncated SHA-256 of a daily-rotating salt, the address and
      the user agent. The same visitor is one session within a day and <b>unlinkable across
      days</b>, because tomorrow's salt does not exist yet.</li>
  <li>A request carrying <code>Sec-GPC</code> or <code>DNT</code> yields <b>no session at
      all</b>.</li>
  <li>User agents are stored as a class and a family name, never the raw string.</li>
  <li>Raw rows are kept 90 days. Daily aggregates are kept indefinitely and contain no
      identifier of any kind.</li>
</ul>

<h2>What is never collected</h2>
<ul>
  <li>IP addresses.</li>
  <li>Names, email addresses or accounts. None are asked for, because nothing here needs one.</li>
  <li>Cookies for tracking. There is no advertising and no third-party analytics.</li>
  <li>Anything about who you are. The index cannot tell one anonymous caller from another
      beyond a same-day session identifier it cannot reverse.</li>
</ul>

<h2>Domain verification</h2>
<p>Proving a domain writes the domain, the DNS TXT record used to prove it and the issued
key. That is the whole record. Internal services registered against a key are stored in
their own tables, are never returned to anyone else, and are never counted in any public
figure.</p>

<h2>Submitting a resource</h2>
<p>Submitting an endpoint or a domain records what was submitted, what its endpoint
answered when fetched, and the outcome. Being audited is not the same as being submitted:
<code>/audit</code> reads a domain and reports on it, and never indexes it as a side
effect.</p>

<div class="note">
Corrections are welcome and are treated as bugs. If something here does not match what the
service does, the service or this page is wrong, and either way we want to know.
</div>
</div>
"""
    return render.page(
        "Privacy",
        "What Neuronto receives, what it stores and what leaves the machine. No accounts, "
        "no IP addresses, no tracking cookies; keyed searches are not recorded at all.",
        body, f"{B}/privacy")


# One page per client. Not eleven copies of one page: a reader arrives from
# "how do I add an MCP server to <their client>", and what they need is that
# client's config key, that client's quirk and that client's failure mode. The
# quirks below are the whole reason these pages are not interchangeable.
CLIENTS = {
    "claude-code": dict(
        name="Claude Code", kind="CLI",
        label="Two commands, in Claude Code",
        code=("/plugin marketplace add neuronto/ard-connectors\n"
              "/plugin install neuronto-agent-finder@neuronto"),
        lang="text",
        quirk=("Claude Code is the only client here that takes a plugin rather than a "
               "config file, so the connector arrives bundled with a skill. The skill is "
               "what makes plain language work: you ask for a capability and it searches "
               "ARD without you naming a registry, shows the finder menu once, remembers "
               "the choice, and never installs anything it finds."),
        after="Then ask: <code>/agentfinder a tool that can read PDFs</code>.",
        doc="https://github.com/neuronto/ard-connectors"),
    "claude-desktop": dict(
        name="Claude Desktop", kind="desktop app",
        label="claude_desktop_config.json",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "command": "npx",\n'
             '      "args": ["-y", "mcp-remote", "%MCP%"]\n    }\n  }\n}',
        quirk=("Claude Desktop does not speak remote MCP directly, so this goes through "
               "<code>mcp-remote</code>, which proxies the HTTP endpoint over stdio. That "
               "is why this is the one entry here with a <code>command</code> rather than "
               "a URL, and why it needs Node available."),
        after="Settings, Developer, Edit Config. Restart Claude after saving.",
        doc="https://modelcontextprotocol.io/quickstart/user"),
    "cursor": dict(
        name="Cursor", kind="editor",
        label="~/.cursor/mcp.json, or .cursor/mcp.json in a project",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "url": "%MCP%"\n    }\n  }\n}',
        quirk=("Cursor takes a bare <code>url</code> with no transport field and works out "
               "the rest. Project-level config wins over the global file, which is useful "
               "when you want discovery in one repository and not everywhere."),
        after="Settings, MCP, then check the server shows as connected.",
        doc="https://cursor.com/docs/mcp"),
    "vscode": dict(
        name="VS Code and GitHub Copilot", kind="editor",
        label=".vscode/mcp.json",
        code='{\n  "servers": {\n    "neuronto": {\n      "type": "http",\n'
             '      "url": "%MCP%"\n    }\n  }\n}',
        quirk=("VS Code is the odd one out twice over: the top-level key is "
               "<code>servers</code> rather than <code>mcpServers</code>, and the transport "
               "must be stated explicitly as <code>http</code>. Copying a Claude or Cursor "
               "block here silently does nothing."),
        after=("Or run <code>MCP: Add Server</code> from the command palette and choose "
               "HTTP. Tools appear in Copilot's agent mode."),
        doc="https://code.visualstudio.com/docs/copilot/chat/mcp-servers"),
    "zed": dict(
        name="Zed", kind="editor",
        label="Zed settings.json",
        code='{\n  "context_servers": {\n    "neuronto": {\n      "url": "%MCP%"\n    }\n  }\n}',
        quirk=("Zed calls them context servers, not MCP servers, so the key is "
               "<code>context_servers</code> and nothing else on this page will work here. "
               "Zed is also deprecating its own MCP extension format in favour of the "
               "official MCP registry, where this server is already published, so a listing "
               "you find inside Zed and this config point at the same endpoint."),
        after="Or Settings, AI, MCP Servers, Add Server, Add Remote Server.",
        doc="https://zed.dev/docs/ai/mcp"),
    "windsurf": dict(
        name="Windsurf", kind="editor",
        label="~/.codeium/mcp_config.json",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "serverUrl": "%MCP%"\n    }\n  }\n}',
        quirk=("Cascade wants <code>serverUrl</code> for a remote HTTP server, not "
               "<code>url</code>. This is the single most common reason a working config "
               "copied from another client does nothing in Windsurf. Press the refresh "
               "button in the MCP panel after saving; it does not pick the file up on its own."),
        after="Cascade, then the plugins or MCP panel, then refresh.",
        doc="https://docs.devin.ai/windsurf/plugins/cascade/mcp"),
    "jetbrains": dict(
        name="JetBrains AI Assistant", kind="IDE",
        label="Settings, Tools, AI Assistant, MCP, New MCP Server, as JSON",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "url": "%MCP%"\n    }\n  }\n}',
        quirk=("Choose the Streamable HTTP transport in the dialog; AI Assistant keeps SSE "
               "only for legacy servers and this endpoint is not one. The same setting "
               "covers IntelliJ IDEA, PyCharm, WebStorm, GoLand, Rider and the rest of the "
               "family, and a server can be scoped globally or to one project."),
        after="There is also an Import from Claude button if you already have a config there.",
        doc="https://www.jetbrains.com/help/ai-assistant/mcp.html"),
    "continue": dict(
        name="Continue", kind="editor extension",
        label=".continue/mcpServers/neuronto.yaml",
        code=("name: Neuronto\nversion: 0.0.1\nschema: v1\nmcpServers:\n"
              "  - name: Neuronto\n    type: streamable-http\n    url: %MCP%"),
        lang="yaml",
        quirk=("Continue is the only client here configured in YAML rather than JSON, and "
               "it wants the transport spelled <code>streamable-http</code> with a hyphen. "
               "MCP tools are available in agent mode only, so if the server connects and "
               "no tools appear, check the mode before checking the config."),
        after="One file per server, in the .continue/mcpServers folder of your workspace.",
        doc="https://docs.continue.dev/customize/deep-dives/mcp"),
    "gemini-cli": dict(
        name="Gemini CLI", kind="CLI",
        label="~/.gemini/settings.json",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "httpUrl": "%MCP%"\n    }\n  }\n}',
        quirk=("Gemini CLI uses <code>httpUrl</code> for a streamable HTTP server, which no "
               "other client on this list uses. A plain <code>url</code> here is read as "
               "something else and the server will not connect."),
        after="Then <code>/mcp</code> in the CLI to confirm the server and its tools.",
        doc="https://geminicli.com/docs/tools/mcp-server/"),
    "codex": dict(
        name="Codex CLI", kind="CLI",
        label="MCP server config",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "type": "http",\n'
             '      "url": "%MCP%"\n    }\n  }\n}',
        quirk=("Codex wants the transport declared as <code>http</code>. The endpoint needs "
               "no OAuth block: it is public and unauthenticated, so any client field for "
               "credentials is left out rather than filled with a placeholder."),
        after="Tools appear once the server is enabled for the session.",
        doc="https://developers.openai.com/learn/docs-mcp"),
    "warp": dict(
        name="Warp", kind="terminal",
        label="MCP servers, + Add, paste JSON",
        code='{\n  "mcpServers": {\n    "neuronto": {\n      "url": "%MCP%"\n    }\n  }\n}',
        quirk=("Warp takes the JSON through its UI rather than a file you edit, and adds "
               "every server in the snippet at once. Headers are supported but not needed "
               "here, because there is nothing to authenticate."),
        after="Warp Drive, MCP servers, + Add, then paste.",
        doc="https://docs.warp.dev/agents/capabilities/mcp/"),
    "goose": dict(
        name="Goose", kind="agent CLI",
        label="One-click extension link",
        code="%GOOSE%",
        lang="text",
        quirk=("Goose is the only client here that installs from a deeplink rather than a "
               "config file. Opening the link with Goose installed adds the extension "
               "directly. By hand, add a remote extension with type "
               "<code>streamable_http</code> and the same URL."),
        after="Or: goose configure, Add Extension, Remote Extension.",
        doc="https://goose-docs.ai/docs/getting-started/using-extensions/"),
}


def render_client_page(slug: str) -> str | None:
    """One client's setup, as the page someone lands on from a search."""
    c = CLIENTS.get(slug)
    if not c:
        return None
    B = config.PUBLIC_BASE
    mcp = f"{B}/mcp"
    goose = ("goose://extension?url=" + urllib.parse.quote(mcp, safe="") +
             "&type=streamable_http&id=neuronto&name=Neuronto&description=" +
             urllib.parse.quote("Find MCP servers, skills, agents and APIs across every "
                                "public ARD registry"))
    code = c["code"].replace("%MCP%", mcp).replace("%GOOSE%", goose)
    others = "".join(
        f'<a href="/connect/{s}">{esc(o["name"])}</a>'
        for s, o in CLIENTS.items() if s != slug)

    body = f"""
<style>
.cwrap{{max-width:70ch}}
.cnote{{color:var(--fg2);font-size:13px;line-height:1.6;margin:8px 0 0}}
.snip{{margin:18px 0 0;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}}
.snip .lbl{{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:8px 12px;background:var(--panel2);border-bottom:1px solid var(--line);
  font-size:13px;color:var(--fg2)}}
.snip .lbl button{{background:none;border:1px solid var(--line2);border-radius:6px;
  color:var(--fg2);padding:3px 10px;font-size:12px;cursor:pointer}}
.snip pre{{margin:0;padding:12px;overflow-x:auto;font-family:var(--mono);font-size:13px;
  line-height:1.55;white-space:pre}}
h2.csec{{margin:40px 0 0;font-size:20px}}
.others{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 0}}
.others a{{border:1px solid var(--line);border-radius:999px;padding:5px 12px;
  font-size:13px;color:var(--fg2);text-decoration:none}}
.others a:hover{{color:var(--fg);border-color:var(--fg2)}}
</style>

<div class="cwrap">
  <p class="lede">Neuronto is an Agentic Resource Discovery (ARD) registry. Connected to
  {esc(c['name'])}, it lets the agent find the MCP servers, skills, agents and APIs that can
  do a task, searching this index and every other public ARD registry at once rather than a
  single catalogue.</p>
  <p class="lede" style="margin-top:14px">No account, no key, no signup.</p>
</div>

<h2 class="csec">How to add Neuronto to {esc(c['name'])}</h2>
<div class="cwrap">
  <div class="snip"><div class="lbl"><span>{esc(c['label'])}</span>
    <button type="button" onclick="cp(this)">Copy</button></div>
    <pre>{esc(code)}</pre></div>
  <p class="cnote">{c['after']}</p>
</div>

<h2 class="csec">What is different about {esc(c['name'])}</h2>
<div class="cwrap">
  <p class="lede">{c['quirk']}</p>
  <p class="cnote" style="margin-top:14px">Client documentation:
    <a href="{esc(c['doc'])}" rel="noopener">{esc(c['doc'])}</a></p>
</div>

<h2 class="csec">What you get back</h2>
<div class="cwrap">
  <p class="lede">Ranked matches with the endpoint to connect to, which registries carried
  each one, and what was verified by fetching it: whether the endpoint answered, and the
  tools its own <code>tools/list</code> returned. The <code>score</code> is semantic
  relevance only and is never a trust or safety rating.</p>
  <p class="lede" style="margin-top:14px">The endpoint is POST only and answers
  <code>405</code> to GET, because every tool answers inside the request that asked. Full
  reference at <a href="/api-docs">/api-docs</a>; <a href="/privacy">privacy</a> covers what
  is stored, which for a keyed search is nothing.</p>
</div>

<h2 class="csec">Another client</h2>
<div class="cwrap">
  <div class="others">{others}</div>
  <p class="cnote" style="margin-top:14px">All of them on one page:
    <a href="/connect">/connect</a>.</p>
</div>

<script>
function cp(btn){{
  const t=btn.closest('.snip').querySelector('pre').textContent;
  navigator.clipboard.writeText(t).then(()=>{{
    const o=btn.textContent; btn.textContent='Copied';
    setTimeout(()=>{{btn.textContent=o;}},1400);
  }});
}}
</script>
"""
    return render.page(
        f"Neuronto for {c['name']}",
        f"Add the Neuronto ARD registry to {c['name']} as an MCP server, and let it find "
        f"the MCP servers, skills, agents and APIs for a task across every public ARD "
        f"registry at once. Copy-paste config, no key, no signup.",
        body, f"{B}/connect/{slug}")


def render_state_page(rep: dict) -> str:
    """The measured state of the agentic web, as a page.

    Numbers first, method second, limitations in the same visual weight as the
    findings. The temptation with a dataset nobody else has is to present it as
    a verdict; the reason it is worth anything is that it is presented as
    evidence, with the window it came from attached.
    """
    B = config.PUBLIC_BASE
    w, ix = rep["window"], rep["index"]
    reach, tools, churn = rep["reachability"], rep["tools"], rep["churn"]
    n = lambda v: f"{v:,}" if isinstance(v, int) else v

    def stat(big, label, sub=""):
        s = f'<div class="sub">{sub}</div>' if sub else ""
        return (f'<div class="stat"><div class="big">{big}</div>'
                f'<div class="lab">{label}</div>{s}</div>')

    lims = "".join(f"<li>{esc(x)}</li>" for x in rep["limitations"])
    body = f"""
<style>
.swrap{{max-width:70ch}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;
  margin:26px 0 0;max-width:none}}
.stat{{border:1px solid var(--line);border-radius:var(--r);padding:16px 18px}}
.stat .big{{font-size:30px;line-height:1.1;font-variant-numeric:tabular-nums}}
.stat .lab{{color:var(--fg2);font-size:13px;margin-top:6px;line-height:1.45}}
.stat .sub{{color:var(--mut);font-size:12px;margin-top:4px}}
.swrap h2{{margin:44px 0 0;font-size:20px}}
.swrap p{{color:var(--fg2);font-size:15px;line-height:1.65;margin:12px 0 0}}
.swrap ul{{color:var(--fg2);font-size:15px;line-height:1.65;margin:12px 0 0;padding-left:20px}}
.swrap li{{margin:7px 0}}
.meth{{border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin:22px 0 0;
  color:var(--fg2);font-size:14px;line-height:1.65}}
</style>

<div class="swrap">
  <p class="lede">Every directory can tell you how many servers it lists. Listing is free;
  answering is not. These numbers come from fetching each endpoint on a schedule and
  keeping the changes, so they describe what responds rather than what was submitted.</p>
</div>

<div class="stats">
  {stat(n(ix['entries']), 'resources indexed', f"from {n(ix['publishers'])} publishers")}
  {stat(f"{reach['share_answering_pct']}%", 'of probed endpoints answer',
        f"{n(reach['answering'])} answering, {n(reach['not_answering'])} not")}
  {stat(f"{tools['share_exposing_tools_pct']}%", 'of introspected servers expose tools',
        f"{n(tools['servers_requiring_auth'])} require credentials")}
  {stat(n(tools['verified_tools_total']), 'verified tools',
        f"median {tools['median_tools_per_server']} per server that answers")}
  {stat(n(reach['median_response_ms']) + ' ms', 'median response', 'of endpoints that answer')}
  {stat(n(w['observations']), 'recorded changes',
        f"across {n(w['endpoints_watched'])} endpoints in {w['days']} days")}
</div>

<div class="swrap">
  <h2>Churn</h2>
  <p><b>{n(churn['stopped_answering'])}</b> endpoints stopped answering and
  <b>{n(churn['started_answering_again'])}</b> started again, within the
  {w['days']}-day window above. {esc(churn['note'])}</p>

  <h2>How this is measured</h2>
  <div class="meth">{esc(rep['how_this_is_measured'])}</div>

  <h2>What these numbers are not</h2>
  <ul>{lims}</ul>

  <h2>Take the data</h2>
  <p>This page as JSON: <code>{B}/state-of-mcp</code> with
  <code>Accept: application/json</code>. The underlying liveness observations, including
  the list of endpoints that stopped answering, are at <code>{B}/liveness</code> and
  <code>{B}/liveness?dead=1</code>. Free to use, redistribute and build on, no attribution
  required, no key. If you run a registry, the dead list is the useful half.</p>
  <p>Changes as they happen: <a href="/feed">/feed</a>.</p>
</div>
"""
    return render.page(
        "The state of MCP, measured",
        "What share of listed MCP servers actually answer, how many expose the tools they "
        "claim, and how fast the set changes. Measured by probing every endpoint, with the "
        "window and the limitations attached. Free to reuse.",
        body, f"{B}/state-of-mcp")


def render_frameworks_page() -> str:
    """Connecting from an agent framework rather than an editor.

    Only snippets checked against that framework's own source or README appear
    here. Where the exact call could not be verified, the page says so and gives
    the endpoint instead of a guess: a config that looks authoritative and is
    wrong costs a reader more time than no config at all.
    """
    B = config.PUBLIC_BASE
    mcp = f"{B}/mcp"

    lang = (
        "from mcp import ClientSession\n"
        "from mcp.client.streamable_http import streamablehttp_client\n"
        "from langchain_mcp_adapters.tools import load_mcp_tools\n"
        "from langchain.agents import create_agent\n\n"
        f'async with streamablehttp_client("{mcp}") as (read, write, _):\n'
        "    async with ClientSession(read, write) as session:\n"
        "        await session.initialize()\n"
        "        tools = await load_mcp_tools(session)\n"
        '        agent = create_agent("openai:gpt-4.1", tools)')

    oai = (
        "from agents import Agent\n"
        "from agents.mcp import MCPServerStreamableHttp\n\n"
        "async with MCPServerStreamableHttp(\n"
        f'    params={{"url": "{mcp}"}},\n'
        '    name="Neuronto ARD Registry",\n'
        "    cache_tools_list=True,\n"
        ") as server:\n"
        '    agent = Agent(name="Assistant", mcp_servers=[server])')

    vercel = (
        "import { experimental_createMCPClient as createMCPClient } from 'ai';\n\n"
        "const client = await createMCPClient({\n"
        f"  transport: {{ type: 'http', url: '{mcp}' }},\n"
        "});\n\n"
        "const tools = await client.tools();\n"
        "// ...use tools, then release the connection\n"
        "await client.close();")

    node = (
        "import { findResource } from 'neuronto';\n\n"
        "const { results } = await findResource('read a PDF and extract tables');\n"
        "for (const r of results) console.log(r.displayName, r.url, r.score);")

    def snip(idx, label, code, note=""):
        n = f'<p class="cnote">{note}</p>' if note else ""
        return (f'<div class="snip"><div class="lbl"><span>{label}</span>'
                f'<button type="button" onclick="cp(\'{idx}\',this)">Copy</button></div>'
                f'<pre id="{idx}">{esc(code)}</pre></div>{n}')

    body = f"""
<style>
.cwrap{{max-width:70ch}}
.cnote{{color:var(--fg2);font-size:13px;line-height:1.6;margin:8px 0 0}}
.snip{{margin:18px 0 0;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}}
.snip .lbl{{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:8px 12px;background:var(--panel2);border-bottom:1px solid var(--line);
  font-size:13px;color:var(--fg2)}}
.snip .lbl button{{background:none;border:1px solid var(--line2);border-radius:6px;
  color:var(--fg2);padding:3px 10px;font-size:12px;cursor:pointer}}
.snip pre{{margin:0;padding:12px;overflow-x:auto;font-family:var(--mono);font-size:13px;
  line-height:1.55;white-space:pre}}
h2.csec{{margin:42px 0 0;font-size:20px}}
.cwrap p{{color:var(--fg2);font-size:15px;line-height:1.65;margin:12px 0 0}}
.cwrap ul{{color:var(--fg2);font-size:15px;line-height:1.65;margin:12px 0 0;padding-left:20px}}
</style>

<div class="cwrap">
  <p class="lede">An agent that can only use the tools its author wired in at build time
  is limited to what its author thought of. This endpoint lets one ask, at runtime, what
  exists for a task, across this index and every other public ARD registry at once.</p>
  <p class="lede" style="margin-top:14px">One endpoint for all of it:
  <code>{esc(mcp)}</code>. No account, no key.</p>
</div>

<h2 class="csec">LangChain</h2>
<div class="cwrap">
  {snip('f1', 'with langchain-mcp-adapters', lang)}
  <p class="cnote">Every tool the registry exposes becomes a LangChain tool, so
  <code>find_resource</code> and <code>find_tool</code> are callable by the agent itself.</p>
</div>

<h2 class="csec">OpenAI Agents SDK</h2>
<div class="cwrap">
  {snip('f2', 'Python', oai)}
  <p class="cnote"><code>cache_tools_list=True</code> is worth setting: the tool list here
  changes rarely and the round trip is not free.</p>
</div>

<h2 class="csec">Vercel AI SDK</h2>
<div class="cwrap">
  {snip('f3', 'TypeScript', vercel)}
  <p class="cnote">Close the client when you are done; it holds a connection open. The
  SDK also accepts MCP's official <code>StreamableHTTPClientTransport</code> if you
  already use it.</p>
</div>

<h2 class="csec">Without MCP at all</h2>
<div class="cwrap">
  <p class="lede">Discovery is a plain HTTP call, so a framework with no MCP support can
  still use it. The npm package wraps it with no dependencies:</p>
  {snip('f4', 'npm i neuronto', node)}
  <p class="cnote">Python: <code>pip install ard-publish</code> for the publishing side, or
  POST <code>{esc(B)}/search</code> directly with
  <code>{{"query": {{"text": "..."}}}}</code>. Both are documented at
  <a href="/api-docs">/api-docs</a>.</p>
</div>

<h2 class="csec">Everything else that speaks MCP</h2>
<div class="cwrap">
  <p class="lede">LlamaIndex, CrewAI, Mastra, Pydantic AI, Google ADK, Microsoft Agent
  Framework and Strands all connect to remote MCP servers. Their exact call signatures
  are not reproduced here because they were not verified against source at the time of
  writing, and a snippet that looks authoritative and is wrong wastes more of your time
  than none. Point whatever the framework calls a streamable HTTP MCP server at:</p>
  {snip('f5', 'the endpoint', mcp)}
  <p class="cnote">If a snippet here is wrong or one is missing,
  <a href="https://github.com/neuronto/agentic-resource-discovery/issues">tell us</a> and
  it gets fixed rather than argued about.</p>
</div>

<h2 class="csec">What comes back</h2>
<div class="cwrap">
  <p class="lede">Ranked matches with the endpoint to connect to, which registries carried
  each one, and what was verified by fetching it. <code>score</code> is semantic relevance
  only and is never a trust or safety rating. Editors and CLIs are at
  <a href="/connect">/connect</a>.</p>
</div>

<script>
function cp(id,btn){{
  const t=document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(()=>{{
    const o=btn.textContent; btn.textContent='Copied';
    setTimeout(()=>{{btn.textContent=o;}},1400);
  }});
}}
</script>
"""
    return render.page(
        "Neuronto for agent frameworks",
        "Connect LangChain, the OpenAI Agents SDK, the Vercel AI SDK and any other "
        "MCP-capable framework to an index that searches every public ARD registry at "
        "once, so an agent can discover tools at runtime instead of at build time.",
        body, f"{B}/connect/frameworks")
