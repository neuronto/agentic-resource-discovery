# Changelog

## 0.1.0

First release.

- Registers the Neuronto ARD registry as an MCP server through the editor's MCP
  server definition provider API, so no `.vscode/mcp.json` editing is needed.
- Adds `Neuronto: Find a tool, MCP server or agent for a task` to the command
  palette, with results showing type, relevance and which registry carried each one.
- Endpoint, search endpoint and federation are all configurable, so the extension
  can be pointed at any other public ARD registry.
