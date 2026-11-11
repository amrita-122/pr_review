"""review_runs.purge_checkpoint: held runs of closed PRs wait for their checkpoint to be deleted

Revision ID: e05a8c3d9f71
Revises: d94f7b2a3c68
Create Date: 2026-11-10 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e05a8c3d9f71"
down_revision: str | Sequence[str] | None = "d94f7b2a3c68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "review_runs",
        sa.Column("purge_checkpoint", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("review_runs", "purge_checkpoint")
