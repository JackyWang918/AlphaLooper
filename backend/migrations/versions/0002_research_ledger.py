"""Persist simulation tasks, orders, events and fills separately from live trading."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "research_orders",
        sa.Column("task_id", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"]),
    )
    op.create_table(
        "research_events",
        sa.Column("task_id", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("clock", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"]),
    )
    op.create_table(
        "research_fills",
        sa.Column("task_id", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "order_id"], ["research_orders.task_id", "research_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "id"], ["research_events.task_id", "research_events.id"]
        ),
    )


def downgrade():
    for name in (
        "research_fills",
        "research_events",
        "research_orders",
        "research_tasks",
    ):
        op.drop_table(name)
