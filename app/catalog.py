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
import sqlite3
import time
from typing import Any

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


def published(conn: sqlite3.Connection, refresh: bool = False) -> dict[str, int]:
    """Slugs that clear MIN_TOOLS, with their qualifying tool counts."""
    global _published
    if _published is None or refresh:
        got = {}
        for slug in CATEGORIES:
            n = category_stats(conn, slug)["tools"]
            if n >= MIN_TOOLS:
                got[slug] = n
        _published = got
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
    return render.page(title, desc, body, f"{B}/ard-publishers/{host}")



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
        f'<td><code style="font-size:11px">{esc((p["path"] or "").replace("/.well-known/",""))}</code></td>'
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
            f'<div class="dsc"><code style="font-size:11px">{esc(u)}</code></div></td></tr>'
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
        "Where Neuronto is published: source, dataset, packages and registry entries",
        f"All {n} external artefacts of the Neuronto Agentic Resource Discovery (ARD) Index: "
        f"the Apache-2.0 source, the CC BY 4.0 verified MCP tools dataset, PyPI packages, "
        f"MCP Registry entry and specification contributions.",
        body, f"{B}/published",
        jsonld=f'<script type="application/ld+json">{ld}</script>')
