"""Neuronto configuration.

Deliberately dependency-light. Everything here is read once at import so a
request never pays for configuration lookup, which matters because the whole
product claim is latency.
"""
from __future__ import annotations

import os
from pathlib import Path

def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

def _i(name: str, default: int) -> int:
    try: return int(os.getenv(name, "").strip() or default)
    except ValueError: return default

PUBLIC_BASE = os.getenv("NEURONTO_BASE", "https://neuronto.com").rstrip("/")
HOST_NAME   = "Neuronto"
PUBLISHER   = "neuronto.com"

DATA_DIR = Path(os.getenv("NEURONTO_DATA", "./data"))
DB_PATH  = Path(os.getenv("NEURONTO_DB", str(DATA_DIR / "neuronto.db")))

# Search defaults, straight from the spec (§5.3.2).
PAGE_SIZE_DEFAULT = 10
PAGE_SIZE_MAX     = 100

# Federation. `auto` is the spec default and the thing no other registry
# implements. Upstreams are mirrored into our own index by the ingest job, so
# the fast path never touches the network; `auto` adds a live fan-out on top
# for freshness, bounded hard so one slow upstream cannot blow the budget.
FEDERATION_BUDGET_MS = _i("NEURONTO_FED_BUDGET_MS", 1800)
UPSTREAM_TIMEOUT_S   = float(os.getenv("NEURONTO_UPSTREAM_TIMEOUT", "1.7"))
FEDERATION_ENABLED   = _b("NEURONTO_FEDERATION", True)

UPSTREAMS = [
    # (id, display name, search URL, the value we report as `source`)
    ("github",      "GitHub Agent Finder",
     "https://agentfinder.github.com/api/v1/search",  "https://agentfinder.github.com/api/v1"),
    ("wellknown",   "WellKnown",
     "https://wellknownhq.com/registry/search",       "https://wellknownhq.com/registry"),
    ("huggingface", "Hugging Face Discover",
     "https://huggingface-hf-discover.hf.space/search","https://huggingface-hf-discover.hf.space"),
    ("desvela",     "Desvela ARD Registry",
     "https://registry.desvela.dev/search",           "https://registry.desvela.dev"),
]

# Liveness. An entry that does not answer is not a discovery result, it is
# noise. We measured 1.7% of one ERC-8004 chain's registered endpoints
# responding, so this is not hypothetical.
LIVENESS_TIMEOUT_S   = float(os.getenv("NEURONTO_LIVENESS_TIMEOUT", "6"))
LIVENESS_MAX_AGE_H   = _i("NEURONTO_LIVENESS_MAX_AGE_H", 24)
LIVENESS_CONCURRENCY = _i("NEURONTO_LIVENESS_CONCURRENCY", 12)
# Dead entries are demoted, never silently deleted: a service can come back,
# and a registry that forgets is as bad as one that lies.
DEAD_PENALTY = float(os.getenv("NEURONTO_DEAD_PENALTY", "0.35"))

USER_AGENT = os.getenv(
    "NEURONTO_UA",
    "Neuronto/1.0 (+https://neuronto.com/about; ARD registry; crawler)")

CRAWL_CONCURRENCY = _i("NEURONTO_CRAWL_CONCURRENCY", 8)
CRAWL_TIMEOUT_S   = float(os.getenv("NEURONTO_CRAWL_TIMEOUT", "10"))

# Egress pool for crawling, supplied by the operator. Comma separated, read from
# the environment so nothing about it is ever committed. Empty means direct.
CRAWL_PROXIES = [p.strip() for p in os.getenv("NEURONTO_CRAWL_PROXIES", "").split(",") if p.strip()]
