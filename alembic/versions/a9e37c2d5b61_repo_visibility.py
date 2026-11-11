"""review_runs.repo_private and private_findings

Revision ID: a9e37c2d5b61
Revises: f8d26e1b4a50
Create Date: 2026-11-10 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9e37c2d5b61"
down_revision: str | Sequence[str] | None = "f8d26e1b4a50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("review_runs", sa.Column("repo_private", sa.Boolean(), nullable=True))
    op.add_column(
        "review_runs",
        sa.Column("private_findings", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("review_runs", "private_findings")
    op.drop_column("review_runs", "repo_private")
