"""The search engine: local retrieval, filtering, and federated fusion."""
from __future__ import annotations

import asyncio
import time
import json
import re
import sqlite3
from typing import Any

from . import config, federation, payments, rank, store
from .normalize import expand_type_filter, media_family, publisher_of

_TOKEN = re.compile(r"[A-Za-z0-9_]+")



# How much a tool match counts against a description match is measured data,
# and it depends on how strictly the tools matched: see rank.tool_leg_weight.


def tool_keys(conn: sqlite3.Connection, text: str, limit: int,
              loose: bool = True) -> list[str]:
    """Entries whose TOOLS match, as their own retrieval ordering.

    An entry's own description is often useless for discovery. Stripe's entire
    OpenAPI description is "The Stripe REST API. Please see
    https://stripe.com/docs/api for more details", which can never match
    "charge a credit card". The verb lives one level down, in an operation that
    says "To charge a credit card or other payment source".

    We already read those: 32,861 tools from MCP servers' own tools/list and
    every operation of the OpenAPI documents we index. They were searchable
    only through /tools, so entry search could not see them. This makes them a
    ranking leg, fused with the others through RRF rather than pasted into the
    entry's text, so an entry surfaces for what its tools do without anything
    being restated as if the publisher had claimed it.

    Returns (entry keys in rank order, {key: the tool that matched}).
    """
    terms = _fts_terms(text)
    if not terms:
        return [], {}

    # Strict before loose. "charge a credit card" as a pure OR matches every
    # tool containing any of those words, thousands of them, and BM25 then ranks
    # a translation API above Stripe. The same query as a conjunction matches
    # 32 tools, and Stripe's `PostCharges` ("To charge a credit card or other
    # payment source") is one of them.
    #
    # So: try the phrase, then the conjunction, then the OR, and take the first
    # that returns anything. The OR is kept as the last resort because it is
    # what makes a long natural-language query work at all when no strict form
    # matches, which was the reason it was chosen in the first place.
    # `strict` marks the attempts where every content word had to be present.
    # A single-term query is strict by definition; the OR fallback never is.
    attempts: list[tuple[str, bool]] = []
    if len(terms) > 1:
        attempts.append((" ".join(t.strip('*') for t in terms), True))  # phrase-ish
        attempts.append((" AND ".join(terms), True))
    # A caller that gives the OR fallback no weight (`loose=False`) does not pay
    # for it either: over every tool description it is the slowest attempt here,
    # and its answer would be discarded.
    if loose or len(terms) == 1:
        attempts.append((" OR ".join(terms), len(terms) == 1))

    rows, strict = [], False
    for match, strict in attempts:
        try:
            rows = conn.execute(
                """SELECT f.entry_key AS entry_key, t.name AS name,
                          t.description AS description
                   FROM (SELECT tool_id, entry_key, bm25(tools_fts) AS bm FROM tools_fts
                         WHERE tools_fts MATCH ? ORDER BY bm LIMIT ?) AS f
                   JOIN tools t ON t.id = f.tool_id
                   ORDER BY f.bm""",
                (match, limit * 4)).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            break
    if not rows:
        return [], {}

    # Carry the tool that matched, not just the fact that one did. An answer of
    # "Stripe" is a guess the caller has to verify; "Stripe, because PostCharges
    # says: To charge a credit card or other payment source" is evidence. It is
    # also what keeps `query_match` honest, since the words that matched are now
    # frequently in the tool rather than in the entry's own prose.
    # The matched tool is only reported when the match was strict. Reaching an
    # entry through the OR fallback means *some* word of the query appeared
    # somewhere in 95,994 tools, which is not evidence and must not be shown as
    # it: the nonsense query "zzzz nonexistent capability qqqq" matches a tool
    # containing the word "capability", and calling that a match would tell a
    # caller we had found something when we had found nothing.
    out: list[str] = []
    detail: dict[str, dict] = {}
    seen: set[str] = set()
    for r in rows:
        k = r["entry_key"]
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
        if strict:
            detail[k] = {"name": r["name"],
                         "description": (r["description"] or "")[:300] or None}
        if len(out) >= limit:
            break
    return out, detail


def diversify(entries: list[dict], page_size: int, cap: int | None = None) -> list[dict]:
    """Stop one publisher owning the page.

    A single vendor can legitimately publish hundreds of distinct services:
    Azure has 653 in the OpenAPI corpus, AWS 271, Google 281. Each is a real
    capability and none should be dropped from the index, but "store a file"
    returning ten Azure services and nothing else is a worse answer than one
    Azure service and nine alternatives, even when the ten score higher.

    So this is a presentation rule, not a scoring one: order is preserved, the
    best entry from a publisher always keeps its place, and surplus entries are
    held back and appended rather than discarded, so a page is never short.
    """
    if cap is None:
        cap = max(1, min(3, (page_size // 4) or 1))
    kept: list[dict] = []
    held: list[dict] = []
    seen: dict[str, int] = {}
    for e in entries:
        # Derived, not read off the entry: a search result carries identifier,
        # displayName, type, url, description, score and source, and no
        # publisher. Reading a field that is not there would have made this
        # whole function a silent no-op that looked like it worked.
        pub = (publisher_of(e.get("identifier"), e.get("url")) or "").lower()
        if not pub:
            kept.append(e)
            continue
        n = seen.get(pub, 0)
        if n < cap:
            seen[pub] = n + 1
            kept.append(e)
        else:
            held.append(e)
    # Never return a short page just because one publisher dominated the tail.
    if len(kept) < page_size:
        kept.extend(held[:page_size - len(kept)])
    return kept


def _label(identifier: str, url: str | None = None) -> str:
    """A human-readable name for an entry whose publisher gave none."""
    if identifier and identifier.startswith("urn:"):
        leaf = identifier.rstrip(":").split(":")[-1]
        if leaf:
            return leaf.replace("-", " ").replace("_", " ").strip() or identifier
    if url:
        return url.replace("https://", "").replace("http://", "").split("/")[0]
    return identifier


_FTS_STOP = {"the", "and", "for", "with", "that", "this", "you", "your", "can",
             "get", "how", "what", "which", "from", "into", "are", "was", "some",
             "any", "all", "one", "out", "use", "using", "want", "need", "please",
             "find", "give", "show", "make"}


def _fts_terms(text: str) -> list[str]:
    """The content words of a query, in order, as FTS5 prefix terms.

    User text is never passed through raw: FTS5 treats bare punctuation as
    syntax and a stray quote or hyphen raises rather than returning nothing.

    Every word stays a prefix term. Prefixing only the last word, and adding
    two-letter stop words, were both measured on 2026-09-23 and REJECTED:
    each was faster (70 and 48 ms against 122 ms median on the fast path) and
    one scored higher on the declared set, but both lost capability-battery
    cases ("charge a credit card" put Stripe at 6, not 5). The battery may not
    regress; see rank.py for the rule and the gate that enforces it.
    """
    toks = [t.lower() for t in _TOKEN.findall(text or "") if len(t) > 1]
    if not toks:
        return []
    keep = ([t for t in toks if t not in _FTS_STOP] or toks)[:16]
    return [f'"{t}"*' for t in keep]


def name_keys(conn: sqlite3.Connection, text: str, limit: int) -> list[str]:
    """Entries whose display name contains the whole query as a phrase.

    Exact names first, then shortest names, since a name that is the query plus
    little else is the closer match. A query of one short word is not a name
    lookup; it would match half the index.
    """
    toks = [t.lower() for t in _TOKEN.findall(text or "") if len(t) > 1]
    if not toks or (len(toks) == 1 and len(toks[0]) < 4) or len(toks) > 8:
        return []
    phrase = " ".join(toks)
    try:
        rows = conn.execute(
            """SELECT f.key, e.display_name FROM
                 (SELECT key FROM entries_fts WHERE entries_fts MATCH ? LIMIT 400) AS f
               JOIN entries e ON e.key = f.key""",
            (f'display_name : "{phrase}"',)).fetchall()
    except sqlite3.OperationalError:
        return []
    def order(r):
        name = " ".join(t.lower() for t in _TOKEN.findall(r["display_name"] or ""))
        return (name != phrase, len(name))
    return [r["key"] for r in sorted(rows, key=order)[:limit]]


def _fts_query(text: str) -> str:
    """The terms OR'd, so a long natural-language query still matches on its
    content words, which is how people actually phrase a need."""
    return " OR ".join(_fts_terms(text))


def _passes_filter(row: sqlite3.Row, flt: dict | None) -> bool:
    """Structured constraints, matched on family rather than exact string.

    This is the normalisation payoff: a client asking for
    `application/mcp-server+json` means "MCP servers", and gets every one of
    them however the publisher spelled the type.
    """
    if not flt:
        return True
    # Payment keys are answered from the stored terms (payments.py); the loop
    # below ignores keys it does not own, so they are not read twice.
    if payments.filter_active(flt) and not payments.passes(payments.row_view(row), flt):
        return False
    for field, values in flt.items():
        if not isinstance(values, list):
            values = [values]
        values = [str(v) for v in values if v is not None]
        if not values:
            continue
        if field == "type":
            if row["type_family"] not in expand_type_filter(values):
                return False
        elif field in ("tags", "capabilities"):
            have = {str(x).lower() for x in json.loads(row[field] or "[]")}
            if not ({v.lower() for v in values} & have):
                return False
        elif field == "publisher":
            if (row["publisher"] or "").lower() not in {v.lower() for v in values}:
                return False
        elif field.startswith("trustManifest."):
            if not row["trust_identity"]:
                return False
        # Unknown filter keys are ignored rather than fatal: the spec expects
        # vocabulary to grow at the edges, and a registry that 400s on a term it
        # does not know breaks federation for everyone downstream.
    return True


# Words that carry no discrimination, so coverage is not credited for them.
_STOP = {"a","an","and","are","as","at","be","by","can","do","for","from","get",
         "how","i","in","is","it","me","my","of","on","or","that","the","to",
         "up","what","when","which","with","you","your"}


def _content_terms(text: str) -> list[str]:
    """The words a match should actually be judged on."""
    ws = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
          if len(w) > 2 and w not in _STOP]
    return list(dict.fromkeys(ws))


def _coverage(row, terms: list[str]) -> float:
    """How much of the query this entry's own text accounts for.

    BM25 cannot be used here. A tenant's private index holds a handful of
    documents, often one, and BM25's IDF term is a statement about a corpus: in
    a single-document index every term appears in 100% of documents, so IDF
    collapses and every score converges on zero. The first version of this
    compared a 1-document BM25 against a 10,000-document BM25 and the private
    entry sorted below every public result for its own owner's query.

    Coverage is corpus-independent, so it means the same thing for a tenant with
    one internal service and a tenant with a thousand: the fraction of the
    query's content words this entry's text accounts for. Prefix matching keeps
    it in step with the stemmed FTS query that selected the row.
    """
    if not terms:
        return 0.0
    hay = " ".join(str(row[c] or "") for c in
                   ("display_name", "description", "rep_queries", "tags")).lower()
    words = set(re.findall(r"[a-z0-9]+", hay))
    hit = sum(1 for t in terms
              if any(w.startswith(t) or t.startswith(w) for w in words))
    return hit / len(terms)


# A private entry must account for at least this much of the query. The FTS
# query is an OR over terms, so without a floor an internal service sharing one
# incidental word with the query would be admitted to the caller's results.
_PRIVATE_MIN_COVERAGE = 0.4


def _private_hits(conn: sqlite3.Connection, match: str, owner_domain: str,
                  limit: int) -> list[sqlite3.Row]:
    """That domain's internal services matching the query."""
    try:
        return conn.execute("""
          SELECT p.* FROM private_fts JOIN private_entries p ON p.key = private_fts.key
          WHERE private_fts MATCH ? AND p.owner_domain = ?
          LIMIT ?""", (match, owner_domain.lower(), limit)).fetchall()
    except sqlite3.OperationalError:
        return []



def local_by_keys(conn: sqlite3.Connection, keys: list[str],
                  flt: dict | None = None) -> list[dict]:
    """Load entries the tool leg found but the lexical leg did not.

    Built exactly as `local_search` builds one, so a result reaching the page
    through its tools is indistinguishable from one that arrived through its
    own text: same shape, same liveness treatment, same verification block.
    The score is a floor rather than a BM25 value, because there is no lexical
    match on this entry to derive one from; RRF decides its final position.

    `flt` is applied here for the same reason it is applied in `local_search`:
    an entry reaching the page through its tools is still an entry, and a client
    that asked for GraphQL must not be handed an OpenAPI document because the
    match happened one level down. Skipping this made a type filter silently
    stop applying the moment the tool leg started running on the fast path.
    """
    if not keys:
        return []
    out: list[dict] = []
    qs = ",".join("?" for _ in keys)
    rows = conn.execute(f"SELECT * FROM entries WHERE key IN ({qs})", keys).fetchall()
    by_key = {r["key"]: r for r in rows}
    for k in keys:                      # preserve the tool leg's ordering
        r = by_key.get(k)
        if r is None or _col(r, "visibility") == "private":
            continue
        if not _passes_filter(r, flt):
            continue
        e = store.row_to_entry(r)
        e["score"] = rank.apply_liveness(rank._FLOOR, r["live"])
        e["source"] = config.PUBLIC_BASE
        e["_key"] = r["key"]
        e["_live"] = r["live"]
        e["_sources"] = len(json.loads(r["sources"] or "[]"))
        _attach_verification(e, r)
        out.append(e)
    return out


def local_search(conn: sqlite3.Connection, text: str, flt: dict | None,
                 limit: int, owner_domain: str | None = None) -> list[dict]:
    """BM25 over our own index, field-weighted, liveness-adjusted.

    `owner_domain` is the verified domain behind the caller's bearer key,
    resolved before this is called. It admits that domain's internal services
    into the same ranking as public ones, so one query answers "what can I use
    for this" across both. The default is None, so a caller who presents nothing
    searches only the public index: private data lives in a separate table that
    the public query cannot reach, and admitting it takes a deliberate argument.
    """
    match = _fts_query(text)
    if not match:
        return []
    w = ",".join(str(x) for x in rank.FTS_WEIGHTS)
    # Rank inside the FTS table first and join only the winners. Joining every
    # match to its full `entries` row before sorting cost more than the search
    # itself once rows grew (provenance, payments, probes): identical results,
    # median 230 -> 102 ms on the declared eval set.
    sql = f"""
      SELECT e.*, f.bm AS bm
      FROM (SELECT key, bm25(entries_fts, {w}) AS bm FROM entries_fts
            WHERE entries_fts MATCH ? ORDER BY bm LIMIT ?) AS f
      JOIN entries e ON e.key = f.key
      ORDER BY f.bm
    """
    try:
        rows = conn.execute(sql, (match, max(limit * 6, 60))).fetchall()
    except sqlite3.OperationalError:
        return []

    # `visibility` is vestigial, but a stray row from the old design must never
    # surface, so the public leg still refuses one.
    kept = [r for r in rows
            if _passes_filter(r, flt) and _col(r, "visibility") != "private"]
    terms = _content_terms(text)
    priv = []
    if owner_domain:
        for r in _private_hits(conn, match, owner_domain, max(limit, 20)):
            if not _passes_filter(r, flt):
                continue
            cov = _coverage(r, terms)
            if cov >= _PRIVATE_MIN_COVERAGE:
                priv.append((r, cov))
        priv.sort(key=lambda rc: -rc[1])
    if not kept and not priv:
        return []
    # bm25() is negative and lower is better; flip so higher is better.
    raw = []
    for r in kept:
        base = -float(r["bm"])
        n_src = len(json.loads(r["sources"] or "[]"))
        raw.append(base * rank.source_bonus(n_src)
                        * rank.verified_bonus(_col(r, "mcp_tools")))
    scores = rank.scale_scores(raw) if raw else []

    out = []
    # An internal service is placed by coverage on the same 0-100 band: one that
    # accounts for the whole query ranks with the best public match, one that
    # accounts for half sits mid-list. It is neither promoted for being the
    # caller's own nor penalised for being unverifiable, and it carries a label
    # so the caller always knows which half of the index answered.
    for r, cov in priv:
        e = store.private_row_to_entry(r)
        e["score"] = int(round(100 * cov))
        e["source"] = config.PUBLIC_BASE
        e["_key"] = r["key"]
        e["_live"] = None
        out.append(e)
    for r, s in zip(kept, scores):
        e = store.row_to_entry(r)
        e["score"] = rank.apply_liveness(s, r["live"])
        e["source"] = config.PUBLIC_BASE
        e["_key"] = r["key"]
        e["_live"] = r["live"]
        e["_sources"] = len(json.loads(r["sources"] or "[]"))
        _attach_verification(e, r)
        out.append(e)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _col(row: sqlite3.Row, name: str):
    """Read a column that may not exist on an older row object."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _attach_verification(entry: dict, row: sqlite3.Row) -> None:
    """Expose what we verified, plainly separated from relevance.

    `verified` says we handshook with this endpoint and read its tool list. It
    is a statement about reachability and capability, never about trustworthi-
    ness: the spec decouples trust evaluation from discovery and so do we.
    """
    n = _col(row, "mcp_tools")
    status = _col(row, "mcp_status")
    if status is None:
        return
    v: dict[str, Any] = {"checked": _col(row, "mcp_checked")}
    if status.startswith("ok"):
        v["reachable"] = True
        v["tools"] = n or 0
    elif status == "auth":
        v["reachable"] = True
        v["authRequired"] = True
        v["tools"] = 0
    elif status == "payment":
        v["reachable"] = True
        v["paymentRequired"] = True
        v["tools"] = 0
    else:
        v["reachable"] = False
        v["tools"] = 0
    entry["verification"] = v


async def search(conn: sqlite3.Connection, text: str, flt: dict | None,
                 page_size: int, mode: str, use_dense: bool | None = None,
                 owner_domain: str | None = None, budget_ms: int | None = None) -> dict:
    """Run a search in the requested federation mode (§5.4).

    `none`       our index only, lexical only. The fast path, tens of ms.
    `referrals`  our index, plus pointers to the other registries.
    `auto`       our index fused with the dense leg and with live upstream
                 results. The default, and the mode nobody else implements.

    The dense leg rides inside the federation budget rather than on the fast
    path. Embedding a query is a network call, and the latency claim is the
    product; in `auto` we are already awaiting upstreams, so the embedding runs
    concurrently with them and costs no extra wall clock. If it is unavailable
    or slow it contributes an empty ranking, which changes nothing.
    """
    mode = (mode or "auto").lower()
    if mode not in ("auto", "referrals", "none"):
        mode = "auto"

    # The lexical leg is synchronous SQLite work and used to run right here, on
    # the event loop, so concurrent searches ran one after another: ten at once
    # took 1.2 seconds each where one takes forty milliseconds. It runs in the
    # threadpool now, on that thread's own connection.
    local = await asyncio.to_thread(
        lambda: local_search(store.tls_conn(), text, flt, max(page_size, 10) * 3, owner_domain))

    # The tool leg runs in EVERY mode. It is pure SQLite against `tools_fts` on
    # the threadpool, single-digit milliseconds, and no network, so the latency
    # argument that keeps the dense leg out of the fast path does not apply to
    # it. It used to run only in `auto`, which meant `federation: none` searched
    # entry prose alone: Stripe's whole OpenAPI description is one sentence
    # pointing at their docs site, so the fast path could not find it for
    # "charge a credit card" while the operation `PostCharges` said exactly that
    # one level down.
    tool_order, tool_detail = await asyncio.to_thread(
        lambda: tool_keys(store.tls_conn(), text, max(page_size, 10) * 3,
                          loose=rank.TOOL_LEG_LOOSE_WEIGHT > 0))
    # `tool_detail` is filled only when the match was strict (phrase or
    # conjunction). An OR-fallback ordering carries the loose weight, which is
    # measured at zero, and a leg with no weight is not fused at all.
    tool_w = rank.tool_leg_weight(bool(tool_detail))
    if tool_w <= 0:
        tool_order = []
    name_order = await asyncio.to_thread(
        lambda: name_keys(store.tls_conn(), text, max(page_size, 10))) \
        if rank.NAME_LEG_WEIGHT > 0 else []
    by_key: dict[str, dict] = {e["_key"]: e for e in local}
    missing = [k for k in tool_order + name_order if k not in by_key]
    if missing:
        extra = await asyncio.to_thread(
            lambda: local_by_keys(store.tls_conn(), missing, flt))
        for e in extra:
            by_key.setdefault(e["_key"], e)
    for k, td in tool_detail.items():
        e = by_key.get(k)
        if e is not None and td.get("name"):
            e["matchedTool"] = td

    if mode in ("none", "referrals"):
        local_fused = _fuse_local(local, tool_order, by_key, page_size, tool_w, name_order)
        out = {"results": local_fused, "_federated": [], "_dense": None}
        if mode == "referrals":
            out["referrals"] = federation.referral_entries()
        return out

    # auto. The local answer first; the other legs only when they could change it.
    local_fused = _fuse_local(local, tool_order, by_key, page_size, tool_w, name_order)
    cov = query_match(text, local_fused)["coverage"] if local_fused else 0.0
    if cov >= rank.FEDERATE_BELOW_COVERAGE and budget_ms is None:
        return {"results": local_fused, "_federated": [], "_dense": None,
                "_gate": {"federated": False, "coverage": cov}}

    # Not confident: ask the other legs, launched together so the dense leg is
    # free in wall-clock terms, and fuse them as ADDITIVE legs (rank.py).
    want_dense = config.DENSE_ENABLED if use_dense is None else bool(use_dense)
    dense_task = None
    dense_started = time.monotonic()
    if want_dense:
        dense_task = asyncio.ensure_future(
            _dense_keys(conn, text, max(page_size, 20) * 3))

    ups = await federation.fan_out(text, page_size=min(max(page_size, 20), 100),
                                   budget_ms=budget_ms)

    dense_order: list[str] = []
    dense_state = "off"
    if dense_task is not None:
        # The deadline runs from when the leg STARTED, not from when the
        # fan-out happened to return. Embedding a query costs ~0.6 s, and a
        # fan-out that came back early, because every circuit was open or the
        # answer was cached, used to leave the leg only EMBED_QUERY_TIMEOUT_S
        # and then throw away work that was nearly finished. The leg vanished
        # from exactly the searches that had the most time to spare.
        budget_s = (budget_ms or config.FEDERATION_BUDGET_MS) / 1000.0
        left = budget_s + config.EMBED_QUERY_TIMEOUT_S - (time.monotonic() - dense_started)
        try:
            dense_order = await asyncio.wait_for(dense_task, timeout=max(0.05, left))
            dense_state = "ok" if dense_order else "empty"
        except (asyncio.TimeoutError, Exception):
            dense_task.cancel()
            dense_order, dense_state = [], "timeout"

    rankings = [[e["_key"] for e in local]]
    leg_weights = [1.0]
    if tool_order:
        rankings.append([k for k in tool_order if k in by_key])
        leg_weights.append(tool_w)
    if name_order:
        rankings.append([k for k in name_order if k in by_key])
        leg_weights.append(rank.NAME_LEG_WEIGHT)
    # What the local legs returned. Additive legs may add to it, never reorder it.
    returned = {k for r in rankings for k in r}
    novel: list[tuple[str, dict]] = []
    fam_filter = expand_type_filter(flt.get("type", [])) if flt and flt.get("type") else None
    pay_filter = payments.filter_active(flt)

    for u in ups:
        if not u.get("ok"):
            continue
        order = []
        for e in u["results"]:
            if fam_filter and e.get("type_family") not in fam_filter:
                continue
            # An upstream result carries no terms we have checked, so it is
            # judged on what it states itself, which for most registries is nothing.
            if pay_filter and not payments.passes(payments.merge(payments.from_entry(e), None), flt):
                continue
            k = e["key"]
            if k in by_key:
                by_key[k].setdefault("_also", []).append(u["source"])
                if k not in returned:
                    order.append(k)
            else:
                order.append(k)
                novel.append((u["id"], e))
                by_key[k] = {
                    "identifier": e["identifier"],
                    # §5.3.2: a response entry MUST carry identifier and nothing
                    # else, so upstreams legitimately omit displayName. Derive a
                    # readable label from the URN's last segment rather than
                    # rendering a blank row.
                    "displayName": e.get("displayName") or _label(e["identifier"], e.get("url")),
                    "type": e.get("type"),
                    "url": e.get("url"),
                    "description": e.get("description"),
                    "tags": e.get("tags") or None,
                    "capabilities": e.get("capabilities") or None,
                    "representativeQueries": e.get("representativeQueries") or None,
                    "source": e["source"],
                    "_key": k, "_live": None,
                }
        if order:
            rankings.append(order)
            leg_weights.append(rank.UPSTREAM_LEG_WEIGHT)
    # Entries only an upstream returned are written back, so the next search
    # finds them here without asking anyone. Off the request path; the writer
    # skips any that another source indexed meanwhile.
    if novel:
        store.absorb(novel)

    # The dense ranking joins the fusion as one more ordering. Entries it
    # surfaces that lexical search missed are the whole point, so they are
    # hydrated from our own index rather than dropped.
    if dense_order:
        dense_keep = []
        for k in dense_order:
            if k in returned:
                continue
            if k in by_key:
                dense_keep.append(k)
                continue
            r = conn.execute("SELECT * FROM entries WHERE key=?", (k,)).fetchone()
            if not r or not _passes_filter(r, flt):
                continue
            if _col(r, "visibility") == "private":
                continue    # unreachable: private rows are not in `entries`
            e = store.row_to_entry(r)
            e.update({"source": config.PUBLIC_BASE, "_key": k, "_live": r["live"]})
            _attach_verification(e, r)
            by_key[k] = e
            dense_keep.append(k)
        if dense_keep:
            rankings.append(dense_keep)
            leg_weights.append(rank.DENSE_LEG_WEIGHT)

    fused = rank.rrf(rankings, weights=leg_weights)
    scored = rank.fuse_to_scores(fused)
    results = []
    for k, s in sorted(scored.items(), key=lambda kv: -kv[1]):
        e = by_key.get(k)
        if not e:
            continue
        e = dict(e)
        e["score"] = rank.apply_liveness(s, e.get("_live"))
        results.append(e)
    results.sort(key=lambda x: -x["score"])
    return {"results": diversify(results, page_size)[:page_size], "_federated": ups,
            "_dense": {"state": dense_state, "candidates": len(dense_order)},
            "_gate": {"federated": True, "coverage": cov}}


def _fuse_local(local: list[dict], tool_order: list[str],
                by_key: dict[str, dict], page_size: int,
                tool_w: float = rank.TOOL_LEG_WEIGHT,
                name_order: list[str] | None = None) -> list[dict]:
    """Fuse the lexical and tool legs for the modes that do not federate.

    Same RRF and the same tool weight as `auto` uses, so the two paths agree
    about what our own index thinks; `auto` then adds the dense and upstream
    legs on top. Without this the fast path and the default path disagreed about
    the contents of one index, which is worse than either being wrong alone.
    """
    name_order = [k for k in (name_order or []) if k in by_key]
    if not tool_order and not name_order:
        return diversify(local, page_size)[:page_size]
    rankings = [[e["_key"] for e in local]]
    weights = [1.0]
    if tool_order:
        rankings.append([k for k in tool_order if k in by_key])
        weights.append(tool_w)
    if name_order:
        rankings.append(name_order)
        weights.append(rank.NAME_LEG_WEIGHT)
    fused = rank.rrf(rankings, weights=weights)
    scored = rank.fuse_to_scores(fused)          # same call the auto path makes
    out = []
    for k, sc in sorted(scored.items(), key=lambda kv: -kv[1]):
        e = by_key.get(k)
        if not e:
            continue
        e = dict(e)
        e["score"] = rank.apply_liveness(sc, e.get("_live"))
        out.append(e)
    out.sort(key=lambda x: -x["score"])
    return diversify(out, page_size)[:page_size]


async def _dense_keys(conn: sqlite3.Connection, text: str, limit: int) -> list[str]:
    """Dense ranking, isolated so a failure here can never fail a search."""
    try:
        from . import embed
        return await embed.dense_ranking(conn, text, limit)
    except Exception:
        return []


def clean(entries: list[dict]) -> list[dict]:
    """Strip internal bookkeeping before the entry goes over the wire."""
    out = []
    for e in entries:
        d = {k: v for k, v in e.items() if not k.startswith("_") and v is not None}
        out.append(d)
    return out


def query_match(text: str, results: list[dict]) -> dict:
    """How much of the query the best result actually accounts for.

    The `score` on each entry is deliberately relative to the best hit in its
    own result set, because BM25 magnitudes are corpus- and query-dependent and
    mean nothing on their own. That is correct for ordering and misleading on
    its own: measured on the live index, the nonsense query "zzzz nonexistent
    capability qqqq" returned a top score of 100, because something always ranks
    first. A caller reading that number, very often an agent about to act
    without a human in the loop, has no way to tell it apart from a real answer.

    So the envelope carries one absolute number next to the relative ones. It is
    corpus-independent, unlike every other signal here, which is exactly the
    property needed: the fraction of the query's content words the top result's
    own text accounts for. It is not a correctness claim, and it cannot be, but
    it separates "this is the best of several good answers" from "this is the
    best of nothing".
    """
    terms = _content_terms(text)
    if not results or not terms:
        return {"coverage": 0.0, "confidence": "none", "queryTerms": terms,
                "note": "no result, or no content words in the query"}
    top = results[0]
    # The tool that matched counts as the top result's own text. Since the tool
    # leg runs on every path, an entry can legitimately be the best answer with
    # none of the query's words in its prose: Stripe's description is one
    # sentence pointing at its docs, and every word of "charge a credit card"
    # lives in `PostCharges`. Reading only the prose scored that as coverage 0.0
    # and confidence "none", which told the caller we had found nothing at the
    # exact moment we had found the right thing.
    mt = top.get("matchedTool") or {}
    hay = " ".join(str(top.get(k) or "") for k in ("displayName", "description")) + " " \
          + " ".join(str(x) for x in (top.get("representativeQueries") or [])) + " " \
          + " ".join(str(x) for x in (top.get("tags") or [])) + " " \
          + str(mt.get("name") or "") + " " + str(mt.get("description") or "")
    words = set(re.findall(r"[a-z0-9]+", hay.lower()))
    hit = [t for t in terms
           if any(w.startswith(t) or t.startswith(w) for w in words)]
    cov = len(hit) / len(terms)
    conf = ("high" if cov >= 0.75 else "medium" if cov >= 0.4
            else "low" if cov > 0 else "none")
    return {
        "coverage": round(cov, 3),
        "confidence": conf,
        "matchedTerms": hit,
        "queryTerms": terms,
        "note": ("each result's `score` is relative to the best hit in this response, "
                 "so the top result always scores near 100 even when nothing matched. "
                 "`coverage` is absolute: the fraction of the query's content words the "
                 "top result's own text, including the tool that matched, accounts for. "
                 "It measures overlap, not "
                 "correctness, and it is never a trust or safety rating."),
    }
