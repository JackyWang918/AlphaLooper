"""Persist order summaries separately from simulation and raw observations."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "account_orders",
        sa.Column("book", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("account_orders")
