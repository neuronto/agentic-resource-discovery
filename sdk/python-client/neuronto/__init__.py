"""Find the MCP servers, skills, agents and APIs that can do a task.

One call searches this index and every other public Agentic Resource Discovery
(ARD) registry at once, fusing the rankings, so you get one answer across the
federation rather than a single catalogue's view.

Thin on purpose. Ranking, federation and verification all happen server-side, so
a fatter client would only add ways for this library and the service to disagree
about what the index contains. Standard library only: a package whose job is
helping you find tools should not drag a tree of them in behind it.

    from neuronto import find_resource
    for r in find_resource("read a PDF and extract tables")["results"]:
        print(r["displayName"], r["url"], r["score"])
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.1.0"
__all__ = ["find_resource", "find_tool", "registry_stats", "liveness", "publish",
           "NeurontoError", "BASE"]

BASE = os.environ.get("NEURONTO_BASE", "https://neuronto.com")
_UA = f"neuronto-python/{__version__} (+https://neuronto.com/connect)"


class NeurontoError(RuntimeError):
    """The index refused or could not answer. Carries the status and body."""

    def __init__(self, message: str, status: int = 0, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _request(path: str, payload=None, *, base: str | None = None,
             api_key: str | None = None, timeout: float = 45.0):
    url = (base or BASE) + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw}
        raise NeurontoError(
            body.get("detail") or body.get("error") or f"the index answered {e.code}",
            e.code, body) from None
    except Exception as e:  # network, DNS, timeout
        raise NeurontoError(str(e)) from None
    return json.loads(raw) if raw else {}


def find_resource(query: str, *, limit: int = 10, kind: str | None = None,
                  federate: bool = True, api_key: str | None = None,
                  base: str | None = None, timeout: float = 45.0) -> dict:
    """Search for something that can do a task.

    `kind` is a media type, e.g. "application/mcp-server+json". `federate=False`
    keeps the query on this index alone. A search sent with `api_key` is not
    recorded at all.

    Returns {"results": [...], "federation": {...} | None}. Each result carries
    displayName, identifier, type, url, description, score and source. `score`
    is semantic relevance only and is never a trust or safety rating.
    """
    q: dict = {"text": query}
    if kind:
        q["filter"] = {"type": [kind]}
    body: dict = {"query": q, "pageSize": limit}
    if not federate:
        body["federation"] = "none"
    d = _request("/search", body, base=base, api_key=api_key, timeout=timeout)
    return {"results": d.get("results") or [], "federation": d.get("federation")}


def find_tool(query: str, *, limit: int = 10, api_key: str | None = None,
              base: str | None = None, timeout: float = 45.0) -> list:
    """Search individual tools rather than whole servers.

    Every tool was read from that server's own tools/list, so results carry
    `tool` (the server's own name for it), `server`, `endpoint`, `score` and
    `verified`. Not `name`: that was assumed once and printed nothing.
    """
    d = _request("/tools", {"query": {"text": query}, "pageSize": limit},
                 base=base, api_key=api_key, timeout=timeout)
    return d.get("results") or d.get("tools") or []


def registry_stats(*, base: str | None = None, timeout: float = 45.0) -> dict:
    """The measured state of the ecosystem, with its window and limitations."""
    return _request("/state-of-mcp", None, base=base, timeout=timeout)


def liveness(*, dead: bool = False, since: int = 0, limit: int = 500,
             cursor: int = 0, base: str | None = None, timeout: float = 45.0) -> dict:
    """Liveness observations, including endpoints that stopped answering.

    Free to use, redistribute and build on. No key, no attribution required.
    If you run a registry, `dead=True` is the useful half.
    """
    qs = {"limit": limit}
    if dead:
        qs["dead"] = 1
    if since:
        qs["since"] = since
    if cursor:
        qs["cursor"] = cursor
    return _request("/liveness?" + urllib.parse.urlencode(qs), None,
                    base=base, timeout=timeout)


def publish(*, endpoint: str = "", domain: str = "", api_key: str | None = None,
            base: str | None = None, timeout: float = 90.0) -> dict:
    """Get an MCP endpoint or a manifest-publishing domain indexed.

    Verified rather than trusted: the endpoint has to complete a handshake, or
    the domain has to serve a manifest that parses. A busy index answers 202
    with a queue id and retries on its own, which costs you nothing.
    """
    return _request("/submit", {"endpoint": endpoint, "domain": domain},
                    base=base, api_key=api_key, timeout=timeout)
