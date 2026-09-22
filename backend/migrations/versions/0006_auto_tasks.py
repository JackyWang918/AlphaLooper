"""Persistent automatic live trading tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auto_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("active", sa.Integer(), nullable=True, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("auto_tasks")
