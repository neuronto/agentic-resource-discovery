# Neuronto Agent Finder (ARD)

Find the MCP servers, skills, agents and APIs that can actually do a task, from
inside VS Code. Searches every public **Agentic Resource Discovery (ARD)**
registry at once and fuses the rankings, so one query covers the federation
rather than a single catalogue.

No account, no API key, no signup.

## What it does

**Registers the MCP server for you.** VS Code nests servers under `servers`
rather than `mcpServers` and wants the transport stated explicitly, so a config
copied from another editor silently does nothing. Installing this extension
removes that failure mode: the server is registered through the editor's own MCP
provider API and appears in Copilot's agent mode.

**Adds a search command.** `Neuronto: Find a tool, MCP server or agent for a task`
in the command palette. Describe what you need, see what exists with the endpoint
and where each result came from, and open the one you want. Useful outside a chat
turn, and it calls the same interface the MCP tools call, so the two cannot
disagree about what the index holds.

**Never installs anything it finds.** Results are shown; connecting one is always
your explicit step.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `neuronto.endpoint` | `https://neuronto.com/mcp` | The MCP endpoint to register. Point it at any other public ARD registry to use that one instead. |
| `neuronto.searchEndpoint` | `https://neuronto.com/search` | REST endpoint used by the search command. |
| `neuronto.federate` | `true` | Search every other public ARD registry too and fuse the rankings. Off keeps the query on one index. |

The extension is not a lock-in: every setting above points somewhere else if you
want it to, and the list of registries is at
[neuronto.com/ard-registries](https://neuronto.com/ard-registries).

## About the score

Results carry a relevance score. It is **semantic relevance only** and is never a
trust, safety or quality rating. Do not present it to anyone as one.

## Privacy

An anonymous search records the query text, how many results it returned and how
long it took, which powers the report a listed publisher can read about their own
domain. A search made with a domain key is not recorded at all. There is no
column for an IP address. Full detail, written from the running code:
[neuronto.com/privacy](https://neuronto.com/privacy).

## Requirements

VS Code 1.101 or later, which is where the MCP server definition provider API
landed.

## Links

- Setup for every other client: [neuronto.com/connect](https://neuronto.com/connect)
- What ARD is: [neuronto.com/what-is-ard](https://neuronto.com/what-is-ard)
- Source: [github.com/neuronto/agentic-resource-discovery](https://github.com/neuronto/agentic-resource-discovery)

## Licence

Apache-2.0.
