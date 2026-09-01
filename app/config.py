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
    # Found 2026-09-01 while tracing how publishers get discovered. It bills
    # itself as "the first public, neutral registry for the ARD specification"
    # and does rank for submission queries, but it serves search at /api/search
    # rather than the spec's /search and has no /agents or /explore, so a
    # conformant client cannot find it. Federating it anyway: the point of
    # federation is to reach indexes clients cannot, and it carries entries
    # (largely mirrored from Ora) that no other upstream gives us.
    ("ardregistry", "ARD Registry Hub",
     "https://ardregistry.org/api/search",            "https://ardregistry.org"),
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

# Tool-level introspection. We handshake with an indexed MCP endpoint and read
# `tools/list`, which turns a publisher's claim into evidence. Read-only: we
# never call a tool. Kept cheap because introspecting somebody's server is a
# courtesy they did not explicitly grant.
INTROSPECT_TIMEOUT_S   = float(os.getenv("NEURONTO_INTROSPECT_TIMEOUT", "12"))
INTROSPECT_CONCURRENCY = _i("NEURONTO_INTROSPECT_CONCURRENCY", 12)
INTROSPECT_MAX_AGE_H   = _i("NEURONTO_INTROSPECT_MAX_AGE_H", 168)   # weekly

# Dense retrieval. Sparse BM25 fused with dense vectors through RRF is the
# configuration SCOUT reports running in production at PayPal, and ToolRet
# (ACL 2025) is the evidence that lexical-only tool retrieval underperforms.
# Absent a key the whole leg is skipped and search behaves exactly as before.
EMBED_API_KEY  = os.getenv("DEEPINFRA_API_KEY", "").strip()
EMBED_URL      = os.getenv("NEURONTO_EMBED_URL",
                           "https://api.deepinfra.com/v1/openai/embeddings")
EMBED_MODEL    = os.getenv("NEURONTO_EMBED_MODEL", "BAAI/bge-m3")
EMBED_BATCH    = _i("NEURONTO_EMBED_BATCH", 64)
EMBED_TIMEOUT_S = float(os.getenv("NEURONTO_EMBED_TIMEOUT", "20"))
# The query-time budget. Dense runs only alongside federation, never on the
# fast path, so it must fit inside the federation budget or be dropped.
EMBED_QUERY_TIMEOUT_S = float(os.getenv("NEURONTO_EMBED_QUERY_TIMEOUT", "1.6"))
EMBED_CACHE_S  = _i("NEURONTO_EMBED_CACHE_S", 300)
DENSE_ENABLED  = _b("NEURONTO_DENSE", True)

# Adoption tracking. The watchlist is public and deliberately includes the
# organisations in the ARD working group, because the interesting measurement
# is whether the people who wrote the spec publish under it.
ADOPTION_WATCHLIST = [
    "openai.com", "anthropic.com", "github.com", "huggingface.co",
    "cloudflare.com", "microsoft.com", "google.com", "aws.amazon.com",
    "stripe.com", "salesforce.com", "cisco.com", "snowflake.com",
    "nvidia.com", "vercel.com", "netlify.com", "shopify.com",
    "atlassian.com", "notion.so", "slack.com", "zapier.com",
]
ADOPTION_TIMEOUT_S   = float(os.getenv("NEURONTO_ADOPTION_TIMEOUT", "8"))
ADOPTION_CONCURRENCY = _i("NEURONTO_ADOPTION_CONCURRENCY", 8)

# Benchmark. Ground truth comes from the publishers themselves: an entry's
# `representativeQueries` is that publisher stating what they should be found
# for, so "does registry X return entry E for query Q" is answerable without
# anyone hand-labelling anything, and identically for every registry.
BENCH_K            = _i("NEURONTO_BENCH_K", 10)
BENCH_TASKS        = _i("NEURONTO_BENCH_TASKS", 120)
BENCH_TIMEOUT_S    = float(os.getenv("NEURONTO_BENCH_TIMEOUT", "12"))
BENCH_CONCURRENCY  = _i("NEURONTO_BENCH_CONCURRENCY", 4)

# Egress pool for crawling, supplied by the operator. Comma separated, read from
# the environment so nothing about it is ever committed. Empty means direct.
CRAWL_PROXIES = [p.strip() for p in os.getenv("NEURONTO_CRAWL_PROXIES", "").split(",") if p.strip()]
