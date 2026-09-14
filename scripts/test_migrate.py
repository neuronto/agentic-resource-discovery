#!/usr/bin/env python3
"""The schema migration is safe when several workers start at once.

Every uvicorn worker runs `store.init` at startup. Adding a column was check then
alter, so with four workers all but one failed with "duplicate column name" and
exited, which rolled back the deploy that added the payment columns. This starts
eight processes against one database with no added columns, releases them at the
same instant, and requires every one to finish and every column to exist.

    python3 scripts/test_migrate.py
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["NEURONTO_DB"] = DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import store                                            # noqa: E402

WORKERS = 8


def worker(barrier, errors):
    try:
        conn = store.connect()
        barrier.wait(timeout=30)
        store.init(conn)
        conn.close()
    except Exception as e:
        errors.put(f"{type(e).__name__}: {e}")


def main() -> int:
    base = sqlite3.connect(DB)
    base.executescript(store._SCHEMA)          # the shape before any added column
    base.commit()
    base.close()
    ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context()
    barrier = ctx.Barrier(WORKERS)
    errors = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(barrier, errors)) for _ in range(WORKERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    failures = []
    while not errors.empty():
        failures.append(errors.get())
    failures += [f"worker exited with {p.exitcode}" for p in procs if p.exitcode not in (0,)]
    cols = {r[1] for r in sqlite3.connect(DB).execute("PRAGMA table_info(entries)")}
    missing = [c for c in store._ADD_COLUMNS if c not in cols]
    print(f"\n  migration under {WORKERS} concurrent workers")
    ok = not failures and not missing
    print(f"  {'PASS' if not failures else 'FAIL'}  every worker finished" + ("" if not failures else f"\n          {failures[:3]}"))
    print(f"  {'PASS' if not missing else 'FAIL'}  every added column exists" + ("" if not missing else f"\n          missing {missing}"))
    print(f"\n  {2 - (bool(failures) + bool(missing))} passed, {bool(failures) + bool(missing)} failed")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB + suffix)
        except OSError:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
