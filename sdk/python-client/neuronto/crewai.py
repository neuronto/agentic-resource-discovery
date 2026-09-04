"""CrewAI tool for Agentic Resource Discovery through Neuronto.

    from neuronto.crewai import ArdSearchTool
    agent = Agent(..., tools=[ArdSearchTool()])
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


def ArdSearchTool(*, limit: int = 5, federate: bool = True,
                  api_key: str | None = None, base: str | None = None):
    """A CrewAI `BaseTool` subclass instance. Requires `pip install crewai`."""
    try:
        from crewai.tools import BaseTool
    except ImportError as e:  # pragma: no cover
        raise ImportError("neuronto.crewai needs crewai: pip install crewai") from e

    class _Tool(BaseTool):
        name: str = "ard_search"
        description: str = ("Find an MCP server, skill, agent or API that can do a task, by "
                            "describing the task in plain language. Returns name, type, "
                            "relevance score and the URL to connect to.")

        def _run(self, query: str) -> str:
            return _search(query, limit, None, federate, api_key, base)

    return _Tool()
