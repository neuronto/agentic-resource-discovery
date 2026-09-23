#!/usr/bin/env python3
"""What /privacy promises about searches is what the code does. Throwaway store.

    python scripts/test_privacy.py
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp(prefix="neuronto-priv-")
os.environ["NEURONTO_DB"] = os.path.join(_tmp, "t.db")
os.environ["NEURONTO_EVENT_SINK"] = ""
from app import config, store  # noqa: E402

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS += bool(cond); FAIL += (not cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")

c = store.connect()
store.init(c)
now = int(time.time())
old = now - (config.SEARCH_LOG_RETENTION_DAYS + 1) * 86400
for ts, q in ((old, "old query"), (old + 5, "old query 2"), (now, "new query")):
    sid = c.execute("INSERT INTO searches(q,mode,results,ms,federated,ts,probe) VALUES(?,?,?,?,?,?,0)",
                    (q, "none", 1, 5, 0, ts)).lastrowid
    c.execute("INSERT INTO impressions(search_id,entry_key,rank,score,ts) VALUES(?,?,?,?,?)",
              (sid, "k", 1, 90, ts))
c.commit()

n = store.prune_search_log(c)
left = [r[0] for r in c.execute("SELECT q FROM searches")]
check("searches past retention are deleted", n == 2 and left == ["new query"])
check("their impressions go with them",
      c.execute("SELECT COUNT(*) FROM impressions").fetchone()[0] == 1)
check("a second prune finds nothing", store.prune_search_log(c) == 0)

# The writer: an opted-out search is counted, its text and impressions are not.
store.log_search(c, "secret words", "none", 1, 5, 0,
                 entries=[{"_key": "k", "score": 50}], opted_out=True)
store.log_search(c, "keyed words", "none", 1, 5, 0,
                 entries=[{"_key": "k", "score": 50}], authenticated=True)
store.log_search(c, "plain words", "none", 1, 5, 0, entries=[{"_key": "k", "score": 50}])
deadline = time.time() + 5
while time.time() < deadline and c.execute("SELECT COUNT(*) FROM searches").fetchone()[0] < 4:
    time.sleep(0.05)
qs = [r[0] for r in c.execute("SELECT q FROM searches ORDER BY id")]
check("an opted-out search is still counted", len(qs) == 4)
check("its text is not kept", "secret words" not in qs and store.OPTED_OUT_QUERY_PLACEHOLDER in qs)
check("a keyed search keeps no text", "keyed words" not in qs and store.PRIVATE_QUERY_PLACEHOLDER in qs)
check("an ordinary search keeps its text", "plain words" in qs)
imp = c.execute("""SELECT s.q FROM impressions i JOIN searches s ON s.id=i.search_id
                   WHERE s.ts >= ?""", (now,)).fetchall()
check("only the ordinary search recorded impressions",
      sorted(r[0] for r in imp) == ["new query", "plain words"])

# The per-response statement.
from app.main import _query_handling  # noqa: E402
fed = [{"name": "A", "ok": True}, {"name": "B", "ok": True, "cached": True}]
h = _query_handling(False, False, False, fed, {"state": "ok"})
check("forwardedTo names only registries asked just now", h["forwardedTo"] == ["A"])
check("an embedded query says so", h["embedded"] is True)
check("an ordinary query states its retention", h["logged"] == f"text-{config.SEARCH_LOG_RETENTION_DAYS}d")
h = _query_handling(False, False, True, [], {})
check("a local, opted-out query forwarded nothing and kept nothing",
      h["forwardedTo"] == [] and not h["embedded"] and h["logged"] == "withheld-opt-out")
check("a keyed query is withheld", _query_handling(False, True, False, [], {})["logged"] == "withheld-api-key")
check("a preview is not logged", _query_handling(True, False, False, [], {})["logged"] == "no")

print(f"  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
