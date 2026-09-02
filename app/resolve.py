"""Find every MCP endpoint behind whatever a publisher pasted.

The submit path used to do exactly one thing per door. `{"endpoint": url}`
POSTed an initialize to that one URL and, if it did not answer, refused.
`{"domain": host}` fetched the ARD manifest and, if there was none, refused.
Neither door consulted the discovery documents our own crawler already reads,
the `/mcp` convention every MCP server follows, or the index we were about to
refuse on behalf of.

The cost was paid by our first verified third-party publisher. They submitted
`https://www.ikeytz.com`; their nginx answered 405, correctly, because you
cannot POST to a homepage; we refused it and queued two days of retries. At that
moment `https://www.ikeytz.com/mcp`, with 45 tools, had been in this index for
eight hours, ingested from the official MCP Registry, and their own
`/.well-known/mcp/server-card.json`, which names that endpoint, had been read by
our crawler six hours earlier. We held the answer twice over and made them guess.

That is the wrong shape for a discovery service. Resolving underspecified intent
into a concrete callable is the entire product; refusing it at our own front
door is the product contradicting itself. So this module is what both doors now
share: given anything about a host, find everything callable on it.

Candidates come from four places, cheapest first, and every one is probed with
the same handshake the sweep uses, so nothing is indexed on a guess:

  1. what we already hold for that host, which costs one SQL query;
  2. the URL as submitted, if it has a path worth trying;
  3. the host's own discovery documents: the MCP server card, which names its
     endpoint outright, and the ARD manifest, which lists its servers;
  4. the conventional paths, `/mcp` first, because that is where they are.

Bounded on purpose: the candidate set is deduplicated and capped, every fetch
has the crawl timeout, and all network happens before any write. A refusal
carries the evidence from every probe, so "we tried these seven things and here
is what each returned" replaces "no".
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import urllib.parse
from typing import Any, Awaitable, Callable

import httpx

from . import config

# Where a host tells the world about its MCP server. The server card is the
# strongest signal: it names the endpoint URL outright in `remotes[].url`. The
# manifest paths are the ARD ones the crawler already reads.
DISCOVERY = (
    "/.well-known/mcp/server-card.json",
    "/.well-known/ard.json",
    "/.well-known/ai-catalog.json",
)

# Where MCP servers live when nobody told us. `/mcp` is overwhelmingly the
# answer; `/sse` is the older transport and still common; the rest are what
# frameworks default to. Order is by how often each has been the right one.
CONVENTIONAL = ("/mcp", "/sse", "/api/mcp", "/mcp/sse")

# Never more than this many handshakes for one submission, however many
# candidates the discovery documents name. A publisher is waiting.
MAX_PROBES = 8

ProbeFn = Callable[[httpx.AsyncClient, str], Awaitable[dict]]


def host_of(raw: str) -> str | None:
    """The bare hostname behind anything a publisher might paste."""
    s = (raw or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        h = urllib.parse.urlparse(s).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return None
    return h.strip(".") or None


def _path_of(raw: str) -> str:
    s = (raw or "").strip()
    if "://" not in s:
        return ""
    try:
        return urllib.parse.urlparse(s).path or ""
    except Exception:
        return ""


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def from_index(conn: sqlite3.Connection, host: str) -> list[str]:
    """MCP endpoints we already verified on this host, most tools first."""
    try:
        rows = conn.execute(
            """SELECT url FROM entries
               WHERE type_family='mcp-server' AND url IS NOT NULL AND url != ''
                 AND COALESCE(mcp_tools,0) > 0
                 AND (lower(publisher)=? OR lower(url) LIKE ? OR lower(url) LIKE ?)
               ORDER BY mcp_tools DESC LIMIT 4""",
            (host, f"https://{host}/%", f"http://{host}/%")).fetchall()
        return [r["url"] for r in rows if r["url"]]
    except Exception:
        return []


def _endpoints_in_card(doc: Any, base: str) -> list[str]:
    """`remotes[].url` from an MCP server card, absolutised."""
    out = []
    if isinstance(doc, dict):
        for r in doc.get("remotes") or []:
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                out.append(urllib.parse.urljoin(base, r["url"]))
    return out


def _endpoints_in_manifest(doc: Any, base: str) -> list[str]:
    """MCP server entries from an ARD manifest, by type, absolutised."""
    out = []
    if isinstance(doc, dict):
        for e in doc.get("entries") or []:
            if not isinstance(e, dict):
                continue
            t = str(e.get("type") or "").lower()
            u = e.get("url")
            if "mcp" in t and isinstance(u, str) and u:
                out.append(urllib.parse.urljoin(base, u))
    return out


async def from_discovery(client: httpx.AsyncClient, host: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """Endpoints named by the host's own discovery documents.

    Returns (candidates as (url, source), what_was_checked). Fetched
    concurrently: a publisher is waiting and these are independent.
    """
    base = f"https://{host}"
    checked: list[dict] = []
    found: list[tuple[str, str]] = []

    async def one(path: str):
        rec = {"path": path, "http": None, "endpoints": 0}
        try:
            r = await client.get(base + path, timeout=config.CRAWL_TIMEOUT_S)
            rec["http"] = r.status_code
            if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
                doc = r.json()
                eps = (_endpoints_in_card(doc, base) if path.endswith("server-card.json")
                       else _endpoints_in_manifest(doc, base))
                rec["endpoints"] = len(eps)
                for u in eps:
                    found.append((u, path))
        except Exception as e:
            rec["error"] = type(e).__name__
        checked.append(rec)

    await asyncio.gather(*(one(p) for p in DISCOVERY), return_exceptions=True)

    # A manifest entry of MCP type frequently points at the server CARD, not
    # the server: our own does, and it is a natural thing for a publisher to
    # write, since the card is the document that describes the server. A card
    # is not an endpoint, so POSTing to it gets a 405 and nothing. Follow it
    # one hop to `remotes[].url`, which is the endpoint it names.
    hop: list[tuple[str, str]] = []
    for u, src in list(found):
        if u.rstrip("/").lower().endswith("server-card.json"):
            try:
                r = await client.get(u, timeout=config.CRAWL_TIMEOUT_S)
                if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
                    for e in _endpoints_in_card(r.json(), u):
                        hop.append((e, f"{src} -> server card"))
            except Exception:
                pass
    found.extend(hop)
    return found, checked


async def resolve(conn: sqlite3.Connection, raw: str, client: httpx.AsyncClient,
                  probe: ProbeFn, *, direct_result: dict | None = None) -> dict:
    """Everything callable behind `raw`, each candidate handshaken.

    `direct_result` is the outcome of probing `raw` itself, when the caller
    already did that, so it is not probed twice. The return carries every
    candidate with where it came from and exactly what it answered, so a
    refusal can show its work.
    """
    host = host_of(raw)
    out: dict[str, Any] = {"submitted": raw, "host": host, "candidates": [],
                           "working": [], "checked": []}
    if not host:
        return out

    seen: set[str] = set()
    cands: list[tuple[str, str]] = []

    def add(url: str, source: str) -> None:
        if not url or not url.startswith(("http://", "https://")):
            return
        # Only endpoints on the host that was submitted. A discovery document
        # may legitimately name a server elsewhere, but indexing a third
        # host on the strength of a second host's claim is not this path's
        # call to make.
        if host_of(url) != host:
            return
        k = _norm(url)
        if k in seen:
            return
        seen.add(k)
        cands.append((url, source))

    # 1. The URL itself, if it names something more than the host.
    if _path_of(raw).strip("/"):
        add(raw if "://" in raw else "https://" + raw, "submitted")

    # 2. What we already hold. Cheapest evidence there is.
    for u in from_index(conn, host):
        add(u, "index")

    # 3. What the host says about itself.
    disc, checked = await from_discovery(client, host)
    out["checked"] = checked
    for u, src in disc:
        add(u, src)

    # 4. Where servers usually are.
    for p in CONVENTIONAL:
        add(f"https://{host}{p}", "convention")

    # Probe, bounded. The submitted URL's own result is reused, not repeated.
    todo = cands[:MAX_PROBES]
    results: list[dict] = []

    async def one(url: str, source: str) -> dict:
        if direct_result is not None and _norm(url) == _norm(raw if "://" in raw else "https://" + raw):
            res = direct_result
        else:
            try:
                res = await probe(client, url)
            except Exception as e:
                res = {"status": f"error:{type(e).__name__}", "tools": [], "auth": False,
                       "server_name": None,
                       "evidence": {"exception": f"{type(e).__name__}: {str(e)[:120]}"}}
        return {"url": url, "source": source, "status": res.get("status"),
                "tools": len(res.get("tools") or []), "auth": bool(res.get("auth")),
                "server_name": res.get("server_name"), "evidence": res.get("evidence"),
                "_raw": res}

    results = list(await asyncio.gather(*(one(u, s) for u, s in todo)))

    # The same server often answers on two paths (`/mcp` and `/sse`), and a
    # discovery document may name the endpoint we also guessed. One server is
    # one entry: collapse on what it said its name was and which tools it
    # listed, keeping the first URL that answered, which is the highest
    # confidence source by construction of the candidate order.
    working: list[dict] = []
    identities: set[tuple] = set()
    for r in results:
        st = str(r["status"] or "")
        if not (st.startswith("ok") or st == "auth"):
            continue
        ident = (r["server_name"], tuple(sorted(t.get("name", "") for t in r["_raw"].get("tools") or [])))
        if ident in identities and r["tools"] > 0:
            r["duplicate_of"] = next(w["url"] for w in working
                                     if (w["server_name"], tuple(sorted(t.get("name", "") for t in w["_raw"].get("tools") or []))) == ident)
            continue
        identities.add(ident)
        working.append(r)

    out["candidates"] = [{k: v for k, v in r.items() if k != "_raw"} for r in results]
    out["working"] = working
    return out


def explain(res: dict) -> str:
    """One sentence a publisher can act on, from a resolution."""
    w = res.get("working") or []
    if w:
        srcs = sorted({r["source"] for r in w})
        return (f"found {len(w)} MCP endpoint(s) on {res['host']} via "
                + ", ".join(srcs) + ": " + ", ".join(r["url"] for r in w))
    tried = res.get("candidates") or []
    if not tried:
        return f"nothing to try on {res.get('host') or 'that input'}"
    return (f"tried {len(tried)} location(s) on {res['host']} and none answered an MCP "
            "handshake: " + "; ".join(f"{c['url']} -> {c['status']}" for c in tried[:6]))
