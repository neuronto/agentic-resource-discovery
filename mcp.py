"""MCP wrapper for the registry (§5.3.5).

The spec permits a registry to expose its search natively as an MCP tool, and
it is the highest-leverage thing we can do for adoption: every orchestrator
already knows how to install an MCP server, and almost none of them yet know
what an ARD registry is. The response follows the same entry model as the REST
API, as §5.3.5 requires.

Streamable HTTP, JSON-RPC 2.0, no auth. Discovery is public by nature; putting
a key in front of it would defeat the point.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse, Response

from . import config, search, store

PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "find_resource",
        "title": "Find an agentic resource",
        "description": (
            "Search for an MCP server, skill, agent, API or other callable capability "
            "for a task, across this index and every other public ARD registry at once. "
            "Returns ranked matches with a relevance score, the endpoint to connect to, "
            "and which registries carry each result. The score is relevance only and is "
            "not a trust or safety rating."),
        "annotations": {"title": "Find an agentic resource", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "The task, in plain language. For example: "
                                         "'scrape a website behind cloudflare'."},
                "kind": {"type": "string",
                         "enum": ["any", "mcp-server", "skill", "a2a-agent", "registry", "openapi"],
                         "description": "Restrict to one family of resource. Matching is "
                                        "normalised, so 'mcp-server' finds them under all "
                                        "media types in circulation.",
                         "default": "any"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                "federate": {"type": "boolean", "default": True,
                             "description": "Also query upstream registries live and fuse "
                                            "the rankings. Off is faster."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "registry_stats",
        "title": "Index statistics",
        "description": "How large this index is, what it holds, and which upstream "
                       "registries are federated.",
        "annotations": {"title": "Index statistics", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True},
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def server_card() -> dict:
    B = config.PUBLIC_BASE
    return {
        "serverInfo": {"name": "neuronto", "version": "1.0.0", "title": "Neuronto Discovery",
                       "description": "Search every public ARD registry at once for an MCP "
                                      "server, skill or agent that can do a task.",
                       "websiteUrl": B},
        "name": "neuronto", "title": "Neuronto Discovery",
        "description": "Federated discovery across every public ARD registry.",
        "protocolVersion": PROTOCOL,
        "capabilities": {"tools": {"listChanged": False}},
        "authentication": {"required": False, "schemes": []},
        "remotes": [{"type": "streamable-http", "url": f"{B}/mcp"}],
        "tools": TOOLS,
    }


def _text(payload: Any) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


async def handle(conn, body: dict) -> tuple[int, dict | None]:
    """One JSON-RPC message. Returns (http_status, response or None).

    Notifications get 202 with no body, which is what the transport expects and
    what the liveness monitors probe for.
    """
    method = body.get("method")
    rid = body.get("id")

    if method in ("notifications/initialized", "notifications/cancelled"):
        return 202, None

    if method == "initialize":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "neuronto", "version": "1.0.0",
                           "title": "Neuronto Discovery"}}}

    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        if name == "registry_stats":
            c = store.counts(conn)
            return 200, {"jsonrpc": "2.0", "id": rid, "result": _text({
                "entries": c["entries"], "publishers": c["publishers"],
                "live": c["live"], "dead": c["dead"], "by_kind": c["families"],
                "sources": c["sources"],
                "federates": [u[1] for u in config.UPSTREAMS]})}

        if name == "find_resource":
            q = (args.get("query") or "").strip()
            if not q:
                return 200, {"jsonrpc": "2.0", "id": rid,
                             "result": {**_text({"error": "query is required"}),
                                        "isError": True}}
            kind = args.get("kind") or "any"
            limit = max(1, min(int(args.get("limit") or 8), 50))
            mode = "auto" if args.get("federate", True) else "none"
            flt = None
            if kind and kind != "any":
                from .normalize import preferred_type
                flt = {"type": [preferred_type(kind) or kind]}
            out = await search.search(conn, q, flt, limit, mode)
            results = []
            for e in search.clean(out["results"]):
                results.append({
                    "name": e.get("displayName") or e.get("identifier"),
                    "identifier": e.get("identifier"),
                    "kind": e.get("type"),
                    "endpoint": e.get("url"),
                    "description": e.get("description"),
                    "score": e.get("score"),
                    "found_in": e.get("source"),
                })
            fed = [f["name"] for f in (out.get("_federated") or []) if f.get("ok")]
            return 200, {"jsonrpc": "2.0", "id": rid, "result": _text({
                "query": q, "results": results,
                "searched": ["Neuronto"] + fed,
                "note": "score is semantic relevance only, not a trust or safety rating"})}

        return 200, {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"unknown tool: {name}"}}

    return 200, {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601, "message": f"unknown method: {method}"}}
