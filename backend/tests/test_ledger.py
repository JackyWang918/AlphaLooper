import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from test_research import event

from app import database, ledger
from app.main import app
from app.market import Snapshot
from app.strategy import State, advance

HEADERS = {"X-AlphaLooper-Client": "local-ui"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "ledger.db")
    command.upgrade(AlembicConfig("alembic.ini"), "head")
    with TestClient(app) as client:
        yield client
        assert app.state.browser._process is None


def fresh(m):
    m.fetched_at = m.book_time = int(time.time() * 1000)
    return m.model_dump(mode="json")


def create(client, m, c):
    r = client.post(
        "/api/research/tasks",
        headers=HEADERS,
        json={
            "id": str(uuid4()),
            "url": "https://www.binance.com/zh-CN/alpha/bsc/0x1",
            "quote": "USDT",
            "config": c.model_dump(mode="json"),
            "snapshot": fresh(m),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def post_event(client, task, m, e):
    return client.post(
        f"/api/research/tasks/{task['id']}/events",
        headers=HEADERS,
        json={
            "expected_version": task["version"],
            "event": e.model_dump(mode="json"),
            "snapshot": fresh(m),
        },
    )


def step(client, task, m, e):
    r = post_event(client, task, m, e)
    assert r.status_code == 200, r.text
    return r.json()


def test_accounting_dedup_recovery_and_atomic_rejection(client, market, config):
    config.fee_bps = D(1)
    task = step(client, create(client, market, config), market, event())
    order = task["state"]["order"]
    fill = event("fill", quantity="1", price=order["price"])
    previous = task
    task = step(client, task, market, fill)
    gross = D(order["price"])
    assert D(task["summary"]["buy_total"]) == gross
    assert D(task["summary"]["estimated_points"]) == gross * 4
    assert D(task["summary"]["fees"]) == gross / 10000
    duplicate = step(client, previous, market, fill)
    assert duplicate["version"] == task["version"] and len(duplicate["fills"]) == 1
    changed = fill.model_copy(update={"quantity": D(2)})
    assert post_event(client, task, market, changed).status_code == 409
    assert post_event(client, previous, market, event()).status_code == 409
    assert (
        post_event(
            client, task, market, event("fill", quantity="999", price="1")
        ).status_code
        == 422
    )
    assert (
        client.get(f"/api/research/tasks/{task['id']}").json()["version"]
        == task["version"]
    )
    task = step(client, task, market, event(advance_seconds=300))
    task = step(client, task, market, event("cancel_confirm"))
    assert (
        next(o for o in task["orders"] if o["id"] == order["id"])["status"]
        == "cancelled"
    )
    sell = task["state"]["order"]["price"]
    task = step(client, task, market, event("fill", quantity="0.4", price=sell))
    expected = D(sell) * D("0.4") * D("0.9999") - gross * D("0.4") * D("1.0001")
    assert D(task["summary"]["realized_pnl"]) == expected
    assert D(task["summary"]["inventory"]) == D("0.6")
    assert D(task["summary"]["remaining_cost"]) == gross * D("0.6") * D("1.0001")
    task = step(client, task, market, event("fill", quantity="0.6", price=sell))
    assert D(task["summary"]["inventory"]) == 0
    assert D(task["summary"]["unrealized_pnl"]) == 0
    assert D(task["summary"]["realized_pnl"]) == D(sell) * D("0.9999") - gross * D(
        "1.0001"
    )
    assert D(task["summary"]["estimated_points"]) == gross * 4
    with TestClient(app) as restarted:
        restored = restarted.get(f"/api/research/tasks/{task['id']}").json()
        assert restored["fills"] == task["fills"]
        assert restored["summary"] == task["summary"]


def test_concurrent_events_only_one_version_wins(client, market, config):
    task = step(client, create(client, market, config), market, event())
    price = task["state"]["order"]["price"]
    snapshot = fresh(market)
    payloads = [
        {
            "expected_version": task["version"],
            "snapshot": snapshot,
            "event": event("fill", quantity="1", price=price).model_dump(mode="json"),
        }
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda body: client.post(
                    f"/api/research/tasks/{task['id']}/events",
                    headers=HEADERS,
                    json=body,
                ),
                payloads,
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(client.get(f"/api/research/tasks/{task['id']}").json()["fills"]) == 1


def test_identity_and_stale_valuation(client, market, config):
    task = step(client, create(client, market, config), market, event())
    task = step(
        client,
        task,
        market,
        event("fill", quantity="1", price=task["state"]["order"]["price"]),
    )
    market.address = "0x2"
    assert post_event(client, task, market, event()).status_code == 422
    with app.state.engine.begin() as connection:
        row = ledger.get_task(connection, task["id"])
        m = Snapshot.model_validate_json(row["snapshot"])
        m.book_time = 1
        connection.execute(
            ledger.tasks.update()
            .where(ledger.tasks.c.id == task["id"])
            .values(snapshot=m.model_dump_json())
        )
    result = client.get(f"/api/research/tasks/{task['id']}").json()
    assert result["summary"]["unrealized_pnl"] is None
    assert result["summary"]["total_pnl"] is None
    assert result["summary"]["realized_pnl"] == "0"


def test_hold_timeout_survives_partial_fill_and_cancel(market, config):
    config.wait_seconds = 3600
    s = advance(State(), event(), market, config)
    s = advance(
        s, event("fill", quantity="1", price=str(s.order.price)), market, config
    )
    first = s.first_buy_at
    s = advance(s, event(advance_seconds=1799), market, config)
    assert not s.exiting
    s = advance(
        s,
        event("fill", advance_seconds=1, quantity="1", price=str(s.order.price)),
        market,
        config,
    )
    assert s.first_buy_at == first and s.exiting and s.order.cancel_requested
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.order.side == "sell" and s.order.price == market.bids[5][0]
    assert s.first_buy_at == first


def test_goal_late_fill_then_exit_and_no_new_buys(market, config):
    s = advance(State(), event(), market, config)
    price = s.order.price
    s.buy_total = config.target_points / config.points_per_u - price
    s = advance(s, event("fill", quantity="1", price=str(price)), market, config)
    assert s.target_reached and s.exiting and s.order.cancel_requested
    s = advance(s, event("fill", quantity="1", price=str(price)), market, config)
    assert s.inventory == 2
    s = advance(s, event("cancel_confirm"), market, config)
    s = advance(
        s, event("fill", quantity="2", price=str(s.order.price)), market, config
    )
    assert s.completed and s.first_buy_at is None
    s = advance(s, event(advance_seconds=300), market, config)
    assert s.order is None and s.target_reached


def test_final_order_precision_and_round_budget(market, config):
    config.target_points = D(1)
    s = advance(State(), event(), market, config)
    assert s.order.quantity * s.order.price >= D("0.25")
    assert (
        s.order.quantity * s.order.price * (1 + config.fee_bps / 10000) <= config.amount
    )
    s = advance(
        s,
        event("fill", quantity=str(s.order.quantity), price=str(s.order.price)),
        market,
        config,
    )
    assert s.target_reached and s.order.side == "sell"
