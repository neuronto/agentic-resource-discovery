"""Verified tool-level indexing.

Every registry in this field indexes *servers*: a name, a URL and whatever
prose the publisher wrote. The thing an agent actually has to match on, the
tool names and their input schemas, exists in no index anywhere.

So we ask. `initialize` then `tools/list` against the live endpoint, and store
what comes back. That turns a claim into evidence, and it is the difference
between "this server says it does PDFs" and "this server exposes
`extract_pdf_text(url, pages)`".

Three findings are worth as much as the tools themselves:
  * an endpoint that demands credentials, which no registry currently marks;
  * an endpoint that handshakes but exposes nothing;
  * drift between the description and the tools actually present.

Kept deliberately conservative. Two POSTs, a short timeout, no retries beyond
the transport's own, and read-only methods only: `initialize`, `tools/list`.
We never call a tool. Introspecting somebody's server is a courtesy they did
not explicitly grant, so it stays cheap and it stays read-only.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from . import config, store

PROTOCOL = "2025-06-18"

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": PROTOCOL, "capabilities": {},
               "clientInfo": {"name": "neuronto-introspect", "version": "1.0"}},
}
_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

_HEADERS = {
    "content-type": "application/json",
    # Streamable HTTP servers may answer either shape, and a server that only
    # speaks SSE will refuse a client that did not say it could read one.
    "accept": "application/json, text/event-stream",
}


def _parse(text: str) -> dict | None:
    """Read a JSON-RPC reply that may have arrived as an SSE stream.

    An SSE event's data may span several `data:` lines and a stream may carry
    several events; the reply we want is the last one that parses. The first
    version kept only the last single `data:` line, which broke on any server
    that pretty-prints its JSON into the frame.
    """
    if "data:" not in text:
        try:
            return json.loads(text)
        except Exception:
            return None
    events: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
        elif line.strip() == "" and buf:
            events.append("\n".join(buf)); buf = []
    if buf:
        events.append("\n".join(buf))
    for ev in reversed(events):
        try:
            d = json.loads(ev)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
    return None


def _evidence(r) -> dict:
    """What the other side actually returned, kept with the outcome. Without
    this a refusal is an opinion; with it, it is a record."""
    ct = r.headers.get("content-type", "")
    body = (r.text or "")[:240]
    return {"http": r.status_code, "content_type": ct[:80], "body": body,
            "server": r.headers.get("server", "")[:40]}


async def introspect_one(client: httpx.AsyncClient, url: str,
                         retries: int = 0) -> dict:
    """Handshake with one MCP endpoint and read its tool list.

    Returns a status dict rather than raising: a sweep over thousands of
    stranger-operated endpoints hits every failure mode there is, and one
    unreachable host must never end the run.

    `evidence` is always filled: the HTTP status, content type and the first
    bytes of the body we got, or the exception we hit. `retries` is for the
    submission path only, where one publisher is waiting and a single blink
    (their cold start, our resolver) should not be the answer; the sweep keeps
    it at zero to stay cheap.
    """
    out: dict[str, Any] = {"status": "error:unknown", "tools": [],
                           "auth": False, "server_name": None, "evidence": None}
    for attempt in range(retries + 1):
        out = await _introspect_once(client, url)
        if out["status"].startswith("ok") or out["status"] == "auth":
            return out
        if attempt < retries and _transient(out):
            await asyncio.sleep(1.5)
            continue
        return out
    return out


def _transient(out: dict) -> bool:
    """Worth a second try: transport errors, 5xx, and a body that did not
    parse. A 404 or a 405 is a real answer and is not retried."""
    st = out.get("status", "")
    ev = out.get("evidence") or {}
    if "exception" in ev:
        return True
    code = ev.get("http") or 0
    return code >= 500 or code == 429 or st == "error:handshake"


async def _introspect_once(client: httpx.AsyncClient, url: str) -> dict:
    out: dict[str, Any] = {"status": "error:unknown", "tools": [],
                           "auth": False, "server_name": None, "evidence": None}
    try:
        r = await client.post(url, json=_INIT, headers=_HEADERS,
                              timeout=config.INTROSPECT_TIMEOUT_S)
    except Exception as e:
        out["status"] = f"error:{type(e).__name__}"
        out["evidence"] = {"exception": f"{type(e).__name__}: {str(e)[:160]}"}
        return out
    out["evidence"] = _evidence(r)

    if r.status_code in (401, 403, 407):
        out["status"] = "auth"
        out["auth"] = True
        return out
    if r.status_code >= 400:
        out["status"] = f"error:http{r.status_code}"
        return out

    d = _parse(r.text)
    if d and "error" in d and "result" not in d:
        # The server spoke JSON-RPC and refused: that is its answer, recorded
        # verbatim, and it is a different thing from not being an MCP server.
        err = d.get("error") or {}
        out["status"] = f"error:rpc{err.get('code', '')}"
        out["evidence"]["rpc_error"] = str(err.get("message") or "")[:160]
        return out
    if not d or "result" not in d:
        out["status"] = "error:handshake"
        return out
    out["server_name"] = ((d.get("result") or {}).get("serverInfo") or {}).get("name")

    headers = dict(_HEADERS)
    # Required by the transport on every request after initialize.
    headers["mcp-protocol-version"] = str(
        (d.get("result") or {}).get("protocolVersion") or PROTOCOL)
    sid = r.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid

    try:
        r2 = await client.post(url, json=_LIST, headers=headers,
                               timeout=config.INTROSPECT_TIMEOUT_S)
    except Exception as e:
        out["status"] = f"error:{type(e).__name__}"
        out["evidence"] = {"exception": f"{type(e).__name__}: {str(e)[:160]}",
                           "stage": "tools/list"}
        return out

    if r2.status_code in (401, 403):
        out["status"] = "auth"
        out["auth"] = True
        return out

    d2 = _parse(r2.text)
    tools = ((d2 or {}).get("result") or {}).get("tools")
    if not isinstance(tools, list):
        out["status"] = "ok:notools"
        return out

    clean = []
    for t in tools:
        if isinstance(t, dict) and t.get("name"):
            clean.append(t)
    out["tools"] = clean[:400]
    out["status"] = "ok" if clean else "ok:notools"
    return out


async def sweep(conn, limit: int = 500, only_stale: bool = True,
                concurrency: int | None = None) -> dict:
    """Introspect a batch of MCP endpoints, least recently checked first."""
    conc = concurrency or config.INTROSPECT_CONCURRENCY
    cutoff = int(time.time()) - config.INTROSPECT_MAX_AGE_H * 3600
    where = ("AND (mcp_checked IS NULL OR mcp_checked < ?)" if only_stale else "")
    args: tuple = (cutoff, limit) if only_stale else (limit,)
    rows = conn.execute(
        f"""SELECT key, url FROM entries
            WHERE type_family='mcp-server' AND url LIKE 'http%'
              AND (live IS NULL OR live = 1)
              {where}
            ORDER BY (mcp_checked IS NULL) DESC, mcp_checked ASC
            LIMIT ?""", args).fetchall()
    if not rows:
        return {"probed": 0, "ok": 0, "auth": 0, "failed": 0, "tools": 0}

    sem = asyncio.Semaphore(conc)
    stats = {"probed": 0, "ok": 0, "auth": 0, "failed": 0, "tools": 0}
    found: list[tuple[str, dict]] = []

    async with httpx.AsyncClient(
            follow_redirects=True,
            limits=httpx.Limits(max_connections=conc * 2)) as client:
        async def one(key: str, url: str):
            async with sem:
                res = await introspect_one(client, url)
            found.append((key, res))
        await asyncio.gather(*(one(r["key"], r["url"]) for r in rows),
                             return_exceptions=True)

    # Writes are serialised here rather than inside the gather: SQLite takes one
    # writer, and doing it after the network phase keeps the transaction short.
    for key, res in found:
        stats["probed"] += 1
        status = res["status"]
        if status.startswith("ok"):
            stats["ok"] += 1
        elif status == "auth":
            stats["auth"] += 1
        else:
            stats["failed"] += 1
        n = 0
        if res["tools"]:
            n = store.replace_tools(conn, key, res["tools"])
            stats["tools"] += n
        store.mark_introspection(conn, key, status, n, res["auth"],
                                 res.get("server_name"))
    conn.commit()
    return stats


def search_tools(conn, text: str, limit: int = 20) -> list[dict]:
    """Tool-level search: return the individual tools, not their servers.

    The complement to `/search`. When an agent knows the shape of the call it
    needs, the server is an implementation detail.
    """
    from .search import _fts_query
    match = _fts_query(text)
    if not match:
        return []
    try:
        rows = conn.execute(
            """SELECT t.entry_key, t.name, t.title, t.description, t.input_schema,
                      e.display_name, e.url, e.identifier, e.live,
                      bm25(tools_fts, 8.0, 4.0, 2.0) AS bm
               FROM tools_fts
               JOIN tools t ON t.id = tools_fts.tool_id
               JOIN entries e ON e.key = t.entry_key
               WHERE tools_fts MATCH ?
               ORDER BY bm LIMIT ?""", (match, max(limit * 4, 40))).fetchall()
    except Exception:
        return []

    from . import rank
    scores = rank.scale_scores([-float(r["bm"]) for r in rows])
    out = []
    for r, s in zip(rows, scores):
        d = {
            "tool": r["name"],
            "description": r["description"] or r["title"] or None,
            "server": r["display_name"] or r["identifier"],
            "identifier": r["identifier"],
            "endpoint": r["url"],
            "score": rank.apply_liveness(s, r["live"]),
            "verified": True,
        }
        if r["input_schema"]:
            try:
                d["inputSchema"] = json.loads(r["input_schema"])
            except Exception:
                pass
        out.append({k: v for k, v in d.items() if v is not None})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]
