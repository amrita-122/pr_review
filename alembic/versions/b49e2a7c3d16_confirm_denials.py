"""confirm_denials: logins refused on a held review (no repeat lookups, one reply each)

Revision ID: b49e2a7c3d16
Revises: a38d1f6b2c05
Create Date: 2026-11-10 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b49e2a7c3d16"
down_revision: str | Sequence[str] | None = "a38d1f6b2c05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "confirm_denials",
        sa.Column("run_id", sa.String(), sa.ForeignKey("review_runs.id"), primary_key=True),
        sa.Column("login", sa.String(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("confirm_denials")
