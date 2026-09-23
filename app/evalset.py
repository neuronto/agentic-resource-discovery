"""Frozen retrieval eval sets: load one, and refuse one that has been edited.

A set is written once by `scripts/build_eval.py` and never regenerated in place; its
`sha256` covers the tasks, so a hand edit is detected rather than silently graded.
"""
from __future__ import annotations

import hashlib
import json


def digest(tasks: list[dict]) -> str:
    blob = json.dumps(tasks, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if digest(d["tasks"]) != d["sha256"]:
        raise ValueError(f"{path}: tasks do not match the recorded sha256; "
                         "a frozen set is never edited, publish a new version")
    return d
