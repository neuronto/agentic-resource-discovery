"""LangChain tool for Agentic Resource Discovery through Neuronto.

    from neuronto.langchain import ard_search_tool
    tools = [ard_search_tool()]

The model calls it with a plain description of the job and gets back a short
list of MCP servers, skills, agents or APIs that can do it, with the URL to
connect to. Discovery only: connecting is the caller's job, over the resource's
own protocol, which is what the ARD specification says should happen.
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


def ard_search_tool(*, limit: int = 5, federate: bool = True,
                    api_key: str | None = None, base: str | None = None):
    """A LangChain `Tool`. Requires `pip install langchain-core`."""
    try:
        from langchain_core.tools import Tool
    except ImportError as e:  # pragma: no cover
        raise ImportError("neuronto.langchain needs langchain-core: "
                          "pip install langchain-core") from e

    def _run(query: str) -> str:
        return _search(query, limit, None, federate, api_key, base)

    return Tool(
        name="ard_search",
        description=("Find an MCP server, skill, agent or API that can do a task, by "
                     "describing the task in plain language. Returns name, type, "
                     "relevance score and the URL to connect to. Use before assuming a "
                     "capability is unavailable."),
        func=_run,
    )
