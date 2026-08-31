<div align="center">

# Neuronto

**The Agentic Resource Discovery index.**

One search across every public ARD registry.

[neuronto.com](https://neuronto.com) · [API](https://neuronto.com/api-docs) · [Manifest](https://neuronto.com/.well-known/ard.json)

</div>

---

## What is ARD?

**ARD** is short for **Agentic Resource Discovery** — an open specification for how
AI agents find the tools, skills, agents and APIs they need, published in June 2026
by a working group including Google, Microsoft, Hugging Face, AWS, Cisco, GitHub,
Nvidia, Salesforce and Snowflake.

An *agentic resource* is anything an AI client can call to get work done: an MCP
server, an A2A agent, a skill, an API, a workflow.

ARD answers one question — **"what is available for this task?"** — and then gets out
of the way. It is not a runtime and does not replace MCP or A2A. It tells an agent
what exists; the agent connects using the resource's own protocol.

## The problem it solves

Today an agent can only use capabilities someone installed for it in advance. Every
tool has to be wired in by hand, and every tool description has to sit in the context
window, competing for space with the actual work. That model does not survive contact
with an ecosystem of thousands of tools, let alone millions.

ARD moves the selection problem out of the context window and into a search service —
the same shift the early web made when it went from curated link directories to
search engines.

For that to work, two sides have to exist. **Publishers** describe what they offer at
a well-known location on their own domain. **Registries** index those descriptions and
answer queries. Neuronto is both.

## Why a federated index

The specification defines three federation modes and makes `auto` the default: a
registry queries its peers, merges their results, and returns one set.

In practice each public registry answers only from its own catalogue, so the same
question asked in four places returns four different answers and the client has to
pick a side. That is the problem Neuronto exists to remove.

Ask Neuronto once and the query fans out across every public ARD registry
concurrently. Results are fused with reciprocal rank fusion, which combines the
orderings rather than the scores — necessary because each registry calibrates
differently, and importing another service's scoring would import its biases with it.

The response says which registries answered and which timed out, so a caller always
knows how much of the federation is behind an answer.

## What it does differently

**Federated by default.** `federation: auto` implemented as specified: concurrent
fan-out under a hard time budget, fused ranking, per-upstream reporting. A slow peer
costs the budget and nothing more.

**Complete conformance.** Passes the specification's official conformance tool as
both a registry and a publisher with zero errors and zero warnings, including the
optional `GET /agents` listing as a properly paginated object.

**Type normalisation.** Three media types for MCP servers are in circulation
(`application/mcp-server`, `application/mcp-server+json`,
`application/mcp-server-card+json`) and two URN prefixes appear as discovery
identifiers (`urn:air:` and `urn:ai:`). Because filters match exactly, entries get
dropped silently. Neuronto normalises both on ingest, so a filter for MCP servers
returns them however the publisher spelled the type.

**Verified liveness.** Indexed endpoints are probed and non-responding ones demoted
in ranking. Registries built on self-published manifests accumulate dead links
quickly; serving them is the fastest way to become the index nobody trusts. Entries
are demoted rather than deleted, because services come back.

**Ranking that separates.** A relevance score is only useful if the gap between the
first and fifth result is legible. Scores are scaled to preserve real separation
instead of compressing everything into a narrow band.

## Where it fits

- **Agent builders** — stop hard-coding integrations. Ask for a capability at runtime
  and connect to whatever currently serves it best.
- **API and tool vendors** — publish one manifest on your own domain and become
  discoverable to every ARD client, without applying to a curated marketplace.
- **Platform teams** — run discovery over internal services so agents inside the
  organisation find them the same way they find public ones.

## Using it

Search this index and the whole federation in one call:

```bash
curl -s https://neuronto.com/search \
  -H 'content-type: application/json' \
  -d '{"query":{"text":"scrape a website behind cloudflare"},"federation":"auto"}'
```

Or install it as an MCP server, so an agent searches from the interface it already
speaks:

```bash
claude mcp add --transport http neuronto https://neuronto.com/mcp
```

### Registry API

| Endpoint | Purpose |
|---|---|
| `POST /search` | Ranked results. `federation`: `auto` (default), `referrals`, `none`. |
| `POST /explore` | Facet counts over the index. |
| `GET /agents` | Deterministic paginated listing, for browsing rather than ranking. |
| `POST /mcp` | The same search as an MCP tool. |

No key and no signup. Relevance scores are semantic only and are never a trust,
compliance or safety rating — the specification is explicit that trust evaluation is
decoupled from discovery.

## Publishing your own resources

Serve a manifest at `/.well-known/ard.json` on your domain describing what you offer.
Neuronto's crawler picks it up; there is no submission form and no allowlist.

Include `representativeQueries` on every entry. It is the term registries build their
semantic index from, and an entry without it is a valid catalogue entry that no
search will ever return.

## Specification

- [Agentic Resource Discovery](https://agenticresourcediscovery.org/spec) — v0.91
- [ards-project/ard-spec](https://github.com/ards-project/ard-spec)

## Licence

Apache-2.0
