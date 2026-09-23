#!/usr/bin/env python3
"""What a publisher wrote is kept apart from what we synthesised, and a loose tool
match cannot bury a publisher's own query. Throwaway store, no network.

    python scripts/test_provenance.py
"""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp(prefix="neuronto-prov-")
os.environ["NEURONTO_DB"] = os.path.join(_tmp, "t.db")
os.environ["NEURONTO_EVENT_SINK"] = ""
from app import rank, search, store  # noqa: E402

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS += bool(cond); FAIL += (not cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")

c = store.connect()   # a write connection; tls_conn is query_only by design
store.init(c)

def row(key):
    return c.execute("SELECT * FROM entries WHERE key=?", (key,)).fetchone()

def cols(key):
    r = row(key)
    return json.loads(r["rep_queries"]), json.loads(r["rep_queries_declared"] or "[]")

ID = "urn:air:acme.example:mcp:weather"

# 1. an aggregator's synthesis matches but is never presented as the publisher's
k = store.upsert_entry(c, {"identifier": ID, "description": "Weather data.",
                           "_inferredQueries": ["weather data", "acme for this task"]}, "glama")
allq, decl = cols(k)
check("synthesised queries are matched on", "weather data" in allq)
check("synthesised queries are not declared", decl == [])
check("the public entry carries no synthesised representativeQueries",
      "representativeQueries" not in store.row_to_entry(row(k)))
fts = c.execute("SELECT rep_queries FROM entries_fts WHERE key=?", (k,)).fetchone()[0]
check("the FTS row still indexes synthesised text", "acme for this task" in fts)

# 2. the publisher's manifest arrives: declared, and it is what the API shows
store.upsert_entry(c, {"identifier": ID, "representativeQueries":
                       ["forecast for my city", "will it rain tomorrow"]}, "crawl")
allq, decl = cols(k)
check("declared queries are declared", decl == ["forecast for my city", "will it rain tomorrow"])
check("the union keeps both provenances", "weather data" in allq and "will it rain tomorrow" in allq)
check("the public entry shows only the publisher's words",
      store.row_to_entry(row(k))["representativeQueries"] == decl)

# 3. the publisher deletes a query: it stops matching (replace, not union)
store.upsert_entry(c, {"identifier": ID, "representativeQueries": ["forecast for my city"]}, "crawl")
allq, decl = cols(k)
check("a withdrawn query leaves the declared column", decl == ["forecast for my city"])
check("a withdrawn query leaves the matching union", "will it rain tomorrow" not in allq)

# 4. a source that states nothing on one harvest has not retracted anything
store.upsert_entry(c, {"identifier": ID}, "crawl")
check("an empty statement does not clear a slot", cols(k)[1] == ["forecast for my city"])

# 5. a query both stated and synthesised is counted once, as declared
store.upsert_entry(c, {"identifier": ID, "_inferredQueries": ["forecast for my city"]}, "smithery")
allq, decl = cols(k)
check("stated and synthesised is one query", allq.count("forecast for my city") == 1)
check("and it stays declared", "forecast for my city" in decl)

# 6. a row from before the split: under-claims until its sources restate
ID2 = "urn:air:old.example:mcp:x"
k2 = store.upsert_entry(c, {"identifier": ID2, "representativeQueries": ["old query here"]}, "crawl")
c.execute("UPDATE entries SET rq_sources=NULL, rep_queries_declared=NULL WHERE key=?", (k2,))
store.upsert_entry(c, {"identifier": ID2, "_inferredQueries": ["x for this task"]}, "glama")
allq, decl = cols(k2)
check("a pre-split row keeps its old queries for matching", "old query here" in allq)
check("a pre-split row claims nothing it cannot prove", decl == [])
store.upsert_entry(c, {"identifier": ID2, "representativeQueries": ["old query here"]}, "crawl")
slots = json.loads(row(k2)["rq_sources"])
check("the legacy slot retires once every source has restated", store.LEGACY not in slots)
check("and the restated query is declared", cols(k2)[1] == ["old query here"])
c.commit()

# 7. fusion: only a strict tool match carries weight
check("a strict tool match is weighted", rank.tool_leg_weight(True) > 0)
check("an OR-fallback tool match is not", rank.tool_leg_weight(False) == 0)

# A publisher's own verbatim query must find it, even when a dozen servers each
# have a tool that shares one incidental word with the query.
TARGET = "urn:air:flights.example:mcp:finder"
store.upsert_entry(c, {"identifier": TARGET, "displayName": "Finder",
                       "description": "Travel search.",
                       "representativeQueries": ["find a flight from London to New York"]}, "crawl")
for i in range(14):
    kk = store.upsert_entry(c, {"identifier": f"urn:air:noise{i}.example:mcp:s",
                                "displayName": f"Noise {i}", "description": "Unrelated."}, "glama")
    store.replace_tools(c, kk, [{"name": f"tool_{i}", "description": f"London borough data {i}"}])
# And the reason the tool leg exists: a verb that lives only in a tool.
PAY = "urn:air:pay.example:mcp:api"
pk = store.upsert_entry(c, {"identifier": PAY, "displayName": "Pay",
                            "description": "See our docs."}, "glama")
store.replace_tools(c, pk, [{"name": "PostCharges",
                             "description": "To charge a credit card or other payment source"}])
c.commit()

async def ids(q):
    out = await search.search(c, q, None, 10, "none")
    return [r["identifier"] for r in out["results"]]

got = asyncio.run(ids("find a flight from London to New York"))
check("a loose tool match cannot bury the publisher's own query",
      bool(got) and got[0] == TARGET)
got = asyncio.run(ids("charge a credit card"))
check("a strict tool match still finds a verb that lives only in a tool", PAY in got[:3])

print(f"  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
