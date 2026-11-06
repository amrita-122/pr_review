"""human confirmation columns on review_runs

Revision ID: c83e6a1f2d57
Revises: b72d5f1c8e49
Create Date: 2026-11-04 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c83e6a1f2d57"
down_revision: str | Sequence[str] | None = "b72d5f1c8e49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("review_runs", sa.Column("installation_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "review_runs", sa.Column("awaiting_since", sa.DateTime(timezone=True), nullable=True)
    )
    for name in ("pending_comment_posted", "expired_comment_posted"):
        op.add_column(
            "review_runs",
            sa.Column(name, sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    op.add_column("review_runs", sa.Column("confirmed_by", sa.String(), nullable=True))
    op.add_column(
        "review_runs", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    for name in (
        "confirmed_at",
        "confirmed_by",
        "expired_comment_posted",
        "pending_comment_posted",
        "awaiting_since",
        "installation_id",
    ):
        op.drop_column("review_runs", name)
