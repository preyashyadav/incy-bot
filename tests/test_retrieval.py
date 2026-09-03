"""Corpus indexing and lexical search.

The recall assertions are a golden set: they encode which document *should* answer a given
question, so a change to chunking, weighting, or query rewriting that quietly degrades retrieval
fails here instead of silently degrading every investigation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from incident_copilot.db.models import KBChunk
from incident_copilot.retrieval.index import (
    KB_DIR,
    Chunk,
    index_chunks,
    load_chunks,
    parse_markdown,
    reindex,
    render_incident,
)
from incident_copilot.retrieval.search import (
    build_symptom_query,
    search,
    search_history,
    search_runbooks,
)


@pytest.fixture
def indexed(db: Session) -> Iterator[Session]:
    reindex(db)
    db.commit()
    yield db


# -- chunking ---------------------------------------------------------------


def test_markdown_splits_on_sections() -> None:
    chunks = parse_markdown(KB_DIR / "runbook-payments-gateway.md")
    assert len(chunks) >= 4
    assert all(c.corpus == "kb" for c in chunks)
    sections = {c.meta["section"] for c in chunks}
    assert {"Symptoms", "Diagnosis", "Mitigation", "Verification"} <= sections


def test_chunk_titles_carry_the_document(indexed: Session) -> None:
    """A retrieved section must still say what it belongs to."""
    chunks = parse_markdown(KB_DIR / "runbook-payments-gateway.md")
    assert all(" — " in c.title for c in chunks)
    assert all("payments-api" in c.title for c in chunks)


def test_front_matter_tags_apply_to_every_chunk() -> None:
    chunks = parse_markdown(KB_DIR / "runbook-payments-gateway.md")
    assert all("enable_new_gateway" in c.tags for c in chunks)


def test_history_chunk_carries_symptoms_and_resolution() -> None:
    """Symptoms make it findable; the resolution makes the find useful."""
    chunk = render_incident(
        {
            "key": "INC-1",
            "title": "t",
            "service": "payments-api",
            "signal": "error_rate_spike",
            "severity": "SEV1",
            "symptoms": "error_rate 9%",
            "cause": "c",
            "resolution": "Restored the timeout",
            "resolution_kind": "set_config_value",
            "time_to_mitigate_minutes": 18,
            "lesson": "l",
        }
    )
    assert "Symptoms:" in chunk.content and "Resolution:" in chunk.content
    assert chunk.meta["resolution_kind"] == "set_config_value"
    assert "set_config_value" in chunk.tags


def test_corpora_load_from_disk() -> None:
    chunks = load_chunks()
    kb = [c for c in chunks if c.corpus == "kb"]
    history = [c for c in chunks if c.corpus == "history"]
    assert len(kb) >= 25
    assert len(history) >= 20
    assert len({c.chunk_id for c in chunks}) == len(chunks)  # ids are unique


# -- indexing ---------------------------------------------------------------


def test_reindex_is_idempotent(db: Session) -> None:
    first = reindex(db)
    db.commit()
    count = db.query(KBChunk).count()

    assert reindex(db) == first
    db.commit()
    assert db.query(KBChunk).count() == count


def test_reindex_updates_in_place(db: Session) -> None:
    index_chunks(db, [Chunk("kb:x", "kb", "Old title", "old body", "s.md", ["t"])])
    db.commit()
    index_chunks(db, [Chunk("kb:x", "kb", "New title", "new body", "s.md", ["t"])])
    db.commit()

    row = db.query(KBChunk).filter_by(chunk_id="kb:x").one()
    assert row.title == "New title"
    assert db.query(KBChunk).count() == 1


def test_reindex_prunes_removed_chunks(db: Session) -> None:
    """A renamed section would otherwise linger and keep being retrieved."""
    index_chunks(db, [Chunk("kb:stale", "kb", "Gone", "body", "s.md")])
    db.commit()
    reindex(db)
    db.commit()
    assert db.query(KBChunk).filter_by(chunk_id="kb:stale").count() == 0


def test_tags_text_is_populated_for_the_index(db: Session) -> None:
    index_chunks(db, [Chunk("kb:t", "kb", "T", "body", "s.md", ["alpha", "beta"])])
    db.commit()
    assert db.query(KBChunk).filter_by(chunk_id="kb:t").one().tags_text == "alpha beta"


# -- search behaviour -------------------------------------------------------


def test_empty_query_returns_nothing(indexed: Session) -> None:
    assert search(indexed, "   ") == []


@pytest.mark.parametrize(
    "query",
    [
        "timeout: 1000ms (!) & error_rate > 10%",
        'the "circuit breaker" is open -- why?',
        "AND OR NOT",
        "'; DROP TABLE kb_chunks; --",
    ],
)
def test_punctuation_never_raises(indexed: Session, query: str) -> None:
    """A model-authored query must not be able to crash retrieval mid-investigation.

    This is the class of bug the previous version worked around with manual token escaping.
    """
    search(indexed, query)
    assert indexed.query(KBChunk).count() > 0  # nothing was dropped


def test_corpus_filter_is_respected(indexed: Session) -> None:
    assert all(h.corpus == "kb" for h in search_runbooks(indexed, "timeout"))
    assert all(h.corpus == "history" for h in search_history(indexed, "timeout"))


def test_tag_boost_promotes_without_excluding(indexed: Session) -> None:
    """A boost, not a filter — the best precedent is sometimes from another service.

    Compared at a limit large enough to hold every match, so the assertion is about the boost
    rather than about which rows survived truncation.
    """
    query = "latency regression scaling did not help"
    plain = search_history(indexed, query, limit=50)
    boosted = search_history(indexed, query, limit=50, boost_tags=["search-api"])

    # Nothing is filtered out by boosting.
    assert {h.chunk_id for h in plain} == {h.chunk_id for h in boosted}

    # Chunks carrying the tag score strictly higher; others are untouched.
    plain_scores = {h.chunk_id: h.score for h in plain}
    for hit in boosted:
        if "search-api" in hit.tags:
            assert hit.score > plain_scores[hit.chunk_id]
        else:
            assert hit.score == pytest.approx(plain_scores[hit.chunk_id])

    # And an off-service precedent is still reachable, which is the point of a boost.
    assert any("search-api" not in h.tags for h in boosted)


def test_scores_are_descending(indexed: Session) -> None:
    hits = search(indexed, "gateway timeout", limit=6)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


# -- recall golden set ------------------------------------------------------

RUNBOOK_RECALL = [
    ("payments are timing out at the gateway", "runbook-payments-gateway"),
    ("users get logged out immediately after signing in", "runbook-auth-sessions"),
    ("p95 latency is up but nothing is failing", "runbook-search-latency"),
    ("memory climbs until the service OOMs", "runbook-checkout-memory"),
    ("traffic doubled and the autoscaler already responded", "runbook-traffic-surge"),
    ("how do I decide between SEV1 and SEV2", "policy-severity"),
    ("which remediation has the smallest blast radius", "policy-change-management"),
    ("what should the first status update say", "template-comms"),
]


@pytest.mark.parametrize(("query", "expected_doc"), RUNBOOK_RECALL)
def test_runbook_recall_at_3(indexed: Session, query: str, expected_doc: str) -> None:
    hits = search_runbooks(indexed, query, limit=3)
    assert any(expected_doc in h.chunk_id for h in hits), (
        f"{expected_doc} not in top 3 for {query!r}: {[h.chunk_id for h in hits]}"
    )


HISTORY_RECALL = [
    ("payments failing gateway timeout lowered", "INC-2025-0412"),
    ("session tokens expiring after a deploy", "INC-2025-0901"),
    ("search latency N+1 queries after ORM change", "INC-2025-0733"),
    ("checkout memory leak cart cache OOM", "INC-2025-0820"),
    ("scaled down during a traffic surge and caused an outage", "INC-2025-0677"),
    ("scaling did not improve latency", "INC-2024-0940"),
]


@pytest.mark.parametrize(("query", "expected_incident"), HISTORY_RECALL)
def test_history_recall_at_3(indexed: Session, query: str, expected_incident: str) -> None:
    hits = search_history(indexed, query, limit=3)
    assert any(expected_incident in h.chunk_id for h in hits), (
        f"{expected_incident} not in top 3 for {query!r}: {[h.chunk_id for h in hits]}"
    )


def test_exact_identifiers_retrieve_precisely(indexed: Session) -> None:
    """Lexical search earns its place on identifiers, which is most of incident evidence."""
    for identifier in ("enable_new_gateway", "CircuitBreakerOpenException", "gateway_timeout_ms"):
        hits = search(indexed, identifier, limit=3)
        assert hits, f"no hits for {identifier}"
        assert any("payments" in h.chunk_id or "payments" in h.title.lower() for h in hits)


# -- query construction -----------------------------------------------------


def test_symptom_query_uses_the_signature(indexed: Session) -> None:
    """Alert prose retrieves poorly; the signature carries the discriminating terms."""
    query = build_symptom_query(
        service="payments-api",
        signal="error_rate_spike",
        metric_highlights=["upstream_timeout_rate 0.098"],
        log_highlights=["CircuitBreakerOpenException"],
    )
    assert "payments-api" in query and "error rate spike" in query

    hits = search_history(indexed, query, limit=3)
    assert any("INC-2025-0412" in h.chunk_id for h in hits)
