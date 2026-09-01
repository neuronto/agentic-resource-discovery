"""Publisher services: manifest generation, domain ownership, private entries.

Three problems, one identity model.

**Most domains will never author a manifest.** They already run an MCP server, or
serve an OpenAPI document, or publish llms.txt, and the ARD manifest is a
restatement of things a crawler can find. So we build it from what the domain
already says and host it, and the publisher's only job is to link it or copy it.
Nothing is invented: every entry we emit points at something we fetched.

**Ownership has to be provable before anything can be private.** A DNS TXT
record is the cheapest proof that survives a change of hosting, and it is the
same mechanism the MCP registry uses for namespaces, so a publisher who has done
it once already understands it.

**An organisation's internal services are a registry problem.** Today that list
lives in a system prompt, which is a list an agent cannot search and nobody can
audit. A verified domain can add private entries that only its own key can see,
and a single query returns internal and public results together, each labelled
with which it is, so the caller always knows what it is looking at.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

import httpx

from . import config, store

TXT_PREFIX = "neuronto-site-verification="
DOH = "https://cloudflare-dns.com/dns-query"


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str) -> tuple[int | None, str]:
    try:
        r = await client.get(url, timeout=8,
                             headers={"user-agent": config.USER_AGENT},
                             follow_redirects=True)
        return r.status_code, r.text[:200000]
    except Exception:
        return None, ""


async def infer_resources(domain: str) -> list[dict]:
    """Find what a domain already exposes, and describe it as ARD entries.

    Only things we actually fetched become entries. A generated manifest that
    guesses is worse than none: it would put a claim on the publisher's domain
    that they never made and cannot defend.
    """
    base = f"https://{domain}"
    found: list[dict] = []
    async with httpx.AsyncClient() as c:
        # 1. An MCP server, proven by handshake rather than by the URL existing.
        from . import tools_index
        for path in ("/mcp", "/sse", "/api/mcp", "/mcp/server"):
            res = await tools_index.introspect_one(c, base + path)
            if res["status"].startswith("ok") or res["status"] == "auth":
                name = res.get("server_name") or domain
                found.append({
                    "identifier": f"urn:air:{domain}:mcp:"
                                  + re.sub(r"[^a-z0-9-]+", "-", name.lower())[:50],
                    "displayName": name,
                    "type": "application/mcp-server-card+json",
                    "url": base + path,
                    "description": (f"MCP server on {domain}, verified by handshake"
                                    + (f", exposing {len(res['tools'])} tools."
                                       if res["tools"] else
                                       ". Requires credentials before listing tools.")),
                    "representativeQueries": [t.get("name", "").replace("_", " ")
                                              for t in res["tools"][:6] if t.get("name")],
                    "_evidence": f"tools/list returned {len(res['tools'])} tools",
                })
                break

        # 2. An OpenAPI document.
        for path in ("/openapi.json", "/openapi.yaml", "/.well-known/openapi.json",
                     "/api/openapi.json", "/swagger.json"):
            st, body = await _get(c, base + path)
            if st == 200 and ('"openapi"' in body[:400] or "openapi:" in body[:400]):
                title = domain
                try:
                    title = (json.loads(body).get("info") or {}).get("title") or domain
                except Exception:
                    pass
                found.append({
                    "identifier": f"urn:air:{domain}:api:openapi",
                    "displayName": str(title)[:120],
                    "type": "application/vnd.oai.openapi+json",
                    "url": base + path,
                    "description": f"HTTP API on {domain} described by an OpenAPI document.",
                    "_evidence": f"{path} parsed as an OpenAPI document",
                })
                break

        # 3. An A2A agent card.
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            st, body = await _get(c, base + path)
            if st == 200 and body.strip().startswith("{"):
                try:
                    card = json.loads(body)
                except Exception:
                    continue
                found.append({
                    "identifier": f"urn:air:{domain}:agent:card",
                    "displayName": str(card.get("name") or domain)[:120],
                    "type": "application/a2a-agent-card+json",
                    "url": base + path,
                    "description": str(card.get("description")
                                       or f"A2A agent published by {domain}.")[:400],
                    "_evidence": f"{path} parsed as an agent card",
                })
                break

        # 4. Machine-readable documentation.
        st, body = await _get(c, base + "/llms.txt")
        if st == 200 and body.strip():
            found.append({
                "identifier": f"urn:air:{domain}:doc:llms-txt",
                "displayName": f"{domain} documentation for language models",
                "type": "text/markdown",
                "url": base + "/llms.txt",
                "description": f"Machine-readable documentation published by {domain}.",
                "_evidence": "llms.txt served content",
            })
    return found


def manifest_for(domain: str, entries: list[dict]) -> dict:
    """Render entries as a conformant ARD manifest."""
    clean = []
    for e in entries:
        d = {k: v for k, v in e.items() if not k.startswith("_") and v}
        if d.get("representativeQueries"):
            d["representativeQueries"] = [q for q in d["representativeQueries"] if q][:6]
            if not d["representativeQueries"]:
                d.pop("representativeQueries")
        clean.append(d)
    return {
        "specVersion": "1.0",
        "host": {"displayName": domain, "identifier": f"did:web:{domain}"},
        "entries": clean,
    }


# ---------------------------------------------------------------------------
# Domain ownership
# ---------------------------------------------------------------------------

def claim_token(domain: str) -> str:
    """Deterministic per domain, so asking twice does not invalidate the record."""
    seed = f"{domain.lower()}|neuronto-claim-v1"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


async def verify_domain(domain: str) -> dict:
    """Look for the proof record over DNS-over-HTTPS.

    DoH rather than a resolver library: it needs no system resolver, no extra
    dependency, and it answers from a public resolver rather than from whatever
    this machine happens to cache.
    """
    want = TXT_PREFIX + claim_token(domain)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(DOH, params={"name": domain, "type": "TXT"},
                            headers={"accept": "application/dns-json"}, timeout=10)
        recs = [a.get("data", "").strip('"') for a in (r.json().get("Answer") or [])]
    except Exception as e:
        return {"verified": False, "error": f"dns lookup failed: {type(e).__name__}",
                "expect": want, "found": []}
    hit = any(want in rec for rec in recs)
    return {"verified": hit, "expect": want,
            "found": [x for x in recs if x.startswith(TXT_PREFIX)] or recs[:5]}


def issue_key(conn, domain: str) -> str:
    key = "nk_" + secrets.token_urlsafe(24)
    now = int(time.time())
    conn.execute("""INSERT INTO claims(domain,token,verified,verified_at,created)
                    VALUES(?,?,1,?,?)
                    ON CONFLICT(domain) DO UPDATE SET verified=1, verified_at=excluded.verified_at""",
                 (domain.lower(), claim_token(domain), now, now))
    conn.execute("INSERT INTO api_keys(key,domain,created) VALUES(?,?,?)",
                 (key, domain.lower(), now))
    conn.commit()
    return key


def domain_for_key(conn, key: str | None) -> str | None:
    """Resolve a bearer key to its verified domain, or None."""
    if not key or not key.startswith("nk_"):
        return None
    r = conn.execute("SELECT domain FROM api_keys WHERE key=?", (key.strip(),)).fetchone()
    if not r:
        return None
    try:
        conn.execute("UPDATE api_keys SET last_used=? WHERE key=?",
                     (int(time.time()), key.strip()))
        conn.commit()
    except Exception:
        pass
    return r["domain"]


# ---------------------------------------------------------------------------
# Private entries
# ---------------------------------------------------------------------------

def add_private(conn, domain: str, entry: dict) -> str:
    """Record an internal service, visible only to its owner's key."""
    name = str(entry.get("displayName") or entry.get("name") or "").strip()
    if not name:
        return ""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower())[:60].strip("-") or "service"
    e = {
        "identifier": entry.get("identifier") or f"urn:air:{domain}:private:{slug}",
        "displayName": name,
        "type": entry.get("type") or "application/mcp-server-card+json",
        "url": entry.get("url"),
        "description": entry.get("description"),
        "tags": entry.get("tags"),
        "capabilities": entry.get("capabilities"),
        "representativeQueries": entry.get("representativeQueries"),
    }
    return store.add_private_entry(conn, domain, e)
