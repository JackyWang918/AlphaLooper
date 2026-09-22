"""Append-only strategy evidence; all reads are independent of trading actions."""

import json

from sqlalchemy import Column, Index, Integer, String, Table, Text, select

from app.database import Base

decisions = Table(
    "auto_decisions",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("payload", Text, nullable=False),
)
Index("ix_auto_decisions_task_id_id", decisions.c.task_id, decisions.c.id)


def append(connection, task_id, kind, timestamp, payload):
    connection.execute(
        decisions.insert().values(
            task_id=task_id,
            kind=kind,
            created_at=int(timestamp * 1000),
            payload=json.dumps(payload, ensure_ascii=False, default=str),
        )
    )


def read(engine, task_id, before=None, kind=None, limit=50):
    query = select(decisions).where(decisions.c.task_id == task_id)
    if before is not None:
        query = query.where(decisions.c.id < before)
    if kind:
        query = query.where(decisions.c.kind == kind)
    with engine.connect() as c:
        rows = (
            c.execute(query.order_by(decisions.c.id.desc()).limit(limit + 1))
            .mappings()
            .all()
        )
    items = [
        {
            "id": r["id"],
            "time": r["created_at"],
            "kind": r["kind"],
            **json.loads(r["payload"]),
        }
        for r in rows[:limit]
    ]
    return {
        "items": items,
        "next_before": items[-1]["id"] if len(rows) > limit else None,
    }


def export(engine, task_id, kind=None):
    before = None
    while True:
        page = read(engine, task_id, before=before, kind=kind, limit=200)
        for item in page["items"]:
            yield json.dumps(item, ensure_ascii=False) + "\n"
        before = page["next_before"]
        if before is None:
            break
