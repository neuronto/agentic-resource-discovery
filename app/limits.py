"""Rate limiting for the endpoints that spend something on a caller's behalf.

Most of this API reads a local index and costs a few milliseconds, and limiting
it would only make the product worse. A handful of routes are different: they
make outbound requests to third parties when an anonymous caller asks them to.
`POST /audit` is the extreme case, fetching the target domain on four paths and
then querying five upstream registries with five queries each, so one request
can become roughly twenty five outbound calls. Unlimited, that is a denial of
service amplifier pointed at other people's servers as much as at ours, and the
first sign of trouble would be an upstream blocking us.

**Storage is a separate SQLite file, deliberately.** Two facts force it. The
service runs more than one worker, so an in memory counter would give each
caller one full allowance per worker and lose everything on the restart that a
crash loop guarantees. And the main database is carrying a long running crawl,
where lock contention has already destroyed real work twice; a write on every
limited request is exactly the kind of contention that caused it. A dedicated
file shares state across workers, survives restarts, and cannot touch the index.
It holds nothing of value and can be deleted at any time.

Fixed windows rather than a sliding log: one row per caller per window instead
of one per request, which is the difference between a table that stays small on
its own and one that needs pruning to stay usable.

**It fails open.** A limiter that returns 500 when its own storage is unhappy
has converted a capacity problem into an outage. Every error path here allows
the request and moves on.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

from . import config

_DB_PATH = Path(os.getenv("NEURONTO_LIMITS_DB",
                          str(config.DATA_DIR / "limits.db")))
_conn: sqlite3.Connection | None = None

ENABLED = os.getenv("NEURONTO_LIMITS", "1").strip().lower() not in ("0", "false", "no")

# Per caller, per window. Anonymous first, then the allowance for a caller
# presenting a key for a DNS verified domain: proving you own a domain is the
# costly, human step, so it is a reasonable thing to reward, and it gives an
# integrator a way out of the limit that does not involve asking us.
#
# (limit, window_seconds, verified_limit)
RULES: dict[str, tuple[int, int, int]] = {
    # ~25 outbound calls each, by far the most expensive thing we offer.
    "audit":          (30,   3600,  200),
    # Up to 11 outbound fetches against a caller-supplied host.
    "manifest_build": (60,   3600,  300),
    # One MCP handshake or manifest fetch each.
    "submit":         (60,   3600,  300),
    # One DNS-over-HTTPS lookup each, cheap but still somebody else's resolver.
    "claim_verify":   (100,  3600,  400),
    # Deliberately loose. This makes no outbound call and writes nothing: the
    # token is a hash of the domain, so asking twice returns the same value and
    # there is no state to exhaust. Set to 60 at first out of symmetry, which
    # was wrong, it only refused honest callers reading the docs.
    "claim":          (200,  3600,  600),
    # Key required already, so this only bounds a compromised or runaway key.
    "private_write":  (300,  3600,  300),
    # The product. Only the federated mode costs anything outbound, and this is
    # deliberately loose: five requests a second sustained, per caller.
    "search_fed":     (300,  60,    600),
}

# A ceiling on outbound work in flight, per worker. Per caller limits bound any
# one abuser; this bounds all of them together, which is the number that decides
# whether the box stays responsive. In memory is correct here: it guards this
# worker's own event loop, and there is nothing to share.
_OUTBOUND_MAX = int(os.getenv("NEURONTO_OUTBOUND_CONCURRENCY", "12"))
_outbound = asyncio.Semaphore(_OUTBOUND_MAX)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
  bucket  TEXT NOT NULL,          -- rule name
  who     TEXT NOT NULL,          -- caller identity, an address or a key
  window  INTEGER NOT NULL,       -- window start, unix seconds
  n       INTEGER NOT NULL,
  PRIMARY KEY (bucket, who, window)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_hits_window ON hits(window);
"""


def _db() -> sqlite3.Connection | None:
    global _conn
    if _conn is not None:
        return _conn
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), timeout=2, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        # This data is disposable, so durability is not worth an fsync per hit.
        c.execute("PRAGMA synchronous=OFF")
        c.execute("PRAGMA busy_timeout=2000")
        c.executescript(_SCHEMA)
        c.commit()
        _conn = c
        return c
    except Exception:
        return None


def client_id(request) -> str:
    """Who is calling, as well as can be known from behind a proxy.

    `CF-Connecting-IP` is set by the edge and is the only header here that is
    not caller-controlled on our path. It is spoofable by anyone who reaches the
    origin directly, which is why this is abuse control and never an
    authorisation boundary.
    """
    h = request.headers
    ip = (h.get("cf-connecting-ip")
          or (h.get("x-forwarded-for") or "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    return ip or "unknown"


def caller(request) -> tuple[str, bool]:
    """Identity and whether it belongs to a verified domain.

    A key identifies its holder better than an address does, so a key holder is
    limited as themselves rather than sharing an allowance with everyone behind
    the same egress address.
    """
    auth = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    if auth.startswith("nk_"):
        return f"key:{auth[:16]}", True
    return f"ip:{client_id(request)}", False


def check(rule: str, request) -> tuple[bool, int, dict]:
    """Count this request. Returns (allowed, retry_after_seconds, headers)."""
    spec = RULES.get(rule)
    if not ENABLED or spec is None:
        return True, 0, {}
    limit, window, verified_limit = spec
    who, is_verified = caller(request)
    if is_verified:
        limit = verified_limit

    c = _db()
    if c is None:
        return True, 0, {}                      # fail open: no storage, no limit

    now = int(time.time())
    start = now - (now % window)
    try:
        c.execute("""INSERT INTO hits(bucket,who,window,n) VALUES(?,?,?,1)
                     ON CONFLICT(bucket,who,window) DO UPDATE SET n = n + 1""",
                  (rule, who, start))
        n = c.execute("SELECT n FROM hits WHERE bucket=? AND who=? AND window=?",
                      (rule, who, start)).fetchone()[0]
        c.commit()
        # Old windows are dropped opportunistically rather than on a timer, so
        # there is no job to forget to run and nothing to schedule.
        if n % 200 == 0:
            c.execute("DELETE FROM hits WHERE window < ?", (now - 86400,))
            c.commit()
    except Exception:
        return True, 0, {}                      # fail open

    remaining = max(0, limit - n)
    reset = start + window
    headers = {"x-ratelimit-limit": str(limit),
               "x-ratelimit-remaining": str(remaining),
               "x-ratelimit-reset": str(reset)}
    if n > limit:
        return False, max(1, reset - now), headers
    return True, 0, headers


# Why each rule exists, in the caller's terms. Kept per rule rather than shared,
# because a single sentence about outbound requests was wrong for `/claim`,
# which makes none: it was refusing people with an untrue explanation.
REASONS: dict[str, str] = {
    "audit":          ("this endpoint fetches your domain and then queries every other "
                       "public registry about it, so one call becomes roughly twenty "
                       "five requests to other people's servers"),
    "manifest_build": ("this endpoint probes a domain you name, so one call becomes "
                       "several requests to a server that did not ask for them"),
    "submit":         "this endpoint connects to the endpoint you name and reads its tool list",
    "claim_verify":   "this endpoint performs a DNS lookup for the domain you name",
    "claim":          ("this endpoint issues verification tokens, and a token is only "
                       "useful once per domain"),
    "private_write":  "this writes to your registry",
    "search_fed":     ("federated search queries every other public registry, so one "
                       "call becomes several requests to other people's servers"),
}


def too_many(rule: str, retry_after: int, headers: dict, verified: bool) -> dict:
    """The body for a refusal. Says what to do, not just that you may not."""
    limit, window, vlimit = RULES.get(rule, (0, 0, 0))
    per = "hour" if window >= 3600 else f"{window} seconds"
    out = {
        "error": "rate_limited",
        "detail": (f"{vlimit if verified else limit} requests per {per} for this "
                   f"endpoint, because {REASONS.get(rule, 'it is expensive to serve')}. "
                   f"Retry in {retry_after} seconds."),
        "retryAfter": retry_after,
    }
    if not verified:
        out["raiseTheLimit"] = (
            f"prove you own a domain and send the key as a bearer token: "
            f"POST {config.PUBLIC_BASE}/claim. Verified callers get {vlimit} per {per}.")
    return out


class outbound:
    """Bound total outbound work in flight in this worker.

    Used as `async with limits.outbound():` around a fan-out.
    """

    async def __aenter__(self):
        await _outbound.acquire()
        return self

    async def __aexit__(self, *exc):
        _outbound.release()
        return False


def stats() -> dict:
    """What the limiter is doing, for /metrics.json."""
    c = _db()
    out: dict = {"enabled": ENABLED, "outbound_concurrency": _OUTBOUND_MAX,
                 "rules": {k: {"limit": v[0], "window_s": v[1], "verified_limit": v[2]}
                           for k, v in RULES.items()}}
    if c is None:
        out["storage"] = "unavailable, limiter is failing open"
        return out
    try:
        now = int(time.time())
        out["active_windows"] = c.execute(
            "SELECT COUNT(*) FROM hits WHERE window > ?", (now - 3600,)).fetchone()[0]
        out["limited_requests_last_hour"] = c.execute(
            "SELECT COALESCE(SUM(n),0) FROM hits WHERE window > ?",
            (now - 3600,)).fetchone()[0]
    except Exception:
        out["storage"] = "unreadable"
    return out
