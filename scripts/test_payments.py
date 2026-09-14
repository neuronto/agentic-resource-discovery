#!/usr/bin/env python3
"""Unit tests for payment terms: declared in manifests, and read live from a 402.

Every fixture is the shape a real publisher or endpoint sends: the specification
repository's own payment example, a catalogue entry with a `<prefix>:catalog` term, an
x402 v2 challenge in a header, an x402 v1 challenge in a body, an MPP challenge,
a 402 with no terms, and a hosting provider's usage limit that only looks like a
payment demand. Addresses and ids are placeholders.

    python3 scripts/test_payments.py
"""
from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import payments as P                                    # noqa: E402

_passed: list[str] = []
_failed: list[tuple[str, str]] = []

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x0000000000000000000000000000000000000001"


def check(name, fn):
    try:
        fn()
        _passed.append(name)
        print(f"  PASS  {name}")
    except AssertionError as e:
        _failed.append((name, str(e)))
        print(f"  FAIL  {name}\n          {e}")
    except Exception as e:
        _failed.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ERROR {name}\n          {type(e).__name__}: {e}")


# ------------------------------------------------------------------ declared

SPEC_X402 = {
    "@context": {"pay": "https://agenticresourcediscovery.org/ns/payment#", "schema": "https://schema.org/"},
    "identifier": "urn:air:example.com:agent:legal-analyzer",
    "type": "application/a2a-agent-card+json",
    "pay:protocol": ["x402"], "pay:price": "0.05", "pay:currency": "USDC", "pay:network": "eip155:8453",
    "pay:accepts": [{"scheme": "exact", "network": "eip155:8453", "asset": BASE_USDC, "payTo": PAY_TO,
                     "maxAmountRequired": "50000", "extra": {"name": "USD Coin", "version": "2"}}],
    "schema:offers": {"@type": "schema:Offer", "schema:price": "0.05", "schema:priceCurrency": "USDC",
                      "schema:category": "x402"},
}
SPEC_CARD = {
    "@context": {"pay": "https://agenticresourcediscovery.org/ns/payment#"},
    "identifier": "urn:air:example.com:agent:financial-summarizer",
    "pay:protocol": ["x402"], "pay:price": "0.02", "pay:currency": "USD", "pay:network": "stripe",
}
SPEC_FREE = {"identifier": "urn:air:example.com:agent:weather", "type": "application/json"}
CATALOG = {
    "@context": {"cat": "https://catalog.example/ns#"},
    "identifier": "urn:air:catalog.example:service:oracle",
    "type": "application/json",
    "cat:catalog": {"protocol": "x402", "priceLabel": "$0.01", "network": "Base, Solana",
                    "endpoints": [{"path": "/status", "method": "GET", "priceLabel": "$0.01"},
                                  {"path": "/bulk", "method": "POST", "priceLabel": "$0.25"}]},
}
CATALOG_RANGE = {
    "@context": {"cat": "https://catalog.example/ns#"},
    "identifier": "urn:air:catalog.example:service:mail",
    "cat:catalog": {"protocol": "mpp", "priceLabel": "$0.01–$10", "network": "Tempo",
                    "endpoints": [{"path": "/inboxes", "priceLabel": ""}]},
}


def t_spec_example_declares_x402_on_base():
    d = P.from_entry(SPEC_X402)
    assert d, "no terms read from the specification's own example"
    assert d["protocols"] == ["x402"], d
    assert abs(d["price"] - 0.05) < 1e-9, d
    assert d["networks"] == ["base"], d
    assert d["currency"] == "USDC", d


def t_card_rail_keeps_its_network():
    d = P.from_entry(SPEC_CARD)
    assert d["protocols"] == ["x402"] and d["networks"] == ["stripe"], d
    assert abs(d["price"] - 0.02) < 1e-9, d


def t_an_entry_without_terms_declares_nothing():
    assert P.from_entry(SPEC_FREE) is None


def t_catalog_entry_price_is_the_cheapest_call():
    d = P.from_entry(CATALOG)
    assert d["protocols"] == ["x402"], d
    assert d["networks"] == ["base", "solana"], d
    assert abs(d["price"] - 0.01) < 1e-9, d


def t_a_price_range_reads_as_its_lowest_figure():
    d = P.from_entry(CATALOG_RANGE)
    assert d["protocols"] == ["mpp"] and d["networks"] == ["tempo"], d
    assert abs(d["price"] - 0.01) < 1e-9, d


def t_a_bare_number_is_not_a_dollar_price():
    d = P.from_entry({"pay:protocol": "x402", "pay:price": "7"})
    assert d and d["price"] is None, f"a price with no unit was read as dollars: {d}"


def t_a_euro_price_is_never_compared_with_dollars():
    d = P.from_entry({"pay:protocol": "card", "pay:price": "0.50", "pay:currency": "EUR"})
    assert d["price"] is None and d["currency"] == "EUR", d


def t_the_pay_prefix_is_read_by_name():
    e = {"@context": {"pay": "https://example.org/something-else#"}, "pay:price": "$1",
         "pay:protocol": "x402"}
    # `pay` is accepted by name whatever it is bound to, because the key is what
    # publishers copy from the specification's example, often without the context.
    assert P.from_entry(e) is not None
    # And a publisher's own prefix bound to the payment namespace reads the same.
    own = {"@context": {"p": "https://agenticresourcediscovery.org/ns/payment#"},
           "p:protocol": "mpp", "p:price": "$0.02"}
    d = P.from_entry(own)
    assert d and d["protocols"] == ["mpp"] and abs(d["price"] - 0.02) < 1e-9, d


def t_an_unbound_catalog_prefix_is_ignored():
    e = {"identifier": "urn:air:x.example:service:y",
         "cat:catalog": {"protocol": "x402", "priceLabel": "$0.01"}}
    assert P.from_entry(e) is None, "a catalogue term was read without its prefix being bound"


def t_malformed_input_never_raises():
    for bad in (None, 3, "x", [], {"pay:price": {"nested": True}}, {"@context": {"cat": "https://catalog.example/ns#"}, "cat:catalog": "nope"},
                {"schema:offers": [1, None, "x"]}, {"pay:accepts": "garbage"}):
        P.from_entry(bad)


# ------------------------------------------------------------------ live

def _b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def t_x402_v2_header_is_read():
    doc = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "5000",
                                          "asset": BASE_USDC, "payTo": PAY_TO}]}
    kind, t = P.classify(402, {"PAYMENT-REQUIRED": _b64(doc)}, "{}")
    assert kind == "payment" and t["protocols"] == ["x402"], (kind, t)
    assert abs(t["price"] - 0.005) < 1e-9 and t["networks"] == ["base"], t


def t_x402_v1_body_is_read():
    body = json.dumps({"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base-sepolia",
                                                      "maxAmountRequired": "1000", "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"}]})
    kind, t = P.classify(402, {"content-type": "application/json"}, body)
    assert kind == "payment" and t["protocols"] == ["x402"], (kind, t)
    assert abs(t["price"] - 0.001) < 1e-9 and t["networks"] == ["base-sepolia"], t


def t_mpp_challenge_is_read():
    req = base64.urlsafe_b64encode(json.dumps({"amount": "10000", "currency": BASE_USDC, "recipient": PAY_TO,
                                               "network": "eip155:8453", "methodDetails": {"chainId": 8453}}).encode()).decode().rstrip("=")
    www = f'Payment id="abc", realm="api.example", method="evm", intent="charge", request="{req}"'
    kind, t = P.classify(402, {"WWW-Authenticate": www}, "")
    assert kind == "payment" and t["protocols"] == ["mpp"], (kind, t)
    assert abs(t["price"] - 0.01) < 1e-9 and t["networks"] == ["base"], t


def t_x402_and_mpp_offered_together():
    doc = {"x402Version": 2, "accepts": [{"network": "eip155:8453", "amount": "5000", "asset": BASE_USDC}]}
    kind, t = P.classify(402, [("payment-required", _b64(doc)),
                               ("www-authenticate", 'Payment method="tempo", intent="charge"')], "")
    assert t["protocols"] == ["x402", "mpp"], t
    assert "tempo" in t["networks"] and "base" in t["networks"], t


def t_a_402_without_terms_is_still_a_payment_requirement():
    kind, t = P.classify(402, {}, '{"error": {"code": "402", "message": "Payment required"}}')
    assert kind == "payment" and t["protocols"] == ["unspecified"] and t["price"] is None, (kind, t)


def t_a_hosting_usage_limit_is_not_an_offer():
    body = '{"jsonrpc":"2.0","error":{"code":-32002,"message":"Request limit reached for the current billing period."}}'
    kind, t = P.classify(402, {}, body)
    assert kind == "quota" and t is None, (kind, t)


def t_not_a_402_is_not_read():
    assert P.classify(200, {}, "{}") == (None, None)
    assert P.classify(401, {"www-authenticate": 'Payment method="evm"'}, "") == (None, None)


def t_garbage_headers_never_raise():
    for h in (None, {"payment-required": "%%%"}, {"www-authenticate": "Payment request=\"!!\""}, 5):
        P.classify(402, h, b"\xff\xfe")


# ------------------------------------------------------------------ merge, output, filters

def t_live_price_wins_and_protocols_union():
    d = P.from_entry(CATALOG)
    live = {"protocols": ["x402", "mpp"], "price": 0.02, "currency": "USDC", "networks": ["base", "tempo"],
            "source": "live", "checked": 1789300000}
    m = P.merge(d, live)
    assert m["protocols"] == ["x402", "mpp"], m
    assert m["price"] == 0.02 and m["priceSource"] == "live" and m["verified"] == "live", m
    assert m["networks"] == ["base", "tempo", "solana"], m


def t_unspecified_live_keeps_the_declared_price():
    m = P.merge(P.from_entry(CATALOG), {"protocols": ["unspecified"], "price": None, "networks": [], "source": "live"})
    assert m["protocols"] == ["x402"] and m["price"] == 0.01 and m["priceSource"] == "declared", m
    assert m["verified"] == "live", "a live 402 was observed, so the requirement is verified"


def t_terms_use_the_specification_example_keys():
    t = P.to_terms(P.merge(P.from_entry(SPEC_X402), None))
    assert t == {"pay:protocol": ["x402"], "pay:price": "0.05", "pay:currency": "USDC", "pay:network": ["base"]}, t


def t_filters():
    paid = P.merge(P.from_entry(CATALOG), None)
    free = None
    assert P.passes(paid, {"pay:protocol": ["payable"]}) and not P.passes(free, {"pay:protocol": ["payable"]})
    assert P.passes(free, {"payment": "free"}) and not P.passes(paid, {"payment": "free"})
    assert P.passes(paid, {"pay:protocol": "x402"}) and not P.passes(paid, {"pay:protocol": "mpp"})
    assert P.passes(paid, {"maxPricePerCall": 0.01}) and not P.passes(paid, {"maxPricePerCall": 0.005})
    assert P.passes(paid, {"pay:price": {"lte": "0.02"}})
    assert not P.passes(P.merge({"protocols": ["x402"], "price": None, "networks": []}, None),
                        {"maxPricePerCall": 1}), "an unknown price passed a price cap"
    assert P.passes(paid, {"network": "eip155:8453"}) and not P.passes(paid, {"pay:network": ["polygon"]})
    assert not P.passes(paid, {"pay:verified": "live"})
    assert P.passes(paid, {"type": ["application/json"]}), "a filter with no payment keys must pass"
    assert P.passes(paid, {"pay:protocol": ["any"]})


def t_columns_are_filterable_strings():
    assert P.columns(P.merge(P.from_entry(CATALOG), None)) == ("x402", 0.01, "base,solana")
    assert P.columns(None) == (None, None, None)


def main():
    print("\n  payment terms")
    for name, fn in list(globals().items()):
        if name.startswith("t_") and callable(fn):
            check(name[2:].replace("_", " "), fn)
    print(f"\n  {len(_passed)} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
