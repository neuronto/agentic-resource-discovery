"""What the index derives: capabilities, publishers, API vendors and connect clients.

Data only, computed from the index and served as JSON or read by a deployment's
pages; nothing here writes HTML. Categories are not invented: each was scored
against the whole corpus of verified tools before being published, and only those
with real volume survive, so no category is a stub.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse

from . import config, pagecache, store

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
    # One pass for every category, and the counts it produced are stored for the
    # pages too, so the index and each category page quote the same numbers.
    stats = _category_scan(conn)
    pagecache._write("category-stats", json.dumps(stats))
    return {slug: s["tools"] for slug, s in stats.items() if s["tools"] >= MIN_TOOLS}


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
        pagecache._write("published-map", _json.dumps(got))
        _published = got
        return got
    _published = {k: int(v) for k, v in
                  pagecache.cached_value("published-map", 1800,
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


def _category_scan(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every category's counts from one pass over the tool table.

    `category_stats` used to run one LIKE scan per category. There are 26
    categories, so the published map took 13.7 s and /tools 15.2 s to build,
    measured 2026-09-10, every time the cache went stale. The rows are the same
    rows for every category and `_score` is plain Python, so they are read once
    and scored against all of them. The prefilter mirrors `_match_sql` exactly
    (a term in the name or the description), then `_score` decides, so the
    counts are the ones the per-category query produced.
    """
    cats = [(slug, [t.lower() for t in CATEGORIES[slug][2]], CATEGORIES[slug][2])
            for slug in CATEGORIES]
    acc = {slug: [0, set(), set(), 0] for slug in CATEGORIES}
    rows = conn.execute("""SELECT t.name, t.description, t.title, t.entry_key,
                                  e.publisher, e.live
                           FROM tools t JOIN entries e ON e.key = t.entry_key""")
    for name, description, title, entry_key, pub, live in rows:
        name = name or ""
        ln, ld = name.lower(), (description or "").lower()
        for slug, low, terms in cats:
            if not any(t in ln or t in ld for t in low):
                continue
            if not _score(name, description or title or "", terms):
                continue
            a = acc[slug]
            a[0] += 1
            a[1].add(entry_key)
            if pub:
                a[2].add(pub)
            if live == 1:
                a[3] += 1
    return {slug: {"tools": a[0], "servers": len(a[1]), "publishers": len(a[2]), "live": a[3]}
            for slug, a in acc.items()}


def category_stats(conn: sqlite3.Connection, slug: str) -> dict:
    """Counts over tools that actually qualify, served from the one-pass map."""
    try:
        stats = pagecache.cached_value("category-stats", 1800, lambda: _category_scan(conn))
        got = stats.get(slug) if isinstance(stats, dict) else None
        if got is not None:
            return got
    except Exception:
        pass
    return _category_stats_sql(conn, slug)


def _category_stats_sql(conn: sqlite3.Connection, slug: str) -> dict:
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
_mcpset: dict[str, str] | None = None


def invalidate_publishers() -> None:
    """Forget the membership caches after an ingest, so a submission appears now."""
    global _pubset, _mcpset, _published
    _pubset = None
    _mcpset = None
    _published = None


def publisher_basis(conn: sqlite3.Connection, host: str) -> str | None:
    """How we verified this publisher, or None if we did not.

    Two things count and both are things we OBSERVED: we fetched a manifest from
    the domain, or we completed an MCP handshake against one of its endpoints
    and counted the tools it returned. A publisher name that a peer registry
    derived from a URN without ever fetching anything is not an observation,
    which is why 4,555 of 6,716 known names still resolve to no page.

    The index at /ard-publishers keeps the narrower manifest test, because that
    is what its title claims. This is the per-host test, and a host that
    answered a handshake for us has earned a page describing that handshake.
    """
    h = (host or "").lower()
    global _pubset, _mcpset
    if _pubset is None:
        _pubset = {p["publisher"].lower() for p in publisher_list(conn)}
    if h in _pubset:
        return "manifest"
    if _mcpset is None:
        _mcpset = {}
        for pub, tools, st in conn.execute(
            """SELECT publisher, MAX(COALESCE(mcp_tools, 0)),
                      GROUP_CONCAT(DISTINCT mcp_status)
               FROM entries
               WHERE mcp_checked IS NOT NULL AND mcp_status IN ('ok', 'auth')
                 AND publisher IS NOT NULL AND publisher != ''
               GROUP BY publisher"""):
            if not pub:
                continue
            # tools we actually enumerated beats an endpoint that only told us
            # it wants credentials, when a publisher has both.
            _mcpset[pub.lower()] = "mcp" if (tools or 0) > 0 else "auth"
    return _mcpset.get(h)


def publisher_ok(conn: sqlite3.Connection, host: str) -> bool:
    return publisher_basis(conn, host) is not None


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


# ---------------------------------------------------------------------------
# Vendor pages: /api/{host}
#
# A stranger asked for these by URL, five ways in one session (/api/stripe.com,
# /apis/stripe.com, /resource/stripe.com, /entry/urn:air:stripe.com:...), which
# is a stronger signal than any plan we could have written for them. They exist
# for the 2,509 API vendors whose indexing produced zero crawlable pages.
#
# The doorway objection, and the gate that answers it. 2,509 templated pages
# built from a 2023 corpus is the pattern our own LP rules forbid. So: a page
# only for a host with at least MIN_OPS documented operations (325 hosts; the
# same discipline as MIN_TOOLS for categories), and a page carries ONLY what was
# observed or what the vendor wrote: their own description, their operations
# with method, path, base URL and auth, whether the endpoint answered our
# probes, and where the specification came from and how old that corpus is,
# stated plainly. No prose of ours describing the vendor, no rating, no proof.
# ---------------------------------------------------------------------------

MIN_OPS = 20
_SHOW_SPECS = 12      # specifications rendered per host (azure.com has 653)
_SHOW_OPS = 40        # operations rendered per specification
_SHOW_OPS_TOTAL = 240 # operations rendered per page (amazonaws.com has 11,829)
_CORPUS_NEWEST = "2023-04-21"


def vendor_hosts(conn: sqlite3.Connection) -> list[dict]:
    """Hosts that clear MIN_OPS, with counts. Cached: 325 rows from a join over
    95k tools is not request-path work."""
    from . import pagecache as _r

    def build():
        rows = conn.execute(
            """SELECT e.publisher AS host,
                      COUNT(t.id) AS ops,
                      COUNT(DISTINCT e.key) AS specs,
                      MAX(CASE WHEN e.live=1 THEN 1 ELSE 0 END) AS live,
                      MAX(COALESCE(e.probe_n,0)) AS probes,
                      MAX(e.display_name) AS name
               FROM entries e JOIN tools t ON t.entry_key = e.key
               WHERE e.type_family='openapi' AND e.publisher IS NOT NULL AND e.publisher != ''
               GROUP BY e.publisher HAVING ops >= ?
               ORDER BY ops DESC""", (MIN_OPS,)).fetchall()
        return [dict(r) for r in rows]
    return _r.cached_value("vendor-hosts", 1800, build)


def _vendor_ok(conn: sqlite3.Connection, host: str) -> bool:
    h = (host or "").lower().strip()
    return bool(h) and any(v["host"] == h for v in vendor_hosts(conn))


def _op_rows(conn: sqlite3.Connection, key: str, limit: int) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT name, description, input_schema FROM tools WHERE entry_key=? LIMIT ?",
            (key, limit)):
        inv, auth = {}, []
        try:
            sch = json.loads(r["input_schema"] or "{}")
            inv = sch.get("invoke") or {}
            auth = sch.get("auth") or []
        except Exception:
            pass
        out.append({"name": r["name"], "summary": (r["description"] or "").strip(),
                    "method": str(inv.get("method") or "").upper(),
                    "path": inv.get("path") or "", "servers": inv.get("servers") or [],
                    "auth": auth})
    return out


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()
