"""Relevance scoring and rank fusion.

Two jobs. First, turn FTS5's BM25 into a 0-100 score that actually separates:
we measured WellKnown returning an average spread of 6.7 points across a result
set, which means its ordering carries almost no information for the client. A
score is only useful if the gap between first and fifth is legible.

Second, fuse result lists from several registries when federating. Reciprocal
rank fusion is the right tool because upstream scores are not comparable.
GitHub crams everything into 90-100 and Desvela spreads 21-85, so fusing on
score would just import their calibration. RRF uses only the ordering each
registry produced, which is the part they are each competent about.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from . import config

# Field weights for bm25(). FTS5 wants one weight per indexed column, in
# declaration order: display_name, description, rep_queries, tags, capabilities,
# tool_text.
# `rep_queries` is weighted highest on purpose, the spec calls it "the signal a
# registry builds its semantic index from", and it is the publisher stating in
# the user's own words what they should be found for.
# `tool_text` sits just under it: the verified tool names read off the running
# server are evidence rather than prose, but they are terse and machine-shaped
# (`extract_pdf_text`), so they must not outrank the sentence a person wrote
# about what the thing is for.
FTS_WEIGHTS = (6.0, 2.0, 9.0, 3.0, 4.0, 7.0)

# How hard to stretch relative relevance across the 0-100 range. Below 1.0 the
# curve lifts mid-ranked results away from the floor, so a genuinely relevant
# fifth result does not read as noise.
_GAMMA = 0.65
_FLOOR = 4


# ── Fusion weights for the tool leg ─────────────────────────────────────────
# MEASURED, not asserted. Chosen on the `tune` half of the frozen declared eval
# set (bench/declared-v1.json) and confirmed on its `holdout` half, with the
# capability battery (scripts/battery.py) held as a second objective that may not
# regress. Change them only by re-running that measurement.
#
# The tool leg tries a phrase, then a conjunction, then a bare OR over the query
# words. Only the first two are evidence: the OR matches almost any query against
# 95,994 tool descriptions. At a flat 2.0 the OR leg's whole top ten outscored the
# lexical leg's first place under RRF (2/(60+r) > 1/61 for every r < 62), so a
# publisher's own verbatim query found its entry 21% of the time while the lexical
# leg alone ranked it first in 140 of 192 cases.
#
#   strict / loose   declared recall@10 / MRR (tune)   battery hit@5
#   2.0 / 2.0        0.209 / 0.087                     6/15   (before)
#   1.0 / 1.0        0.919 / 0.454                     4/15   (a flat cut costs the battery)
#   2.0 / 0.0        0.930 / 0.811                     6/15   (chosen)
TOOL_LEG_WEIGHT = 2.0         # a phrase or conjunction matched a tool
TOOL_LEG_LOOSE_WEIGHT = 0.0   # only the OR fallback matched: not evidence


# ── Federation and the dense leg: coverage, not ranking (2026-09-23) ─────────
# Recorded every leg's ranking for all 192 declared tasks and the battery, then
# swept fusion offline. Findings:
#   * lexical alone had 183/192 targets in its top 10; `auto` had 174. The
#     upstream legs rescued 2 and the dense leg 6, but together they pushed out 11.
#   * 95.5% of what upstream registries return is already in this index (we
#     harvest them every six hours), and 16 of 2,070 fused top-10 slots held an
#     entry only an upstream had. Re-ranking our own entries in THEIR order is
#     what cost accuracy, and waiting for them cost the 700 ms budget.
#   * dense fused at 1.0 cost the battery 7/15 -> 4/15; as an additive leg
#     (candidates lexical did not return) it costs nothing at weight <= 1.0.
# So upstream and dense legs are ADDITIVE: they may add candidates the local legs
# did not return, never reorder ones they did. At 0.5 an additive candidate
# (best 0.5/61) cannot outrank a local one inside the first 30 (1/90), which is
# what makes the gate below exact rather than heuristic.
DENSE_LEG_WEIGHT = 0.5
UPSTREAM_LEG_WEIGHT = 0.5

# Federate only when the local answer is not confident: the top result accounts
# for less than this share of the query's content words (`search.query_match`).
# A confident local page cannot be changed by an additive leg, so the query is
# not sent to other services and not embedded, which is both the latency win and
# data minimisation. A caller that asks for a longer window (the homepage's
# "every registry" call) always federates.
FEDERATE_BELOW_COVERAGE = 0.75


# Known-item queries: someone typing the name of the thing ("Notion API"). BM25
# ranked that exact display name 22nd, under entries that mention both words many
# times in long tool text. The name leg orders entries whose display_name holds
# the query as a phrase, exact matches first. Measured on the tune half with each
# target's own display name as the query (a known-item set built from the same
# frozen tasks), declared queries and the battery held fixed:
#   weight   name recall@10 / MRR   declared r@10 / MRR   battery hit@5
#   0.0      0.860 / 0.688          0.942 / 0.830         6/15
#   1.0      0.988 / 0.830          0.942 / 0.830         6/15
#   2.0      1.000 / 0.927          0.942 / 0.830         6/15   (chosen: smallest at full recall)
#   3.0      1.000 / 0.939          0.942 / 0.830         6/15
NAME_LEG_WEIGHT = 2.0


def tool_leg_weight(strict: bool) -> float:
    return TOOL_LEG_WEIGHT if strict else TOOL_LEG_LOOSE_WEIGHT


def scale_scores(raw: Sequence[float]) -> list[int]:
    """Map raw relevance (higher is better) onto 0-100, preserving separation.

    Scored relative to the best hit in this result set rather than against an
    absolute scale, because BM25 magnitudes are corpus- and query-dependent and
    mean nothing on their own. If everything really is equally relevant the
    scores really are close together, which is honest; what we refuse to do is
    compress genuinely different results into the same band.
    """
    if not raw:
        return []
    top = max(raw)
    if top <= 0:
        return [_FLOOR for _ in raw]
    out = []
    for r in raw:
        rel = max(0.0, r / top)
        out.append(max(_FLOOR, min(100, int(round(100 * (rel ** _GAMMA))))))
    return out


def apply_liveness(score: int, live: int | None) -> int:
    """Demote what does not answer.

    A dead endpoint is not a discovery result, it is noise wearing a result's
    clothes. We demote rather than delete: services come back, and a registry
    that silently forgets is its own kind of unreliable. Never-probed entries
    are left alone rather than punished for our own crawl backlog.
    """
    if live == 0:
        return max(1, int(round(score * config.DEAD_PENALTY)))
    return score


def rrf(rankings: Iterable[Sequence[str]], k: int = 60,
        weights: Sequence[float] | None = None) -> dict[str, float]:
    """Reciprocal rank fusion over several ordered key lists.

    score(d) = sum over lists of w/(k + rank(d)), rank starting at 1. k=60 is
    the value from the original TREC work and is what our own search stack
    already uses, so the behaviour is one we have watched in production.

    `weights` lets one ordering count for more than another. It exists for the
    tool leg: a match on a tool is the server's own statement that it performs
    exactly this operation, read back from its tools/list or its published
    OpenAPI document. That is the same class of evidence `verified_bonus`
    already prefers over prose, so weighting it is consistent with how the rest
    of this module treats what we have verified against what we were told.
    Unweighted lists default to 1.0 and behave exactly as before.
    """
    fused: dict[str, float] = {}
    ws = list(weights or [])
    for idx, ranking in enumerate(rankings):
        w = ws[idx] if idx < len(ws) else 1.0
        for i, key in enumerate(ranking, start=1):
            if not key:
                continue
            fused[key] = fused.get(key, 0.0) + w / (k + i)
    return fused


def fuse_to_scores(fused: dict[str, float]) -> dict[str, int]:
    """Turn RRF weights into the same 0-100 scale as local scoring."""
    if not fused:
        return {}
    keys = list(fused)
    scaled = scale_scores([fused[k] for k in keys])
    return dict(zip(keys, scaled))


def verified_bonus(n_tools: int | None) -> float:
    """A small lift for an entry whose capability we have actually verified.

    An entry where we handshook with the endpoint and read back real tools is
    strictly better evidence than one that is only prose. Deliberately small,
    and capped: it breaks ties and nudges, it never promotes a weak match over
    a strong one, and it must never become a proxy for trust. Entries we have
    not yet introspected are left alone rather than punished for our own
    backlog, exactly as liveness treats unprobed entries.
    """
    if not n_tools:
        return 1.0
    return 1.0 + min(0.10, 0.02 * n_tools)


def source_bonus(n_sources: int) -> float:
    """A small lift for entries several independent registries agree on.

    Corroboration is weak evidence, so it is deliberately weak: enough to break
    a tie between otherwise equal matches, never enough to outrank a better one.
    """
    return 1.0 + min(0.08, 0.03 * max(0, n_sources - 1))
