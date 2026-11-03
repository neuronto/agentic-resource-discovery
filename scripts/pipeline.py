#!/usr/bin/env python3
"""The index pipeline: ordered stages, one process, one record per stage.

    python -m scripts.pipeline            # run every stage that is due
    python -m scripts.pipeline --status   # print the last run of each stage

Stages, in order:

  ingest       `ingest_cli all`, under the index lock (the only writer lock in the
               system), waiting up to LOCK_WAIT_S for the hourly crawl or liveness
               sweep to finish. It ends by refreshing every stored statistic.
  subregistry  the MCP Registry read API snapshot, at most once per DAILY_S
  evalgate     the retrieval regression gate, at most once per DAILY_S

Only `ingest` takes the lock. The other two only read the index, and SQLite WAL
gives a reader a consistent snapshot, so they run lock-free; they run here, right
after a good ingest, because that is when the index is fresh and quiet. Before
2026-11-04 they ran on their own timers, queued for the lock behind a 2.5-4 hour
ingest, and failed on the timeout as if the gate had failed.

Every stage ends with one outcome in `pipeline_runs`:
  ok | regression | instrument | lock_timeout | error
and a stage that did not end ok is reported with that cause, so "the gate could
not run" is never read as "the gate failed". A later stage runs only after a good
ingest. The report hook is `deploy/pipeline_alert.py`, server-only, called when
present.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import store                                                    # noqa: E402

LOCK = os.getenv("NEURONTO_INDEX_LOCK", "/var/lock/neuronto-index.lock")
LOCK_WAIT_S = int(os.getenv("NEURONTO_PIPELINE_LOCK_WAIT", "3300"))
DAILY_S = 20 * 3600
PY = sys.executable
ALERT = ROOT / "deploy" / "pipeline_alert.py"

# stage -> (command, takes the lock, minimum seconds between good runs, timeout s,
#           exit code -> outcome)
STAGES = {
    "ingest":      ([PY, "-m", "scripts.ingest_cli", "all"], True, 0, 6 * 3600, {0: "ok"}),
    "subregistry": ([PY, "-m", "app.subregistry", "build"], False, DAILY_S, 3600, {0: "ok"}),
    "evalgate":    ([PY, "-P", "-m", "scripts.evalgate"], False, DAILY_S, 3600,
                    {0: "ok", 1: "regression", 3: "instrument"}),
}


def _acquire(wait_s: int):
    """flock(2) on the shared lock file, the same lock the crawl and liveness units
    take with flock(1). Returns the open file holding it, or None on timeout."""
    f = open(LOCK, "a")
    deadline = time.time() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() >= deadline:
                f.close()
                return None
            time.sleep(5)


def _due(conn, stage: str, every_s: int) -> bool:
    if not every_s:
        return True
    row = conn.execute("""SELECT MAX(ended) FROM pipeline_runs WHERE stage=?
                          AND outcome IN ('ok','regression','instrument')""", (stage,)).fetchone()
    return not row or not row[0] or time.time() - row[0] >= every_s


def _alert(stage: str, outcome: str, cause: str, detail: str) -> None:
    if not ALERT.exists():
        return
    try:
        subprocess.run([PY, "-P", str(ALERT), stage, outcome, cause, detail[-1500:]],
                       cwd=str(ROOT), timeout=60)
    except Exception as e:
        print(f"pipeline: alert hook failed: {type(e).__name__}: {e}", flush=True)


def run_stage(conn, stage: str) -> str:
    cmd, locked, every_s, timeout_s, outcomes = STAGES[stage]
    if not _due(conn, stage, every_s):
        print(f"[{stage}] not due", flush=True)
        return "skipped"
    rid = store.pipeline_start(conn, stage)
    held = None
    if locked:
        held = _acquire(LOCK_WAIT_S)
        if held is None:
            cause = f"the index lock was held by another job for {LOCK_WAIT_S // 60} minutes"
            store.pipeline_end(conn, rid, "lock_timeout", cause)
            print(f"[{stage}] lock_timeout: {cause}", flush=True)
            _alert(stage, "lock_timeout", cause, "")
            return "lock_timeout"
    t0 = time.time()
    tail: list[str] = []
    killed = False
    try:
        print(f"[{stage}] start: {' '.join(cmd[1:])}", flush=True)
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        # The limit is enforced by a timer, not by the read loop: a stage that hangs
        # without printing would never reach a check placed between lines.
        killer = threading.Timer(timeout_s, p.kill)
        killer.start()
        try:
            for line in p.stdout:                    # stream to the journal, keep the tail
                print(f"[{stage}] {line}", end="", flush=True)
                tail.append(line.rstrip())
                del tail[:-40]
            code = p.wait()
        finally:
            killed = not killer.is_alive() and p.returncode is not None and p.returncode < 0
            killer.cancel()
    finally:
        if held is not None:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
    took = int(time.time() - t0)
    if killed:
        outcome, cause = "error", f"killed after the {timeout_s // 60}-minute stage limit"
    else:
        outcome = outcomes.get(code, "error")
        cause = None if outcome == "ok" else f"exit {code}"
    detail = "\n".join(tail[-12:])
    store.pipeline_end(conn, rid, outcome, cause, detail)
    print(f"[{stage}] {outcome} in {took}s", flush=True)
    if outcome != "ok":
        _alert(stage, outcome, cause or "", detail)
    return outcome


def status(conn) -> None:
    for stage in STAGES:
        r = conn.execute("""SELECT started, ended, outcome, cause FROM pipeline_runs
                            WHERE stage=? ORDER BY id DESC LIMIT 1""", (stage,)).fetchone()
        if not r:
            print(f"{stage:12s} never run")
            continue
        took = (r["ended"] - r["started"]) if r["ended"] else None
        print(f"{stage:12s} {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['started']))} UTC  "
              f"{r['outcome'] or 'running'}  {'' if took is None else f'{took}s'}  {r['cause'] or ''}")


def main(argv: list[str]) -> int:
    conn = store.connect()
    store.init(conn)
    if "--status" in argv:
        status(conn)
        return 0
    if run_stage(conn, "ingest") not in ("ok", "skipped"):
        return 1                                     # nothing downstream of a failed ingest
    worst = 0
    for stage in ("subregistry", "evalgate"):
        if run_stage(conn, stage) not in ("ok", "skipped"):
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
