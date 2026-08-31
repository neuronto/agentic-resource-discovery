<div align="center">

# Neuronto

**The Agentic Resource Discovery index.**

One search across every public ARD registry.

[neuronto.com](https://neuronto.com) · [API](https://neuronto.com/api-docs) · [Manifest](https://neuronto.com/.well-known/ard.json)

</div>

---

## What is ARD?

**ARD** is short for **Agentic Resource Discovery**, an open specification for how
AI agents find the tools, skills, agents and APIs they need, published in June 2026
by a working group including Google, Microsoft, Hugging Face, AWS, Cisco, GitHub,
Nvidia, Salesforce and Snowflake.

An *agentic resource* is anything an AI client can call to get work done: an MCP
server, an A2A agent, a skill, an API, a workflow.

ARD answers one question, **"what is available for this task?"**, and then gets out
of the way. It is not a runtime and does not replace MCP or A2A. It tells an agent
what exists; the agent connects using the resource's own protocol.

## The problem it solves

Today an agent can only use capabilities someone installed for it in advance. Every
tool has to be wired in by hand, and every tool description has to sit in the context
window, competing for space with the actual work. That model does not survive contact
with an ecosystem of thousands of tools, let alone millions.

ARD moves the selection problem out of the context window and into a search service, the same shift the early web made when it went from curated link directories to
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
orderings rather than the scores, necessary because each registry calibrates
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

**A verified tool index, not just a server index.** Every other registry stores a server
name and whatever prose its publisher wrote. Neuronto handshakes with each indexed MCP
endpoint and reads its `tools/list`, so the index holds the real tool names and input
schemas, the thing an agent actually has to match on. Currently **31,411 verified tools
across 2,223 servers**, plus **1,918 endpoints recorded as requiring credentials**, which
no other registry reports. Introspection is read only: a tool is never called.

**Hybrid retrieval.** Sparse BM25 and dense vectors, fused with the same reciprocal rank
fusion used for federation, so one query runs lexical, semantic and federated retrieval and
returns a single ordering. The dense leg rides inside the federation budget and contributes
nothing if it is unavailable, so the lexical fast path is never slowed by it.

**Verified liveness.** Indexed endpoints are probed and non-responding ones demoted
in ranking. Registries built on self-published manifests accumulate dead links
quickly; serving them is the fastest way to become the index nobody trusts. Entries
are demoted rather than deleted, because services come back.

**Ranking that separates.** A relevance score is only useful if the gap between the
first and fifth result is legible. Scores are scaled to preserve real separation
instead of compressing everything into a narrow band.

## Where it fits

- **Agent builders**, stop hard-coding integrations. Ask for a capability at runtime
  and connect to whatever currently serves it best.
- **API and tool vendors**, publish one manifest on your own domain and become
  discoverable to every ARD client, without applying to a curated marketplace.
- **Platform teams**, run discovery over internal services so agents inside the
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
compliance or safety rating, the specification is explicit that trust evaluation is
decoupled from discovery.

## Searching tools instead of servers

When you already know the shape of the call you need, the server hosting it is an
implementation detail:

```bash
curl -s 'https://neuronto.com/tools?q=extract+text+from+a+pdf&limit=5'
```

Every tool returned was read from that server's own `tools/list`. The same search is
available to agents as the MCP tool `find_tool`, alongside `find_resource` and
`registry_stats`.

## Measuring whether any of this works

`GET /bench` publishes ARD-Bench, a head to head retrieval measurement across the public
ARD registries. Ground truth is the publishers' own `representativeQueries`, so nothing is
hand labelled, and the harness is `app/bench.py` in this repository.

The response separates two things that are easy to confuse: `coverage`, whether a registry
indexes the target at all, and `recall@k_when_carried`, whether it retrieves the target
when it does hold it. It also states its own known bias, and it reports the results that
do not flatter us. In the current run, federated search scores slightly below lexical only
and costs far more latency.

## Who publishes an ARD manifest

`GET /adoption` tracks adoption of the specification itself: a named watchlist of
organisations, and the manifest rate across every host the crawler has seen. At the time
of writing, three of the twenty organisations on the watchlist publish a manifest:
Hugging Face, Vercel and Zapier. All three serve it at `/.well-known/ai-catalog.json`,
the path v0.91 renamed. Of 178 publishers our crawler has found, **157 serve the older
`ai-catalog.json` and only 14 serve `ard.json`**, which is why the tracker checks both:
measuring the path rather than the practice gets the answer wrong.

## The badge

If your MCP server is in the index, a badge states what we verified: how many tools your
server returned to `tools/list`, and whether the endpoint answers.

```markdown
[![Neuronto verified tools](https://neuronto.com/badge/your.publisher.id.svg)](https://neuronto.com/console?domain=your.publisher.id)
```

The publisher id is the publisher segment of your URN, or your domain. The badge is a
statement about what was observed, never a trust, safety or quality rating. Not indexed
yet? Publish a manifest and the crawler will find you, or run the
[console audit](https://neuronto.com/console).

## Publishing your own resources

Serve a manifest at `/.well-known/ard.json` on your domain describing what you offer.
Neuronto's crawler picks it up; there is no submission form and no allowlist.

Include `representativeQueries` on every entry. It is the term registries build their
semantic index from, and an entry without it is a valid catalogue entry that no
search will ever return.

## Specification

- [Agentic Resource Discovery](https://agenticresourcediscovery.org/spec), v0.91
- [ards-project/ard-spec](https://github.com/ards-project/ard-spec)

## Licence

Apache-2.0
