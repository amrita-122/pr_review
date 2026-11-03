"""jobs table, unique review runs, run reliability columns

Revision ID: b72d5f1c8e49
Revises: a41c7e92d0b3
Create Date: 2026-11-03 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b72d5f1c8e49"
down_revision: str | Sequence[str] | None = "a41c7e92d0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rows that are not the newest run for their (repo, pr_number, head_sha). Before Phase 6 a reopened
# PR could create a second run for the same commit; the unique constraint below needs one per key.
_OLDER_DUPLICATES = """
    SELECT id FROM (
        SELECT id, row_number() OVER (
            PARTITION BY repo, pr_number, head_sha ORDER BY started_at DESC, id DESC
        ) AS rn
        FROM review_runs
    ) ranked WHERE rn > 1
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("review_runs", sa.Column("failure_reason", sa.String(), nullable=True))
    op.add_column("review_runs", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "review_runs",
        sa.Column(
            "failure_comment_posted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    # Keep the newest run of each key (and its findings); drop older duplicates.
    op.execute(f"DELETE FROM findings WHERE run_id IN ({_OLDER_DUPLICATES})")  # noqa: S608  (constant SQL, no input)
    op.execute(f"DELETE FROM review_runs WHERE id IN ({_OLDER_DUPLICATES})")  # noqa: S608
    op.create_unique_constraint(
        "uq_review_runs_pr_head", "review_runs", ["repo", "pr_number", "head_sha"]
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_table("jobs")
    op.drop_constraint("uq_review_runs_pr_head", "review_runs", type_="unique")
    op.drop_column("review_runs", "failure_comment_posted")
    op.drop_column("review_runs", "posted_at")
    op.drop_column("review_runs", "failure_reason")
