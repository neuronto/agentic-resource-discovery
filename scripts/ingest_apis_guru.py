#!/usr/bin/env python3
"""Index the world's real APIs from the APIs.guru corpus.

The index was 8,371 MCP servers and 159 APIs, which meant an agent asking to
charge a credit card got three obscure wrappers and not Stripe. A discovery
registry that cannot name the software the world actually runs on is a
catalogue of hobby projects. This fixes the corpus, not the ranking.

APIs.guru is a curated, openly licensed collection of OpenAPI descriptions,
2,529 of them, including AWS, Google, Stripe, Slack, Shopify, Twilio, Microsoft
and Atlassian. Nobody's permission is needed: these are specifications their
owners publish so that clients can be written against them.

Three decisions worth stating, because each could have been made carelessly:

  The entry points at the VENDOR's own specification URL, taken from
  `x-origin`, never at the apis.guru mirror. All 2,529 records carry one. This
  keeps `publisher` honest and makes a liveness probe mean something about the
  vendor rather than about a CDN.

  A stale specification is labelled, not hidden. Stripe's newest record in the
  corpus is from 2023. That is worth knowing and is carried through as a tag
  rather than quietly presented as current.

  AWS alone contributes several hundred services. They are not collapsed,
  because "upload a file to S3" and "run a Lambda" are different capabilities
  and merging them would make the index worse. The crowding they cause is a
  ranking problem and is fixed in ranking, by capping how many results one
  publisher may take on a page.

Usage:
  python3 scripts/ingest_apis_guru.py --limit 50      # pilot
  python3 scripts/ingest_apis_guru.py                 # the corpus
  python3 scripts/ingest_apis_guru.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store  # noqa: E402

LIST_URL = "https://api.apis.guru/v2/list.json"
SOURCE = "apis.guru"
UA = "Neuronto/1.0 (+https://neuronto.com/about; ARD registry; crawler)"
STALE_YEARS = 2


def fetch_list() -> dict:
    req = urllib.request.Request(LIST_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _newest(api: dict) -> dict | None:
    """The version the corpus considers current, by its own `updated` stamp."""
    versions = api.get("versions") or {}
    if not versions:
        return None
    pref = api.get("preferred")
    if pref and pref in versions:
        return versions[pref]
    return max(versions.values(), key=lambda v: v.get("updated") or "")


def _origin_url(ver: dict) -> str | None:
    """The vendor's own specification URL, never the mirror."""
    o = (ver.get("info") or {}).get("x-origin")
    if isinstance(o, list):
        for item in o:
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    if isinstance(o, dict) and o.get("url"):
        return o["url"]
    return None


def _slug(s: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in (s or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:60] or "api"


def _queries(title: str, desc: str, cats: list) -> list[str]:
    """What this API is a good answer to, in the words someone would ask.

    Two to five, which is what the conformance tool recommends for embedding,
    and derived from the publisher's own title and categories rather than
    invented: an entry that claims to answer a question it cannot is worse for
    the index than one that claims nothing.
    """
    t = (title or "").strip().rstrip(".")
    out: list[str] = []
    if t:
        out.append(f"use the {t}".lower())
        # Only append the word when the title does not already end in it,
        # or you get "stripe api api", which is what the first run produced.
        if not t.lower().rstrip("s").endswith("api"):
            out.append(f"{t} api".lower())
    first = (desc or "").strip().split(".")[0].strip()
    if 12 < len(first) < 110:
        out.append(first.lower())
    for c in (cats or [])[:2]:
        out.append(f"{c} api".lower())
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q); uniq.append(q)
    return uniq[:5]


def a_pref(api: dict) -> str:
    """The version key `_newest` chose, so the spec matches the entry."""
    versions = api.get("versions") or {}
    pref = api.get("preferred")
    if pref and pref in versions:
        return pref
    return max(versions, key=lambda k: (versions[k].get("updated") or ""), default="")


def build_entry(key: str, api: dict) -> dict | None:
    ver = _newest(api)
    if not ver:
        return None
    info = ver.get("info") or {}
    title = (info.get("title") or "").strip()
    provider = (info.get("x-providerName") or key.split(":")[0] or "").strip().lower()
    if not title or not provider:
        return None
    url = _origin_url(ver) or ver.get("swaggerUrl")
    if not url:
        return None

    service = info.get("x-serviceName") or ""
    ident = f"urn:air:{provider}:api:{_slug(service or title)}"

    desc = (info.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 600:
        desc = desc[:597].rsplit(" ", 1)[0] + "..."

    cats = info.get("x-apisguru-categories") or []
    tags = ["api", "openapi"] + [str(c) for c in cats]
    if service:
        tags.append(_slug(service))

    updated = ver.get("updated") or ""
    try:
        age_years = (time.time() - time.mktime(time.strptime(updated[:10], "%Y-%m-%d"))) / 31557600
    except Exception:
        age_years = 0.0
    if age_years >= STALE_YEARS:
        # Said out loud rather than hidden: a reader deciding whether to depend
        # on this deserves to know the description is years old.
        tags.append("spec-not-recently-updated")

    return {
        "identifier": ident,
        "displayName": title,
        "description": desc or f"{title}, an HTTP API described by an OpenAPI document.",
        "type": "application/vnd.oai.openapi+json",
        "url": url,
        "tags": sorted(set(tags)),
        "representativeQueries": _queries(title, desc, cats),
        "version": str(info.get("version") or "")[:40],
    }



def fetch_spec(url: str, timeout: int = 60) -> dict | None:
    """The normalised JSON mirror, because vendor specs are YAML half the time."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def operations(spec: dict, cap: int = 400) -> list[dict]:
    """Every callable operation, as a tool.

    This is the reason the ingest exists. A vendor's top-level description is
    almost always one useless sentence: Stripe's entire `info.description` is
    "The Stripe REST API. Please see https://stripe.com/docs/api for more
    details." Nothing in that can ever match "charge a credit card". The verbs
    live one level down, in the operations, where `POST /v1/charges` says what
    it does. Indexing the document without reading its operations produces a
    listing, not a discovery index.

    Deliberately shallow: method, path, summary, description, tags. No request
    schemas. They are megabytes on the large vendors and add nothing to
    retrieval, and the box is small.
    """
    out: list[dict] = []
    methods = ("get", "post", "put", "patch", "delete")
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for m in methods:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            summary = (op.get("summary") or "").strip()
            desc = (op.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 400:
                desc = desc[:397].rsplit(" ", 1)[0] + "..."
            name = (op.get("operationId") or f"{m.upper()} {path}").strip()
            title = summary or name
            if not (summary or desc):
                continue          # an operation that describes nothing is noise
            tags = " ".join(str(t) for t in (op.get("tags") or [])[:4])
            body = " ".join(x for x in (summary, desc, tags) if x).strip()
            out.append({"name": name[:200], "title": title[:200], "description": body})
            if len(out) >= cap:
                return out
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="pilot with N APIs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="substring filter on the corpus key")
    ap.add_argument("--with-operations", action="store_true",
                    help="fetch each spec and index its operations as tools. "
                         "This is what makes the entries findable by what they do.")
    ap.add_argument("--op-cap", type=int, default=400)
    a = ap.parse_args()

    print("fetching the corpus...", flush=True)
    data = fetch_list()
    keys = sorted(data)
    if a.only:
        keys = [k for k in keys if a.only.lower() in k.lower()]
    if a.limit:
        keys = keys[:a.limit]
    print(f"{len(keys)} APIs selected", flush=True)

    conn = store.connect() if not a.dry_run else None
    built = skipped = 0
    n_ops = [0]
    providers: dict[str, int] = {}
    for k in keys:
        e = build_entry(k, data[k])
        if not e:
            skipped += 1
            continue
        built += 1
        prov = e["identifier"].split(":")[2]
        providers[prov] = providers.get(prov, 0) + 1
        if a.dry_run:
            if built <= 5:
                print(f"  {e['identifier']}\n    {e['displayName']}  ->  {e['url'][:80]}")
            continue
        key = store.upsert_entry(conn, e, SOURCE)
        if a.with_operations and key:
            spec = fetch_spec(data[k]["versions"][
                a_pref(data[k])].get("swaggerUrl") or "")
            ops_ = operations(spec, a.op_cap) if spec else []
            if ops_:
                # Written to `tools`, never to the mcp_* columns. Reading an
                # OpenAPI document and completing an MCP handshake are different
                # evidence, and the published "verified tools" figure counts the
                # handshakes only. Blurring them would corrupt a number we
                # publish and invite others to quote.
                store.replace_tools(conn, key, ops_)
                n_ops[0] += len(ops_)
                conn.commit()

    if conn:
        conn.commit()
    print(f"\nbuilt {built}, skipped {skipped}, operations indexed {n_ops[0]}")
    top = sorted(providers.items(), key=lambda kv: -kv[1])[:8]
    print("largest providers:", ", ".join(f"{p}={n}" for p, n in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
