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
