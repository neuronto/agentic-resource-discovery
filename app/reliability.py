"""How reliably an indexed endpoint answers, over time.

Every registry in this field publishes a list. A list says an endpoint existed
once. It cannot say whether the thing answers today, and it certainly cannot say
whether it answered yesterday and the day before. We probe on a timer, so we can.

**Why this needed new storage.** `observations` records state *changes*, which is
the right shape for a timeline and the wrong shape for a rate: an endpoint probed
hourly for a month and always up leaves exactly one row, which is
indistinguishable from one probed once and never revisited. The denominator was
being discarded an hour at a time. Counters on the entry restore it at O(1) per
probe and no row growth, and the transition table keeps doing what it is good at.

**Three rules this module will not break.**

  * *A rate needs a denominator worth dividing by.* One probe that succeeded is
    not "100% uptime", it is one probe that succeeded. Below `MIN_PROBES` we
    report the count and refuse the percentage, because a confident number from
    n=1 is worse than no number: it gets quoted.

  * *Report the interval, not just the point.* 9 of 10 and 900 of 1000 are both
    90% and are not remotely the same claim. The Wilson lower bound separates
    them (73.2% against 88.0%) and is what a reader should rank on, so we publish
    both and say which is which.

  * *This is not an SLA and must never be sold as one.* An SLA is a promise made
    by whoever runs the service, backed by a remedy. This is a third party's
    observation from outside, with our own network and timeouts in the path. We
    say `observed_uptime`, never `guaranteed`, `SLA` or `%uptime` bare.

A reachable endpoint is also not a good one. Everything here answers "does it
respond", which is a floor under quality and never a measure of it.
"""
from __future__ import annotations

import math
import sqlite3
import time

# Below this many probes we publish the count and withhold the rate. Set from
# the probe cadence: the liveness timer runs roughly hourly, so five probes is
# a handful of hours of evidence, which is the least that can honestly be
# called "over time".
MIN_PROBES = 5

# Bands, so a consumer can filter without inventing its own thresholds. Cut on
# the Wilson lower bound rather than the point estimate, which is the whole
# reason for computing it: a band should express confidence, not luck.
BANDS = ((0.98, "steady"), (0.90, "mostly"), (0.50, "flaky"))


def wilson_lower(ok: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson score interval for ok/n.

    Chosen over the normal approximation because it stays inside [0,1] and
    behaves at the extremes, which is exactly where this data lives: most
    endpoints are at or near 1.0, where the naive interval runs off the end.
    """
    if n <= 0:
        return 0.0
    p = ok / n
    d = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / d)


def _band(lo: float) -> str:
    for cut, name in BANDS:
        if lo >= cut:
            return name
    return "unreliable"


def summarise(row) -> dict:
    """Reliability for one entry row, from its counters.

    Accepts anything subscriptable that carries the four counters, so callers
    can pass a `sqlite3.Row` straight out of a join without re-querying.
    """
    def g(k, default=0):
        try:
            v = row[k]
        except (KeyError, IndexError, TypeError):
            return default
        return default if v is None else v

    n, ok = int(g("probe_n")), int(g("probe_ok"))
    first, ms_sum = g("probe_first", None), int(g("probe_ms_sum"))
    last = g("live_checked", None)

    out: dict = {
        "probes": n,
        "answered": ok,
        "first_probe": first,
        "last_probe": last,
        "currently_answering": (None if g("live", None) is None else bool(row["live"])),
    }
    if ok:
        out["mean_response_ms"] = int(round(ms_sum / ok))
    if n < MIN_PROBES:
        out["observed_uptime_pct"] = None
        out["confidence"] = "insufficient"
        out["why"] = (f"{n} probe(s); a rate is withheld below {MIN_PROBES}. "
                      "The count is the honest answer at this sample size.")
        return out

    lo = wilson_lower(ok, n)
    out["observed_uptime_pct"] = round(100.0 * ok / n, 1)
    out["lower_bound_pct"] = round(100.0 * lo, 1)
    out["band"] = _band(lo)
    out["confidence"] = "observed"
    out["basis"] = ("share of our probes that got any answer, with the 95% Wilson "
                    "lower bound. Not an SLA: measured from outside, by a third "
                    "party, with our network in the path.")
    if first and last and last > first:
        out["window_days"] = round((last - first) / 86400.0, 2)
    return out


def for_entry(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        """SELECT live, live_checked, probe_n, probe_ok, probe_first, probe_ms_sum
           FROM entries WHERE key=?""", (key,)).fetchone()
    if row is None:
        return None
    out = summarise(row)
    # The timeline, from the table that is good at timelines. Cheap: this is
    # one indexed lookup per entry and only on the single-entry path.
    tr = conn.execute(
        """SELECT ts, live FROM observations
           WHERE entry_key=? AND kind='liveness' ORDER BY ts DESC LIMIT 6""",
        (key,)).fetchall()
    if len(tr) > 1:
        out["changes"] = [{"at": r["ts"], "answering": bool(r["live"])} for r in tr]
    return out


def corpus(conn: sqlite3.Connection) -> dict:
    """Reliability across the whole index, for `/state-of-mcp`.

    Every share here is over endpoints that cleared `MIN_PROBES`, and that
    denominator is published beside the shares rather than left implicit, so a
    reader can see how much of the index the figure actually speaks for.
    """
    now = int(time.time())
    rows = conn.execute(
        """SELECT probe_n, probe_ok FROM entries
           WHERE probe_n >= ?""", (MIN_PROBES,)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    probed_any = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE probe_n > 0").fetchone()[0]

    bands: dict[str, int] = {}
    perfect = 0
    for r in rows:
        n, ok = int(r["probe_n"]), int(r["probe_ok"])
        lo = wilson_lower(ok, n)
        b = _band(lo)
        bands[b] = bands.get(b, 0) + 1
        if ok == n:
            perfect += 1

    eligible = len(rows)
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None
    span = conn.execute(
        "SELECT MIN(probe_first), MAX(live_checked) FROM entries WHERE probe_n>0"
    ).fetchone()
    first_ts, last_ts = (span[0] or None), (span[1] or None)

    return {
        "generated": now,
        "min_probes_for_a_rate": MIN_PROBES,
        "coverage": {
            "entries": total,
            "probed_at_least_once": probed_any,
            "with_enough_probes_for_a_rate": eligible,
            "share_rateable_pct": pct(eligible, total),
        },
        "window": {
            "first_probe": first_ts,
            "last_probe": last_ts,
            "days": (round((last_ts - first_ts) / 86400.0, 2)
                     if first_ts and last_ts and last_ts > first_ts else 0.0),
        },
        "bands": bands,
        "answered_every_probe": perfect,
        "share_answered_every_probe_pct": pct(perfect, eligible),
        "how_this_is_measured": (
            "Each endpoint is fetched on a timer and both the outcome and the "
            "attempt are counted. Bands cut on the 95% Wilson lower bound, not "
            "the raw share, so an endpoint is only called steady once there is "
            "enough evidence to say so. Any HTTP answer below 500 counts as "
            "answering, including 401, 403 and 405: all three mean a server is "
            "there and talking, which is the question."),
        "limitations": [
            f"Endpoints with fewer than {MIN_PROBES} probes are excluded from every "
            "share above rather than counted as healthy. The rateable share says "
            "how much of the index the figures speak for.",
            "Measured from one network. An outage between us and the endpoint is "
            "recorded as the endpoint failing, and we cannot tell the two apart.",
            "This is an outside observation, not an SLA, and carries no promise "
            "from the publisher and no remedy.",
            "Answering is a floor under usefulness, never a measure of it, and "
            "never a trust or safety rating.",
        ],
    }
