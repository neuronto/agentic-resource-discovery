"""ard-publish: make your API discoverable by AI agents.

Publishing an ARD manifest is a small job that is easy to get subtly wrong, and
the failures are silent: a catalogue with a malformed URN, or entries with no
representative queries, validates as JSON, serves a 200, and is never returned
by any search. This builds one that works.

    from ard_publish import Manifest, Entry

    m = Manifest(host="example.com", display_name="Example")
    m.add(Entry.mcp_server(
        name="weather",
        display_name="Weather API",
        url="https://example.com/.well-known/mcp/server-card.json",
        description="Current conditions and forecasts for any location.",
        queries=["what is the weather in Berlin",
                 "will it rain in London tomorrow"],
    ))
    m.save(".well-known/ard.json")

Then serve it at /.well-known/ard.json, add an Agentmap: line to robots.txt,
and a <link rel="ard"> tag. `python -m ard_publish check example.com` will tell
you whether all of that worked.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__version__ = "1.2.0"
__all__ = ["Entry", "Manifest", "validate", "ValidationError"]

_URN = re.compile(r"^urn:air:[a-zA-Z0-9.-]+(:[a-zA-Z0-9._-]+)+$")
_SLUG = re.compile(r"[^a-z0-9._-]+")

# The types the official conformance tool accepts without a warning.
MCP    = "application/mcp-server-card+json"
A2A    = "application/a2a-agent-card+json"
SKILL  = "application/agent-skills+gzip"
CATALOG = "application/ai-catalog+json"
REGISTRY = "application/ai-registry+json"


class ValidationError(ValueError):
    """Raised with every problem at once, rather than one per run."""


def _slug(s: str) -> str:
    return _SLUG.sub("-", (s or "").strip().lower()).strip("-") or "resource"


@dataclass
class Entry:
    """One agentic resource."""
    identifier: str
    display_name: str
    type: str
    url: str | None = None
    data: dict | None = None
    description: str = ""
    queries: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: str | None = None
    trust_identity: str | None = None

    @classmethod
    def _make(cls, kind: str, host: str, name: str, display_name: str,
              url: str, type_: str, description: str = "",
              queries: Iterable[str] = (), **kw) -> "Entry":
        return cls(identifier=f"urn:air:{host}:{kind}:{_slug(name)}",
                   display_name=display_name, type=type_, url=url,
                   description=description, queries=list(queries), **kw)

    @classmethod
    def mcp_server(cls, name: str, display_name: str, url: str, *, host: str = "",
                   description: str = "", queries: Iterable[str] = (), **kw) -> "Entry":
        return cls._make("mcp", host, name, display_name, url, MCP, description, queries, **kw)

    @classmethod
    def agent(cls, name: str, display_name: str, url: str, *, host: str = "",
              description: str = "", queries: Iterable[str] = (), **kw) -> "Entry":
        return cls._make("agent", host, name, display_name, url, A2A, description, queries, **kw)

    @classmethod
    def skill(cls, name: str, display_name: str, url: str, *, host: str = "",
              description: str = "", queries: Iterable[str] = (), **kw) -> "Entry":
        return cls._make("skill", host, name, display_name, url, SKILL, description, queries, **kw)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"identifier": self.identifier,
                             "displayName": self.display_name, "type": self.type}
        if self.url:  d["url"] = self.url
        if self.data: d["data"] = self.data
        if self.description:  d["description"] = self.description
        if self.queries:      d["representativeQueries"] = self.queries[:5]
        if self.capabilities: d["capabilities"] = self.capabilities
        if self.tags:         d["tags"] = self.tags
        if self.version:      d["version"] = self.version
        if self.trust_identity: d["trustManifest"] = {"identity": self.trust_identity}
        return d


@dataclass
class Manifest:
    host: str
    display_name: str = ""
    documentation_url: str | None = None
    did: str | None = None
    entries: list[Entry] = field(default_factory=list)

    def __post_init__(self):
        self.host = re.sub(r"^https?://", "", self.host).strip("/").lower()
        self.display_name = self.display_name or self.host
        if self.did is None:
            self.did = f"did:web:{self.host}"

    def add(self, entry: Entry) -> "Manifest":
        # Entries built without a host get ours, so the publisher-authority
        # binding lines up with the domain actually serving the manifest.
        if ":air::" in entry.identifier or entry.identifier.startswith("urn:air::"):
            entry.identifier = entry.identifier.replace("urn:air::", f"urn:air:{self.host}:", 1)
        if entry.trust_identity is None:
            entry.trust_identity = self.did
        self.entries.append(entry)
        return self

    def to_dict(self) -> dict:
        return {"specVersion": "1.0",
                "host": {k: v for k, v in (
                    ("displayName", self.display_name),
                    ("identifier", self.did),
                    ("documentationUrl", self.documentation_url)) if v},
                "entries": [e.to_dict() for e in self.entries]}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    def validate(self) -> list[str]:
        return validate(self.to_dict())

    def save(self, path: str | Path, *, strict: bool = True) -> Path:
        problems = self.validate()
        if strict and problems:
            raise ValidationError("Manifest is not publishable:\n  - " +
                                  "\n  - ".join(problems))
        p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    def robots_line(self) -> str:
        return f"Agentmap: https://{self.host}/.well-known/ard.json"

    def link_tags(self) -> str:
        return (f'<link rel="ard" href="https://{self.host}/.well-known/ard.json">\n'
                f'<link rel="ai-catalog" href="https://{self.host}/.well-known/ai-catalog.json">')


def validate(manifest: dict) -> list[str]:
    """Every problem with a manifest, as plain instructions."""
    out: list[str] = []
    if manifest.get("specVersion") != "1.0":
        out.append('specVersion must be "1.0".')
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        out.append("entries must be a non-empty array.")
        return out
    seen: set[str] = set()
    for i, e in enumerate(entries):
        who = e.get("displayName") or e.get("identifier") or f"entry {i}"
        if not _URN.match(str(e.get("identifier", ""))):
            out.append(f"{who}: identifier must look like "
                       "urn:air:<domain>:<namespace>:<name>.")
        elif e["identifier"] in seen:
            out.append(f"{who}: duplicate identifier.")
        else:
            seen.add(e["identifier"])
        if not e.get("displayName"): out.append(f"{who}: displayName is required.")
        if not e.get("type"):        out.append(f"{who}: type is required.")
        if ("url" in e) == ("data" in e):
            out.append(f"{who}: provide exactly one of url or data.")
        q = e.get("representativeQueries")
        if not q:
            out.append(f"{who}: add 2-5 representativeQueries. Registries build "
                       "their semantic index from this field; without it the entry "
                       "will not be returned by any search.")
        elif not (2 <= len(q) <= 5):
            out.append(f"{who}: representativeQueries should hold 2-5 items, has {len(q)}.")
        if not e.get("description"):
            out.append(f"{who}: add a description; it feeds the semantic index.")
    return out
