"""Legacy snapshot table metadata; preserved for existing databases only."""

from sqlalchemy import Column, Integer, String, Table, Text

from app.database import Base

observations = Table(
    "account_observations",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("captured_at", Integer, nullable=False),
    Column("payload", Text, nullable=False),
)
