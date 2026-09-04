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
# Multi-step discovery makes a model call per request. Off until staging has
# shown it answers well and within budget; on there first, then here.
PLAN_ENABLED = os.getenv("NEURONTO_PLAN_ENABLED", "0").strip().lower() in ("1", "true", "yes")
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
# 700, not 1800. Measured 2026-09-04 at 1800/900/700/500 ms: every value from
# 900 down returned the same four upstreams and the same sixty upstream results
# as 1800 did. The only leg that ever needed the long budget was one that is
# dead (Hugging Face Discover, unreachable at 12 s), so the long budget bought
# nothing but a 1.9 s wait on every federated search. 700 leaves headroom over
# the slowest live leg (~500 ms) and halves the latency.
FEDERATION_BUDGET_MS = _i("NEURONTO_FED_BUDGET_MS", 700)
# Well under the budget on purpose: an upstream that hangs must surface as
# ITS timeout (a breaker failure) before OUR budget expires (not one).
UPSTREAM_TIMEOUT_S   = float(os.getenv("NEURONTO_UPSTREAM_TIMEOUT", "0.5"))
FEDERATION_ENABLED   = _b("NEURONTO_FEDERATION", True)

# Concurrency cap on live fan-outs, per worker process. Each fan-out is one
# request to every upstream, so this bounds what we send to other people's
# registries as much as what we spend ourselves. ARD Registry Hub answered 429
# to every request at sixteen concurrent fan-outs. A caller who cannot get a
# slot within FED_SHED_WAIT_S gets the local answer with the federation block
# saying `capacity`, not a slow one and not an error.
FED_MAX_INFLIGHT = _i("NEURONTO_FED_MAX_INFLIGHT", 4)
FED_SHED_WAIT_S  = float(os.getenv("NEURONTO_FED_SHED_WAIT", "0.15"))

# Circuit breaker per upstream. After FED_BREAKER_FAILS consecutive failures an
# upstream is skipped for FED_BREAKER_COOLDOWN_S, then allowed one probe; a
# success closes the circuit. A skipped upstream is still listed in the
# response as `circuit open`, so the federation claim stays honest.
# Five and 120 s, not three and 300 s. At 60 visitors/s an upstream that is
# merely slow under our own fan-out trips a 0.5 s timeout a few times in a row,
# and with a threshold of three WellKnown opened twelve times in 25 s and sat
# dark for five minutes each time while perfectly healthy. Five consecutive
# failures still catches a dead host or a 429 storm within a couple of seconds
# of load; two minutes is long enough to stop hammering it and short enough
# that a healthy upstream is back before anyone notices.
FED_BREAKER_FAILS      = _i("NEURONTO_FED_BREAKER_FAILS", 5)
FED_BREAKER_COOLDOWN_S = _i("NEURONTO_FED_BREAKER_COOLDOWN", 120)

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
# The bound on a whole probe, not on one request within it. Redirects are
# followed, so the timeout above is a per-hop budget and cannot bound a probe.
LIVENESS_DEADLINE_S  = float(os.getenv("NEURONTO_LIVENESS_DEADLINE", "12"))
LIVENESS_CONCURRENCY = _i("NEURONTO_LIVENESS_CONCURRENCY", 12)
# How many probes are committed together. Small enough that a killed sweep
# loses little, large enough that the writer is not opened once per host.
LIVENESS_CHUNK       = _i("NEURONTO_LIVENESS_CHUNK", 100)
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
# Waited on AFTER the fan-out returns, so this is the extra allowance beyond
# the federation budget, not a parallel one. 1.6 s on top of a 700 ms budget
# would put the worst case back above two seconds.
EMBED_QUERY_TIMEOUT_S = float(os.getenv("NEURONTO_EMBED_QUERY_TIMEOUT", "0.3"))
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
