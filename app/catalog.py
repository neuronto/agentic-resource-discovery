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

from . import config, render
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


def sitemap_urls(conn: sqlite3.Connection) -> list[str]:
    """Only pages that actually exist. Never advertise a URL that 404s."""
    return ([f"{B}/tools/"] + [f"{B}/tools/{s}" for s in published(conn)] +
            [f"{B}/publishers/"] +
            [f"{B}/publishers/{p['publisher']}" for p in publisher_list(conn)] +
            [f"{B}/bench", f"{B}/adoption"])


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
    rows = conn.execute(
        """SELECT publisher,
                  COUNT(*) n,
                  SUM(CASE WHEN live=1 THEN 1 ELSE 0 END) live,
                  SUM(CASE WHEN live IS NOT NULL THEN 1 ELSE 0 END) probed,
                  SUM(COALESCE(mcp_tools,0)) tools,
                  GROUP_CONCAT(DISTINCT type_family) fams,
                  MIN(first_seen) first_seen,
                  MAX(updated_at) seen
           FROM entries
           WHERE sources LIKE '%crawl%' AND publisher IS NOT NULL AND publisher != ''
             AND publisher NOT LIKE '%modelcontextprotocol%'
           GROUP BY publisher
           ORDER BY n DESC, publisher""").fetchall()
    return [{"publisher": r["publisher"], "entries": r["n"], "live": r["live"],
             "probed": r["probed"], "tools": r["tools"] or 0,
             "kinds": sorted(x for x in (r["fams"] or "").split(",") if x),
             "first_seen": r["first_seen"], "seen": r["seen"]} for r in rows]


_pubset: set[str] | None = None


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
    title = f"{host}: what it publishes for AI agents (ARD manifest)"
    desc = (f"{host} publishes an ARD manifest declaring {fmt(n)} agentic resources: "
            f"{kindline}. Endpoints, identifiers, and what each is published to be found "
            f"for, read from the domain's own /.well-known/ard.json.")
    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / <a href="/publishers/">ARD publishers</a> / {esc(host)}</div>
  <h1>{esc(host)}</h1>
  <p class="lede">{esc(host)} publishes an Agentic Resource Discovery manifest at
  <code>/.well-known/ard.json</code>, declaring what an AI agent can call on this domain.
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
  <b>Source</b>: <code>https://{esc(host)}/.well-known/ard.json</code>, this publisher's own
  file. Identifiers are reproduced exactly as published.
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
    return render.page(title, desc, body, f"{B}/publishers/{host}")



def render_publishers_index(conn: sqlite3.Connection) -> str:
    pubs = publisher_list(conn)
    total_domains = conn.execute("SELECT COUNT(*) FROM crawl_seen").fetchone()[0]
    with_manifest = conn.execute(
        "SELECT COUNT(*) FROM crawl_seen WHERE entries > 0").fetchone()[0]
    rows = "".join(
        f'<tr><td><a href="/publishers/{esc(p["publisher"])}">{esc(p["publisher"])}</a>'
        f'<div class="dsc">{esc(" · ".join(KIND_LABEL.get(k, k).lower() for k in p["kinds"][:4] if k))}</div></td>'
        f'<td class="num">{fmt(p["entries"])}</td>'
        f'<td class="num">{fmt(p["live"]) if p["probed"] else "<span style=color:var(--dim)>not checked</span>"}</td></tr>'
        for p in pubs)
    body = f"""
<div class="pgh">
  <div class="crumb"><a href="/">Index</a> / ARD publishers</div>
  <h1>Who publishes an ARD manifest</h1>
  <p class="lede">An ARD manifest is a file at <code>/.well-known/ard.json</code> in which a
  domain declares what an AI agent can call on it. It is machine-readable and, for almost
  every publisher below, described nowhere else in human-readable form. These pages are that
  description.</p>
  <ul class="statline">
    <li><b>{fmt(len(pubs))}</b>publishers listed</li>
    <li><b>{fmt(with_manifest)}</b>manifests found</li>
    <li><b>{fmt(total_domains)}</b>domains crawled</li>
    <li><b>{round(1000*with_manifest/max(total_domains,1), 2)}</b>manifests per 1,000 domains</li>
  </ul>
</div>

<table class="tl">
  <thead><tr><th>Publisher</th><th class="num">Resources</th><th class="num">Answering</th></tr></thead>
  <tbody>{rows}</tbody>
</table>

<div class="note">
  Adoption is early: {round(100*with_manifest/max(total_domains,1), 2)}% of the domains we
  crawled serve a manifest. That is the honest state of the specification today, and it is
  tracked over time on the <a href="/adoption">adoption page</a>.
  Publishing one is a few lines of JSON: see <a href="/publish">how to publish</a>.
</div>
"""
    return render.page(
        "ARD publishers: who serves an ard.json",
        f"{fmt(len(pubs))} domains publishing an Agentic Resource Discovery manifest, "
        f"what each declares for AI agents, and whether the endpoints answer.",
        body, f"{B}/publishers/")
