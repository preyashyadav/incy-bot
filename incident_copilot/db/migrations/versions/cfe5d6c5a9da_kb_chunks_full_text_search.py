"""kb_chunks full-text search

Revision ID: cfe5d6c5a9da
Revises: 18fb2dd99f4c
Create Date: 2026-09-03 00:11:16.374645
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "cfe5d6c5a9da"
down_revision: str | None = "18fb2dd99f4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default is required, not cosmetic: without it this ALTER fails on any table
    # that already has rows, which is every environment except a fresh one.
    op.add_column("kb_chunks", sa.Column("tags_text", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "kb_chunks",
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('english', coalesce(tags_text, '')), 'A') || "
                "setweight(to_tsvector('english', coalesce(content, '')), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_kb_chunks_tsv", table_name="kb_chunks", postgresql_using="gin")
    op.drop_column("kb_chunks", "tsv")
    op.drop_column("kb_chunks", "tags_text")
