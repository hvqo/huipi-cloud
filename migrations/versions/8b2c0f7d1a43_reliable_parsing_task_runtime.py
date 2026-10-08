"""Add durable parsing task state and lease fields.

Revision ID: 8b2c0f7d1a43
Revises: fd1e8d651902
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8b2c0f7d1a43"
down_revision: Union[str, Sequence[str], None] = "fd1e8d651902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add task execution metadata without rewriting existing pending tasks."""
    op.add_column(
        "parsing_tasks",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "parsing_tasks",
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column(
        "parsing_tasks",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("parsing_tasks", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "parsing_tasks",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "parsing_tasks",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "parsing_tasks",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "parsing_tasks",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "parsing_tasks",
        sa.Column("last_error_message", sa.String(length=240), nullable=True),
    )

    op.drop_index("ix_parsing_tasks_status_created_at", table_name="parsing_tasks")
    op.drop_constraint("ck_parsing_tasks_status", "parsing_tasks", type_="check")
    op.create_check_constraint(
        "ck_parsing_tasks_status",
        "parsing_tasks",
        "status IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_parsing_tasks_attempt_count",
        "parsing_tasks",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_parsing_tasks_max_attempts",
        "parsing_tasks",
        "max_attempts > 0 AND attempt_count <= max_attempts",
    )
    op.create_check_constraint(
        "ck_parsing_tasks_lease",
        "parsing_tasks",
        "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_parsing_tasks_next_run",
        "parsing_tasks",
        "(status = 'retry_wait' AND next_run_at IS NOT NULL) "
        "OR (status <> 'retry_wait' AND next_run_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_parsing_tasks_finished_at",
        "parsing_tasks",
        "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL) "
        "OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)",
    )
    op.create_index(
        "ix_parsing_tasks_status_next_run_created_at",
        "parsing_tasks",
        ["status", "next_run_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_parsing_tasks_status_lease_expires_at",
        "parsing_tasks",
        ["status", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Refuse to discard execution state after a worker has processed tasks."""
    bind = op.get_bind()
    non_pending_count = bind.scalar(
        sa.text("SELECT count(*) FROM parsing_tasks WHERE status <> 'pending'")
    )
    if non_pending_count:
        raise RuntimeError("Cannot downgrade parsing task runtime while non-pending tasks exist")

    op.drop_index("ix_parsing_tasks_status_lease_expires_at", table_name="parsing_tasks")
    op.drop_index("ix_parsing_tasks_status_next_run_created_at", table_name="parsing_tasks")
    for constraint_name in (
        "ck_parsing_tasks_finished_at",
        "ck_parsing_tasks_next_run",
        "ck_parsing_tasks_lease",
        "ck_parsing_tasks_max_attempts",
        "ck_parsing_tasks_attempt_count",
        "ck_parsing_tasks_status",
    ):
        op.drop_constraint(constraint_name, "parsing_tasks", type_="check")
    for column_name in (
        "last_error_message",
        "last_error_code",
        "finished_at",
        "started_at",
        "lease_expires_at",
        "lease_token",
        "next_run_at",
        "max_attempts",
        "attempt_count",
    ):
        op.drop_column("parsing_tasks", column_name)
    op.create_check_constraint(
        "ck_parsing_tasks_status",
        "parsing_tasks",
        "status = 'pending'",
    )
    op.create_index(
        "ix_parsing_tasks_status_created_at",
        "parsing_tasks",
        ["status", "created_at"],
        unique=False,
    )
