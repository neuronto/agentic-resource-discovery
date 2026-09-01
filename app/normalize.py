"""Normalise the two things the ecosystem disagrees about.

Measured on the live registries, 2026-08-31:

  * Three MCP media types are in circulation at once, `application/mcp-server`
    (most common in WellKnown's index), `application/mcp-server+json` (GitHub
    Agent Finder, and the filter the official connector skill documents), and
    `application/mcp-server-card+json` (the conformance tool's allowlist).
  * Two URN prefixes, `urn:air:` per the spec, `urn:ai:` at GitHub.

Filters are exact-match, so this fragmentation makes them miss silently: asking
WellKnown for `application/mcp-server+json` returns three results and drops
entries that are plainly MCP servers. Normalising on ingest is the difference
between a filter that works across sources and one that quietly lies.

We keep the publisher's original string in `type_raw` and never rewrite what
they said. `type_family` is ours, used for matching only.
"""
from __future__ import annotations

import re

# Canonical families. The key is what we match on; the values are every spelling
# seen in the wild that means the same thing.
_FAMILIES: dict[str, tuple[str, ...]] = {
    "mcp-server": (
        "application/mcp-server",
        "application/mcp-server+json",
        "application/mcp-server-card+json",
        "application/vnd.mcp+json",
        "application/mcp+json",
        # Seen in the wild spelled out in full. The substring fallback below
        # looks for "mcp" and missed this entirely, so real MCP servers were
        # being filed as "other" and dropped from every MCP filter. This is the
        # exact failure we criticise other registries for.
        "application/vnd.modelcontextprotocol.server+json",
        "application/modelcontextprotocol-server+json",
    ),
    "a2a-agent": (
        "application/a2a-agent-card+json",
        "application/agent-card+json",
        "application/vnd.a2a.agent-card+json",
    ),
    "skill": (
        "application/ai-skill",
        "application/ai-skill+md",
        "application/agent-skill+json",
        "application/agent-skills+md",
        "application/agent-skills+zip",
        "application/agent-skills+gzip",
        "application/ai-skill-archive+gzip",
        "application/vnd.github.copilot-plugin",
    ),
    # Agent descriptors that are not A2A agent cards. ACP (IBM), OASF (AGNTCY /
    # Cisco), NANDA AgentFacts and the generic Agent Manifest all describe an
    # agent, so a caller asking for agents must find them, but they are not
    # interchangeable with an A2A card and are not filed as one.
    "agent": (
        "application/agent-manifest+json",
        "application/acp-agent-manifest+json",
        "application/vnd.acp.agent+json",
        "application/oasf-record+json",
        "application/vnd.oasf.record+json",
        "application/agentfacts+json",
        "application/vnd.nanda.agentfacts+json",
        "application/vnd.openclaw.agent+json",
        "application/nlweb+json",
    ),
    # Tools exposed by a page to a browser agent, W3C WebMCP. Not a callable
    # server: it exists only while the page is open, so it is not an mcp-server.
    "webmcp": (
        "application/webmcp-tools+json",
        "application/vnd.webmcp+json",
    ),
    # Plugin manifests, including the OpenAI-era one still in circulation.
    "plugin": (
        "application/ai-plugin+json",
        "application/agent-plugins+json",
        "application/vnd.ai-plugin+json",
    ),
    "graphql": (
        "application/graphql+json",
        "application/graphql-response+json",
        "application/vnd.graphql.sdl",
    ),
    "dataset": (
        "application/dataset+json",
        "application/vnd.dataset+json",
    ),
    "catalog":  ("application/ai-catalog+json", "application/ai-catalog"),
    "registry": ("application/ai-registry+json", "application/ai-registry"),
    "openapi":  ("application/vnd.oai.openapi+json", "application/openapi+json",
                 "application/vnd.oai.openapi", "application/rest-api+json"),
    "package":  ("application/vnd.npm.package+json", "application/vnd.npm+json",
                 "application/vnd.pypi.package+json", "application/vnd.pypi+json",
                 "application/vnd.oci.image.manifest.v1+json",
                 "application/cli-manifest+json"),
    "doc":      ("text/llms.txt", "text/markdown", "text/plain", "text/html",
                 "text/asciidoc", "application/schema+json",
                 "application/linkset+json"),
}

_LOOKUP: dict[str, str] = {}
for _fam, _types in _FAMILIES.items():
    for _t in _types:
        _LOOKUP[_t] = _fam

# The conformance tool's allowlist. Publishing a type outside it is legal but
# earns a warning, so when we emit an entry of our own we prefer one of these.
CONFORMANT = {
    "application/ai-catalog+json", "application/agent-card+json",
    "application/a2a-agent-card+json", "application/mcp-server-card+json",
    "application/agent-skills+zip", "application/agent-skills+gzip",
    'text/markdown; profile="urn:air:agent-skills"',
    "application/ai-registry", "application/ai-registry+json",
}

# The spelling we emit for each family when asked for a canonical form.
# One canonical media type per family, so preferred_type() never returns None
# for a family we actually index. An incomplete table here was not obviously
# broken: callers do `preferred_type(kind) or kind`, and the bare family name
# then round-tripped through media_family by substring accident, which held for
# "plugin" and "webmcp" and failed silently for "agent" and "doc", both of which
# resolved to "other".
_PREFERRED = {
    "mcp-server": "application/mcp-server-card+json",
    "a2a-agent":  "application/a2a-agent-card+json",
    "agent":      "application/agent-manifest+json",
    "skill":      "application/agent-skills+gzip",
    "catalog":    "application/ai-catalog+json",
    "registry":   "application/ai-registry+json",
    "openapi":    "application/vnd.oai.openapi+json",
    "webmcp":     "application/webmcp-tools+json",
    "plugin":     "application/ai-plugin+json",
    "graphql":    "application/graphql+json",
    "dataset":    "application/dataset+json",
    "package":    "application/vnd.npm.package+json",
    "doc":        "text/markdown",
}

# Every family we recognise. A caller may legitimately pass one of these instead
# of a media type, and it must resolve to itself rather than to whatever the
# substring rules happen to say.
FAMILIES: frozenset[str] = frozenset(list(_FAMILIES) + ["other"])


def media_family(media_type: object | None) -> str:
    """Map any spelling to its canonical family, or 'other'.

    Takes `object` rather than `str` deliberately: this runs on third-party
    manifests, where a field the specification calls a string arrives as a
    number, a list or an object often enough to matter.
    """
    if not media_type:
        return "other"
    if not isinstance(media_type, str):
        media_type = str(media_type)
    t = media_type.strip().lower()
    t = t.split(";")[0].strip()          # drop parameters: profile=, charset=
    # A family name is a legal input, and must map to itself exactly. Leaving
    # this to the substring rules made kind="agent" resolve to "other".
    if t in FAMILIES:
        return t
    if t in _LOOKUP:
        return _LOOKUP[t]
    # Unseen spelling: fall back to substring shape rather than giving up, so a
    # new variant of a known family still lands in the right bucket.
    # Order matters: the most specific shape wins. "webmcp" contains "mcp", and
    # an OASF record contains "agent", so the narrow tests come first.
    if "webmcp" in t:   return "webmcp"
    if ("mcp" in t or "modelcontextprotocol" in t) and "server" in t:
        return "mcp-server"
    if "a2a" in t or "agent-card" in t: return "a2a-agent"
    if "agentfacts" in t or "oasf" in t or "agent-manifest" in t or "nlweb" in t:
        return "agent"
    if "skill" in t:    return "skill"
    if "plugin" in t:   return "plugin"
    if "graphql" in t:  return "graphql"
    if "registry" in t: return "registry"
    if "catalog" in t:  return "catalog"
    if "openapi" in t or "swagger" in t: return "openapi"
    if "asyncapi" in t: return "openapi"
    if "dataset" in t:  return "dataset"
    if "npm" in t or "pypi" in t or "package" in t or "oci.image" in t:
        return "package"
    return "other"


def preferred_type(family: str) -> str | None:
    return _PREFERRED.get(family)


def expand_type_filter(values: list[str]) -> set[str]:
    """Turn a caller's `type` filter into every family it could mean.

    This is the whole point: a client filtering on `application/mcp-server+json`
    means "MCP servers", not "entries whose type string is exactly this". We
    match on family so the filter finds them however the publisher spelled it.
    """
    fams = {media_family(v) for v in values if v}
    # A2A cards and other agent descriptors are different things, but a caller
    # asking for one almost always means "an agent". Expanding across the pair
    # is the same courtesy we already extend across MCP spellings.
    if fams & {"a2a-agent", "agent"}:
        fams |= {"a2a-agent", "agent"}
    return fams


_URN_RE = re.compile(r"^urn:(air|ai):([^:]+):(.+)$", re.I)


def normalize_identifier(ident: str | None) -> str | None:
    """Canonicalise a discovery URN to the spec's `urn:air:` form.

    GitHub emits `urn:ai:` where the spec says `urn:air:`. Anything parsing
    identifiers across registries has to reconcile that or it double-counts the
    same resource. Non-URN identifiers (plain URLs) pass through untouched.
    """
    if not ident:
        return None
    s = ident if isinstance(ident, str) else str(ident)
    s = s.strip()
    m = _URN_RE.match(s)
    if not m:
        return s
    _prefix, publisher, rest = m.groups()
    return f"urn:air:{publisher.lower()}:{rest}"


def publisher_of(ident: str | None, url: str | None = None) -> str | None:
    """The authority segment, which is what publisher-authority binding anchors on."""
    m = _URN_RE.match(str(ident or "").strip())
    if m:
        return m.group(2).lower()
    for candidate in (url, ident):
        candidate = candidate if isinstance(candidate, str) else (
            str(candidate) if candidate else None)
        if candidate and "://" in candidate:
            host = candidate.split("://", 1)[1].split("/", 1)[0]
            return host.split("@")[-1].split(":")[0].lower() or None
    return None


def dedupe_key(ident: str | None, url: str | None) -> str:
    """Identity for merging the same resource seen in several registries.

    The normalised URN when there is one; otherwise the URL with scheme and
    trailing slash stripped, because the same endpoint appears as both http and
    https and with and without a slash across sources.
    """
    n = normalize_identifier(ident)
    if n and n.startswith("urn:"):
        return n
    u = str(url or ident or "").strip().lower()
    u = re.sub(r"^https?://", "", u).rstrip("/")
    return u
