"""findings.fixed_in_sha: the commit after which a posted finding no longer reproduces

Revision ID: e7c15d0a3f49
Revises: d6b04c9f2e38
Create Date: 2026-11-10 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c15d0a3f49"
down_revision: str | Sequence[str] | None = "d6b04c9f2e38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("findings", sa.Column("fixed_in_sha", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("findings", "fixed_in_sha")
