"""Every submission is kept, and retried, until it is indexed or we have given up
out loud.

On 2026-09-01 a publisher called `publish_resource` four times in five minutes.
One call was indexed; three were refused. Nothing recorded what their server
had answered, the refusals kept no state, and the only reason anyone knew is
that the analytics happened to show four events. Whether the cause was their
deploy, our resolver or our write lock is unrecoverable, and that is the real
defect: a registry whose acceptance depends on one synchronous handshake and
one synchronous write, with no memory of the attempt, will lose publishers on
every transient there is.

So a submission is now a record before it is an outcome. The row is written
here, in its own database file, *before* verification runs, and it survives
whatever the verification does. A failed attempt schedules the next one on a
backoff that spans about two and a half days: long enough to outlast a broken
deploy, a DNS hiccup, a maintenance window on our side, or a publisher who
submitted a minute before the endpoint was up. Each attempt keeps the evidence
of what came back, so "was that us or them" is answered by reading a row
instead of by an investigation.

Its own file, deliberately, for the same reason as `limits.db`: the index takes
exactly one writer, and the moment we most need to record a submission is the
moment the index is busy. Nothing here is a source of truth for the index
itself; a row saying `indexed` points at the entry key, and the entry is what
search reads.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import config

DB_PATH = Path(os.getenv("NEURONTO_SUBMISSIONS_DB",
                         str(config.DB_PATH.parent / "submissions.db")))

# Seconds until the next attempt, indexed by how many have already failed. The
# first retry is quick because the commonest transient is a publisher who
# submitted seconds before their endpoint was ready; the tail is long because
# a broken deploy can stay broken over a weekend. Eight attempts, ~2.7 days.
BACKOFF_S = [60, 300, 900, 3600, 4 * 3600, 12 * 3600, 24 * 3600, 24 * 3600]

# A refusal that came from our own index being busy is not the publisher's
# transient and should not spend one of their eight attempts on a long wait.
BUSY_RETRY_S = 30

# How long a row stays claimed by a retrier before another worker may take it.
CLAIM_S = 300

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,          -- endpoint | domain
  target        TEXT NOT NULL,          -- the URL or hostname as normalised
  source        TEXT NOT NULL,          -- http | mcp | retry
  probe         INTEGER NOT NULL DEFAULT 0,   -- our own test traffic
  created       INTEGER NOT NULL,
  updated       INTEGER NOT NULL,
  status        TEXT NOT NULL,          -- pending | indexed | gave_up
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_at       INTEGER,                -- when pending
  claimed_until INTEGER NOT NULL DEFAULT 0,
  reason        TEXT,                   -- machine readable, last attempt
  detail        TEXT,                   -- the sentence we answered with
  evidence      TEXT,                   -- json: what the other side returned
  entry_key     TEXT,
  tools         INTEGER,
  history       TEXT NOT NULL DEFAULT '[]'  -- json: one line per attempt
);
CREATE INDEX IF NOT EXISTS idx_sub_due ON submissions(status, next_at);
CREATE INDEX IF NOT EXISTS idx_sub_target ON submissions(kind, target, created);
"""


def _conn() -> sqlite3.Connection | None:
    c = getattr(_local, "c", None)
    if c is not None:
        return c
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=5, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.executescript(_SCHEMA)
        c.commit()
    except Exception:
        return None
    _local.c = c
    return c


def new_id() -> str:
    return secrets.token_hex(6)


def open(kind: str, target: str, source: str, probe: bool = False) -> str | None:
    """Record the intent. Returns the submission id, reusing an open one for the
    same target so a publisher who retries by hand joins the queue they are
    already in rather than starting a second one."""
    c = _conn()
    if c is None:
        return None
    now = int(time.time())
    try:
        row = c.execute("""SELECT id FROM submissions
                           WHERE kind=? AND target=? AND status='pending'
                           ORDER BY created DESC LIMIT 1""", (kind, target)).fetchone()
        if row:
            c.execute("UPDATE submissions SET updated=?, claimed_until=? WHERE id=?",
                      (now, now + CLAIM_S, row["id"]))
            c.commit()
            return row["id"]
        sid = new_id()
        c.execute("""INSERT INTO submissions(id,kind,target,source,probe,created,updated,
                                             status,attempts,next_at,claimed_until)
                     VALUES(?,?,?,?,?,?,?,'pending',0,?,?)""",
                  (sid, kind, target, source, 1 if probe else 0, now, now, now, now + CLAIM_S))
        c.commit()
        return sid
    except Exception:
        return None


# Outcomes that will never change on their own. A retry schedule exists for
# things that are temporarily broken: a connection refused, a timeout, a 5xx, a
# host mid-deploy. It is the wrong instrument for an answer the server has
# already given definitively.
#
# A 405 to our POST means this URL does not accept POST and never will, so it is
# not an MCP endpoint. (Note the direction: 405 to a *GET* on our own /mcp means
# the opposite, that the endpoint exists and we used the wrong verb. Same status
# code, opposite meaning, because there the verb was wrong and here the verb is
# the whole point.) 404, 410 and 501 are the same kind of settled answer.
#
# We were retrying these for days. `https://example.com/definitely-not-mcp` was
# probed 28 times and `https://example.com/` 20 times, which is pointless for us
# and rude to them, and it meant a real publisher who submitted the wrong URL was
# told to wait 2.7 days for an attempt that could not succeed.
#
# 401, 403, 408 and 429 are deliberately NOT here: the first two mean a server is
# there and wants credentials, and the last two are explicitly "try again".
PERMANENT = {"http400", "http404", "http405", "http410", "http501"}


def is_permanent(reason: str) -> bool:
    r = (reason or "").strip().lower()
    return r.startswith("error:") and r.split("error:", 1)[1] in PERMANENT


def record(sid: str | None, *, indexed: bool, reason: str, detail: str = "",
           evidence: dict | None = None, entry_key: str | None = None,
           tools: int | None = None, busy: bool = False,
           scheduled: bool = True) -> dict | None:
    """Close one attempt. Decides whether there will be another.

    `scheduled` is True for the first attempt and for every attempt the
    retrier makes; False for a publisher re-submitting a target that is
    already queued. Only scheduled attempts spend the backoff. Before this
    distinction a target re-submitted nine times in an hour was `gave_up`
    after fifty-nine minutes, on a schedule that promised 2.7 days.
    """
    c = _conn()
    if c is None or not sid:
        return None
    now = int(time.time())
    try:
        row = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        attempts = int(row["attempts"]) + (0 if busy else 1)
        try:
            hist = json.loads(row["history"] or "[]")
        except Exception:
            hist = []
        manual = (not busy) and (not scheduled) and int(row["attempts"]) > 0
        hist.append({"ts": now, "ok": indexed, "reason": reason, "busy": busy,
                     "manual": manual})
        hist = hist[-40:]
        # The position on the schedule counts the initial attempt plus the
        # retrier's, never a hand re-submit; a hand re-submit that fails only
        # restarts the current wait from now.
        spent = attempts - sum(1 for h in hist if h.get("manual"))
        if indexed:
            status, next_at = "indexed", None
        elif busy:
            status, next_at = "pending", now + BUSY_RETRY_S
        elif is_permanent(reason):
            # Keep the row: the submission is still a record of intent, and a
            # re-submit after a fix starts a fresh set of attempts. We simply
            # stop asking a question that has been answered.
            status, next_at = "rejected", None
        elif spent <= len(BACKOFF_S):
            status, next_at = "pending", now + BACKOFF_S[max(0, spent - 1)]
        else:
            status, next_at = "gave_up", None
        c.execute("""UPDATE submissions
                     SET updated=?, status=?, attempts=?, next_at=?, claimed_until=0,
                         reason=?, detail=?, evidence=?, entry_key=COALESCE(?, entry_key),
                         tools=COALESCE(?, tools), history=?
                     WHERE id=?""",
                  (now, status, attempts, next_at, reason[:120], (detail or "")[:400],
                   json.dumps(evidence, ensure_ascii=False)[:2000] if evidence else None,
                   entry_key, tools, json.dumps(hist), sid))
        c.commit()
        return dict(c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone())
    except Exception:
        return None


def get(sid: str) -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        row = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def latest_for(kind: str, target: str) -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        row = c.execute("""SELECT * FROM submissions WHERE kind=? AND target=?
                           ORDER BY created DESC LIMIT 1""", (kind, target)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def due(limit: int = 5, now: int | None = None) -> list[dict]:
    """Claim up to `limit` pending rows whose time has come. The claim is a
    conditional UPDATE, so two workers running this loop never take the same
    row, and a worker that dies mid-attempt releases it after CLAIM_S."""
    c = _conn()
    if c is None:
        return []
    now = now or int(time.time())
    out: list[dict] = []
    try:
        rows = c.execute("""SELECT id FROM submissions
                            WHERE status='pending' AND next_at IS NOT NULL AND next_at<=?
                              AND claimed_until<?
                            ORDER BY next_at ASC LIMIT ?""", (now, now, limit)).fetchall()
        for r in rows:
            cur = c.execute("""UPDATE submissions SET claimed_until=?
                               WHERE id=? AND status='pending' AND claimed_until<?""",
                            (now + CLAIM_S, r["id"], now))
            if cur.rowcount == 1:
                out.append(dict(c.execute("SELECT * FROM submissions WHERE id=?",
                                          (r["id"],)).fetchone()))
        c.commit()
    except Exception:
        pass
    return out


def public(row: dict | None) -> dict | None:
    """The shape a caller sees. Everything a publisher needs to know what
    happened and what will happen, nothing about anyone else."""
    if not row:
        return None
    now = int(time.time())
    try:
        ev = json.loads(row["evidence"]) if row.get("evidence") else None
    except Exception:
        ev = None
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "target": row["target"],
        "status": row["status"],
        "attempts": row["attempts"],
        "created": row["created"],
        "updated": row["updated"],
        "reason": row.get("reason"),
        "detail": row.get("detail"),
        "evidence": ev,
        "status_url": f"{config.PUBLIC_BASE}/submit/status/{row['id']}",
    }
    if row["status"] == "pending" and row.get("next_at"):
        out["next_attempt_at"] = row["next_at"]
        out["next_attempt_in_s"] = max(0, int(row["next_at"]) - now)
        left = max(0, len(BACKOFF_S) - _spent(row))
        out["attempts_left"] = left
        out["retrying_until"] = row["created"] + sum(BACKOFF_S)
        out["note"] = ("not indexed yet. We keep this submission and retry it "
                       f"automatically ({left} more attempts over the next "
                       f"{_human(sum(BACKOFF_S[_spent(row):]))}), so you do not "
                       "have to. Fix whatever the evidence shows and the next attempt "
                       "will pick it up; re-submitting is harmless and triggers a "
                       "fresh attempt now.")
    elif row["status"] == "indexed":
        out["entry_key"] = row.get("entry_key")
        out["verified_tools"] = row.get("tools")
        out["note"] = ("indexed" + (f" on attempt {row['attempts']}" if row["attempts"] > 1 else ""))
    elif row["status"] == "rejected":
        out["note"] = ("not indexed, and not queued for retry: the endpoint gave a "
                       "settled answer rather than a temporary failure, so waiting "
                       "would change nothing. The evidence above is exactly what it "
                       "returned. Fix it, or submit the correct URL, and this starts "
                       "a fresh set of attempts.")
    elif row["status"] == "gave_up":
        out["note"] = (f"we tried {row['attempts']} times over "
                       f"{_human(sum(BACKOFF_S))} and it never verified. The last evidence "
                       "is above. Submitting again starts a fresh set of attempts.")
    return out


def _spent(row: dict) -> int:
    """How many scheduled attempts a row has used (initial + retrier)."""
    try:
        hist = json.loads(row.get("history") or "[]")
    except Exception:
        hist = []
    return max(0, int(row["attempts"]) - sum(1 for h in hist if h.get("manual")))


def _human(s: int) -> str:
    if s < 3600:
        return f"{max(1, s // 60)} minutes"
    if s < 2 * 86400:
        return f"{s // 3600} hours"
    return f"{s / 86400:.1f} days"


def stats() -> dict:
    """For /metrics.json. Our own test traffic is excluded so the public
    number is about publishers, not about the suite."""
    c = _conn()
    if c is None:
        return {"storage": "unavailable"}
    try:
        q = lambda s, *a: c.execute(s, a).fetchone()[0]
        return {
            "pending": q("SELECT COUNT(*) FROM submissions WHERE status='pending' AND probe=0"),
            "indexed": q("SELECT COUNT(*) FROM submissions WHERE status='indexed' AND probe=0"),
            "indexed_on_retry": q("SELECT COUNT(*) FROM submissions WHERE status='indexed' "
                                  "AND attempts>1 AND probe=0"),
            "gave_up": q("SELECT COUNT(*) FROM submissions WHERE status='gave_up' AND probe=0"),
            "retry_schedule_s": BACKOFF_S,
            "note": ("a submission that fails verification is kept and retried on this "
                     "schedule; nothing a publisher sends is dropped on a transient"),
        }
    except Exception:
        return {"storage": "error"}


def prune(days: int = 90) -> int:
    """Drop closed rows older than `days`; pending ones are never pruned."""
    c = _conn()
    if c is None:
        return 0
    try:
        cur = c.execute("DELETE FROM submissions WHERE status!='pending' AND updated<?",
                        (int(time.time()) - days * 86400,))
        c.commit()
        return cur.rowcount
    except Exception:
        return 0
