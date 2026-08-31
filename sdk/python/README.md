# ard-publish

**Make your API, MCP server or AI agent discoverable by AI agents.**

Build, validate and verify an **Agentic Resource Discovery (ARD)** manifest — the
`/.well-known/ard.json` file that lets AI agents find what you offer at runtime.

```bash
pip install git+https://github.com/neuronto/ard-publish
```

## What this solves

Publishing an ARD manifest is a small job that is easy to get subtly wrong, and the
failures are silent. A catalogue with a malformed URN, or entries with no
representative queries, validates as JSON, serves a 200, and is **never returned by
any search**. There is no error to notice.

This builds one that works, then checks whether registries actually return you.

## Quick start

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

Then advertise it, which `robots_line()` and `link_tags()` write for you:

```
Agentmap: https://example.com/.well-known/ard.json
<link rel="ard" href="https://example.com/.well-known/ard.json">
```

## Command line

```bash
python -m ard_publish init example.com > .well-known/ard.json   # scaffold
python -m ard_publish validate .well-known/ard.json             # check it locally
python -m ard_publish check example.com                         # check it for real
```

`check` is the one that matters. It fetches your live manifest, validates it, and asks
**every public ARD registry** whether they return your domain for your own
representative queries — because publishing and being indexed are different things.

```
example.com  grade B  76/100

   15/15   Serves a manifest                found
   10/10   Advertised on all four paths     4 of 4
   25/25   Conformance                      0 errors, 0 warnings
   20/20   Entries are searchable           2 of 2 carry representativeQueries
    6/30   Returned by registries           1 of 5 return this domain
```

## The mistake this exists to prevent

Leaving out `representativeQueries`. It is the field registries build their semantic
index from, so an entry without it is a valid catalogue entry that no search will ever
return. `save()` refuses by default rather than letting you publish something
unfindable.

Write 2 to 5 per entry, phrased as the request someone would actually make:

| Written for a brochure | Written for retrieval |
|---|---|
| enterprise document intelligence | read this PDF and pull out the invoice total |
| scalable web extraction platform | scrape a website that blocks bots |

## Media types

Three spellings for MCP servers are in circulation and filters match exactly, so the
wrong one gets you silently dropped. `Entry.mcp_server()` uses
`application/mcp-server-card+json`, the one the official conformance tool accepts.

## Links

- [Agentic Resource Discovery specification](https://agenticresourcediscovery.org/spec)
- [Publishing guide](https://neuronto.com/publish)
- [Free ARD audit](https://neuronto.com/console)
- [Neuronto ARD index](https://neuronto.com)

## Licence

Apache-2.0
