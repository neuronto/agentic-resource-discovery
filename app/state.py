"""The state of the agentic web, measured rather than asserted.

Every directory in this ecosystem can tell you how many servers it lists.
None can tell you how many of them answer, because listing is free and probing
is not. We probe, and we keep the transitions rather than overwriting them, so
this module can answer questions nobody else can: what share of listed servers
respond, how many expose the tools they claim, and how fast the set decays.

Two rules, both learned the hard way elsewhere in this project. State the window
the numbers come from, because a decay rate from a short window extrapolated to
a year is fiction. And publish the limitations in the payload, not in a footnote
nobody reads: a number whose weaknesses are stated is evidence, and one whose
weaknesses are hidden is marketing.
"""
from __future__ import annotations

import sqlite3
import time


def _one(conn, sql, *a):
    r = conn.execute(sql, a).fetchone()
    return r[0] if r else 0


def report(conn: sqlite3.Connection) -> dict:
    now = int(time.time())
    total = _one(conn, "SELECT COUNT(*) FROM entries")
    live = _one(conn, "SELECT COUNT(*) FROM entries WHERE live=1")
    dead = _one(conn, "SELECT COUNT(*) FROM entries WHERE live=0")
    unprobed = _one(conn, "SELECT COUNT(*) FROM entries WHERE live IS NULL")
    probed = live + dead

    mcp_total = _one(conn, "SELECT COUNT(*) FROM entries WHERE mcp_checked IS NOT NULL")
    with_tools = _one(conn, "SELECT COUNT(*) FROM entries WHERE mcp_tools>0")
    auth_req = _one(conn, "SELECT COUNT(*) FROM entries WHERE mcp_status='auth'")
    tools_total = _one(conn, "SELECT COALESCE(SUM(mcp_tools),0) FROM entries")

    span = conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM observations").fetchone()
    o_from, o_to, o_n = (span[0] or now), (span[1] or now), (span[2] or 0)
    days = max((o_to - o_from) / 86400.0, 0.0)

    went_dead = _one(conn, """
        SELECT COUNT(*) FROM observations o WHERE o.kind='liveness' AND o.live=0
          AND EXISTS (SELECT 1 FROM observations p WHERE p.entry_key=o.entry_key
                      AND p.kind='liveness' AND p.live=1 AND p.ts < o.ts)""")
    came_back = _one(conn, """
        SELECT COUNT(*) FROM observations o WHERE o.kind='liveness' AND o.live=1
          AND EXISTS (SELECT 1 FROM observations p WHERE p.entry_key=o.entry_key
                      AND p.kind='liveness' AND p.live=0 AND p.ts < o.ts)""")
    watched = _one(conn, "SELECT COUNT(DISTINCT entry_key) FROM observations")

    # Median tool count over servers that returned any, which is the honest
    # denominator: a server exposing nothing is not a small server, it is a
    # server that did not answer the question.
    tool_rows = [r[0] for r in conn.execute(
        "SELECT mcp_tools FROM entries WHERE mcp_tools>0 ORDER BY mcp_tools")]
    median_tools = tool_rows[len(tool_rows) // 2] if tool_rows else 0

    ms_rows = [r[0] for r in conn.execute(
        "SELECT live_ms FROM entries WHERE live=1 AND live_ms>0 ORDER BY live_ms")]
    median_ms = ms_rows[len(ms_rows) // 2] if ms_rows else 0

    publishers = _one(conn, "SELECT COUNT(DISTINCT publisher) FROM entries WHERE publisher<>''")

    pct = lambda a, b: round(100.0 * a / b, 1) if b else 0.0

    return {
        "generated": now,
        "window": {
            "observations": o_n,
            "endpoints_watched": watched,
            "from": o_from,
            "to": o_to,
            "days": round(days, 2),
        },
        "index": {
            "entries": total,
            "publishers": publishers,
            "probed": probed,
            "never_probed": unprobed,
        },
        "reachability": {
            "answering": live,
            "not_answering": dead,
            "share_answering_pct": pct(live, probed),
            "median_response_ms": median_ms,
        },
        "tools": {
            "servers_introspected": mcp_total,
            "servers_exposing_tools": with_tools,
            "share_exposing_tools_pct": pct(with_tools, mcp_total),
            "servers_requiring_auth": auth_req,
            "verified_tools_total": tools_total,
            "median_tools_per_server": median_tools,
        },
        "churn": {
            "stopped_answering": went_dead,
            "started_answering_again": came_back,
            "note": ("counts transitions inside the window above, not a rate. "
                     "Multiply nothing by 365: a short window over a young index "
                     "says what happened, not what happens."),
        },
        "how_this_is_measured": (
            "Every endpoint in the index is fetched on a schedule. `answering` means "
            "it responded at all, not that it responded correctly, and never that it "
            "is safe or good. Tool counts are read from each server's own tools/list, "
            "so a server behind credentials reports as auth-required rather than as "
            "empty. Only state CHANGES are stored, so a stable endpoint costs one row "
            "rather than one per probe."),
        "limitations": [
            "The index is what we have found, not the whole ecosystem, so every share "
            "here is over our sample and not over all MCP servers that exist.",
            "A server can answer a probe and still be useless, and a server can refuse "
            "one and be healthy behind auth. Reachability is a floor, not a verdict.",
            "The observation window is short while this dataset is young. Churn figures "
            "are counts inside that window and are not annualised.",
            "Nothing here is a trust, safety or quality rating, and it must not be "
            "presented as one.",
        ],
    }
