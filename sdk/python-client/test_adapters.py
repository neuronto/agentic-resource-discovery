# -*- coding: utf-8 -*-
"""The adapters must import with none of their frameworks installed, and must
say which package to install when used without it. Run with plain python."""
import importlib
import sys

sys.path.insert(0, ".")
ok = 0
for mod, fn in (("neuronto.langchain", "ard_search_tool"),
                ("neuronto.crewai", "ArdSearchTool"),
                ("neuronto.autogen", "ard_search")):
    m = importlib.import_module(mod)
    assert hasattr(m, fn), f"{mod} lacks {fn}"
    print(f"  import ok  {mod}.{fn}")
    ok += 1

# The two that construct a framework object must fail loudly and helpfully.
for mod, fn, pkg in (("neuronto.langchain", "ard_search_tool", "langchain-core"),
                     ("neuronto.crewai", "ArdSearchTool", "crewai")):
    m = importlib.import_module(mod)
    try:
        importlib.import_module(pkg.replace("-", "_").split("_")[0])
        print(f"  ({pkg} is installed here; skipping the missing-package check)")
        continue
    except ImportError:
        pass
    try:
        getattr(m, fn)()
        raise AssertionError(f"{mod}.{fn} should have raised without {pkg}")
    except ImportError as e:
        assert pkg in str(e), f"error does not name the package: {e}"
        print(f"  missing-package error names {pkg}")

# The renderer is pure and must not need the network.
from neuronto.autogen import _render
out = _render([{"displayName": "X", "type": "application/mcp-server-card+json",
                "url": "https://x/mcp", "score": 91, "description": "does x"}], 5)
assert "X [mcp-server-card+json] score=91 url=https://x/mcp :: does x" in out, out
assert _render([], 5) == "No matching resource found."
print("  renderer ok")
print(f"adapters: {ok} modules import cleanly")
