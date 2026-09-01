#!/usr/bin/env python3
"""Unit tests for the rate limiter, against a temporary database.

Refusal has to be tested by actually exhausting a window. Doing that against
production would spend the operator's real allowance on every test run, and a
test nobody can afford to run is a test nobody runs. So this drives
`limits.check` directly with a fake request and its own throwaway storage.

    python3 scripts/test_limits.py
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["NEURONTO_LIMITS_DB"] = tempfile.mktemp(suffix=".db")
os.environ.setdefault("NEURONTO_DB", tempfile.mktemp(suffix=".db"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import limits                                          # noqa: E402

_passed: list[str] = []
_failed: list[tuple[str, str]] = []


class FakeRequest:
    """Only the two things the limiter reads."""

    def __init__(self, ip="1.2.3.4", key=None):
        self.headers = {"cf-connecting-ip": ip}
        if key:
            self.headers["authorization"] = f"Bearer {key}"
        self.client = None


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


def t_refuses_exactly_at_the_limit():
    limit = limits.RULES["audit"][0]
    r = FakeRequest(ip="10.0.0.1")
    allowed = sum(1 for _ in range(limit + 20) if limits.check("audit", r)[0])
    assert allowed == limit, f"allowed {allowed}, limit is {limit}"


def t_refusal_carries_a_retry_and_a_way_out():
    r = FakeRequest(ip="10.0.0.2")
    for _ in range(limits.RULES["submit"][0] + 1):
        ok, retry, hdrs = limits.check("submit", r)
    assert not ok, "never refused"
    assert retry > 0, "no retry-after"
    body = limits.too_many("submit", retry, hdrs, verified=False)
    assert body["error"] == "rate_limited"
    assert "raiseTheLimit" in body, "refuses without telling the caller how to proceed"
    assert str(retry) in body["detail"]


def t_every_rule_explains_itself_truthfully():
    """A refusal reason that is wrong is worse than a generic one.

    `/claim` was refusing people with 'because it makes requests to other
    people's servers', which it does not do.
    """
    for rule in limits.RULES:
        assert rule in limits.REASONS, f"{rule} has no reason, callers get a vague refusal"
        assert len(limits.REASONS[rule]) > 20, f"{rule}: reason too thin to be useful"
    outbound_free = {"claim", "private_write"}
    for rule in outbound_free:
        assert "other people's servers" not in limits.REASONS[rule], \
            f"{rule} claims an outbound call it does not make"


def t_callers_are_isolated_from_each_other():
    a, b = FakeRequest(ip="10.0.1.1"), FakeRequest(ip="10.0.1.2")
    for _ in range(limits.RULES["claim"][0] + 1):
        limits.check("claim", a)
    assert not limits.check("claim", a)[0], "first caller not limited"
    assert limits.check("claim", b)[0], "one caller's usage limited a different caller"


def t_a_verified_key_gets_its_own_larger_allowance():
    ip = FakeRequest(ip="10.0.2.1")
    keyed = FakeRequest(ip="10.0.2.1", key="nk_averifiedlookingkey123456")
    anon_limit = limits.RULES["audit"][0]
    for _ in range(anon_limit + 1):
        limits.check("audit", ip)
    assert not limits.check("audit", ip)[0], "anonymous caller not limited"
    ok, _, hdrs = limits.check("audit", keyed)
    assert ok, "a key sharing the address inherited the anonymous refusal"
    assert int(hdrs["x-ratelimit-limit"]) == limits.RULES["audit"][2], \
        "verified caller did not get the verified allowance"


def t_verified_allowance_is_never_smaller():
    for rule, (limit, _w, vlimit) in limits.RULES.items():
        assert vlimit >= limit, f"{rule}: proving ownership lowers your allowance"


def t_fails_open_when_storage_is_gone():
    """A broken limiter must degrade to no limiting, never to an outage."""
    saved = limits._conn
    try:
        limits._conn = None
        os.environ["NEURONTO_LIMITS_DB"] = "/proc/nonexistent/cannot/create.db"
        limits._DB_PATH = __import__("pathlib").Path(os.environ["NEURONTO_LIMITS_DB"])
        ok, retry, hdrs = limits.check("audit", FakeRequest(ip="10.0.3.1"))
        assert ok and retry == 0 and hdrs == {}, "did not fail open with no storage"
    finally:
        limits._conn = saved


def t_unknown_rule_is_not_silently_limited():
    ok, _, _ = limits.check("no_such_rule", FakeRequest())
    assert ok, "an unknown rule refused a request"


def main():
    print(f"\n  limits unit tests (storage: {limits._DB_PATH})\n  " + "-" * 60)
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            check(name[2:].replace("_", " "), fn)
    print("  " + "-" * 60)
    print(f"  {len(_passed)} passed, {len(_failed)} failed")
    for n, e in _failed:
        print(f"   - {n}: {e}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
