"""A small event hook.

The application announces what happened at a few points that matter: a search
ran, a resource was submitted, a manifest was generated, a domain was claimed.
Anything that wants to listen registers a sink by module path in
`NEURONTO_EVENT_SINK`. With nothing configured every call here is a no-op that
costs a dictionary lookup, and the application neither knows nor cares whether
a listener exists.

Keeping the hook this thin is the point. The application's job is discovery;
what an operator does with the fact that a search happened is the operator's
business and lives with the operator.
"""
from __future__ import annotations

import importlib
import os
import time
from typing import Any, Callable

_sink: Callable[[str, dict], None] | None = None
_installed = False


def _load() -> None:
    global _sink, _installed
    if _installed:
        return
    _installed = True
    path = os.getenv("NEURONTO_EVENT_SINK", "").strip()
    if not path:
        return
    try:
        mod = importlib.import_module(path)
        _sink = getattr(mod, "receive", None)
    except Exception:
        _sink = None


def emit(name: str, **fields: Any) -> None:
    """Announce an event. Never raises, never blocks the caller."""
    if not _installed:
        _load()
    if _sink is None:
        return
    try:
        fields.setdefault("ts", int(time.time()))
        _sink(name, fields)
    except Exception:
        pass


def install(app) -> None:
    """Give the sink a chance to attach middleware, if it has any."""
    _load()
    path = os.getenv("NEURONTO_EVENT_SINK", "").strip()
    if not path:
        return
    try:
        mod = importlib.import_module(path)
        hook = getattr(mod, "install", None)
        if hook:
            hook(app)
    except Exception:
        pass
