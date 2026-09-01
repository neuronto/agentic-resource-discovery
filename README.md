<div align="center">

# Neuronto Agentic Resource Discovery (ARD) Index

**One search across every public ARD registry, plus a verified index of what MCP servers
actually expose.**

[neuronto.com](https://neuronto.com) · [API](https://neuronto.com/api-docs) · [Submit your server](https://neuronto.com/submit) · [Benchmark](https://neuronto.com/bench) · [Dataset](https://huggingface.co/datasets/AgenticResourceDiscovery/verified-mcp-tools) · [Manifest](https://neuronto.com/.well-known/ard.json)

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
schemas, the thing an agent actually has to match on. Currently **32,183 verified tools
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

**Manifests generated from evidence, not from a form.** Most domains will never author a
manifest by hand. They already run an MCP server, or serve an OpenAPI document, or publish
`llms.txt`, and the manifest is a restatement of things a crawler can already find. Neuronto
probes a domain, emits an entry only for each resource that actually answered, records what
proved it, and hosts the result. Nothing is inferred, because a generated manifest that
guesses would put a claim on somebody's domain that they never made and cannot defend.

**A private half of the index.** The list of internal services an organisation's own agents
may call usually lives in a system prompt, where nothing can search it and nobody can audit
it. A domain that proves ownership by DNS can register those services, and one query then
returns internal and public results together, each labelled with which it is. Private
entries are held in separate storage from the public index rather than behind a flag, so no
public search, count or page can reach them by construction.

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
| `POST /mcp` | Search, tool search, index statistics and publishing, as MCP tools. |
| `POST /tools`, `GET /tools?q=` | Tool level search over verified tools rather than servers. |
| `POST /submit` | Index an MCP endpoint or a manifest-publishing domain. |
| `POST /audit` | Publishing report: discovery, conformance, coverage, competition. |
| `POST /manifest/build` | Generate a manifest for a domain from resources fetched there. |
| `GET /m/{host}.json` | That generated manifest, hosted. |
| `POST /claim`, `POST /claim/verify` | Prove domain ownership by DNS TXT, receive a key. |
| `POST /private/entries` | Register internal services. Key required. |
| `GET /bench`, `GET /adoption` | Retrieval measurement, and who publishes a manifest. |
| `GET /.well-known/ard.json` | Our own publisher manifest. |
| `GET /openapi.json` | OpenAPI 3.1 for everything above. |

No key and no signup for anything that reads the public index. A key exists only to admit a
verified domain's own private entries, and is issued only against a DNS proof of ownership.

Relevance scores are semantic only and are never a trust, compliance or safety rating, the
specification is explicit that trust evaluation is decoupled from discovery.

## Searching tools instead of servers

When you already know the shape of the call you need, the server hosting it is an
implementation detail:

```bash
curl -s 'https://neuronto.com/tools?q=extract+text+from+a+pdf&limit=5'
```

Every tool returned was read from that server's own `tools/list`. The same search is
available to agents as the MCP tool `find_tool`, alongside `find_resource`,
`registry_stats` and `publish_resource`. Only `publish_resource` writes, and it is the only
one declaring `readOnlyHint: false`, so a client can tell from the tool list alone which
call has an effect.

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

## The open dataset

The verified tool corpus is published as an open dataset, CC BY 4.0:
**[huggingface.co/datasets/AgenticResourceDiscovery/verified-mcp-tools](https://huggingface.co/datasets/AgenticResourceDiscovery/verified-mcp-tools)**

`tools.jsonl` carries all 31,411 verified tools with their input schemas, `servers.jsonl`
carries 7,708 introspection results including the auth requirement and failure kind. It
exists because tool-retrieval research (ToolRet, ACL Findings 2025) has been benchmarked on
assembled corpora rather than the live ecosystem, and this is the live ecosystem.

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

Four ways in, in ascending order of effort. There is no allowlist and no signup for any
of them.

**You already run an MCP server.** Submit the endpoint. Neuronto completes an `initialize`
handshake and reads the server's own `tools/list`, which is stronger evidence than a
manifest claim because the server answered for itself.

```bash
curl -X POST https://neuronto.com/submit \
  -H 'content-type: application/json' \
  -d '{"endpoint":"https://example.com/mcp"}'
```

**You are working inside an agent.** The same thing as an MCP tool, so a resource can be
listed from inside a conversation without leaving it.

```json
{"method":"tools/call","params":{"name":"publish_resource",
 "arguments":{"endpoint":"https://example.com/mcp"}}}
```

It verifies rather than trusts, exactly as the HTTP route does, and calls that route rather
than reimplementing it so the two cannot drift apart.

**You have no manifest and do not want to write one.** Ask for one to be generated from
what your domain already exposes, and either copy it or link it.

```bash
curl -X POST https://neuronto.com/manifest/build \
  -H 'content-type: application/json' -d '{"domain":"example.com"}'
```

Only resources that actually answered become entries, and each carries the evidence that
produced it. The hosted copy at `https://neuronto.com/m/example.com.json` says in its own
response headers that it was generated rather than authored by the domain owner.

**You have a manifest.** Serve it at `/.well-known/ard.json` and submit the domain, or wait
for the crawler.

```bash
curl -X POST https://neuronto.com/submit \
  -H 'content-type: application/json' -d '{"domain":"example.com"}'
```

Serve it at `/.well-known/ai-catalog.json` as well. Version 0.91 of the specification
renamed the file, but the deployed base has not moved: of the ARD publishers verified so
far, the large majority still serve only the older path, so a consumer that checks one name
misses most of the ecosystem.

Include `representativeQueries` on every entry. It is the term registries build their
semantic index from, and an entry without it is a valid catalogue entry that no
search will ever return.

### Checking whether it worked

```bash
curl -X POST https://neuronto.com/audit \
  -H 'content-type: application/json' -d '{"domain":"example.com"}'
```

Reports whether the manifest is reachable on each of the four discovery paths, whether it
satisfies the specification entry by entry, **which registries actually return you**, and
**who is returned instead of you** for the queries you asked to be found for, with what
those entries have that you may not. Free, no signup. There is a browser version at
[/console](https://neuronto.com/console).

## Private entries

An organisation can register the internal services its own agents may call, and search
across public and internal resources in one query.

```bash
# 1. ask for the proof record, publish it as a TXT record at your apex
curl -X POST https://neuronto.com/claim \
  -H 'content-type: application/json' -d '{"domain":"example.com"}'

# 2. verify, which returns an API key
curl -X POST https://neuronto.com/claim/verify \
  -H 'content-type: application/json' -d '{"domain":"example.com"}'

# 3. register an internal service
curl -X POST https://neuronto.com/private/entries \
  -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"entry":{"displayName":"Staff Directory","url":"https://internal/mcp",
       "description":"Look up an employee record by name or badge number.",
       "representativeQueries":["look up an employee record"]}}'

# 4. the same key on search admits them, alongside public results
curl -X POST https://neuronto.com/search \
  -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"query":{"text":"look up an employee record"}}'
```

The proof value is derived from the domain and never changes, so asking again does not
invalidate a record already published. It is read over DNS over HTTPS, so it verifies as
soon as the authoritative zone serves it. Verification is the only thing that issues a key.

Every result says whether it came from the public index or from your own entries. Private
entries are never ranked against public ones by corpus statistics, because a single tenant's
index is too small for those statistics to mean anything; they are placed by how much of the
query their own text accounts for, which means the same thing at any size.

## Specification

- [Agentic Resource Discovery](https://agenticresourcediscovery.org/spec), v0.91
- [ards-project/ard-spec](https://github.com/ards-project/ard-spec)

## Licence

Apache-2.0
