"""review_runs.head_updated_at: orders out-of-order push webhooks

Revision ID: d94f7b2a3c68
Revises: c83e6a1f2d57
Create Date: 2026-11-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d94f7b2a3c68"
down_revision: str | Sequence[str] | None = "c83e6a1f2d57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "review_runs", sa.Column("head_updated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("review_runs", "head_updated_at")
