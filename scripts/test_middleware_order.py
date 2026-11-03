#!/usr/bin/env python3
"""The analytics recorder sees every response, refusals included.

Until 2026-11-04 the analytics middleware was installed early, so the commercial
meters, installed later, sat OUTSIDE it: a 402 they answered never reached the
recorder. This loads the real app with a stand-in sink that installs a recording
middleware the same way the real one does (events.install -> sink.install(app)),
then requires:
  * the recorder is the outermost user middleware, and the hang-up layer next;
  * a response produced by a middleware registered after the engine (as the
    commercial package's are) is recorded, status and all;
  * a client hang-up is recorded as a 499 hit.

    python3 scripts/test_middleware_order.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types

DB = tempfile.mktemp(suffix=".db")
os.environ["NEURONTO_DB"] = DB

# A stand-in sink module, importable by name, installing a recording ASGI middleware.
HITS: list[tuple[str, int]] = []


class _Recorder:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        code = {"v": 0}

        async def wrapped(m):
            if m["type"] == "http.response.start":
                code["v"] = m["status"]
            elif m["type"] == "http.response.body" and not m.get("more_body"):
                HITS.append((scope["path"], code["v"]))
            await send(m)
        await self.app(scope, receive, wrapped)


sink = types.ModuleType("neuronto_test_sink")
sink.install = lambda app: app.add_middleware(_Recorder)
sink.receive = lambda name, fields: None
sys.modules["neuronto_test_sink"] = sink
os.environ["NEURONTO_EVENT_SINK"] = "neuronto_test_sink"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import Request                                               # noqa: E402
from fastapi.responses import JSONResponse                                # noqa: E402
from app import main                                                      # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


async def call(method, path, messages):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("203.0.113.9", 5000), "server": ("neuronto.com", 443),
             "headers": [(b"host", b"neuronto.com"), (b"content-type", b"application/json"),
                         (b"content-length", b"64"), (b"user-agent", b"neuronto-e2e test")]}

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(m):
        pass
    await main.app(scope, receive, send)


def main_() -> int:
    names = [m.cls.__name__ for m in main.app.user_middleware]
    check("the analytics recorder is the outermost middleware", names[0] == "_Recorder", names[:4])
    check("the hang-up layer is next", names[1] == "_ClientGoneLayer", names[:4])

    # A meter registered after the engine, as commercial/ registers its own. Starlette
    # builds the stack on first use, so it must be added before the first request.
    main.app.user_middleware.insert(2, main.app.user_middleware[0].__class__(_Paywall))
    asyncio.run(call("POST", "/paid-route-for-test", [{"type": "http.request", "body": b"{}"}]))
    check("a 402 answered by an outer meter is recorded", ("/paid-route-for-test", 402) in HITS, HITS)

    asyncio.run(call("POST", "/mcp", [{"type": "http.disconnect"}]))
    check("a client hang-up is recorded as a 499", ("/mcp", 499) in HITS, HITS)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


class _Paywall:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/paid-route-for-test":
            r = JSONResponse({"error": "payment required"}, status_code=402)
            return await r(scope, receive, send)
        return await self.app(scope, receive, send)


if __name__ == "__main__":
    try:
        raise SystemExit(main_())
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(DB + suf)
            except OSError:
                pass
