"""AutoGen function for Agentic Resource Discovery through Neuronto.

    from neuronto.autogen import ard_search
    assistant.register_for_llm(name="ard_search",
        description="Find a tool or agent that can do a task")(ard_search)
    user_proxy.register_for_execution(name="ard_search")(ard_search)

A plain function with a type-annotated signature, which is what AutoGen turns
into a tool schema. Nothing to install beyond this package.
"""
from __future__ import annotations

def _render(results: list, limit: int) -> str:
    """One line per hit, the fields a model needs to choose and then connect."""
    lines = []
    for r in (results or [])[:limit]:
        name = r.get("displayName") or r.get("identifier") or "?"
        url = r.get("url") or ""
        kind = (r.get("type") or "").replace("application/", "")
        score = r.get("score")
        desc = (r.get("description") or "").strip().replace("\n", " ")[:160]
        lines.append(f"- {name} [{kind}] score={score} url={url}" + (f" :: {desc}" if desc else ""))
    return "\n".join(lines) if lines else "No matching resource found."


def _search(query: str, limit: int, kind, federate: bool, api_key, base) -> str:
    from . import find_resource
    d = find_resource(query, limit=limit, kind=kind, federate=federate,
                      api_key=api_key, base=base)
    return _render(d.get("results") or [], limit)


def ard_search(query: str, limit: int = 5, federate: bool = True) -> str:
    """Find an MCP server, skill, agent or API that can do a task.

    Args:
        query: the task in plain language, e.g. "send an SMS to a phone number".
        limit: how many candidates to return, at most.
        federate: search every public ARD registry, not only this index.
    """
    return _search(query, limit, None, federate, None, None)
