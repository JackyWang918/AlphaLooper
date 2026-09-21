"""Durable simulation ledger. No browser or exchange execution capability."""

import json
import time
from decimal import Decimal as D
from decimal import localcontext

from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    Text,
    select,
)

from app.database import Base
from app.market import Snapshot, validate_snapshot
from app.strategy import Config, Event, State, advance, exposure

tasks = Table(
    "research_tasks",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    Column("url", Text, nullable=False),
    Column("config", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("version", Integer, nullable=False),
)
orders = Table(
    "research_orders",
    Base.metadata,
    Column("task_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("payload", Text, nullable=False),
    ForeignKeyConstraint(["task_id"], ["research_tasks.id"]),
)
events = Table(
    "research_events",
    Base.metadata,
    Column("task_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("payload", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("clock", Integer, nullable=False),
    ForeignKeyConstraint(["task_id"], ["research_tasks.id"]),
)
fills = Table(
    "research_fills",
    Base.metadata,
    Column("task_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("order_id", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("payload", Text, nullable=False),
    ForeignKeyConstraint(
        ["task_id", "order_id"], ["research_orders.task_id", "research_orders.id"]
    ),
    ForeignKeyConstraint(
        ["task_id", "id"], ["research_events.task_id", "research_events.id"]
    ),
)


def dump(value):
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)


class Conflict(ValueError):
    pass


def get_task(connection, task_id):
    row = (
        connection.execute(select(tasks).where(tasks.c.id == task_id))
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError("模拟任务不存在。")
    return row


def statistics(records, state, snapshot, config):
    with localcontext() as ctx:
        ctx.prec = 40
        bought = sold = fees = inventory = cost = realized = D(0)
        for fill in records:
            quantity, gross, fee = (D(fill[k]) for k in ("quantity", "gross", "fee"))
            fees += fee
            if fill["side"] == "buy":
                bought += gross
                inventory += quantity
                cost += gross + fee
            else:
                allocated = (
                    cost if quantity == inventory else cost * quantity / inventory
                )
                realized += gross - fee - allocated
                cost -= allocated
                inventory -= quantity
                sold += gross
        fresh = True
        try:
            validate_snapshot(snapshot, int(time.time() * 1000))
        except ValueError:
            fresh = False
        mark = exposure(state, snapshot, config)
        unrealized = (
            D(0)
            if inventory == 0
            else (mark["exit_net"] - cost if fresh and mark["covered"] else None)
        )
        return {
            "buy_total": bought,
            "sell_total": sold,
            "fees": fees,
            "inventory": inventory,
            "remaining_cost": cost,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized if unrealized is not None else None,
            "estimated_points": bought * config.points_per_u,
            "target_points": config.target_points,
            "remaining_buy_amount": max(
                D(0), config.target_points / config.points_per_u - bought
            ),
            "session_loss": state.session_loss,
            "valuation_fresh": fresh,
        }


def detail(connection, task_id):
    row = get_task(connection, task_id)
    state = State.model_validate_json(row["state"])
    config = Config.model_validate_json(row["config"])
    snapshot = Snapshot.model_validate_json(row["snapshot"])
    trade_rows = (
        connection.execute(
            select(fills).where(fills.c.task_id == task_id).order_by(fills.c.version)
        )
        .mappings()
        .all()
    )
    trades = [
        dict(id=r["id"], order_id=r["order_id"], **json.loads(r["payload"]))
        for r in trade_rows
    ]
    order_rows = (
        connection.execute(select(orders).where(orders.c.task_id == task_id))
        .mappings()
        .all()
    )
    history = (
        connection.execute(
            select(events)
            .where(events.c.task_id == task_id)
            .order_by(events.c.version.desc())
        )
        .mappings()
        .all()
    )
    return {
        "id": task_id,
        "mode": "simulation",
        "url": row["url"],
        "created_at": row["created_at"],
        "version": row["version"],
        "config": config.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "snapshot": snapshot.model_dump(mode="json"),
        "orders": sorted(
            [dict(status=r["status"], **json.loads(r["payload"])) for r in order_rows],
            key=lambda order: (order["placed_at"], order["id"]),
        ),
        "fills": trades,
        "events": [dict(r) for r in history],
        "summary": statistics(trades, state, snapshot, config),
        "simulation_only": True,
    }


def save_order(connection, task_id, order, status):
    where = (orders.c.task_id == task_id) & (orders.c.id == order.id)
    values = {"payload": order.model_dump_json(), "status": status}
    if connection.execute(select(orders.c.id).where(where)).first():
        connection.execute(orders.update().where(where).values(**values))
    else:
        connection.execute(
            orders.insert().values(task_id=task_id, id=order.id, **values)
        )


def record_event(
    connection, task_id, expected_version, event: Event, snapshot: Snapshot
):
    row = get_task(connection, task_id)
    prior = connection.execute(
        select(events.c.payload).where(
            (events.c.task_id == task_id) & (events.c.id == event.id)
        )
    ).scalar_one_or_none()
    if prior is not None:
        if prior != event.model_dump_json():
            raise Conflict("事件编号已用于不同内容，拒绝重复记账。")
        return detail(connection, task_id)
    if row["version"] != expected_version:
        raise Conflict("任务已被其他页面更新，请重新载入记录后继续。")
    validate_snapshot(snapshot, int(time.time() * 1000))
    original = Snapshot.model_validate_json(row["snapshot"])
    if any(
        getattr(snapshot, key) != getattr(original, key)
        for key in ("symbol", "chain", "address", "quote", "token")
    ):
        raise ValueError("任务不能中途更换币种或计价币。")
    config = Config.model_validate_json(row["config"])
    before = State.model_validate_json(row["state"])
    # The database event key is the persistent deduplication authority.
    before.event_ids = []
    after = advance(before, event, snapshot, config)
    after.event_ids = []
    version = row["version"] + 1
    connection.execute(
        events.insert().values(
            task_id=task_id,
            id=event.id,
            version=version,
            payload=event.model_dump_json(),
            message=after.message,
            clock=after.clock,
        )
    )
    if before.order:
        old = before.order.model_copy(deep=True)
        if event.kind == "fill":
            old.filled += event.quantity
        if after.order and old.id == after.order.id:
            old = after.order
            status = (
                "cancel_requested"
                if old.cancel_requested
                else "partial"
                if old.filled
                else "open"
            )
        else:
            status = "filled" if old.filled == old.quantity else "cancelled"
        save_order(connection, task_id, old, status)
        if event.kind == "fill":
            gross = event.quantity * event.price
            connection.execute(
                fills.insert().values(
                    task_id=task_id,
                    id=event.id,
                    order_id=old.id,
                    version=version,
                    payload=dump(
                        {
                            "side": old.side,
                            "quantity": event.quantity,
                            "price": event.price,
                            "gross": gross,
                            "fee": gross * config.fee_bps / 10000,
                            "fee_currency": snapshot.quote,
                            "clock": after.clock,
                            "recorded_at": int(time.time()),
                            "source": "manual_simulation",
                        }
                    ),
                )
            )
    if after.order and (not before.order or after.order.id != before.order.id):
        save_order(connection, task_id, after.order, "open")
    connection.execute(
        tasks.update()
        .where(tasks.c.id == task_id)
        .values(
            state=after.model_dump_json(),
            snapshot=snapshot.model_dump_json(),
            version=version,
            updated_at=int(time.time()),
        )
    )
    return detail(connection, task_id)
