"""Durable single live order intent."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "live_orders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("active", sa.Integer(), nullable=True, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("live_orders")
