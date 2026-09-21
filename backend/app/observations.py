"""Persist unverified page observations, separately from both trading ledgers."""

import json
from uuid import uuid4

from sqlalchemy import Column, Integer, String, Table, Text, select

from app.database import Base

observations = Table(
    "account_observations",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("captured_at", Integer, nullable=False),
    Column("payload", Text, nullable=False),
)


def save(engine, payload):
    record_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            observations.insert().values(
                id=record_id,
                captured_at=payload["captured_at"],
                payload=json.dumps(payload, ensure_ascii=False),
            )
        )
    return {"id": record_id, **payload}


def recent(engine):
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(observations)
                .order_by(observations.c.captured_at.desc(), observations.c.id)
                .limit(20)
            )
            .mappings()
            .all()
        )
    return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]
