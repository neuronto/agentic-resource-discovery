"""Publisher analytics: validate a domain, and tell it where it actually stands.

Publishing an ARD manifest is easy. Knowing whether it worked is not, and there
is no console for it: a publisher cannot see which registries indexed them, what
their entries are competing against, or why a well-formed catalogue still
returns nothing. This module answers those questions.

Three parts, in ascending order of what they are worth to the publisher:

  1. **Discovery**, is the manifest reachable, on how many of the four paths a
     consumer may check? A catalogue served on one path is invisible to any
     client that checks another.
  2. **Conformance**, does it satisfy the specification, entry by entry, with
     findings written as instructions rather than error codes.
  3. **Coverage**, do the registries that exist actually return this domain?
     This is the part nobody else measures, and it is the only one that decides
     whether an agent will ever find them.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from . import config
from .normalize import media_family

HEADERS = {"user-agent": config.USER_AGENT, "accept": "application/json,*/*"}

# The four ways a publisher may advertise entries (spec 5.1). A consumer MUST
# fetch ard.json; the rest are belt and braces, and each one a publisher skips
# is a class of client that will never see them.
WELL_KNOWN = "/.well-known/ard.json"
LEGACY     = "/.well-known/ai-catalog.json"

URN_RE = re.compile(r"^urn:air:([a-zA-Z0-9.-]+)(?::([a-zA-Z0-9._:-]+))?:([a-zA-Z0-9._-]+)$")
CONFORMANT_TYPES = {
    "application/ai-catalog+json", "application/agent-card+json",
    "application/a2a-agent-card+json", "application/mcp-server-card+json",
    "application/agent-skills+zip", "application/agent-skills+gzip",
    'text/markdown; profile="urn:air:agent-skills"',
    "application/ai-registry", "application/ai-registry+json",
}


async def _get(client: httpx.AsyncClient, url: str) -> tuple[int | None, str]:
    try:
        r = await client.get(url)
        return r.status_code, r.text
    except Exception:
        return None, ""


async def discovery(client: httpx.AsyncClient, base: str) -> dict:
    """Which of the four advertisement paths actually resolve.

    The four fetches run concurrently. They used to run one after another, so
    a domain that let each hang to the timeout cost four timeouts in a row, and
    with the coverage probe behind it an audit could take longer than the
    reverse proxy in front of this service will wait. The caller then got a
    gateway error while the audit finished for nobody. Concurrent, the worst
    case is one timeout, whatever the domain does.
    """
    out: dict[str, Any] = {"paths": {}, "manifest": None, "manifest_url": None}
    (wk, lg, rb, hp) = await asyncio.gather(
        _get(client, base + WELL_KNOWN), _get(client, base + LEGACY),
        _get(client, base + "/robots.txt"), _get(client, base + "/"))

    for label, path, (st, body) in (("well_known", WELL_KNOWN, wk), ("legacy", LEGACY, lg)):
        ok = st == 200 and body.strip().startswith("{")
        out["paths"][label] = {"found": ok, "status": st}
        if ok and out["manifest"] is None:
            try:
                out["manifest"] = json.loads(body)
                out["manifest_url"] = base + path
            except Exception:
                out["paths"][label]["found"] = False

    st, body = rb
    out["paths"]["agentmap"] = {"found": bool(st == 200 and re.search(r"(?im)^\s*Agentmap:", body)),
                                "status": st}
    st, body = hp
    out["paths"]["link_tag"] = {
        "found": bool(st == 200 and re.search(r'rel=["\'](ard|ai-catalog)["\']', body or "", re.I)),
        "status": st}
    return out


def conformance(manifest: dict | None) -> dict:
    """Validate a manifest and phrase every finding as something to do."""
    findings: list[dict] = []
    if not isinstance(manifest, dict):
        return {"valid": False, "entries": 0, "findings": [
            {"severity": "error", "message":
             "No manifest found. Serve a JSON document at /.well-known/ard.json "
             "with a specVersion and an entries array."}]}

    if manifest.get("specVersion") != "1.0":
        findings.append({"severity": "error",
                         "message": 'Set "specVersion": "1.0" at the top level.'})
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        return {"valid": False, "entries": 0, "findings": findings + [
            {"severity": "error", "message":
             "entries must be a non-empty array. A manifest with no entries "
             "describes nothing and will never be returned by a search."}]}

    for i, e in enumerate(entries):
        label = (e.get("displayName") or e.get("identifier") or f"entry {i}") if isinstance(e, dict) else f"entry {i}"
        if not isinstance(e, dict):
            findings.append({"severity": "error", "entry": label,
                             "message": "Entry is not an object."}); continue

        ident = e.get("identifier")
        if not ident:
            findings.append({"severity": "error", "entry": label, "message":
                "Missing identifier. Use urn:air:<your-domain>:<namespace>:<name>."})
        elif not URN_RE.match(str(ident)):
            findings.append({"severity": "error", "entry": label, "message":
                f"identifier {ident!r} is not a valid discovery URN. "
                "Expected urn:air:<publisher>:<namespace>:<name>."})

        if not e.get("displayName"):
            findings.append({"severity": "error", "entry": label,
                             "message": "Missing displayName."})

        t = e.get("type")
        if not t:
            findings.append({"severity": "error", "entry": label,
                             "message": "Missing type. Use an IANA media type."})
        elif t not in CONFORMANT_TYPES:
            fam = media_family(t)
            hint = (f" It looks like a {fam} resource; the conformant spelling is "
                    "application/mcp-server-card+json." if fam == "mcp-server" else "")
            findings.append({"severity": "info", "entry": label, "message":
                f"type {t!r} is outside the conformance tool's allowlist, so it "
                f"will raise a warning there.{hint}"})

        if ("url" in e) == ("data" in e):
            findings.append({"severity": "error", "entry": label, "message":
                "Provide exactly one of url or data."})

        rq = e.get("representativeQueries")
        if not rq:
            findings.append({"severity": "warning", "entry": label, "message":
                "No representativeQueries. This is the field registries build "
                "their semantic index from: without it the entry is a valid "
                "catalogue entry that no search will ever return. Add 2 to 5, "
                "phrased the way someone would ask."})
        elif not isinstance(rq, list) or not (2 <= len(rq) <= 5):
            findings.append({"severity": "warning", "entry": label, "message":
                f"representativeQueries has {len(rq) if isinstance(rq, list) else '?'} "
                "items; 2 to 5 is recommended."})

        if not e.get("description"):
            findings.append({"severity": "warning", "entry": label, "message":
                "No description. It contributes to the semantic index."})

        tm = e.get("trustManifest")
        if isinstance(tm, dict) and not tm.get("identity"):
            findings.append({"severity": "error", "entry": label,
                             "message": "trustManifest is present but has no identity."})

    return {"valid": not any(f["severity"] == "error" for f in findings),
            "entries": len(entries), "findings": findings}


async def coverage(client: httpx.AsyncClient, domain: str, manifest: dict | None,
                   local_hits: int) -> list[dict]:
    """Which registries actually return this domain.

    Queried with the publisher's own representativeQueries where they exist,
    because that is what they are asking to be found for. Anything else measures
    our imagination rather than their intent.
    """
    queries: list[str] = []
    for e in (manifest or {}).get("entries", [])[:4]:
        if isinstance(e, dict):
            queries += [str(q) for q in (e.get("representativeQueries") or [])[:2]]
    if not queries:
        queries = [domain.split(".")[0]]
    queries = queries[:5]

    async def ask(url, q):
        try:
            r = await client.post(url, json={"query": {"text": q}},
                                  headers={**HEADERS, "content-type": "application/json"},
                                  timeout=8)
            return True, (r.status_code == 200 and domain.lower() in r.text.lower())
        except Exception:
            return False, False

    async def probe(uid, name, url, source):
        # All of one registry's queries at once. Sequential, five queries at an
        # eight second timeout was forty seconds against a registry that had
        # gone quiet, which alone exceeds what the proxy in front of us waits.
        res = await asyncio.gather(*(ask(url, q) for q in queries))
        asked = sum(1 for ok, _ in res if ok)
        hits = sum(1 for _, hit in res if hit)
        return {"registry": name, "source": source, "queries": asked,
                "returned_for": hits,
                "indexed": hits > 0}

    results = await asyncio.gather(*(probe(*u) for u in config.UPSTREAMS))
    results.insert(0, {"registry": "Neuronto", "source": config.PUBLIC_BASE,
                       "queries": len(queries), "returned_for": local_hits,
                       "indexed": local_hits > 0})
    return results


def score(disc: dict, conf: dict, cov: list[dict]) -> dict:
    """A published, recomputable score. A number you cannot rederive is a number
    you cannot act on."""
    parts = []
    paths_found = sum(1 for v in disc["paths"].values() if v["found"])

    served = 15 if disc["manifest"] else 0
    parts.append(("Serves a manifest", served, 15,
                  "found" if served else "no manifest on any advertised path"))

    redundancy = round(10 * paths_found / 4)
    parts.append(("Advertised on all four paths", redundancy, 10,
                  f"{paths_found} of 4"))

    errs = sum(1 for f in conf["findings"] if f["severity"] == "error")
    warns = sum(1 for f in conf["findings"] if f["severity"] == "warning")
    cscore = 0 if not disc["manifest"] else max(0, 25 - errs * 8 - warns * 3)
    parts.append(("Conformance", cscore, 25,
                  f"{errs} errors, {warns} warnings"))

    with_rq = 0
    for e in (disc["manifest"] or {}).get("entries", []):
        if isinstance(e, dict) and e.get("representativeQueries"):
            with_rq += 1
    total_e = conf["entries"] or 1
    rq = round(20 * with_rq / total_e)
    parts.append(("Entries are searchable", rq, 20,
                  f"{with_rq} of {total_e} carry representativeQueries"))

    indexed = sum(1 for c in cov if c["indexed"])
    covs = round(30 * indexed / max(1, len(cov)))
    parts.append(("Returned by registries", covs, 30,
                  f"{indexed} of {len(cov)} return this domain"))

    total = sum(p[1] for p in parts)
    grade = ("A" if total >= 90 else "B" if total >= 75 else
             "C" if total >= 55 else "D" if total >= 35 else "F")
    return {"total": total, "grade": grade,
            "breakdown": [{"check": c, "points": p, "max": m, "detail": d}
                          for c, p, m, d in parts]}


def recommendations(disc: dict, conf: dict, cov: list[dict], domain: str = "") -> list[str]:
    """The next actions, most valuable first."""
    out = []
    if not disc["manifest"]:
        return ["Serve an ARD manifest at /.well-known/ard.json. Nothing else "
                "matters until a consumer can fetch one."]
    if not disc["paths"]["well_known"]["found"] and disc["paths"]["legacy"]["found"]:
        out.append("Your manifest is only on the predecessor path. A conformant "
                   "consumer MUST fetch /.well-known/ard.json and MAY ignore "
                   "ai-catalog.json, so serve both.")
    missing = [k for k, v in disc["paths"].items() if not v["found"]]
    if "agentmap" in missing:
        out.append("Add an Agentmap: line to robots.txt pointing at your manifest. "
                   "A crawler that reads robots.txt and nothing else then finds you.")
    if "link_tag" in missing:
        out.append('Add <link rel="ard"> and <link rel="ai-catalog"> to your '
                   "homepage head. Some crawlers check only the link tag.")
    no_rq = [f for f in conf["findings"] if "representativeQueries" in f["message"]]
    if no_rq:
        out.append("Add representativeQueries to every entry. It is the single "
                   "highest-value field: registries index on it, and an entry "
                   "without it will not be returned by any semantic search.")
    errs = [f for f in conf["findings"] if f["severity"] == "error"]
    if errs:
        out.append(f"Fix {len(errs)} conformance error(s): " +
                   "; ".join(f["message"] for f in errs[:3]))
    # Being indexed here and being indexed elsewhere are different problems with
    # different answers, so they are different recommendations. The single line
    # this replaced said only that "registries crawl on their own schedule",
    # which is true of the ones with no submission path and misleading about the
    # rest: it told a publisher to wait when the fix was one request. This text
    # is rendered by the console, by `ard-publish check` and by the API, so it
    # is the one place worth getting right.
    here = next((c for c in cov if c["registry"].lower().startswith("neuronto")), None)
    others = [c["registry"] for c in cov if not c["indexed"] and c is not here]

    if here is not None and not here["indexed"]:
        d = domain or "<your-domain>"
        out.append(
            f"You are not in this index yet, and this audit already fetched and validated "
            f"your manifest, so there is nothing left to check. One request indexes it: "
            f"POST {{\"domain\": \"{d}\"}} to /submit, or run `ard-publish submit {d}`. "
            f"Nothing is taken on your word: the manifest is fetched from your domain again "
            f"at that moment.")
    if others:
        # Measured on 2026-09-01: WellKnown has a submission form; Desvela, the
        # Hugging Face finder and GitHub's have none, so for those the honest
        # advice really is that crawling is on their schedule.
        takes_submissions = {"WellKnown"}
        can_ask = [r for r in others if r in takes_submissions]
        crawl_only = [r for r in others if r not in takes_submissions]
        if can_ask:
            out.append("Not returned by " + ", ".join(can_ask) +
                       ", which accepts submissions: send your domain to its own submit form.")
        if crawl_only:
            out.append("Not returned by " + ", ".join(crawl_only) +
                       ". These have no submission path we could find, so they will reach "
                       "you on their own crawl schedule or not at all. What you control is "
                       "being conformant, reachable, and served on every discovery path.")
    return out or ["Nothing outstanding. Re-run after any change to your catalogue."]


async def run(domain: str, local_hits: int = 0) -> dict:
    """Audit one domain end to end."""
    dom = re.sub(r"^https?://", "", (domain or "").strip().lower()).strip("/").split("/")[0]
    if not dom or "." not in dom:
        return {"error": "invalid_domain",
                "detail": "Pass a hostname, for example example.com"}
    base = "https://" + dom
    t0 = time.perf_counter()
    # 10s per fetch, all fetches within a phase concurrent, two phases: the
    # worst case is about 20s, inside the 30s the reverse proxy allows.
    async with httpx.AsyncClient(headers=HEADERS, timeout=10, follow_redirects=True) as client:
        disc = await discovery(client, base)
        conf = conformance(disc["manifest"])
        cov = await coverage(client, dom, disc["manifest"], local_hits)
    return {"domain": dom, "checked_at": int(time.time()),
            "took_ms": int((time.perf_counter() - t0) * 1000),
            "discovery": disc["paths"], "manifest_url": disc["manifest_url"],
            "conformance": conf, "coverage": cov,
            "score": score(disc, conf, cov),
            "recommendations": recommendations(disc, conf, cov, dom),
            # Internal: the caller may pass this to `competition`. Stripped
            # before the report goes over the wire.
            "_manifest": disc["manifest"]}


# ---------------------------------------------------------------------------
# Competition
# ---------------------------------------------------------------------------

def _why_ahead(e: dict) -> str:
    """Say, in the publisher's terms, what this rival has that they may not."""
    v = e.get("verification") or {}
    bits = []
    if v.get("tools"):
        bits.append(f"{v['tools']} tools read from its own tools/list")
    if v.get("reachable"):
        bits.append("endpoint answered when probed")
    n = e.get("_sources") or 0
    if n > 1:
        bits.append(f"listed by {n} independent registries")
    if e.get("description") and len(e["description"]) > 120:
        bits.append("a description long enough to match on")
    if e.get("representativeQueries"):
        bits.append(f"{len(e['representativeQueries'])} stated queries")
    return "; ".join(bits) or "a closer text match on this query"


def competition(conn, domain: str, manifest: dict | None,
                queries: list[str] | None = None) -> dict:
    """For the publisher's own stated queries, who is beating them, and why.

    Coverage answers "does any registry return me". This answers the question
    that follows it, and the one a publisher actually cares about: when an agent
    asks for what I do, what comes back instead of me, and what do those entries
    have that I do not?

    The queries come from the publisher's own `representativeQueries`, so this
    is scored against their stated intent rather than ours. The reasons are read
    off the winning entries rather than asserted: how many tools we read from
    the server itself, whether it answered when probed, how many independent
    registries carry it. Every one of those is something the publisher can go
    and change, which is the only reason to put a number in front of them.
    """
    from . import search as _search

    qs = list(queries or [])
    if not qs:
        for e in (manifest or {}).get("entries", [])[:6]:
            if isinstance(e, dict):
                qs += [str(q) for q in (e.get("representativeQueries") or [])[:2]]
    # A domain with no manifest is exactly the one that needs this most, so fall
    # back to what we already hold for it, and then to its own name.
    if not qs:
        for r in conn.execute(
                """SELECT rep_queries, display_name FROM entries
                   WHERE lower(publisher)=? LIMIT 8""", (domain.lower(),)):
            try:
                qs += [str(q) for q in (json.loads(r["rep_queries"] or "[]"))[:2]]
            except Exception:
                pass
            if not qs and r["display_name"]:
                qs.append(str(r["display_name"]))
    qs = [q for q in dict.fromkeys(qs) if q.strip()][:5]
    if not qs:
        return {"queries": [], "note": ("nothing to test against: this domain publishes no "
                                        "representativeQueries and has no indexed entries. "
                                        "Add representativeQueries to each entry, they are "
                                        "what an agent matches on.")}

    dom = domain.lower()
    rows = []
    for q in qs:
        hits = _search.local_search(conn, q, None, 10)
        rank_of, ahead = None, []
        for i, e in enumerate(hits, 1):
            mine = (e.get("url") or "").lower().find("//" + dom) >= 0 \
                   or dom in (e.get("identifier") or "").lower()
            if mine and rank_of is None:
                rank_of = i
            elif not mine and rank_of is None and len(ahead) < 3:
                ahead.append({"name": e.get("displayName") or e.get("identifier"),
                              "identifier": e.get("identifier"),
                              "score": e.get("score"),
                              "has_that_you_may_not": _why_ahead(e)})
        rows.append({"query": q,
                     "your_best_rank": rank_of,
                     "results_considered": len(hits),
                     "ahead_of_you": ahead})

    ranked = [r["your_best_rank"] for r in rows if r["your_best_rank"]]
    return {
        "queries": rows,
        "summary": {
            "tested": len(rows),
            "you_appear_in": len(ranked),
            "best_rank": min(ranked) if ranked else None,
            "median_rank": (sorted(ranked)[len(ranked) // 2] if ranked else None),
        },
        "note": ("measured against this index only, over the top 10 for each of your own "
                 "representativeQueries. Ranking is relevance, never endorsement, and it "
                 "is recomputed on every request rather than stored."),
    }


def competition_advice(comp: dict) -> list[str]:
    """Turn the comparison into instructions, in the report's existing voice."""
    out: list[str] = []
    qs = comp.get("queries") or []
    if not qs:
        return out
    summ = comp.get("summary") or {}
    missing = [q["query"] for q in qs if not q["your_best_rank"]]
    if missing and len(missing) == len(qs):
        out.append("You are not returned for any of your own representativeQueries. "
                   "The text an agent matches on is your displayName, description and "
                   "those queries, so rewrite them to say what the resource does in the "
                   "words someone would ask for it: " + ", ".join(repr(m) for m in missing[:3]))
    elif missing:
        out.append("Not returned for " + ", ".join(repr(m) for m in missing[:3])
                   + ". Those entries need a description that contains the words in the query.")

    # One-word queries lose to anything specific, and it is a common mistake.
    vague = [q["query"] for q in qs if len(q["query"].split()) < 2]
    if vague:
        out.append("Single-word representativeQueries like "
                   + ", ".join(repr(v) for v in vague[:3])
                   + " compete against the whole index. A query should read like the "
                     "request an agent would actually make, not like a category.")

    beaten = [q for q in qs if q["your_best_rank"] and q["your_best_rank"] > 3]
    if beaten:
        out.append(f"Ranked {beaten[0]['your_best_rank']} for {beaten[0]['query']!r}. "
                   "Entries above you are there on evidence we could read: a reachable "
                   "endpoint, tools listed by the server itself, or corroboration from "
                   "another registry. Publishing a callable endpoint is what closes that.")
    if summ.get("you_appear_in") == summ.get("tested") and summ.get("best_rank") == 1:
        out.append("You rank first for every query you asked to be found for. "
                   "The remaining work is coverage, not relevance.")
    return out
