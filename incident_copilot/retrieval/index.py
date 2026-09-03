"""Builds the searchable corpora.

Two sources, one table:

* **kb** — runbooks and policies from `seed/kb/*.md`, split on `##` headings so a hit returns
  the relevant section rather than a whole document. A retrieved chunk goes into a prompt, so
  chunk size is a cost decision as much as a relevance one.
* **history** — resolved incidents from `seed/history/incidents.json`, one chunk each, rendered
  so that the symptoms *and* the resolution are both searchable. Matching on symptoms is what
  makes "have we seen this before?" answerable; carrying the resolution in the same chunk is what
  makes the answer useful.

Indexing is idempotent: chunks are keyed by a stable `chunk_id` and upserted, so re-running after
editing a runbook updates in place rather than duplicating.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from incident_copilot.db.models import KBChunk

SEED_DIR = Path(__file__).resolve().parents[2] / "seed"
KB_DIR = SEED_DIR / "kb"
HISTORY_FILE = SEED_DIR / "history" / "incidents.json"

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass
class Chunk:
    chunk_id: str
    corpus: str
    title: str
    content: str
    source: str
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "corpus": self.corpus,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "tags": self.tags,
            # The generated tsvector reads this column; tags stay JSONB for structured filters.
            "tags_text": " ".join(self.tags),
            "meta": self.meta,
        }


def _slug(text: str) -> str:
    return _SLUG_UNSAFE.sub("-", text.lower()).strip("-")[:60]


def parse_markdown(path: Path) -> list[Chunk]:
    """Split one runbook into per-section chunks, inheriting the document's front matter."""
    raw = path.read_text(encoding="utf-8")
    tags: list[str] = []
    doc_title = path.stem

    if (match := _FRONT_MATTER.match(raw)) is not None:
        meta = yaml.safe_load(match.group(1)) or {}
        doc_title = str(meta.get("title", doc_title))
        tags = [str(t) for t in meta.get("tags", [])]
        raw = raw[match.end() :]

    chunks: list[Chunk] = []
    # Sections are `## Heading`; text before the first heading (rare) is dropped rather than
    # emitted as an untitled chunk that would retrieve without context.
    parts = re.split(r"^## +(.+)$", raw, flags=re.MULTILINE)
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        body = body.strip()
        if not body:
            continue
        chunks.append(
            Chunk(
                chunk_id=f"kb:{path.stem}:{_slug(heading)}",
                corpus="kb",
                # The document title travels with every chunk so a retrieved section still says
                # what it belongs to.
                title=f"{doc_title} — {heading.strip()}",
                content=body,
                source=f"seed/kb/{path.name}#{_slug(heading)}",
                tags=tags,
                meta={"document": doc_title, "section": heading.strip()},
            )
        )
    return chunks


def render_incident(record: dict[str, Any]) -> Chunk:
    """One resolved incident as a searchable chunk."""
    content = (
        f"Symptoms: {record['symptoms']}\n"
        f"Cause: {record['cause']}\n"
        f"Resolution: {record['resolution']} (action: {record['resolution_kind']})\n"
        f"Time to mitigate: {record['time_to_mitigate_minutes']} minutes\n"
        f"Lesson: {record['lesson']}"
    )
    return Chunk(
        chunk_id=f"history:{record['key']}",
        corpus="history",
        title=f"{record['key']} — {record['title']}",
        content=content,
        source=f"incident/{record['key']}",
        tags=[record["service"], record["signal"], record["severity"], record["resolution_kind"]],
        meta={
            "incident_key": record["key"],
            "service": record["service"],
            "signal": record["signal"],
            "severity": record["severity"],
            "resolution_kind": record["resolution_kind"],
            "time_to_mitigate_minutes": record["time_to_mitigate_minutes"],
        },
    )


def load_chunks(kb_dir: Path | None = None, history_file: Path | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted((kb_dir or KB_DIR).glob("*.md")):
        chunks.extend(parse_markdown(path))
    records = json.loads((history_file or HISTORY_FILE).read_text(encoding="utf-8"))
    chunks.extend(render_incident(record) for record in records)
    return chunks


def index_chunks(session: Session, chunks: list[Chunk]) -> int:
    """Upsert chunks by `chunk_id`."""
    if not chunks:
        return 0
    for chunk in chunks:
        row = chunk.as_row()
        session.execute(
            pg_insert(KBChunk)
            .values(**row)
            .on_conflict_do_update(
                index_elements=["chunk_id"],
                set_={k: v for k, v in row.items() if k != "chunk_id"},
            )
        )
    session.flush()
    return len(chunks)


def reindex(session: Session, *, prune: bool = True) -> int:
    """Rebuild the index from disk.

    `prune` removes chunks no longer produced by the seed files — otherwise a renamed section
    would linger and keep being retrieved long after its source was deleted.
    """
    chunks = load_chunks()
    count = index_chunks(session, chunks)
    if prune:
        keep = {c.chunk_id for c in chunks}
        stale = session.execute(
            select(KBChunk.chunk_id).where(KBChunk.chunk_id.notin_(keep))
        ).scalars()
        stale_ids = list(stale)
        if stale_ids:
            session.execute(delete(KBChunk).where(KBChunk.chunk_id.in_(stale_ids)))
            session.flush()
    return count


def is_indexed(session: Session) -> bool:
    return session.execute(select(KBChunk.id).limit(1)).scalar_one_or_none() is not None
