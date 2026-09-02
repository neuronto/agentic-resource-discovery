"""Tool-description integrity: what a tool's own text tells the model to do.

An MCP tool description is not documentation. It is text that goes into the
model's context and is read as instruction, which makes it the one part of a
third-party server that acts on the agent before the agent ever calls anything.
A description that says "do not tell the user" is doing something no README can.

So this scans the text we already hold, 95,994 tool descriptions read from the
servers' own `tools/list`, and counts what is there.

**The result is the opposite of the pitch that asked for it.** The corpus is
close to clean: about 0.02% of tools carry model-directed instructions. There is
no epidemic to score, and that finding is worth more than a scanner would be.
Which is why this module reports *counts and excerpts for a human to read*, and
does not compute a safety score, issue a "secure" badge, or feed ranking. See
`badge.py`: verified means observed, never endorsed, and that rule holds here.

**Every detector here was corrected against real output before shipping.** The
first draft flagged 33 tools for hidden Unicode; all 33 were U+200C, the zero
width non-joiner, which is *required orthography* in Persian (بخشنامه‌ها) and
was flagging an Iranian legal-services publisher as an attacker. It flagged
`detect_prompt_injection`, `scan_injection` and `check_endpoint`, three security
vendors, because their descriptions name the attack they defend against. It
flagged `create_persona` for saying "system prompt", which is the field it
edits.

That is the shape of the whole problem: a naive scanner mostly reports
legitimate publishers, and shipping one would have defamed four of them. The
detectors below are narrowed accordingly, and each carries what it deliberately
does not match.

Nothing here is a verdict. A flag means *a human should read this line*.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

# Invisible characters that have no business in a tool description, kept
# strictly apart from the ones that do. ZWNJ (U+200C) and ZWJ (U+200D) are
# excluded on purpose: they are load-bearing in Persian, Arabic and Indic
# scripts, and matching them means telling publishers who write in those
# languages that their own orthography is an attack.
#
# What is left is genuinely inert to a human reader and visible to a model:
# the zero-width space, the invisible maths operators, a byte-order mark that
# is not at the start, the bidi overrides behind Trojan Source (USENIX 2021),
# and the Unicode tags block, which is the actual steganographic channel used
# to smuggle instructions past review.
_INVISIBLE = re.compile(
    "[​⁠-⁤‪-‮⁦-⁩\U000e0000-\U000e007f]")


def _bom_midstring(s: str) -> bool:
    return "﻿" in s[1:]


# Directive phrasing aimed at the assistant. The distinction that matters, and
# the one the first draft missed, is between text that INSTRUCTS the model and
# text that DESCRIBES a capability. "Classify a prompt before it reaches your
# LLM" describes; "do not tell the user" instructs. Only the second is here.
CLASSES: dict[str, dict] = {
    "withhold_from_user": {
        "pattern": re.compile(
            r"\b(?:do\s*n[o']?t|never|avoid)\s+(?:ever\s+)?"
            r"(?:tell|inform|show|reveal|disclose|mention\s+(?:this\s+)?to)\s+"
            r"(?:the\s+)?(?:user|human|customer)\b", re.I),
        "why": ("the description instructs the assistant to keep something from "
                "the person it is acting for"),
        "not_matched": "a tool that merely handles confidential data without saying this",
    },
    "demand_precedence": {
        "pattern": re.compile(
            r"\b(?:always\s+call\s+this\s+(?:tool\s+)?first"
            r"|before\s+(?:calling|using)\s+any\s+other\s+tool"
            r"|use\s+this\s+tool\s*[,—-]?\s*not\s+(?:web\s+)?search"
            r"|instead\s+of\s+(?:any\s+)?other\s+tools?)\b", re.I),
        "why": ("the description tries to order the assistant's tool choice rather "
                "than describe what the tool does"),
        "not_matched": "'call this after X' and other honest sequencing notes",
    },
    "override_instructions": {
        "pattern": re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?"
            r"(?:previous|prior|above|earlier|the\s+following)\s+"
            r"(?:instructions?|prompts?|rules?|messages?|context)\b", re.I),
        "why": "classic prompt-override phrasing inside a tool description",
        "not_matched": "'ignore case', 'ignore whitespace' and other parameter semantics",
    },
    "role_markers": {
        "pattern": re.compile(
            r"(?:<\|im_(?:start|end)\|>|\[/?INST\]|<\|(?:system|assistant|user)\|>"
            r"|^\s*###\s*(?:system|instruction)s?\s*:)", re.I | re.M),
        "why": ("chat-template control markers embedded in prose, which a model may "
                "read as a turn boundary"),
        "not_matched": "the words 'system prompt' used to name a field the tool edits",
    },
    "invisible_characters": {
        "pattern": _INVISIBLE,
        "why": ("characters a reviewer cannot see but a model reads, including the "
                "Unicode tags block and bidi overrides"),
        "not_matched": "ZWNJ and ZWJ, which are required orthography in many scripts",
    },
}

# A description that SCREENS for an attack contains the attack's own words.
# `check_instruction` ("Is this CONTENT safe for an agent to ACT ON?") quotes
# "ignore previous instructions" because that is the string it detects, and the
# first narrowed draft still flagged it. So for the two classes where quoting an
# attack is a plausible and legitimate reason to contain it, a screening context
# suppresses the finding.
#
# The guard is applied to those two classes ONLY. There is no honest reason for a
# description to say "never tell the user" or "use this tool, not web search"
# while merely describing something, so those two are never suppressed. Every
# suppression is counted and published, because a filter nobody can see is a
# filter nobody can check.
_SCREENS = re.compile(
    r"\b(?:screen(?:s|ing)?|detect(?:s|ing|ion)?|classif(?:y|ies|ication)|"
    r"scan(?:s|ning)?|guard(?:s|rail)?|sanitis|sanitiz|is\s+this\s+\w+\s+safe|"
    r"prompt[\s-]?injection|jailbreak|untrusted\s+(?:text|content|input))\b", re.I)
_GUARDED = ("override_instructions", "role_markers")

# Text drawn from the tool row. The input schema is included because a
# description can be clean while a parameter's `description` carries the
# payload, and that field reaches the model too.
_SCHEMA_KEYS = ("description", "title", "default", "const")


def _schema_text(raw: str | None, cap: int = 4000) -> str:
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return ""
    out: list[str] = []

    def walk(o):
        if len(" ".join(out)) > cap:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in _SCHEMA_KEYS and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return " ".join(out)[:cap]


# Process-wide tally of guard suppressions, so `scan_corpus` can publish how
# many findings the guard removed rather than hiding the filtering.
_SUPPRESSED: dict[str, int] = {}


def scan_text(name: str | None, description: str | None,
              input_schema: str | None = None) -> list[dict]:
    """Findings for one tool. Empty list is the overwhelmingly common answer."""
    parts = [(("name", name or "")), ("description", description or "")]
    st = _schema_text(input_schema)
    if st:
        parts.append(("inputSchema", st))

    findings = []
    for cls, spec in CLASSES.items():
        for field, text in parts:
            if not text:
                continue
            m = spec["pattern"].search(text)
            if not m:
                if cls == "invisible_characters" and _bom_midstring(text):
                    m = None
                    findings.append({"class": cls, "field": field,
                                     "match": "U+FEFF mid-string",
                                     "why": spec["why"]})
                    break
                continue
            if cls in _GUARDED and _SCREENS.search(text):
                _SUPPRESSED[cls] = _SUPPRESSED.get(cls, 0) + 1
                continue
            excerpt = m.group(0)
            if cls == "invisible_characters":
                excerpt = " ".join(f"U+{ord(ch):04X}" for ch in set(excerpt))
            findings.append({"class": cls, "field": field,
                             "match": excerpt[:80], "why": spec["why"]})
            break  # one finding per class per tool; the first is enough to read
    return findings


def scan_corpus(conn: sqlite3.Connection, examples_per_class: int = 5) -> dict:
    """Count model-directed text across every verified tool we hold."""
    now = int(time.time())
    _SUPPRESSED.clear()
    codepoints: dict[str, int] = {}
    counts = {c: 0 for c in CLASSES}
    entries = {c: set() for c in CLASSES}
    samples: dict[str, list] = {c: [] for c in CLASSES}
    scanned = 0
    flagged_tools = 0
    flagged_entries: set[str] = set()

    for key, name, desc, schema in conn.execute(
            "SELECT entry_key, name, description, input_schema FROM tools"):
        scanned += 1
        fs = scan_text(name, desc, schema)
        if not fs:
            continue
        flagged_tools += 1
        flagged_entries.add(key)
        for f in fs:
            c = f["class"]
            counts[c] += 1
            entries[c].add(key)
            if c == "invisible_characters":
                for cp in f["match"].split():
                    codepoints[cp] = codepoints.get(cp, 0) + 1
            if len(samples[c]) < examples_per_class:
                samples[c].append({
                    "entry": key, "tool": name, "field": f["field"],
                    "match": f["match"],
                    "excerpt": (desc or "")[:180].replace("\n", " "),
                })

    total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    pct = lambda a, b: round(100.0 * a / b, 4) if b else 0.0

    return {
        "generated": now,
        "scanned": {"tools": scanned, "servers_with_tools": _servers(conn),
                    "entries_in_index": total_entries},
        "flagged": {
            "tools": flagged_tools,
            "share_of_tools_pct": pct(flagged_tools, scanned),
            "servers": len(flagged_entries),
        },
        "by_class": {
            c: {
                "tools": counts[c],
                "servers": len(entries[c]),
                "why_it_matters": CLASSES[c]["why"],
                "deliberately_not_matched": CLASSES[c]["not_matched"],
                "examples": samples[c],
            } for c in CLASSES
        },
        # Which invisible codepoints were actually present. This matters because
        # they are not equivalent: a stray U+200B is a copy-paste artefact from a
        # web page, while a run from the tags block (U+E0000-E007F) is a channel
        # with no innocent explanation. Publishing the breakdown lets a reader
        # tell those apart instead of trusting one aggregate count.
        "invisible_codepoints": dict(sorted(codepoints.items(),
                                            key=lambda kv: -kv[1])),
        "suppressed_by_screening_guard": dict(_SUPPRESSED),
        "headline": (
            f"{flagged_tools} of {scanned} verified tool descriptions "
            f"({pct(flagged_tools, scanned)}%) contain text aimed at the model "
            f"rather than at a reader."),
        "how_this_is_measured": (
            "Every tool description in the index was read from that server's own "
            "tools/list, then matched against patterns for text that instructs the "
            "assistant rather than describing the tool. Tool names, descriptions "
            "and input-schema field descriptions are all scanned, because all three "
            "reach the model."),
        "limitations": [
            "A flag is a prompt to read the line, never a verdict. Some flagged "
            "text is legitimate: a tool can have a real reason to sequence itself.",
            "Only text is examined. What a server returns when actually called is "
            "not covered here, and that is where most real risk lives. We never "
            "call a tool.",
            "Detection is by pattern, so paraphrase evades it. Treat the counts as "
            "a floor, and never as proof that the rest of the corpus is safe.",
            "Nothing here is a security audit, a certification or a safety rating, "
            "and it must not be presented as one.",
            "The detectors deliberately do not match ZWNJ and ZWJ, security tools "
            "that name the attacks they detect, or tools that manage a system "
            "prompt as a data field. An earlier draft matched all three and was "
            "wrong about four legitimate publishers.",
        ],
    }


def _servers(conn) -> int:
    return conn.execute("SELECT COUNT(DISTINCT entry_key) FROM tools").fetchone()[0]


def for_entry(conn: sqlite3.Connection, key: str) -> dict:
    """Findings for one server's tools, for the publisher's own console."""
    out = []
    for name, desc, schema in conn.execute(
            "SELECT name, description, input_schema FROM tools WHERE entry_key=?",
            (key,)):
        for f in scan_text(name, desc, schema):
            out.append({"tool": name, **f})
    return {"entry": key, "findings": out,
            "note": ("a finding means a human should read that line. It is not a "
                     "verdict, a score or a security audit.")}
