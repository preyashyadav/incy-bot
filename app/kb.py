# app/kb.py
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from app.db import DB_PATH

KB_FTS_TABLE = "kb_chunks"

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_kb() -> None:
    conn = get_conn()
    try:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {KB_FTS_TABLE}
            USING fts5(
              chunk_id,
              title,
              tags,
              content,
              source
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def seed_kb_if_empty() -> None:
    """
    Idempotent seed:
    - Ensures required seed chunks exist by chunk_id.
    - Safe to run on every startup.
    """
    conn = get_conn()
    try:
        init_kb()

        seed_rows = [
            {
                "chunk_id": "rb-payments-001",
                "title": "Runbook: Payments failing — gateway timeouts",
                "tags": "payments_failing checkout_api gateway timeout circuit_breaker",
                "content": (
                    "Checks: confirm health endpoint unhealthy; look for upstream timeout errors; "
                    "inspect upstream_timeout_rate and error_rate spikes; review recent deploys, flags, config.\n"
                    "Mitigations: revert gateway timeout to prior value; disable enable_new_gateway flag; rollback recent deploy.\n"
                    "Post-mitigation: confirm circuit breaker closes and error_rate drops."
                ),
                "source": "runbooks/payments_failing.md#gateway-timeouts",
            },
            {
                "chunk_id": "pol-sev-001",
                "title": "Policy: Severity rubric",
                "tags": "sev sev1 sev2 sev3 policy",
                "content": (
                    "SEV1: payments failing or login outage with clear customer impact.\n"
                    "SEV2: partial degradation (elevated latency or partial failures).\n"
                    "SEV3: minor issue with limited/no customer impact."
                ),
                "source": "policies/severity.md",
            },
            {
                "chunk_id": "tpl-comms-001",
                "title": "Comms: Status update guidance",
                "tags": "comms status_update template guidance",
                "content": (
                    "Initial update should avoid absolute root cause. Use: 'under investigation', 'appears related to'. "
                    "Include: what’s happening, customer impact, what we’re doing, next update ETA.\n"
                    "After mitigation: what changed, current status, remaining risk, next steps."
                ),
                "source": "templates/comms.md#status-updates",
            },
        ]

        for row in seed_rows:
            exists = conn.execute(
                f"SELECT 1 FROM {KB_FTS_TABLE} WHERE chunk_id = ? LIMIT 1;",
                (row["chunk_id"],),
            ).fetchone()

            if not exists:
                conn.execute(
                    f"""
                    INSERT INTO {KB_FTS_TABLE} (chunk_id, title, tags, content, source)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (row["chunk_id"], row["title"], row["tags"], row["content"], row["source"]),
                )

        conn.commit()
    finally:
        conn.close()




def _to_fts_match_query(text: str) -> str:
    """
    Convert arbitrary user text into a safe FTS5 MATCH query.
    - Extracts word tokens (letters/digits/_)
    - Wraps each token in double quotes (prevents operator parsing like '-' or ':')
    - Joins tokens with AND (space)
    """
    tokens = _WORD_RE.findall(text or "")
    if not tokens:
        return ""
    return " ".join(f"\"{t}\"" for t in tokens)


def kb_search(q: str, k: int = 3, tags: Optional[str] = None) -> List[Dict[str, Any]]:
    init_kb()

    q2 = q.strip()

    # If tags exist, OR them in, don't AND them
    if tags:
        tag_terms = [t for t in tags.split() if t]
        if tag_terms:
            q2 = f"({q2}) OR ({' OR '.join(tag_terms)})"

    conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT
              chunk_id, title, tags, content, source,
              bm25({KB_FTS_TABLE}) as score
            FROM {KB_FTS_TABLE}
            WHERE {KB_FTS_TABLE} MATCH ?
            ORDER BY score
            LIMIT ?;
            """,
            (q2, int(k)),
        ).fetchall()

        return [
            {
                "chunk_id": r["chunk_id"],
                "title": r["title"],
                "source": r["source"],
                "score": float(r["score"]),
                "snippet": r["content"],
                "tags": r["tags"],
            }
            for r in rows
        ]
    finally:
        conn.close()
