# ard-publish

**Make your API, MCP server or AI agent discoverable by AI agents.**

[![PyPI](https://img.shields.io/pypi/v/ard-publish)](https://pypi.org/project/ard-publish/)
[![Python](https://img.shields.io/pypi/pyversions/ard-publish)](https://pypi.org/project/ard-publish/)
[![Licence](https://img.shields.io/pypi/l/ard-publish)](https://github.com/neuronto/ard-publish/blob/main/LICENSE)

Build, validate and verify an **Agentic Resource Discovery (ARD)** manifest, the
`/.well-known/ard.json` file that lets AI agents find what you offer at runtime,
without anyone installing your tool in advance.

```bash
pip install ard-publish
```

---

## How do I make my API discoverable by AI agents?

Serve an ARD manifest on your own domain describing what you offer, with 2 to 5
representative queries per entry. Registries crawl it. There is no marketplace to apply
to and no allowlist. If you already run an MCP server you do not even need the manifest:
`ard-publish submit https://yourdomain.com/mcp` has the registry handshake with it and
index what the server itself reports.

```python
from ard_publish import Manifest, Entry

m = Manifest(host="example.com", display_name="Example Inc")
m.add(Entry.mcp_server(
    name="weather",
    display_name="Weather API",
    host="example.com",
    url="https://example.com/.well-known/mcp/server-card.json",
    description="Current conditions and forecasts for any location.",
    queries=["what is the weather in Berlin",
             "will it rain in London tomorrow"],
))
m.save(".well-known/ard.json")     # raises if it would not be findable
```

## What is ARD?

**ARD** stands for **Agentic Resource Discovery**: an open specification for how AI
agents find the tools, skills, agents and APIs they need, published in June 2026 by a
working group including Google, Microsoft, Hugging Face, AWS, Cisco, GitHub, Nvidia,
Salesforce and Snowflake.

It answers one question, *"what is available for this task?"*, then gets out of the
way. It is a discovery layer, not a runtime, and it does not replace MCP or A2A.

## Why this package exists

Publishing a manifest is a small job that is easy to get subtly wrong, and **the
failures are silent**. A malformed URN, or entries with no representative queries,
validates as JSON, serves a 200, and is never returned by any search. There is no
error to notice.

`save()` refuses to write a manifest that would not be findable.

## Command line

```bash
ard-publish init example.com > .well-known/ard.json   # scaffold a valid manifest
ard-publish validate .well-known/ard.json             # check it locally
ard-publish check example.com                         # check it for real
ard-publish generate example.com                      # build one from what you already serve
ard-publish submit https://example.com/mcp            # index an MCP server, verified by handshake
ard-publish submit example.com                        # index a domain that serves a manifest
ard-publish status <id|endpoint|domain>               # where a submission stands
ard-publish claim example.com                         # DNS TXT record proving you own the domain
ard-publish verify example.com                        # verify it, receive a key
```

`generate` is for the common case where you have no manifest and do not want to write
one: it probes your domain for an MCP server, an OpenAPI document, an agent card and
`llms.txt`, and emits an entry only for what actually answered, each with the evidence
that produced it. Nothing is guessed.

`claim` and `verify` give you a key. Export it as `ARD_KEY` to raise your rate limit,
and to register private entries: internal services only your own agents should find,
searched in the same query as the public index and never visible to anyone else.

`check` is the one that matters. It fetches your live manifest, validates it, and asks
**every public ARD registry** whether they return your domain for your own
representative queries, because publishing and being indexed are different things.

```
example.com  grade B  82/100

   15/15   Serves a manifest                found
   10/10   Advertised on all four paths     4 of 4
   25/25   Conformance                      0 errors, 0 warnings
   20/20   Entries are searchable           3 of 3 carry representativeQueries
   12/30   Returned by registries           2 of 5 return this domain

  registries returning you:
    yes  Neuronto
     no  GitHub Agent Finder
    yes  WellKnown

  for your own representative queries, who is returned instead of you:
    'what is the weather in Berlin': you rank 4
       ahead: OpenWeather MCP  (12 tools read from its own tools/list; endpoint answered when probed)
```

## How do I publish my MCP server so agents can find it?

Four steps, about ten minutes.

**1. Write the manifest:** `ard-publish init yourdomain.com`

**2. Advertise it on all four discovery paths.** Serving only one makes you invisible
to any client that checks another:

```
/.well-known/ard.json          the path a consumer MUST fetch
Agentmap: <url>                in robots.txt, the agent-facing Sitemap:
<link rel="ard" href="...">    in your page head
DNS service records            optional
```

`Manifest.robots_line()` and `.link_tags()` generate two of those for you.

**3. Tell a registry it exists:** `ard-publish submit yourdomain.com`

This is the step people skip, and it is the one that decides whether any of the previous
work is visible. Serving a manifest is not being indexed. Registries crawl domain lists they
chose; one public registry crawls a top-100,000 list, so a domain outside it is invisible to
that registry indefinitely.

Nothing is taken on your word: the manifest is fetched from your domain at that moment, so a
submission cannot list anything you do not actually publish. If you have an MCP server and
never wrote a manifest, submit the endpoint instead and skip steps 1 and 2 entirely:

```bash
ard-publish submit https://yourdomain.com/mcp
```

A submission that cannot be verified right now is not dropped. The registry keeps it,
answers `pending` with the exact response your endpoint gave, and retries on its own
schedule for about two and a half days, so a deploy that was a minute late or a
server that was down when you typed the command still ends up indexed with no second
command from you. `ard-publish status <id>` (the id is printed) shows where it stands.

**4. Verify:** `ard-publish check yourdomain.com`

## The mistake that costs you everything

Leaving out `representativeQueries`. It is the field registries build their semantic
index from, so an entry without it is a valid catalogue entry that **no search will
ever return**.

Write 2 to 5 per entry, phrased as the request someone would actually make:

| Written for a brochure | Written for retrieval |
| --- | --- |
| enterprise document intelligence | read this PDF and pull out the invoice total |
| scalable web extraction platform | scrape a website that blocks bots |
| unified communications API | send a text message to a phone number |

## Media types

Three spellings for MCP servers are in circulation, and because filters match exactly,
the wrong one gets you silently dropped by registries that do not normalise.
`Entry.mcp_server()` uses `application/mcp-server-card+json`, the spelling the official
conformance tool accepts.

Helpers are provided for each resource kind:

| Helper | Media type |
| --- | --- |
| `Entry.mcp_server()` | `application/mcp-server-card+json` |
| `Entry.agent()` | `application/a2a-agent-card+json` |
| `Entry.skill()` | `application/agent-skills+gzip` |

## API

| | |
| --- | --- |
| `Manifest(host, display_name, documentation_url, did)` | the catalogue |
| `.add(entry)` | attach an entry, inheriting host and identity |
| `.validate()` | list of problems, as plain instructions |
| `.save(path, strict=True)` | write, refusing an unfindable manifest |
| `.robots_line()` / `.link_tags()` | the advertisement snippets |
| `validate(dict)` | validate a manifest you built elsewhere |

## Links

- [Agentic Resource Discovery specification](https://agenticresourcediscovery.org/spec)
- [Publishing guide](https://neuronto.com/publish)
- [Free ARD audit, which registries return you](https://neuronto.com/console)
- [Neuronto, the federated ARD index](https://neuronto.com)
- [Source](https://github.com/neuronto/ard-publish)

## Licence

Apache-2.0
