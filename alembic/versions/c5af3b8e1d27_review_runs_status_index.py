"""index review_runs(status, awaiting_since)

Revision ID: c5af3b8e1d27
Revises: b49e2a7c3d16
Create Date: 2026-11-10 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5af3b8e1d27"
down_revision: str | Sequence[str] | None = "b49e2a7c3d16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_review_runs_status_awaiting_since", "review_runs", ["status", "awaiting_since"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_review_runs_status_awaiting_since", table_name="review_runs")
