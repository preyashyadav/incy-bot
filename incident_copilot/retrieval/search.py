"""Lexical search over the runbook and incident-history corpora.

Postgres full text, ranked with `ts_rank_cd`. Phase 3 adds a vector half and fuses the two with
reciprocal rank fusion; this module is the lexical half, and it is deliberately good enough to
stand alone — exact identifiers (`enable_new_gateway`, `CircuitBreakerOpenException`,
`gateway_timeout_ms`) are precisely what lexical search is best at, and they carry most of the
signal in incident evidence.

`websearch_to_tsquery` is used rather than `plainto_tsquery` because it never raises on
punctuation. A model-authored query containing a colon, quotes, or a stray operator would make
`to_tsquery` throw mid-investigation; `websearch_to_tsquery` degrades to a reasonable
interpretation instead. The previous version of this project hit exactly that class of bug and
worked around it with manual token escaping.

Its output is then rewritten from AND to OR — see `_or_tsquery`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Float, Text, case, cast, func, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.orm import Session

from incident_copilot.db.models import KBChunk

Corpus = Literal["kb", "history"]

# ts_rank_cd normalisation flags, OR-ed together:
#   4  — divide by the mean harmonic distance between match extents, so a chunk whose matched
#        terms sit close together outranks one that merely mentions them in scattered places.
#   32 — divide by (rank + 1), mapping scores into (0, 1) so they are comparable across queries.
#
# Chosen by measurement, not taste: swept against the recall golden set in
# `tests/test_retrieval.py`, flag 4 is the only setting that retrieves all 15 targets in the top
# 3. Plain 32 gets 13/15 — it compresses scores toward 1.0 and lets a short document dense in one
# common term outrank the document that actually answers the question.
_RANK_NORMALISATION = 4 | 32


def _or_tsquery(query: str) -> Any:
    """The same query with its operators rewritten to OR.

    `websearch_to_tsquery` joins every lexeme with `&`, so "tokens expiring immediately after
    login" only matches a chunk containing all four stems — which no runbook section does. Used
    alone it produces silent empty retrieval on exactly the natural-language queries a model
    writes.

    Rewriting the already-parsed query preserves every safety property (the input has been
    sanitised, so no punctuation reaches the tsquery parser) while turning relevance into a
    ranking decision rather than a filtering one.
    """
    parsed = cast(func.websearch_to_tsquery("english", query), Text)
    return cast(func.replace(parsed, " & ", " | "), TSQUERY)


class SearchHit(BaseModel):
    chunk_id: str
    corpus: str
    title: str
    content: str
    source: str
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    score: float

    def cite(self) -> str:
        """The identifier the agent puts in `evidence_cited`, so a claim can be traced back."""
        return self.chunk_id


def search(
    session: Session,
    query: str,
    *,
    corpus: Corpus | None = None,
    limit: int = 5,
    boost_tags: list[str] | None = None,
) -> list[SearchHit]:
    """Rank chunks against a free-text query.

    `boost_tags` (service, signal, severity) adds a small bonus to chunks carrying them. It is a
    bonus rather than a filter on purpose: the most useful precedent is sometimes from another
    service, and a hard filter would hide it. `INC-2024-0940` — latency wrongly diagnosed as
    capacity — is exactly the sort of cross-service lesson a filter would suppress.
    """
    if not query.strip():
        return []

    # An additional term weighting full-query matches was tried here and measured: it changed
    # no golden-set outcome, so it was removed rather than kept on intuition.
    or_query = _or_tsquery(query)
    score: ColumnElement[float] = cast(
        func.ts_rank_cd(KBChunk.tsv, or_query, _RANK_NORMALISATION), Float
    )
    if boost_tags:
        # 0.05 per matching tag: enough to break ties between comparably relevant chunks,
        # not enough to float an irrelevant one above a strong textual match.
        for tag in boost_tags:
            # CASE rather than casting the boolean: Postgres has no boolean->float coercion.
            score = score + case((KBChunk.tags.contains([tag]), 0.05), else_=0.0)

    stmt = (
        select(KBChunk, score.label("score"))
        .where(KBChunk.tsv.op("@@")(or_query))
        .order_by(score.desc())
        .limit(limit)
    )
    if corpus is not None:
        stmt = stmt.where(KBChunk.corpus == corpus)

    return [
        SearchHit(
            chunk_id=row.KBChunk.chunk_id,
            corpus=row.KBChunk.corpus,
            title=row.KBChunk.title,
            content=row.KBChunk.content,
            source=row.KBChunk.source,
            tags=list(row.KBChunk.tags),
            meta=dict(row.KBChunk.meta),
            score=float(row.score),
        )
        for row in session.execute(stmt).all()
    ]


def search_runbooks(
    session: Session, query: str, *, limit: int = 4, boost_tags: list[str] | None = None
) -> list[SearchHit]:
    return search(session, query, corpus="kb", limit=limit, boost_tags=boost_tags)


def search_history(
    session: Session, query: str, *, limit: int = 4, boost_tags: list[str] | None = None
) -> list[SearchHit]:
    return search(session, query, corpus="history", limit=limit, boost_tags=boost_tags)


def build_symptom_query(
    *,
    service: str,
    signal: str,
    metric_highlights: list[str] | None = None,
    log_highlights: list[str] | None = None,
) -> str:
    """Compose a history query from an incident's signature rather than its prose.

    Searching history with the alert text alone retrieves poorly: alerts are short, generic, and
    share vocabulary across unrelated incidents. The discriminating terms are the service, the
    signal, and the identifiers appearing in logs and metrics.
    """
    parts = [service, signal.replace("_", " ")]
    parts.extend(metric_highlights or [])
    parts.extend(log_highlights or [])
    return " ".join(p for p in parts if p)
