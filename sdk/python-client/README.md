# neuronto

Find the MCP servers, skills, agents and APIs that can actually do a task.

One call searches this index **and every other public Agentic Resource Discovery
(ARD) registry at once**, fusing the rankings, so you get one answer across the
federation instead of a single catalogue's view.

No account, no API key, no signup. Python 3.9+. **No dependencies.**

## Command line

```bash
pip install neuronto

neuronto "read a PDF and extract tables"
neuronto tools "convert currency"     # individual tools, not whole servers
neuronto stats                        # what the index holds, measured
neuronto dead                         # endpoints that stopped answering
```

Options: `--limit N`, `--kind mcp|api|skill|agent`, `--local` (do not federate),
`--json`, `--key KEY`.

## From code

```python
from neuronto import find_resource, find_tool, registry_stats, liveness

out = find_resource("post a message to Slack", limit=5)
for r in out["results"]:
    print(r["displayName"], r["url"], r["score"])

# Tool-level search. Names come from each server's own tools/list.
for t in find_tool("convert currency", limit=5):
    print(t["tool"], "on", t["server"])
```

### `find_resource(query, *, limit, kind, federate, api_key)`

Returns `{"results": [...], "federation": {...} | None}`. `federation["registries"]`
says which registries answered and how long each took. A search sent with
`api_key` is **not recorded at all**.

### `find_tool(query, *, limit)`

Results carry `tool` (the server's own name for it), `server`, `endpoint`,
`score`, `verified`.

### `registry_stats()`

The measured state of the ecosystem: what share of probed endpoints answer, how
many expose the tools they claim, median response. The window the numbers come
from and their limitations travel in the payload, so a reader who quotes a
number also gets its caveats.

### `liveness(*, dead=False, since=0, limit=500, cursor=0)`

Liveness observations, including the endpoints that stopped answering.
**Free to use, redistribute and build on. No key, no attribution required.**
If you run a registry, `dead=True` is the useful half.

### `publish(*, endpoint="", domain="")`

Get an endpoint or a domain indexed. Verified rather than trusted: the endpoint
has to complete a handshake, or the domain has to serve a manifest that parses.

## About the score

`score` is **semantic relevance only**. It is not a trust, safety or quality
rating and must not be presented as one. `verified` is separate and reports what
was observed by fetching.

## Publishing your own resources

This package is the read side. To build and validate an ARD manifest for your own
domain, use [`ard-publish`](https://pypi.org/project/ard-publish/).

## Privacy

There is no column for an IP address, and a keyed search is not recorded.
Detail, written from the running code: <https://neuronto.com/privacy>.

## Pointing it elsewhere

Set `NEURONTO_BASE` to any other ARD registry's base URL; they implement the same
search interface. The list is at <https://neuronto.com/ard-registries>.

## Licence

Apache-2.0.
