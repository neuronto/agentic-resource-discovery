"""MCP wrapper for the registry (§5.3.5).

The spec permits a registry to expose its search natively as an MCP tool, and
it is the highest-leverage thing we can do for adoption: every orchestrator
already knows how to install an MCP server, and almost none of them yet know
what an ARD registry is. The response follows the same entry model as the REST
API, as §5.3.5 requires.

Streamable HTTP, JSON-RPC 2.0, no auth. Discovery is public by nature; putting
a key in front of it would defeat the point.

Three tools read and one writes. `publish_resource` is unauthenticated like the
rest, which is safe only because it verifies rather than trusts: an endpoint has
to complete an MCP handshake and a domain has to serve a manifest that parses,
so the cost of listing something is owning something that answers. That is the
same bar the HTTP submit route applies, and it calls that route rather than
reimplementing it, so the two cannot drift apart.
"""
from __future__ import annotations

import json
from typing import Any


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
                         "enum": ["any", "mcp-server", "skill", "a2a-agent", "agent",
                                  "webmcp", "plugin", "openapi", "graphql", "dataset",
                                  "package", "doc", "catalog", "registry"],
                         "description": (
                             "Restrict to one family of resource. Matching is normalised, "
                             "so 'mcp-server' finds them under every media type in "
                             "circulation, and 'a2a-agent' or 'agent' both reach A2A cards "
                             "and ACP, OASF and AgentFacts descriptors. 'webmcp' is "
                             "browser-page tools, which are not callable servers."),
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
        "name": "find_tool",
        "title": "Find a specific verified tool",
        "description": (
            "Search individual MCP tools by name and behaviour, not the servers that "
            "host them. Every tool returned here was read back from a live server's "
            "tools/list, so the name and input schema are what the server actually "
            "exposes rather than what its description claims. Use this when you know "
            "the shape of the call you need; use find_resource when you are looking "
            "for a service."),
        "annotations": {"title": "Find a specific verified tool", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What the tool should do. For example: "
                                         "'extract text from a pdf'."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "with_schema": {"type": "boolean", "default": False,
                                "description": "Include each tool's full JSON input "
                                               "schema. Verbose; off by default."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "publish_resource",
        "title": "Publish a resource to the index",
        "description": (
            "List an MCP server or an ARD-publishing domain in this index so other agents "
            "can discover it. Give `endpoint` for an MCP server URL, or `domain` for a site "
            "that serves an ARD manifest. The submission is verified before it is indexed: "
            "an endpoint must complete an MCP initialize handshake, and a domain must serve "
            "a manifest that parses. Nothing is taken on trust, so a listing that succeeds "
            "here is one an agent can actually call."),
        "annotations": {"title": "Publish a resource", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string",
                             "description": "Absolute URL of an MCP server, e.g. https://example.com/mcp"},
                "domain":   {"type": "string",
                             "description": "A domain serving /.well-known/ard.json, e.g. example.com"},
            },
        },
    },
    {
        "name": "registry_stats",
        "title": "Index statistics",
        "description": "How large this index is, what it holds, how much of it has been "
                       "verified by introspection, and which upstream registries are "
                       "federated.",
        "annotations": {"title": "Index statistics", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True},
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def server_card() -> dict:
    B = config.PUBLIC_BASE
    return {
        "serverInfo": {"name": "neuronto", "version": "1.0.0", "title": "Neuronto ARD Registry: Agentic Resource Discovery (ARD) Index",
                       "description": "Agentic Resource Discovery (ARD) index. Search every public ARD "
                                      "registry and the MCP ecosystem at once for a server, skill or "
                                      "agent that can do a task.",
                       "websiteUrl": B},
        "name": "neuronto", "title": "Neuronto ARD Registry: Agentic Resource Discovery (ARD) Index",
        "description": "Federated Agentic Resource Discovery (ARD) index. One search across every "
                       "public ARD registry and the MCP ecosystem.",
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

        if name == "publish_resource":
            endpoint = str(args.get("endpoint") or "").strip()
            domain = str(args.get("domain") or "").strip()
            if not endpoint and not domain:
                return 200, {"jsonrpc": "2.0", "id": rid, "result": {
                    **_text({"error": "give either `endpoint` (an MCP server URL) or "
                                      "`domain` (a site serving an ARD manifest)"}),
                    "isError": True}}
            # Deferred import: main imports this module, so the reference can
            # only be resolved at call time. Calling the endpoint's own handler
            # rather than reimplementing it means the agent route and the HTTP
            # route cannot drift apart, and both apply the same verification.
            from .main import submit_endpoint
            resp = await submit_endpoint({"endpoint": endpoint, "domain": domain})
            payload = json.loads(bytes(resp.body).decode() or "{}")
            indexed = resp.status_code < 400 and payload.get("status") == "indexed"
            pending = resp.status_code == 202 and payload.get("status") == "pending"
            if indexed:
                payload.setdefault("note",
                    "indexed and searchable. It will be re-checked periodically; an "
                    "endpoint that stops answering is demoted, not deleted.")
            elif pending:
                # Not an error from the agent's point of view: the submission
                # is accepted and we are the ones who will keep trying. Marking
                # it isError made one agent call this four times in five
                # minutes on 2026-09-01, each time starting from nothing.
                payload["indexed"] = False
                payload["next_step"] = ("nothing to do on your side unless the evidence "
                                        "shows a fault in the endpoint; we retry on the "
                                        "schedule in `retry`. Check `submission.status_url` "
                                        "for the outcome.")
            return 200, {"jsonrpc": "2.0", "id": rid,
                         "result": {**_text(payload),
                                    **({} if (indexed or pending) else {"isError": True})}}

        if name == "registry_stats":
            c = store.counts(conn)
            t = store.tool_counts(conn)
            return 200, {"jsonrpc": "2.0", "id": rid, "result": _text({
                "entries": c["entries"], "publishers": c["publishers"],
                "live": c["live"], "dead": c["dead"], "by_kind": c["families"],
                "sources": c["sources"],
                "verified_tools": t["tools"],
                "servers_introspected": t["introspected"],
                "servers_with_tools": t["servers_with_tools"],
                "servers_requiring_auth": t["auth_required"],
                "federates": [u[1] for u in config.UPSTREAMS]})}

        if name == "find_tool":
            q = (args.get("query") or "").strip()
            if not q:
                return 200, {"jsonrpc": "2.0", "id": rid,
                             "result": {**_text({"error": "query is required"}),
                                        "isError": True}}
            limit = max(1, min(int(args.get("limit") or 10), 50))
            with_schema = bool(args.get("with_schema"))
            from . import tools_index
            hits = tools_index.search_tools(conn, q, limit)
            if not with_schema:
                hits = [{k: v for k, v in h.items() if k != "inputSchema"} for h in hits]
            return 200, {"jsonrpc": "2.0", "id": rid, "result": _text({
                "query": q, "tools": hits,
                "note": ("every tool listed was read from the server's own tools/list; "
                         "score is semantic relevance only, not a trust or safety rating")})}

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
                r = {
                    "name": e.get("displayName") or e.get("identifier"),
                    "identifier": e.get("identifier"),
                    "kind": e.get("type"),
                    "endpoint": e.get("url"),
                    "description": e.get("description"),
                    "score": e.get("score"),
                    "found_in": e.get("source"),
                }
                # What we verified, kept as its own field so it can never be
                # read as part of the relevance score.
                v = e.get("verification")
                if v:
                    r["verified"] = {k: v[k] for k in
                                     ("reachable", "tools", "authRequired") if k in v}
                results.append(r)
            fed = [f["name"] for f in (out.get("_federated") or []) if f.get("ok")]
            dense = (out.get("_dense") or {}).get("state")
            return 200, {"jsonrpc": "2.0", "id": rid, "result": _text({
                "query": q, "results": results,
                "searched": ["Neuronto"] + fed,
                "retrieval": ("lexical + semantic + federated" if dense == "ok"
                              else "lexical + federated"),
                "note": "score is semantic relevance only, not a trust or safety rating"})}

        return 200, {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"unknown tool: {name}"}}

    return 200, {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601, "message": f"unknown method: {method}"}}
