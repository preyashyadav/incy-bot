"""baseline: enable pgvector extension

Revision ID: 3decffad9bc8
Revises:
Create Date: 2026-09-02 23:35:37.056818
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3decffad9bc8"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector backs semantic retrieval (phase 3). Creating the extension in the baseline
    # keeps it out of the migration that adds the embedding column, so that migration stays
    # a pure schema change and can run against a database where the extension already exists.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
