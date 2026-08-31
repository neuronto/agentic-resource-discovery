# Neuronto - Agentic Resource Discovery (ARD) Index

**Alias package.** This installs [`ard-publish`](https://pypi.org/project/ard-publish/)
and re-exports it, so the tool is installable under the full term as well as the
short one.

```bash
pip install agentic-resource-discovery      # this package
pip install ard-publish                     # identical, shorter to type
```

Both give you the same module and the same CLI.

## What it does

Build, validate and verify an **Agentic Resource Discovery (ARD)** manifest — the
`/.well-known/ard.json` file that lets AI agents find your API, MCP server or agent at
runtime, without anyone installing it in advance.

```python
from agentic_resource_discovery import Manifest, Entry

m = Manifest(host="example.com", display_name="Example Inc")
m.add(Entry.mcp_server(
    name="weather", display_name="Weather API", host="example.com",
    url="https://example.com/.well-known/mcp/server-card.json",
    description="Current conditions and forecasts for any location.",
    queries=["what is the weather in Berlin", "will it rain in London tomorrow"],
))
m.save(".well-known/ard.json")
```

```bash
ard-publish check example.com     # which registries actually return you
```

## What is ARD?

**ARD** stands for **Agentic Resource Discovery**: the open specification for how AI
agents find the tools, skills, agents and APIs they need, published in June 2026 by a
working group including Google, Microsoft, Hugging Face, AWS, Cisco, GitHub, Nvidia,
Salesforce and Snowflake.

Full documentation lives on the main package:
**[pypi.org/project/ard-publish](https://pypi.org/project/ard-publish/)**

## Links

- [Neuronto - Agentic Resource Discovery (ARD) Index](https://neuronto.com)
- [Publishing guide](https://neuronto.com/publish)
- [Free ARD audit](https://neuronto.com/console)
- [ARD specification](https://agenticresourcediscovery.org/spec)
- [Source](https://github.com/neuronto/ard-publish)

## Licence

Apache-2.0
