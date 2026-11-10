"""review_runs.held_posted_at: the follow-up review of confirmed findings

Revision ID: a38d1f6b2c05
Revises: f16b9d4e0a82
Create Date: 2026-11-10 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a38d1f6b2c05"
down_revision: str | Sequence[str] | None = "f16b9d4e0a82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "review_runs", sa.Column("held_posted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("review_runs", "held_posted_at")
