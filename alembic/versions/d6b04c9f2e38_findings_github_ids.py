"""findings: GitHub review id, comment id and node id

Revision ID: d6b04c9f2e38
Revises: c5af3b8e1d27
Create Date: 2026-11-10 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6b04c9f2e38"
down_revision: str | Sequence[str] | None = "c5af3b8e1d27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("findings", sa.Column("github_review_id", sa.BigInteger(), nullable=True))
    op.add_column("findings", sa.Column("github_comment_id", sa.BigInteger(), nullable=True))
    op.add_column("findings", sa.Column("github_node_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("findings", "github_node_id")
    op.drop_column("findings", "github_comment_id")
    op.drop_column("findings", "github_review_id")
