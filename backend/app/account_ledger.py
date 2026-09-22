"""Legacy table metadata only; platform history accounting has been removed."""

from sqlalchemy import Column, String, Table, Text

from app.database import Base

orders = Table(
    "account_orders",
    Base.metadata,
    Column("book", String, primary_key=True),
    Column("order_id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)
