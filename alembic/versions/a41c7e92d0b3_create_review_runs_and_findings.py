"""create review_runs and findings

Revision ID: a41c7e92d0b3
Revises: 546fad85a739
Create Date: 2026-11-02 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41c7e92d0b3"
down_revision: str | Sequence[str] | None = "546fad85a739"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "review_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("file", sa.String(), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("posted", sa.Boolean(), nullable=False),
        sa.Column("drop_reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["review_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_findings_run_id"), "findings", ["run_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_findings_run_id"), table_name="findings")
    op.drop_table("findings")
    op.drop_table("review_runs")
