"""Keep unverified read-only page evidence outside the simulation ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "account_observations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("captured_at", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("account_observations")
