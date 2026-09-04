#!/usr/bin/env python3
"""Consistent snapshots of every SQLite database, taken while the service runs.

A WAL-mode database is not a file you can copy. The main index carries a hot
write-ahead log beside it, so `cp` or a plain `rsync` of the pair either misses
committed transactions that live only in the WAL or catches it mid-write. The
backup API walks the source under a read transaction and folds the WAL in,
producing one self-consistent file, without stopping the service.

`pages` and `sleep` matter: a single-shot backup holds the read lock for the
whole copy and starves writers.

    python scripts/db_snapshot.py <destination-dir>

Source directory comes from NEURONTO_DATA, so this carries no absolute path.
On the destination, delete any pre-existing *.db-wal / *.db-shm before starting
the service, or a stale WAL shadows the snapshot you just shipped.
"""
import os
import shutil
import sqlite3
import sys
import time

SRC = os.environ.get("NEURONTO_DATA") or os.path.dirname(
    os.environ.get("NEURONTO_DB", "./data/neuronto.db")) or "./data"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-6], file=sys.stderr)
        print("usage: db_snapshot.py <destination-dir>", file=sys.stderr)
        return 2
    dst_dir = argv[0]
    os.makedirs(dst_dir, exist_ok=True)
    names = sorted(f for f in os.listdir(SRC)
                   if f.endswith(".db") and not f.endswith(".bak"))
    total = 0
    for name in names:
        s, d = os.path.join(SRC, name), os.path.join(dst_dir, name)
        t0 = time.time()
        src = sqlite3.connect(f"file:{s}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(d)
        try:
            src.backup(dst, pages=400, sleep=0.02)
            dst.execute("PRAGMA journal_mode=DELETE")  # land as one plain file
            dst.commit()
        finally:
            dst.close()
            src.close()
        size = os.path.getsize(d)
        total += size
        ok = sqlite3.connect(d).execute("PRAGMA integrity_check").fetchone()[0]
        print(f"  {name:24} {size / 1e6:8.1f} MB  {time.time() - t0:5.1f}s  integrity={ok}")
    for extra in ("analytics.secret",):
        p = os.path.join(SRC, extra)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dst_dir, extra))
            print(f"  {extra:24} copied")
    print(f"  TOTAL {total / 1e6:.1f} MB into {dst_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
