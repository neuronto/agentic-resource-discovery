#!/usr/bin/env python3
"""Can the index name the obvious vendor for an ordinary capability request?

  python -m scripts.battery [base_url] [--fed none|auto|both]

This is the question a benchmark over publisher-written `representativeQueries`
cannot ask. ARD-Bench (`app/bench.py`) measures whether a registry returns the
entry whose own publisher wrote the query, which is fair across registries and
is exactly why it is easy: the query and the target were written by the same
person. Here the query is written by *us*, in a user's words, and the expected
answer is the vendor any developer would name.

The failure this was built to catch is vocabulary, not ranking. Twilio publishes
151 operations and its specification calls the object a *Message*, so "send an
SMS" matched nothing: an index that holds the right document and cannot retrieve
it is indistinguishable, to the caller, from one that never had it.

Targets are matched on publisher host or display name, so a different Twilio
product answering "send an SMS" still counts. Anything that is genuinely
ambiguous is given several acceptable answers rather than a judgement call.

Exit status is 0 always: this reports, it does not gate. Numbers, not a verdict.
"""
import json
import sys
import urllib.request

# (query, [acceptable target substrings]). Every target below was confirmed
# present in the index before being asked for, so a miss is always a retrieval
# failure and never a coverage failure. That distinction is the entire value of
# the harness: it isolates the one thing we can fix by ranking.
CASES: list[tuple[str, list[str]]] = [
    ("charge a credit card",                 ["stripe.com", "squareup.com"]),
    ("send an sms",                          ["twilio.com", "clicksend", "messagebird",
                                              "vonage", "sinch", "plivo"]),
    ("send a transactional email",           ["sendgrid.com", "mailgun", "postmark",
                                              "resend", "mailchimp", "brevo", "sendinblue"]),
    ("post a message to a slack channel",    ["slack.com"]),
    ("create a calendar event",              ["google", "microsoft", "zoom.us",
                                              "calendly", "cronofy"]),
    ("get someone to sign a document",       ["docusign", "adobe", "hellosign", "dropbox"]),
    ("read and write rows in a spreadsheet", ["google", "microsoft", "airtable", "smartsheet"]),
    ("look up a company in a business register", ["companieshouse", "opencorporates",
                                                  "kompany", "brreg", "sec.gov"]),
    ("issue an invoice and record a payment", ["xero.com", "quickbooks", "intuit",
                                               "freshbooks", "invoiced", "stripe.com"]),
    ("transcribe a recording to text",       ["deepgram", "assemblyai", "openai.com",
                                              "speechmatics", "rev.ai", "elevenlabs",
                                              "google", "microsoft"]),
    ("track a parcel",                       ["shippo", "easypost", "aftership", "dhl",
                                              "ups", "fedex", "postnord"]),
    ("check whether an email address is valid", ["zerobounce", "neverbounce", "kickbox",
                                                 "abstractapi", "mailboxlayer", "hunter"]),
    ("convert one currency to another",      ["fixer", "currencylayer", "exchangerate",
                                              "openexchangerates", "apilayer", "xe.com"]),
    ("create a customer support ticket",     ["zendesk", "freshdesk", "intercom",
                                              "helpscout", "atlassian", "jira"]),
    ("find a place by address and get its coordinates", ["google", "mapbox", "here",
                                                         "tomtom", "opencagedata",
                                                         "positionstack", "geoapify"]),
]


def search(base: str, q: str, fed: str, limit: int = 10) -> list[dict]:
    req = urllib.request.Request(
        base.rstrip("/") + "/search",
        data=json.dumps({"query": {"text": q}, "limit": limit,
                         "federation": fed}).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("results") or []


def hit_rank(results: list[dict], targets: list[str]) -> int | None:
    """1-based rank of the first acceptable answer, or None."""
    for i, r in enumerate(results, 1):
        hay = " ".join(str(r.get(k) or "") for k in
                       ("identifier", "displayName", "url", "publisher")).lower()
        if any(t in hay for t in targets):
            return i
    return None


def run(base: str, fed: str) -> dict:
    hits5 = hits10 = 0
    rr = 0.0
    rows = []
    for q, targets in CASES:
        try:
            res = search(base, q, fed)
        except Exception as e:              # a battery that dies on one query is useless
            rows.append((q, None, f"error: {type(e).__name__}"))
            continue
        rank = hit_rank(res, targets)
        top = (res[0].get("displayName") or res[0].get("identifier")) if res else "-"
        rows.append((q, rank, top))
        if rank:
            rr += 1.0 / rank
            hits10 += 1
            if rank <= 5:
                hits5 += 1
    n = len(CASES)
    return {"federation": fed, "n": n, "hit@5": hits5, "hit@10": hits10,
            "mrr": round(rr / n, 4), "rows": rows}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = args[0] if args else "http://localhost:8700"
    mode = "both"
    for a in sys.argv[1:]:
        if a.startswith("--fed"):
            mode = a.split("=", 1)[1] if "=" in a else "both"
    feds = ["none", "auto"] if mode == "both" else [mode]

    for fed in feds:
        out = run(base, fed)
        print(f"\n=== federation={fed}  hit@5 {out['hit@5']}/{out['n']}  "
              f"hit@10 {out['hit@10']}/{out['n']}  MRR {out['mrr']}")
        for q, rank, top in out["rows"]:
            mark = f"#{rank}" if rank else "MISS"
            print(f"  {mark:>5}  {q:<48.48s} top={top:<34.34s}")


if __name__ == "__main__":
    main()
