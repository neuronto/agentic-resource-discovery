"""Payment terms: what calling a resource costs, and how it is paid.

Discovery answers "what can do this". An agent about to call a paid service also
needs "what will it cost, and can I pay it", and today it learns that only when
its call comes back `402 Payment Required`. The specification has no payment
fields yet (ard-spec#20 is open); the one worked example in the specification
repository carries them as JSON-LD extension terms under a `pay:` prefix, live
catalogues carry them in a `<prefix>:catalog` extension term with a price label
per endpoint, and some publishers use schema.org offers. All three are read here. So is the one source a publisher
cannot overstate: what the endpoint itself answered when it was called without
payment.

Two kinds of term, kept apart and labelled wherever they are shown:

  declared  what the publisher's manifest says
  live      what the endpoint's own 402 said: x402 terms in a PAYMENT-REQUIRED
            header (x402 v2) or in the response body (v1), or an MPP
            `WWW-Authenticate: Payment` challenge. A 402 without machine-readable
            terms is recorded too: payment is required, the price is not stated.

These are facts for filtering and display. They never change a score, and this
registry never pays, holds or routes a payment: the caller pays the provider.

Pure standard library, no database, so every rule here is unit-testable.
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Mapping

NS = "https://agenticresourcediscovery.org/ns/payment#"

# A per-call price is reported in dollars only when its unit is a dollar or a
# dollar stablecoin. Anything else keeps its currency and no dollar figure, so a
# price cap never compares euros with dollars.
_USD_LIKE = {"usd", "usdc", "usdt", "usd coin", "pathusd", "usdc.e", "pyusd", "usdg", "dai"}

# Stablecoin contracts with known decimals, so an atomic x402 or MPP amount can
# be read as dollars. EVM addresses lowercased; the Solana mint as published.
_ASSETS: dict[str, tuple[str, int]] = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6),   # Base
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e": ("USDC", 6),   # Base Sepolia
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC", 6),   # Polygon
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),   # Ethereum
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": ("USDC", 6),   # Arbitrum
    "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": ("USDC", 6),   # Avalanche
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85": ("USDC", 6),   # Optimism
    "epjfwdd5aufqssqem2qn1xzybapc8g4wegggkzwytdt1v": ("USDC", 6),  # Solana
}

# CAIP-2 identifiers to the names people filter by. Unknown ones pass through.
_CAIP = {
    "eip155:8453": "base", "eip155:84532": "base-sepolia", "eip155:137": "polygon",
    "eip155:1": "ethereum", "eip155:42161": "arbitrum", "eip155:43114": "avalanche",
    "eip155:10": "optimism", "eip155:4217": "tempo",
    "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp": "solana",
    "solana:etwtrabzayq6imfeykourru166vu2xqa1": "solana-devnet",
}
_NETWORK_ALIASES = {
    "base mainnet": "base", "base-mainnet": "base", "solana-mainnet": "solana",
    "mainnet-beta": "solana", "tempo mainnet": "tempo", "polygon-pos": "polygon",
    "matic": "polygon", "eth": "ethereum", "avax": "avalanche",
}
_PROTOCOL_ALIASES = {
    "x402": "x402", "mpp": "mpp", "machine payments protocol": "mpp", "l402": "l402",
    "card": "card", "stripe": "card", "braintree": "card", "fiat": "card",
    "credit card": "card",
}
KNOWN_PROTOCOLS = ("x402", "mpp", "card", "l402", "unspecified")

# Filter keys this module owns. The `pay:` ones follow the specification's
# worked example; the plain ones are what a hand-written client reaches for.
FILTER_KEYS = ("pay:protocol", "payment", "pay:price", "pay:maxPrice", "maxPricePerCall",
               "pay:network", "network", "pay:verified")

# A 402 that is a hosting provider's usage limit rather than an offer to sell.
_QUOTA = re.compile(r"(?i)request limit|rate limit|quota|billing period|usage limit|"
                    r"limit (has been )?reached|limit exceeded")
_NUM = re.compile(r"(\d+(?:\.\d+)?)")


# ---------------------------------------------------------------- normalising

def _strings(v: Any) -> list[str]:
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, (str, int, float)):
        return [str(v)]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if isinstance(x, (str, int, float)) and not isinstance(x, bool)]
    return []


def _first(v: Any) -> str | None:
    s = _strings(v)
    return s[0].strip() if s and s[0].strip() else None


def network(v: Any) -> list[str]:
    """Network names, normalised: `eip155:8453` and `Base` are both `base`."""
    out: list[str] = []
    for part in _strings(v):
        for p in re.split(r"[,/|]", part):
            p = p.strip().lower()
            if not p:
                continue
            p = _CAIP.get(p, _NETWORK_ALIASES.get(p, p))
            p = re.sub(r"\s+", "-", p)
            if len(p) <= 64 and p not in out:
                out.append(p)
    return out[:8]


def protocol(v: Any) -> list[str]:
    """Protocol names, normalised: `stripe` and `braintree` are card rails."""
    out: list[str] = []
    for part in _strings(v):
        for p in re.split(r"[,/|]", part):
            k = p.strip().lower()
            if not k:
                continue
            k = _PROTOCOL_ALIASES.get(k, k if re.fullmatch(r"[a-z0-9.+-]{2,24}", k) else "")
            if k and k not in out:
                out.append(k)
    return out[:6]


def _price(v: Any, currency: str | None = None) -> tuple[float | None, str | None]:
    """A per-call price in dollars from a number, a decimal string or a label.

    A label may be a range ("$0.01–$10"), in which case the lowest figure is the
    price of the cheapest call. A bare number with no currency is not assumed to
    be dollars: without a unit it is not a price anyone can compare.
    """
    cur = (currency or "").strip()
    if v is None or isinstance(v, bool):
        return None, cur or None
    if isinstance(v, (int, float)):
        amount = float(v)
    else:
        s = str(v).strip()
        if not s:
            return None, cur or None
        if s.lower() == "free":
            return 0.0, cur or "USD"
        if "$" in s and not cur:
            cur = "USD"
        m = _NUM.search(s)
        if not m:
            return None, cur or None
        amount = float(m.group(1))
    if not cur or amount < 0 or amount > 1_000_000:
        return None, cur or None
    if cur.lower() not in _USD_LIKE:
        return None, cur
    return amount, cur


def _atomic(amount: Any, asset: Any, asset_name: Any = None) -> tuple[float | None, str | None]:
    """An atomic amount of a known stablecoin, in dollars."""
    known = _ASSETS.get(str(asset or "").strip().lower())
    name = str(asset_name or "").lower()
    if known is None and ("usd coin" in name or name == "usdc"):
        known = ("USDC", 6)
    if known is None or amount is None:
        return None, known[0] if known else None
    try:
        return int(str(amount)) / (10 ** known[1]), known[0]
    except (TypeError, ValueError):
        return None, known[0]


def _from_accepts(accepts: Any) -> tuple[list[float], list[str], str | None]:
    """Prices, networks and currency from an x402 `accepts` array."""
    prices: list[float] = []
    nets: list[str] = []
    currency = None
    for a in accepts if isinstance(accepts, list) else [accepts]:
        if not isinstance(a, Mapping):
            continue
        nets += [n for n in network(a.get("network")) if n not in nets]
        extra = a.get("extra") if isinstance(a.get("extra"), Mapping) else {}
        usd, cur = _atomic(a.get("amount", a.get("maxAmountRequired")), a.get("asset"),
                           extra.get("name"))
        if usd is not None:
            prices.append(usd)
        currency = currency or cur
    return prices, nets, currency


def _b64json(value: Any) -> Any:
    """A JSON value sent base64 (standard or URL-safe, padding optional) or raw."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return json.loads(decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
        except Exception:
            continue
    try:
        return json.loads(raw)
    except Exception:
        return None


def _context(ctx: Any) -> dict[str, str]:
    """Prefix bindings from an entry's @context, which may be a list."""
    out: dict[str, str] = {}
    for c in ctx if isinstance(ctx, list) else [ctx]:
        if isinstance(c, Mapping):
            for k, v in c.items():
                if isinstance(k, str) and isinstance(v, str):
                    out[k] = v
    return out


def _record(protocols: list[str], prices: list[float], currency: str | None,
            networks: list[str], source: str) -> dict:
    protos = list(dict.fromkeys(protocols))
    if len(protos) > 1 and "unspecified" in protos:
        protos.remove("unspecified")
    price = min(prices) if prices else None
    return {"protocols": protos[:6], "price": price, "currency": currency,
            "networks": list(dict.fromkeys(networks))[:8], "source": source}


# ---------------------------------------------------------------- declared

def from_entry(e: Any) -> dict | None:
    """Payment terms a manifest entry declares, or None if it declares none."""
    if not isinstance(e, Mapping):
        return None
    ctx = _context(e.get("@context"))
    ns_root = NS.rstrip("#/")
    pay_pre = {"pay"} | {p for p, iri in ctx.items() if iri.rstrip("#/") == ns_root}
    # A catalogue vocabulary is recognised by shape, not by name: any prefix the
    # entry binds whose `catalog` object states a protocol or a price label, or
    # whose `payment` object lists plans or x402 `accepts`.
    cat_pre = set(ctx)
    schema_pre = {"schema"} | {p for p, iri in ctx.items() if "schema.org" in iri.lower()}

    protos: list[str] = []
    nets: list[str] = []
    prices: list[float] = []
    currency: str | None = None
    saw = False

    fields: dict[str, Any] = {}
    for k, v in e.items():
        if not isinstance(k, str):
            continue
        if k.startswith(NS):
            fields[k[len(NS):]] = v
            continue
        pre, sep, local = k.partition(":")
        if sep and pre in pay_pre:
            fields[local] = v
    if fields:
        saw = True
        protos += protocol(fields.get("protocol"))
        nets += network(fields.get("network"))
        cur = _first(fields.get("currency"))
        p, c = _price(fields.get("price"), cur)
        if p is not None:
            prices.append(p)
        currency = c or cur
        if fields.get("accepts"):
            ap, an, ac = _from_accepts(fields.get("accepts"))
            prices += ap
            nets += an
            currency = currency or ac
            if "x402" not in protos:
                protos.append("x402")

    for pre in cat_pre:
        cat = e.get(f"{pre}:catalog")
        if isinstance(cat, Mapping) and (cat.get("protocol") or cat.get("priceLabel")):
            saw = True
            protos += protocol(cat.get("protocol"))
            nets += network(cat.get("network"))
            labels = [cat.get("priceLabel")] + [
                ep.get("priceLabel") for ep in (cat.get("endpoints") or []) if isinstance(ep, Mapping)]
            for label in labels:
                p, c = _price(label)
                if p is not None:
                    prices.append(p)
                    currency = currency or c
        pay = e.get(f"{pre}:payment")
        if isinstance(pay, Mapping) and (pay.get("plans") or pay.get("accepts")):
            saw = True
            plans = pay.get("plans") if isinstance(pay.get("plans"), list) else [pay]
            for plan in plans:
                if not isinstance(plan, Mapping):
                    continue
                if plan.get("accepts"):
                    ap, an, ac = _from_accepts(plan.get("accepts"))
                    prices += ap
                    nets += an
                    currency = currency or ac
                    protos.append("x402")
                protos += protocol(plan.get("protocol") or plan.get("paymentProtocol"))
                p, c = _price(plan.get("price") or plan.get("priceLabel"), _first(plan.get("currency")))
                if p is not None:
                    prices.append(p)
                    currency = currency or c

    for pre in schema_pre:
        offers = e.get(f"{pre}:offers")
        for o in offers if isinstance(offers, list) else [offers]:
            if not isinstance(o, Mapping):
                continue
            saw = True
            get = lambda n, o=o, pre=pre: o.get(f"{pre}:{n}", o.get(n))
            p, c = _price(get("price"), _first(get("priceCurrency")))
            if p is not None:
                prices.append(p)
                currency = currency or c
            protos += [x for x in protocol(get("category")) if x in KNOWN_PROTOCOLS]

    if not saw or not (protos or prices or nets):
        return None
    if not protos:
        if prices and max(prices) == 0:
            # Declared free, and nothing else: no payment requirement.
            return _record([], prices, currency, nets, "declared")
        protos = ["unspecified"]
    return _record(protos, prices, currency, nets, "declared")


# ---------------------------------------------------------------- live

def classify(status: int | None, headers: Any, body: Any) -> tuple[str | None, dict | None]:
    """Read an unpaid response. Returns (kind, terms).

    kind is "payment" for a 402 that asks to be paid, with whatever terms it
    states; "quota" for a 402 that is a hosting provider's usage limit rather
    than an offer; None for anything that is not a 402.
    """
    if status != 402:
        return None, None
    h: dict[str, str] = {}
    try:
        if headers is None:
            items = []
        elif hasattr(headers, "multi_items"):          # httpx.Headers, repeats kept apart
            items = headers.multi_items()
        elif hasattr(headers, "items"):
            items = headers.items()
        else:                                          # a list of (name, value) pairs
            items = list(headers)
        for k, v in items:
            name = str(k).lower()
            h[name] = f"{h[name]}, {v}" if name in h else str(v)
    except Exception:
        h = {}
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    text = text[:65536]

    protos: list[str] = []
    nets: list[str] = []
    prices: list[float] = []
    currency: str | None = None

    doc = _b64json(h.get("payment-required"))
    if not (isinstance(doc, Mapping) and isinstance(doc.get("accepts"), list)):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        doc = parsed if isinstance(parsed, Mapping) and isinstance(parsed.get("accepts"), list) else None
    if doc is not None:
        protos.append("x402")
        ap, an, ac = _from_accepts(doc.get("accepts"))
        prices += ap
        nets += an
        currency = currency or ac
    elif (h.get("payment-required") or "").strip().lower() == "x402":
        protos.append("x402")

    www = h.get("www-authenticate") or ""
    for m in re.finditer(r'(?i)\bpayment\s+((?:[a-z0-9_-]+="[^"]*"\s*,?\s*)+)', www):
        params = {k.lower(): v for k, v in re.findall(r'([A-Za-z0-9_-]+)="([^"]*)"', m.group(1))}
        protos.append("mpp")
        method = (params.get("method") or "").lower()
        req = _b64json(params.get("request"))
        if isinstance(req, Mapping):
            nets += network(req.get("network"))
            details = req.get("methodDetails") if isinstance(req.get("methodDetails"), Mapping) else {}
            if details.get("chainId") and not req.get("network"):
                nets += network(f"eip155:{details['chainId']}")
            usd, cur = _atomic(req.get("amount"), req.get("currency"))
            if usd is not None:
                prices.append(usd)
            currency = currency or cur
        if method == "tempo":
            nets.append("tempo")

    if not protos:
        if _QUOTA.search(text):
            return "quota", None
        return "payment", _record(["unspecified"], [], None, [], "live")
    return "payment", _record(protos, prices, currency, nets, "live")


# ---------------------------------------------------------------- combined

def merge(declared: Any, live: Any) -> dict | None:
    """One view of both sources. Live wins on price; nothing is invented."""
    d = declared if isinstance(declared, Mapping) else None
    l = live if isinstance(live, Mapping) else None
    if not d and not l:
        return None
    protos: list[str] = []
    nets: list[str] = []
    for src in (l, d):
        for p in (src or {}).get("protocols") or []:
            if p not in protos:
                protos.append(p)
        for n in (src or {}).get("networks") or []:
            if n not in nets:
                nets.append(n)
    if len(protos) > 1 and "unspecified" in protos:
        protos.remove("unspecified")
    if l and l.get("price") is not None:
        price, currency, psrc = l["price"], l.get("currency"), "live"
    elif d and d.get("price") is not None:
        price, currency, psrc = d["price"], d.get("currency"), "declared"
    else:
        price, currency, psrc = None, (l or d or {}).get("currency"), None
    return {"protocols": protos, "price": price, "currency": currency, "networks": nets,
            "verified": "live" if l else "declared", "priceSource": psrc,
            "checked": (l or {}).get("checked")}


def columns(m: Any) -> tuple[str | None, float | None, str | None]:
    """The three filterable columns: protocols, dollar price, networks."""
    if not isinstance(m, Mapping):
        return None, None, None
    return (",".join(m.get("protocols") or []) or None, m.get("price"),
            ",".join(m.get("networks") or []) or None)


def _fmt_price(p: float) -> str:
    return f"{p:.6f}".rstrip("0").rstrip(".") or "0"


def to_terms(m: Any) -> dict:
    """The entry's `pay:` extension terms, in the specification example's shape."""
    if not isinstance(m, Mapping):
        return {}
    out: dict[str, Any] = {}
    if m.get("protocols"):
        out["pay:protocol"] = list(m["protocols"])
    if m.get("price") is not None:
        out["pay:price"] = _fmt_price(float(m["price"]))
        out["pay:currency"] = m.get("currency") or "USD"
    if m.get("networks"):
        out["pay:network"] = list(m["networks"])
    return out


def evidence(m: Any) -> dict | None:
    """Where the terms came from. Separate from the terms, like `verification`."""
    if not isinstance(m, Mapping):
        return None
    out: dict[str, Any] = {"source": m.get("verified") or "declared"}
    if m.get("priceSource"):
        out["priceSource"] = m["priceSource"]
    if m.get("checked"):
        out["checkedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(m["checked"])))
    return out


def summary(m: Any) -> dict | None:
    """Plain-keyed terms for MCP and A2A answers, where prefixed keys read badly."""
    if not isinstance(m, Mapping) or not (m.get("protocols") or m.get("price") is not None):
        return None
    out: dict[str, Any] = {"protocols": list(m.get("protocols") or [])}
    if m.get("price") is not None:
        out["pricePerCallUsd"] = float(m["price"])
    if m.get("networks"):
        out["networks"] = list(m["networks"])
    out["source"] = m.get("verified") or "declared"
    return out


def summary_from_entry(e: Mapping) -> dict | None:
    """The same summary, rebuilt from an entry's `pay:` terms and evidence."""
    if not isinstance(e, Mapping) or not any(k in e for k in ("pay:protocol", "pay:price")):
        return None
    out: dict[str, Any] = {"protocols": list(e.get("pay:protocol") or [])}
    try:
        if e.get("pay:price") is not None:
            out["pricePerCallUsd"] = float(e["pay:price"])
    except (TypeError, ValueError):
        pass
    if e.get("pay:network"):
        out["networks"] = list(e["pay:network"])
    out["source"] = (e.get("paymentEvidence") or {}).get("source") or "declared"
    return out


# ---------------------------------------------------------------- filtering

def filter_active(flt: Any) -> bool:
    return isinstance(flt, Mapping) and any(k in flt for k in FILTER_KEYS)


def _cap(v: Any) -> float | None:
    if isinstance(v, Mapping):
        for k in ("lte", "max", "$lte", "le"):
            if k in v:
                return _cap(v[k])
        return None
    if isinstance(v, list):
        return _cap(v[0]) if v else None
    if isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip().lstrip("$"))
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def passes(m: Any, flt: Any) -> bool:
    """Whether terms satisfy the payment keys of a filter.

    `payable` means some payment requirement is known; `free` means none is
    known, which is not a promise that nothing will ever be charged. A price cap
    excludes entries whose price is not known, because an unknown price is not
    one below the cap.
    """
    if not filter_active(flt):
        return True
    m = m if isinstance(m, Mapping) else {}
    protos = list(m.get("protocols") or [])
    for key in ("pay:protocol", "payment"):
        if key not in flt:
            continue
        raw = flt[key] if isinstance(flt[key], list) else [flt[key]]
        want = [str(x).strip().lower() for x in raw if str(x).strip()]
        if not want or "any" in want:
            continue
        ok = False
        for w in want:
            if w in ("payable", "paid"):
                ok = ok or bool(protos)
            elif w == "free":
                ok = ok or not protos
            else:
                ok = ok or any(p in protos for p in (protocol(w) or [w]))
        if not ok:
            return False
    for key in ("maxPricePerCall", "pay:maxPrice", "pay:price"):
        if key in flt:
            cap = _cap(flt[key])
            if cap is not None:
                price = m.get("price")
                if price is None or float(price) > cap + 1e-12:
                    return False
            break
    for key in ("pay:network", "network"):
        if key in flt:
            raw = flt[key] if isinstance(flt[key], list) else [flt[key]]
            want = network(raw)
            if want and not set(want) & set(m.get("networks") or []):
                return False
    if "pay:verified" in flt:
        raw = flt["pay:verified"] if isinstance(flt["pay:verified"], list) else [flt["pay:verified"]]
        if "live" in {str(x).lower() for x in raw} and m.get("verified") != "live":
            return False
    return True


def row_view(row: Any) -> dict | None:
    """The stored combined terms of a database row, if it has any."""
    try:
        raw = row["pay_terms"]
    except (IndexError, KeyError, TypeError):
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
