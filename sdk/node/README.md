# neuronto

Find the MCP servers, skills, agents and APIs that can actually do a task.

One call searches this index **and every other public Agentic Resource Discovery
(ARD) registry at once**, fusing the rankings, so you get one answer across the
federation instead of a single catalogue's view.

No account, no API key, no signup. Node 18+. Zero dependencies.

## Command line, no install

```bash
npx neuronto "read a PDF and extract tables"
npx neuronto tools "convert currency"     # individual tools, not whole servers
npx neuronto stats                        # what the index holds, measured
npx neuronto dead                         # endpoints that stopped answering
```

Options: `--limit N`, `--kind mcp|api|skill|agent`, `--local` (do not federate),
`--json`, `--key KEY`.

## From code

```js
import { findResource, findTool, registryStats, liveness } from 'neuronto';

const { results, federation } = await findResource('post a message to Slack');
for (const r of results) {
  console.log(r.displayName, r.url, r.score);
}

// Individual tools, with the name each server gave it in its own tools/list
const tools = await findTool('convert currency', { limit: 5 });
console.log(tools[0].tool, 'on', tools[0].server);
```

### `findResource(query, opts)`

`opts`: `limit` (default 10), `kind` (a media type), `federate` (default `true`),
`apiKey`. Returns `{ results, federation }`. `federation.registries` says which
registries answered and how long each took.

### `findTool(query, opts)`

Tool-level search. Every tool was read from that server's own `tools/list`, so
the name and description are the server's, not a directory's summary. Results
carry `tool`, `server`, `endpoint`, `score`, `verified`.

### `registryStats()`

The measured state of the ecosystem: what share of probed endpoints answer, how
many expose the tools they claim, median response. The window the numbers come
from and their limitations travel in the payload.

### `liveness({ dead, since, limit, cursor })`

Liveness observations, including the endpoints that stopped answering.
**Free to use, redistribute and build on. No key, no attribution required.**
If you run a registry, `{ dead: true }` is the useful half.

### `publish({ endpoint, domain })`

Get a domain or an MCP endpoint indexed. Verified rather than trusted: the
endpoint has to complete a handshake, or the domain has to serve a manifest that
parses. A busy index answers `202` with a queue id and retries on its own.

## About the score

`score` is **semantic relevance only**. It is not a trust, safety or quality
rating and must not be presented to anyone as one. `verified` is separate and
reports what was observed by fetching: whether the endpoint answered and what
its own `tools/list` returned.

## Privacy

An anonymous search records the query text and how many results it returned,
which powers the report a listed publisher can read about their own domain.
A search with a domain key is not recorded at all. There is no column for an IP
address. Detail: [neuronto.com/privacy](https://neuronto.com/privacy).

## Pointing it somewhere else

Set `NEURONTO_BASE` to any other ARD registry's base URL. They implement the same
search interface; the list is at
[neuronto.com/ard-registries](https://neuronto.com/ard-registries).

## Links

- Setup for editors and CLIs: [neuronto.com/connect](https://neuronto.com/connect)
- What ARD is: [neuronto.com/what-is-ard](https://neuronto.com/what-is-ard)
- Source: [github.com/neuronto/agentic-resource-discovery](https://github.com/neuronto/agentic-resource-discovery)

## Licence

Apache-2.0.
