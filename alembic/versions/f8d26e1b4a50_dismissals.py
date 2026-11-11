"""dismissals: findings.false_positive/dismissed_by, review_runs.dismissed_count

Revision ID: f8d26e1b4a50
Revises: e7c15d0a3f49
Create Date: 2026-11-10 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8d26e1b4a50"
down_revision: str | Sequence[str] | None = "e7c15d0a3f49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "findings",
        sa.Column("false_positive", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("findings", sa.Column("dismissed_by", sa.String(), nullable=True))
    op.add_column(
        "review_runs",
        sa.Column("dismissed_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("review_runs", "dismissed_count")
    op.drop_column("findings", "dismissed_by")
    op.drop_column("findings", "false_positive")
