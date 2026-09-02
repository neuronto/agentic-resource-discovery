"""A2A binding for the registry.

Discovery has two halves that do not talk to each other. An orchestrator that
speaks MCP finds us through `/mcp`; an orchestrator that speaks A2A looks for an
Agent Card at a well-known path and, finding nothing, concludes the host is not
an agent at all. We were answering 404 to both card paths while trust and
reputation crawlers asked for them by the hundred.

Publishing a card is only honest if something answers behind it, so this module
is the card *and* a working JSON-RPC endpoint. One skill is exposed per tool the
MCP side already offers, and the handler delegates to the same search path, so
the two bindings cannot drift into disagreeing about what the index contains.

Scope is deliberate: `message/send` returns a Message, not a Task. Search is
synchronous and finishes inside the request, so there is nothing for a task
lifecycle to describe, and inventing one would mean storing state we would never
read. Streaming and task methods answer a proper JSON-RPC error rather than
pretending. The card declares exactly that, which is the point of a card.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import config, search, store

# The JSON-RPC binding of the spec, which is what agent frameworks and the
# card crawlers actually speak. The proto enums (ROLE_AGENT) are accepted on
# input but not emitted, because every client seen in the wild sends "user".
PROTOCOL_VERSION = "0.3"
VERSION = "1.0.0"

SKILLS = [
    {
        "id": "find_resource",
        "name": "Find an agentic resource",
        "description": (
            "Search for an MCP server, skill, agent, API or other callable capability "
            "for a task, across this index and every other public ARD registry at once. "
            "Returns ranked matches with the endpoint to connect to and which registries "
            "carry each result. The score is relevance only, never a trust or safety rating."),
        "tags": ["discovery", "search", "mcp", "ard", "agents", "tools"],
        "examples": [
            "find me an MCP server that can read PDFs",
            "what agent can post to Slack",
            "is there an API for company registry lookups",
        ],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
    },
    {
        "id": "find_tool",
        "name": "Find a specific tool",
        "description": (
            "Search individual tools rather than whole servers. Every tool was read from "
            "the server's own tools/list, so the name and description are the server's, "
            "not a directory's summary of it."),
        "tags": ["tools", "search", "mcp", "verified"],
        "examples": ["a tool that converts currency", "which tool takes a screenshot"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
    },
    {
        "id": "registry_stats",
        "name": "Registry statistics",
        "description": ("How many entries, publishers and verified tools this index holds, "
                        "how many endpoints answer, and which registries it federates."),
        "tags": ["stats", "registry", "ard"],
        "examples": ["how big is this registry", "which registries do you federate"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
    },
]


def card() -> dict:
    """The Agent Card, served at both well-known paths.

    Carries the current `supportedInterfaces` shape and the older top-level
    `url` / `preferredTransport` / `protocolVersion` triple at the same time.
    They do not conflict, and the crawlers in the wild are split across both
    revisions of the spec; a card only one of them can read is a card that
    fails for half its readers.
    """
    B = config.PUBLIC_BASE
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Neuronto ARD Registry",
        "description": (
            "Agentic Resource Discovery (ARD) registry and MCP index. Ask once and get "
            "ranked, verified answers from this index and every other public ARD registry "
            "at the same time, plus a tool index read from each MCP server's own "
            "tools/list. Also publishes: it can build and verify an ARD manifest for a "
            "domain and get that domain listed."),
        "version": VERSION,
        "documentationUrl": f"{B}/api-docs",
        "iconUrl": f"{B}/icon.svg",
        "provider": {"organization": "Neuronto", "url": B},
        "supportedInterfaces": [
            {"url": f"{B}/a2a", "protocolBinding": "JSONRPC",
             "protocolVersion": PROTOCOL_VERSION},
        ],
        # Older readers look for these three at the top level.
        "url": f"{B}/a2a",
        "preferredTransport": "JSONRPC",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": SKILLS,
        # Not part of A2A, and deliberately additive: a reader that arrives via
        # the agent card should be able to reach the ARD manifest and the MCP
        # endpoint without a second guess about where they live.
        "additionalInterfaces": [
            {"url": f"{B}/mcp", "protocolBinding": "MCP", "protocolVersion": "2025-06-18"},
            {"url": f"{B}/.well-known/ard.json", "protocolBinding": "ARD",
             "protocolVersion": "0.91"},
        ],
    }


def _text_of(msg: dict) -> str:
    """Pull the prompt out of a Message, whichever Part revision it uses."""
    out = []
    for p in (msg or {}).get("parts") or []:
        if not isinstance(p, dict):
            continue
        t = p.get("text")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
            continue
        # 0.2-era parts nested the payload under `text`/`data` with a `kind`.
        d = p.get("data")
        if isinstance(d, dict):
            q = d.get("query") or d.get("text")
            if isinstance(q, str) and q.strip():
                out.append(q.strip())
    return " ".join(out).strip()


def _msg(parts: list[dict], context_id: str | None, task_id: str | None) -> dict:
    m = {
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "role": "agent",
        "parts": parts,
        "metadata": {"generatedAt": int(time.time())},
    }
    if context_id:
        m["contextId"] = context_id
    if task_id:
        m["taskId"] = task_id
    return m


def _parts(summary: str, data: Any) -> list[dict]:
    return [
        {"kind": "text", "text": summary},
        {"kind": "data", "data": data, "mediaType": "application/json"},
    ]


def _err(rid: Any, code: int, message: str, data: Any = None) -> tuple[int, dict]:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return 200, {"jsonrpc": "2.0", "id": rid, "error": e}


async def _find_resource(conn, q: str, limit: int) -> tuple[str, dict]:
    out = await search.search(conn, q, None, limit, "auto")
    results = []
    for e in search.clean(out["results"]):
        r = {
            "name": e.get("displayName") or e.get("identifier"),
            "identifier": e.get("identifier"),
            "kind": e.get("type"),
            "endpoint": e.get("url"),
            "description": e.get("description"),
            "score": e.get("score"),
            "found_in": e.get("source"),
        }
        v = e.get("verification")
        if v:
            r["verified"] = {k: v[k] for k in ("reachable", "tools", "authRequired")
                             if k in v}
        results.append(r)
    fed = [f["name"] for f in (out.get("_federated") or []) if f.get("ok")]
    data = {
        "query": q,
        "results": results,
        "searched": ["Neuronto"] + fed,
        "note": "score is semantic relevance only, not a trust or safety rating",
    }
    if not results:
        return f"No match for {q!r} in this index or the registries it federates.", data
    lines = [f"{len(results)} match(es) for {q!r}, searched "
             f"{', '.join(data['searched'])}:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['name']} ({r.get('kind') or 'resource'}) "
                     f"{r.get('endpoint') or ''}".rstrip())
        if r.get("description"):
            lines.append(f"   {str(r['description'])[:200]}")
    return "\n".join(lines), data


async def handle(conn, body: dict) -> tuple[int, dict | None]:
    """One A2A JSON-RPC message. Returns (http_status, response or None)."""
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    # Both the JSON-RPC binding names and the proto RPC names, because the
    # generated clients send the latter.
    if method in ("message/send", "SendMessage"):
        msg = params.get("message") or {}
        q = _text_of(msg)
        ctx = msg.get("contextId") or msg.get("context_id")
        tid = msg.get("taskId") or msg.get("task_id")
        if not q:
            return _err(rid, -32602, "message.parts must contain a text part "
                                     "describing what you are looking for")

        skill = str(params.get("skillId") or (params.get("metadata") or {}).get("skillId")
                    or "").strip()
        limit = 8
        cfg = params.get("configuration") or {}
        try:
            limit = max(1, min(int(cfg.get("maxResults") or limit), 50))
        except (TypeError, ValueError):
            pass

        if skill == "registry_stats" or q.lower() in ("stats", "registry stats"):
            c = store.counts(conn)
            t = store.tool_counts(conn)
            data = {"entries": c["entries"], "publishers": c["publishers"],
                    "live": c["live"], "verified_tools": t["tools"],
                    "federates": [u[1] for u in config.UPSTREAMS]}
            summary = (f"{data['entries']} entries from {data['publishers']} publishers, "
                       f"{data['verified_tools']} verified tools, federating "
                       f"{', '.join(data['federates'])}.")
            return 200, {"jsonrpc": "2.0", "id": rid,
                         "result": _msg(_parts(summary, data), ctx, tid)}

        if skill == "find_tool":
            from . import tools_index
            hits = [{k: v for k, v in h.items() if k != "inputSchema"}
                    for h in tools_index.search_tools(conn, q, limit)]
            data = {"query": q, "tools": hits,
                    "note": ("every tool listed was read from the server's own "
                             "tools/list; score is relevance only")}
            summary = (f"{len(hits)} tool(s) for {q!r}." if hits
                       else f"No verified tool matches {q!r}.")
            return 200, {"jsonrpc": "2.0", "id": rid,
                         "result": _msg(_parts(summary, data), ctx, tid)}

        summary, data = await _find_resource(conn, q, limit)
        return 200, {"jsonrpc": "2.0", "id": rid,
                     "result": _msg(_parts(summary, data), ctx, tid)}

    if method in ("message/stream", "SendStreamingMessage", "tasks/resubscribe",
                  "SubscribeToTask"):
        return _err(rid, -32004, "streaming is not supported by this agent",
                    {"hint": "use message/send; the card declares capabilities.streaming "
                             "false. Search completes inside one request."})

    if method in ("tasks/get", "GetTask", "tasks/cancel", "CancelTask", "tasks/list",
                  "ListTasks", "tasks/pushNotificationConfig/set",
                  "tasks/pushNotificationConfig/get"):
        return _err(rid, -32003, "this agent does not create tasks",
                    {"hint": "message/send returns a Message directly; there is no task "
                             "to poll, cancel or subscribe to."})

    if method in ("agent/getAuthenticatedExtendedCard", "GetExtendedAgentCard"):
        return _err(rid, -32601, "no extended card: the public card is complete",
                    {"card": "/.well-known/agent-card.json"})

    return _err(rid, -32601, f"unknown method: {method}")
