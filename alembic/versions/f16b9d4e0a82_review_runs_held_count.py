"""review_runs.held_count: lets the 'pending' comment be re-sent after a failed POST

Revision ID: f16b9d4e0a82
Revises: e05a8c3d9f71
Create Date: 2026-11-10 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f16b9d4e0a82"
down_revision: str | Sequence[str] | None = "e05a8c3d9f71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "review_runs", sa.Column("held_count", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("review_runs", "held_count")
