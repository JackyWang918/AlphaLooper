from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app import account_ledger
from app.automatic import Automatic, StartTask, tasks
from app.decision_log import decisions
from app.live_orders import LiveOrders, SubmitOrder, intents


class Browser:
    def __init__(self):
        self.calls = []
        self.balance = D(0)
        self.pending = None
        self.latest = None
        self.progress = {"quantity": "0", "gross": "0"}
        self.serial = 0
        self.cancel_fails = False

    def execute(self, action, payload=None):
        self.calls.append(action)
        if action in {"order_readiness", "live_prepare"}:
            return {
                "ok": True,
                "baseline_id": self.latest["order_id"] if self.latest else None,
            }
        if action == "live_balance":
            return {"ok": True, "available": str(self.balance)}
        if action == "live_submit":
            assert self.pending is None
            self.pending = payload
            self.progress = {"quantity": "0", "gross": "0"}
            return {
                "ok": True,
                "confirmation_clicked": True,
                "confirmation": {"fee_currency": "USDT"},
            }
        if action == "live_progress":
            return {
                "ok": True,
                "pending": bool(self.pending),
                "progress": self.progress,
                "order": self.latest,
            }
        if action == "live_cancel":
            if self.cancel_fails:
                raise TimeoutError("撤单超时")
            self.finish(self.progress["quantity"], status="已撤销")
            return {"ok": True, "cancel_clicked": True}
        raise AssertionError(action)

    def finish(self, quantity=None, status="已成交"):
        r = self.pending
        q = D(quantity if quantity is not None else r["quantity"])
        self.balance += q if r["side"] == "buy" else -q
        self.serial += 1
        self.latest = {
            "order_id": str(self.serial),
            "created_at": f"2026-09-22 12:00:{self.serial:02d}",
            "symbol": "TEST",
            "quote": "USDT",
            "chain": "bsc",
            "address": "0x1",
            "side": "买入" if r["side"] == "buy" else "卖出",
            "quantity": str(q),
            "gross": str(q * D(r["price"])),
            "requested_quantity": r["quantity"],
            "limit_price": r["price"],
            "status": status,
            "captured_at": 100,
        }
        self.pending = None


@pytest.fixture
def rig(market, monkeypatch):
    engine = create_engine("sqlite://")
    for table in (intents, tasks, account_ledger.orders, decisions):
        table.create(engine)
    browser = Browser()
    now = [market.fetched_at / 1000]
    monkeypatch.setattr("app.live_orders.time.time", lambda: now[0])
    live = LiveOrders(engine, browser)

    def snapshot(*args):
        m = market.model_copy(deep=True)
        delta = int(now[0] * 1000) - m.fetched_at
        m.fetched_at += delta
        m.book_time += delta
        for candle in m.candles:
            candle.time += delta
            candle.close_time += delta
        return m

    auto = Automatic(engine, live, SimpleNamespace(snapshot=snapshot), lambda: now[0])
    body = StartTask(
        request_id=uuid4(),
        url="https://www.binance.com/zh-CN/alpha/bsc/0x1",
        expected_symbol="TEST",
        config={"window": 3},
    )
    auto.create(body)
    yield SimpleNamespace(
        auto=auto, live=live, browser=browser, now=now, body=body, market=market
    )
    engine.dispose()


def tick(r, seconds=0):
    r.now[0] += seconds
    r.auto.tick()
    return r.auto.get()


def test_buy_sell_next_round_with_real_results_and_exactly_once(rig):
    r = rig
    t = tick(r)
    assert t["pending"]["side"] == "buy"
    r.browser.finish()
    t = tick(r, 60)
    assert t["pending"]["side"] == "sell"
    bought = t["buy_total"]
    r.browser.finish()
    t = tick(r, 60)
    assert t["rounds"] == 1 and t["pending"]["side"] == "buy"
    assert t["buy_total"] == bought
    assert r.browser.calls.count("live_submit") == 3
    tick(r, 5)
    assert r.browser.calls.count("live_submit") == 3


def test_partial_buy_timeout_cancel_final_then_only_sell_partial(rig):
    r = rig
    tick(r)
    price = r.browser.pending["price"]
    r.browser.progress = {"quantity": "1", "gross": str(D(price))}
    t = tick(r, 300)
    assert r.browser.calls.count("live_cancel") == 1
    assert r.browser.calls.count("live_submit") == 1
    first = t["first_buy_at"]
    t = tick(r, 15)
    assert t["pending"]["side"] == "sell" and D(t["pending"]["quantity"]) == 1
    assert t["first_buy_at"] == first


def test_empty_buy_cancel_requotes_new_market(rig):
    r = rig
    old = tick(r)["pending"]["price"]
    tick(r, 300)
    for candle in r.market.candles:
        candle.quote_volume += D("0.1")
    t = tick(r, 15)
    assert t["pending"]["side"] == "buy"
    assert D(t["pending"]["price"]) > D(old)


def test_cancel_timeout_never_replayed_after_resume(rig):
    r = rig
    tick(r)
    r.browser.cancel_fails = True
    tick(r, 300)
    assert not r.auto.running
    r.auto.control(r.body.request_id, "resume")
    tick(r, 60)
    assert not r.auto.running
    assert r.browser.calls.count("live_cancel") == 1
    assert r.browser.calls.count("live_submit") == 1


def test_restarted_task_only_reads_then_resume_sells(rig):
    r = rig
    tick(r)
    r.browser.finish()
    restarted = Automatic(r.auto.engine, r.live, r.auto.market, r.auto.clock)
    restarted.tick()
    assert not restarted.running
    assert D(restarted.get()["inventory"]) > 0
    assert r.browser.calls.count("live_submit") == 1
    restarted.control(r.body.request_id, "resume")
    restarted.tick()
    assert restarted.get()["pending"]["side"] == "sell"
    assert r.browser.calls.count("live_submit") == 2


def test_start_retry_does_not_resume_and_manual_submit_is_blocked(rig):
    r = rig
    r.auto.control(r.body.request_id, "pause")
    assert r.auto.create(r.body)["id"] == str(r.body.request_id)
    assert not r.auto.running
    with pytest.raises(ValueError, match="自动任务"):
        r.live.submit(
            SubmitOrder(**r.auto.probe(r.body.model_dump()), request_id=uuid4())
        )


def test_target_stops_buying_exits_and_completes(rig):
    r = rig
    t = r.auto.get()
    t["request"]["config"]["target_points"] = "4"
    r.auto.save(t)
    tick(r)
    r.browser.finish()
    t = tick(r, 60)
    assert t["stop_buying"] and t["exiting"] and t["pending"]["side"] == "sell"
    assert D(t["pending"]["price"]) == D("9.9")
    r.browser.finish()
    tick(r, 15)
    assert r.auto.get() is None and not r.auto.running
    assert r.browser.calls.count("live_submit") == 2


def test_pause_does_not_cancel_but_finish_cancels_buy(rig):
    r = rig
    tick(r)
    r.auto.control(r.body.request_id, "pause")
    tick(r, 301)
    assert "live_cancel" not in r.browser.calls
    r.auto.control(r.body.request_id, "finish")
    tick(r)
    assert r.browser.calls.count("live_cancel") == 1
    tick(r, 15)
    assert r.auto.get() is None


def test_hold_timeout_keeps_first_buy_time_across_rehang(rig):
    r = rig
    tick(r)
    r.browser.finish()
    t = tick(r, 60)
    start = t["first_buy_at"]
    t = tick(r, 1740)
    assert t["exiting"] and r.browser.calls.count("live_cancel") == 1
    t = tick(r, 15)
    assert t["first_buy_at"] == start and t["pending_exit"]
    assert D(t["pending"]["price"]) == D("9.9")


def test_minute_stop_loss_and_budget_stop_new_buys(rig):
    r = rig
    tick(r)
    r.browser.finish()
    t = tick(r, 60)
    r.market.candles[-1].close = D("9.5")
    t = r.auto.get()
    t["session_loss"] = "9"
    r.auto.save(t)
    t = tick(r, 5)
    assert not t["exiting"]
    t = tick(r, 55)
    assert t["exiting"]
    assert r.browser.calls.count("live_cancel") == 1
    t = tick(r, 15)
    assert t["stop_buying"]  # mark-to-exit loss plus prior loss exceeds budget


def test_dust_is_not_reported_as_cleared(rig):
    r = rig
    tick(r)
    r.browser.finish(quantity="0.001", status="已撤销")
    t = tick(r, 60)
    assert not r.auto.running and t["active"] and D(t["inventory"]) > 0
    assert "最小" in t["message"]
    assert r.browser.calls.count("live_submit") == 1


def test_balance_mismatch_and_stale_market_stop_before_sell(rig):
    r = rig
    tick(r)
    r.browser.finish()
    r.browser.balance += 10
    t = tick(r, 60)
    assert not r.auto.running and "余额" in t["message"]
    assert r.browser.calls.count("live_submit") == 1
    r.auto.control(r.body.request_id, "resume")
    r.auto.market = SimpleNamespace(snapshot=lambda *args: r.market)
    t = tick(r, 60)
    assert not r.auto.running and "过期" in t["message"]


def test_regressive_partial_result_never_replaces_order(rig):
    r = rig
    tick(r)
    p = D(r.browser.pending["price"])
    r.browser.progress = {"quantity": "1", "gross": str(p)}
    tick(r, 60)
    r.browser.finish(quantity="0.5")
    t = tick(r, 60)
    assert not r.auto.running and "最终成交" in t["message"]
    assert r.live.get()["active"]


def test_completed_fill_consumption_survives_restart_without_double_count(rig):
    r = rig
    tick(r)
    r.auto.control(r.body.request_id, "pause")
    r.browser.finish()
    r.live.check()  # crash between live accounting and task accounting
    restarted = Automatic(r.auto.engine, r.live, r.auto.market, r.auto.clock)
    restarted.tick()
    amount = restarted.get()["buy_total"]
    restarted.tick()
    assert restarted.get()["buy_total"] == amount
    assert restarted.get()["pending"] is None


def test_late_fill_during_cancel_is_included_in_sell(rig):
    r = rig
    tick(r)
    price = D(r.browser.pending["price"])
    r.browser.progress = {"quantity": "1", "gross": str(price)}
    original = r.browser.execute

    def late(action, payload=None):
        if action == "live_cancel":
            r.browser.progress = {"quantity": "2", "gross": str(2 * price)}
        return original(action, payload)

    r.browser.execute = late
    tick(r, 300)
    t = tick(r, 15)
    assert D(t["inventory"]) == 2
    assert D(t["pending"]["quantity"]) == 2


def test_token_fee_uses_available_net_balance_and_preserves_baseline(rig):
    r = rig
    t = r.auto.get()
    t["baseline_balance"] = "7"
    r.auto.save(t)
    r.browser.balance = D(7)
    tick(r)
    record = r.live.get()
    record["confirmation"]["fee_currency"] = "TEST"
    r.live.save(record)
    r.browser.finish(quantity="1")
    r.browser.balance -= D("0.0001")
    t = tick(r, 60)
    assert D(t["inventory"]) == D("0.9999")
    assert D(t["pending"]["quantity"]) == D("0.99")
    assert D(t["cost"]) == D("9.95")


def test_prepare_expiry_stops_before_submission_and_can_resume_once(rig):
    r = rig
    original = r.browser.execute

    def slow(action, payload=None):
        result = original(action, payload)
        if action == "live_prepare":
            r.now[0] += 16
        return result

    r.browser.execute = slow
    t = tick(r)
    assert not r.auto.running and t["pending"] is None
    assert "live_submit" not in r.browser.calls
    r.browser.execute = original
    r.auto.control(r.body.request_id, "resume")
    tick(r)
    assert r.browser.calls.count("live_submit") == 1


def test_no_new_buy_when_budget_reserve_exhausted(rig):
    r = rig
    t = r.auto.get()
    t["session_loss"] = "8.1"
    r.auto.save(t)
    tick(r)
    assert r.auto.get() is None
    assert "live_submit" not in r.browser.calls


def test_no_book_does_not_trigger_exit(rig):
    r = rig
    tick(r)
    r.browser.finish()
    tick(r, 60)
    r.market.bids = r.market.asks = []
    t = tick(r, 60)
    assert not t["exiting"]
    assert t["risk"]["basis"] == "latest_closed_1m_candle"


def test_low_ask_does_not_block_model_buy(rig):
    r = rig
    r.market.asks = [(D("0.8"), D(100))]
    t = tick(r)
    assert t["estimate"]["buy_blockers"] == []
    assert t["pending"]["side"] == "buy"
    assert r.browser.calls.count("live_submit") == 1


def test_finish_before_first_tick_does_not_submit(rig):
    r = rig
    r.auto.control(r.body.request_id, "finish")
    tick(r)
    assert r.auto.get() is None
    assert "live_submit" not in r.browser.calls


def test_live_task_crossed_book_does_not_block_buy(rig):
    r = rig
    r.market.bids = [(D("10.2"), D(100))]
    t = tick(r)
    assert t["pending"]["side"] == "buy"
    assert r.browser.calls.count("live_submit") == 1


def test_auto_api_local_guard_conflict_and_migration(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    from app import database
    from app.main import app

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "auto.db")
    command.upgrade(Config("alembic.ini"), "head")
    with TestClient(app) as client:
        browser = Browser()
        monkeypatch.setattr(app.state.browser, "execute", browser.execute)
        body = StartTask(
            request_id=uuid4(),
            url="https://www.binance.com/alpha/bsc/0x1",
            expected_symbol="TEST",
        ).model_dump(mode="json")
        headers = {"X-AlphaLooper-Client": "local-ui"}
        assert client.post("/api/automatic/start", json=body).status_code == 403
        assert not browser.calls
        assert (
            client.post("/api/automatic/start", json=body, headers=headers).status_code
            == 200
        )
        control = {"task_id": body["request_id"], "action": "pause"}
        assert (
            client.post(
                "/api/automatic/control", json=control, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post("/api/automatic/start", json=body, headers=headers).status_code
            == 200
        )
        assert client.get("/api/automatic").json()["running"] is False
        log_url = f"/api/automatic/{body['request_id']}/decisions"
        page = client.get(log_url, params={"limit": 1}).json()
        assert len(page["items"]) == 1 and page["next_before"]
        older = client.get(log_url, params={"before": page["next_before"]}).json()
        assert older["items"][0]["id"] != page["items"][0]["id"]
        exported = client.get(log_url + "/export", params={"kind": "control"})
        import json

        lines = [json.loads(line) for line in exported.text.splitlines()]
        assert exported.status_code == 200 and lines[0]["type"] == "task"
        assert (
            len(lines) == 3
        )  # task metadata, start, pause; duplicate start logs nothing
        assert client.get(log_url, params={"limit": 10000}).status_code == 422
        assert client.get(f"/api/automatic/{uuid4()}/decisions").status_code == 404
        assert browser.calls == ["order_readiness", "live_balance"]
        assert (
            client.post(
                "/api/browser/open",
                json={"url": body["url"].replace("0x1", "0x2")},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/live/enabled", json={"enabled": True}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/automatic/control",
                json={**control, "task_id": str(uuid4())},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/automatic/start",
                json={**body, "request_id": str(uuid4()), "config": {"amount": "51"}},
                headers=headers,
            ).status_code
            == 422
        )
