#!/usr/bin/env python3
"""A client that hangs up is not a server error.

Drives the real app at the ASGI level with a request whose body never arrives: the
client sends `http.disconnect` instead. Requires a 499, a `client_disconnect` event,
and no `error` event. Then a genuine exception must still be an `error` event and a
500, so the change cannot hide real faults. Throwaway database, no live analytics.

    python3 scripts/test_disconnect.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["NEURONTO_DB"] = DB
os.environ["NEURONTO_EVENT_SINK"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import events, main                                              # noqa: E402

FAILS: list[str] = []
GOT: list[str] = []
LAST: list[dict] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


async def call(method: str, path: str, messages: list[dict]) -> list[dict]:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "client": ("203.0.113.9", 5000),
             "server": ("neuronto.com", 443),
             "headers": [(b"host", b"neuronto.com"), (b"content-type", b"application/json"),
                         (b"content-length", b"64"), (b"user-agent", b"neuronto-e2e test")]}
    sent: list[dict] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    LAST.clear()
    try:
        await main.app(scope, receive, send)
    finally:
        LAST.extend(sent)
    return sent


def status_of(sent):
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


def main_() -> int:
    real = events.emit
    events.emit = lambda name, **f: GOT.append(name)
    try:
        sent = asyncio.run(call("POST", "/mcp", [{"type": "http.disconnect"}]))
        check("a hang-up before the body is answered 499", status_of(sent) == 499, status_of(sent))
        check("it is recorded as client_disconnect", "client_disconnect" in GOT, GOT)
        check("it is not recorded as an error", "error" not in GOT, GOT)

        GOT.clear()

        @main.app.get("/__boom_for_test")
        def _boom():
            raise RuntimeError("planted")
        # move the planted route ahead of the website catch-all
        main.app.router.routes.insert(0, main.app.router.routes.pop())
        sent: list[dict] = []
        try:
            sent = asyncio.run(call("GET", "/__boom_for_test", [{"type": "http.request", "body": b""}]))
        except RuntimeError:
            # Starlette answers 500 and then re-raises a real fault so the server logs it;
            # what was sent before the re-raise is in the list call() filled.
            pass
        sent = sent or LAST
        check("a real fault is still a 500", status_of(sent) == 500, status_of(sent))
        check("a real fault is still recorded as error", "error" in GOT, GOT)
    finally:
        events.emit = real
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_())
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(DB + suf)
            except OSError:
                pass
