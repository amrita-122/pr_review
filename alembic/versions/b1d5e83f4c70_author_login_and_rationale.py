"""review_runs.author_login (rate limit) and findings.rationale (/explain)

Revision ID: b1d5e83f4c70
Revises: a9e37c2d5b61
Create Date: 2026-11-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d5e83f4c70"
down_revision: str | Sequence[str] | None = "a9e37c2d5b61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("review_runs", sa.Column("author_login", sa.String(), nullable=True))
    op.create_index("ix_review_runs_author_login", "review_runs", ["author_login"])
    op.add_column("findings", sa.Column("rationale", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("findings", "rationale")
    op.drop_index("ix_review_runs_author_login", table_name="review_runs")
    op.drop_column("review_runs", "author_login")
