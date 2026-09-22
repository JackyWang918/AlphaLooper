from alembic import context

from app import (
    account_ledger,  # noqa: F401
    automatic,  # noqa: F401
    ledger,  # noqa: F401 -- register ledger tables in migration metadata
    live_orders,  # noqa: F401
    observations,  # noqa: F401 -- register read-only observations
)
from app.database import Base, make_engine

target_metadata = Base.metadata


def run_migrations():
    engine = make_engine()
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
