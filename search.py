"""The search engine: local retrieval, filtering, and federated fusion."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import config, federation, rank, store
from .normalize import expand_type_filter, media_family

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def _label(identifier: str, url: str | None = None) -> str:
    """A human-readable name for an entry whose publisher gave none."""
    if identifier and identifier.startswith("urn:"):
        leaf = identifier.rstrip(":").split(":")[-1]
        if leaf:
            return leaf.replace("-", " ").replace("_", " ").strip() or identifier
    if url:
        return url.replace("https://", "").replace("http://", "").split("/")[0]
    return identifier


def _fts_query(text: str) -> str:
    """Build an FTS5 MATCH expression that degrades gracefully.

    User text is never passed through raw: FTS5 treats bare punctuation as
    syntax and a stray quote or hyphen raises rather than returning nothing.
    We OR the terms so a long natural-language query still matches on its
    content words, which is how people actually phrase a need.
    """
    toks = [t.lower() for t in _TOKEN.findall(text or "") if len(t) > 1]
    if not toks:
        return ""
    stop = {"the", "and", "for", "with", "that", "this", "you", "your", "can",
            "get", "how", "what", "which", "from", "into", "are", "was", "some",
            "any", "all", "one", "out", "use", "using", "want", "need", "please",
            "find", "give", "show", "make"}
    keep = [t for t in toks if t not in stop] or toks
    return " OR ".join(f'"{t}"*' for t in keep[:16])


def _passes_filter(row: sqlite3.Row, flt: dict | None) -> bool:
    """Structured constraints, matched on family rather than exact string.

    This is the normalisation payoff: a client asking for
    `application/mcp-server+json` means "MCP servers", and gets every one of
    them however the publisher spelled the type.
    """
    if not flt:
        return True
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


def local_search(conn: sqlite3.Connection, text: str, flt: dict | None,
                 limit: int) -> list[dict]:
    """BM25 over our own index, field-weighted, liveness-adjusted."""
    match = _fts_query(text)
    if not match:
        return []
    w = ",".join(str(x) for x in rank.FTS_WEIGHTS)
    sql = f"""
      SELECT e.*, bm25(entries_fts, {w}) AS bm
      FROM entries_fts JOIN entries e ON e.key = entries_fts.key
      WHERE entries_fts MATCH ?
      ORDER BY bm
      LIMIT ?
    """
    try:
        rows = conn.execute(sql, (match, max(limit * 6, 60))).fetchall()
    except sqlite3.OperationalError:
        return []

    kept = [r for r in rows if _passes_filter(r, flt)]
    if not kept:
        return []
    # bm25() is negative and lower is better; flip so higher is better.
    raw = []
    for r in kept:
        base = -float(r["bm"])
        n_src = len(json.loads(r["sources"] or "[]"))
        raw.append(base * rank.source_bonus(n_src))
    scores = rank.scale_scores(raw)

    out = []
    for r, s in zip(kept, scores):
        e = store.row_to_entry(r)
        e["score"] = rank.apply_liveness(s, r["live"])
        e["source"] = config.PUBLIC_BASE
        e["_key"] = r["key"]
        e["_live"] = r["live"]
        out.append(e)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


async def search(conn: sqlite3.Connection, text: str, flt: dict | None,
                 page_size: int, mode: str) -> dict:
    """Run a search in the requested federation mode (§5.4).

    `none`       our index only.
    `referrals`  our index, plus pointers to the other registries.
    `auto`       our index fused with live upstream results. The default, and
                 the mode nobody else implements.
    """
    mode = (mode or "auto").lower()
    if mode not in ("auto", "referrals", "none"):
        mode = "auto"

    local = local_search(conn, text, flt, max(page_size, 10) * 3)

    if mode == "none":
        return {"results": local[:page_size], "_federated": []}
    if mode == "referrals":
        return {"results": local[:page_size],
                "referrals": federation.referral_entries(), "_federated": []}

    # auto: fuse our ordering with each upstream's ordering via RRF.
    ups = await federation.fan_out(text, page_size=max(page_size, 20))
    rankings = [[e["_key"] for e in local]]
    by_key: dict[str, dict] = {e["_key"]: e for e in local}
    fam_filter = expand_type_filter(flt.get("type", [])) if flt and flt.get("type") else None

    for u in ups:
        if not u.get("ok"):
            continue
        order = []
        for e in u["results"]:
            if fam_filter and e.get("type_family") not in fam_filter:
                continue
            k = e["key"]
            order.append(k)
            if k in by_key:
                by_key[k].setdefault("_also", []).append(u["source"])
            else:
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

    fused = rank.rrf(rankings)
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
    return {"results": results[:page_size], "_federated": ups}


def clean(entries: list[dict]) -> list[dict]:
    """Strip internal bookkeeping before the entry goes over the wire."""
    out = []
    for e in entries:
        d = {k: v for k, v in e.items() if not k.startswith("_") and v is not None}
        out.append(d)
    return out
