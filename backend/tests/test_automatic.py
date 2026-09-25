from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.automatic import Automatic, StartTask, tasks
from app.decision_log import decisions
from app.live_orders import LiveOrders, SubmitOrder, intents


class Browser:
    """Wallet with locked funds and incremental partial fills; no history API."""

    def __init__(self):
        self.calls = []
        self.balance = D(0)
        self.cash = D(100)
        self.pending = None
        self.filled = D(0)
        self.cancel_fails = False
        self.cancel_not_clicked = 0
        self.hide_frozen = False

    def wallet(self):
        quote_locked = base_locked = D(0)
        if self.pending:
            left = D(self.pending["quantity"]) - self.filled
            if self.pending["side"] == "buy":
                quote_locked = max(
                    D(0),
                    D(self.pending["quote_amount"])
                    - self.filled * D(self.pending["price"]),
                )
            else:
                base_locked = left
        return {
            "quote_available": str(self.cash - quote_locked),
            "base_available": str(self.balance - base_locked),
            "quote_total": None
            if self.hide_frozen and quote_locked
            else str(self.cash),
            "base_total": None
            if self.hide_frozen and base_locked
            else str(self.balance),
        }

    def execute(self, action, payload=None):
        self.calls.append(action)
        if action in {"order_readiness", "live_prepare"}:
            assert self.pending is None
            return {"ok": True, "balances": self.wallet()}
        if action == "live_balance":
            return {"ok": True, **self.wallet()}
        if action == "live_submit":
            assert self.pending is None
            self.pending = dict(payload)
            if payload["side"] == "sell" and payload.get("sell_all"):
                self.pending["quantity"] = str(self.balance)
            self.filled = D(0)
            return {"ok": True, "confirmation_clicked": True}
        if action in {"live_progress", "live_inspect", "live_unsubmitted"}:
            return {
                "ok": True,
                "pending": self.pending is not None,
                "balances": self.wallet(),
                "current_order": self.pending,
                "page_refreshed": bool(payload.get("refresh_before_check")),
            }
        if action == "live_cancel":
            if self.cancel_fails:
                raise TimeoutError("撤单超时")
            if self.cancel_not_clicked:
                self.cancel_not_clicked -= 1
                return {
                    "ok": True,
                    "cancel_clicked": False,
                    "retryable": True,
                    "message": "未找到撤单控件，未点击。",
                }
            self.pending = None
            return {
                "ok": True,
                "cancel_clicked": True,
                "cancel_all": True,
                "confirmation_clicked": True,
            }
        raise AssertionError(action)

    def partial(self, quantity):
        q = D(quantity) - self.filled
        assert q >= 0
        p = D(self.pending["price"])
        if self.pending["side"] == "buy":
            self.balance += q
            self.cash -= q * p
        else:
            self.balance -= q
            self.cash += q * p
        self.filled += q

    def finish(self, quantity=None):
        self.partial(self.pending["quantity"] if quantity is None else quantity)
        self.pending = None


@pytest.fixture
def rig(market, monkeypatch):
    engine = create_engine("sqlite://")
    for table in (intents, tasks, decisions):
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


def reconciled(r, seconds=60):
    initial = r.live.get()
    initial_id = initial["id"] if initial else None
    task = tick(r, seconds)
    for _ in range(3):
        current = r.live.get()
        if current is None or current["id"] != initial_id:
            return task
        task = tick(r, 5)
    return task


def test_full_cash_cycle_and_no_double_count(rig):
    r = rig
    t = tick(r)
    assert t["pending"]["quote_amount"] == "50"
    assert t["round_start_quote"] == "100"
    r.browser.finish()
    assert (
        tick(r, 60)["pending"]["side"] == "buy"
    )  # disappearance alone is insufficient
    tick(r, 5)  # refreshed balance becomes the settlement candidate
    t = tick(r, 5)
    assert t["pending"]["side"] == "sell" and t["pending"]["sell_all"]
    bought = t["buy_total"]
    r.browser.finish()
    t = reconciled(r)
    assert t["rounds"] == 1 and t["pending"] is None
    assert D(t["realized_pnl"]) == r.browser.cash - 100
    assert t["buy_total"] == bought
    assert t["next_buy_check_at"] == r.now[0] + 20
    tick(r)
    assert r.browser.calls.count("live_submit") == 2
    tick(r, 5)
    assert r.browser.calls.count("live_submit") == 2
    tick(r, 14)
    assert r.browser.calls.count("live_submit") == 2
    tick(r, 1)
    assert r.browser.calls.count("live_submit") == 3


def test_small_partial_buys_accumulate_and_only_top_up_remaining(rig):
    r = rig
    tick(r)
    r.browser.partial("1")
    t = tick(r, 300)
    first = t["first_buy_at"]
    assert r.browser.calls.count("live_cancel") == 1
    t = reconciled(r, 15)
    assert t["pending"]["side"] == "buy" and D(t["pending"]["quote_amount"]) == D(
        "40.10"
    )
    assert t["buy_rehangs"] == 1 and t["round_start_quote"] == "100"
    r.browser.finish("2")
    t = reconciled(r)
    assert t["pending"]["side"] == "sell" and D(t["cost"]) == D("29.70")
    assert t["first_buy_at"] == first


def test_exactly_half_keeps_buying_strictly_over_half_sells(rig):
    r = rig
    tick(r)
    r.browser.finish("1")
    r.browser.cash = D(75)
    r.browser.balance = D("2.52")
    t = reconciled(r)
    assert t["pending"]["side"] == "buy" and D(t["cost"]) == 25
    r.browser.finish("0.01")
    t = reconciled(r)
    assert t["pending"]["side"] == "sell"


def test_ten_rehangs_excludes_initial_and_is_bounded_after_resume(rig):
    r = rig
    tick(r)
    for rehang in range(1, 11):
        tick(r, 300)
        t = reconciled(r, 15)
        assert t["buy_rehangs"] == rehang and t["pending"]["side"] == "buy"
    tick(r, 300)
    t = reconciled(r, 15)
    assert not r.auto.running and t["pending"] is None
    assert "10 次撤单重挂" in t["message"]
    r.auto.control(r.body.request_id, "resume")
    tick(r)
    assert not r.auto.running and r.browser.calls.count("live_submit") == 11
    r.auto.control(r.body.request_id, "finish")
    tick(r)
    tick(r)
    assert r.auto.get() is None


def test_stop_denominator_is_start_cash_and_includes_locked_assets(rig):
    r = rig
    tick(r)
    r.browser.partial("3")  # 29.70 bought, remainder locked
    t = tick(r, 60)
    assert not t["exiting"]
    assert t["risk"]["denominator"] == "100"
    assert D(t["risk"]["equity"]) == D("100.00")
    # 2 U exactly is 2% of the initial 100 U, regardless of the 29.70 invested.
    risk = r.auto.risk(t, {"quote_total": "70", "base_total": "3"}, D(28) / 3)
    assert D(risk["loss_pct"]) == 2


def test_partial_sell_risk_counts_proceeds_and_locked_coins(rig):
    r = rig
    tick(r)
    r.browser.finish()
    t = reconciled(r)
    r.browser.partial("1")
    t = tick(r, 60)
    assert D(t["risk"]["equity"]) == r.browser.cash + r.browser.balance * D("9.9")
    r.market.candles[-1].close = D(8)
    t = tick(r, 60)
    assert t["exiting"] and r.browser.calls.count("live_cancel") == 1
    t = reconciled(r, 15)
    assert t["pending"]["side"] == "sell" and t["pending"]["sell_all"]
    assert D(t["pending"]["price"]) == 8


def test_missing_frozen_is_not_false_loss_or_early_cancel(rig):
    r = rig
    tick(r)
    r.browser.hide_frozen = True
    t = tick(r, 60)
    assert t["risk"]["loss"] is None and not t["exiting"]
    assert r.browser.calls.count("live_cancel") == 0
    assert r.browser.calls.count("live_submit") == 1
    tick(r, 240)
    assert r.browser.calls.count("live_cancel") == 1


def test_dust_at_two_is_finished_cash_difference_not_asset_pnl(rig):
    r = rig
    tick(r)
    r.browser.finish()
    reconciled(r)
    q = r.browser.balance - D("0.2")
    r.browser.finish(str(q))
    t = reconciled(r)
    assert t["rounds"] == 1 and t["round_stage"] == "idle"
    assert D(t["dust"]) == D("0.2")
    assert D(t["realized_pnl"]) == r.browser.cash - 100
    assert D(t["last_round"]["residual_value"]) <= 2


def test_above_two_residual_is_sold_again(rig):
    r = rig
    tick(r)
    r.browser.finish()
    reconciled(r)
    r.browser.finish(str(r.browser.balance - D("0.3")))
    t = reconciled(r)
    assert t["rounds"] == 0 and t["pending"]["side"] == "sell"


def test_existing_tokens_included_in_sell_100(rig):
    r = rig
    r.browser.balance = D(7)
    tick(r)
    r.browser.finish()
    t = reconciled(r)
    assert D(t["pending"]["quantity"]) == r.browser.balance
    assert D(t["pending"]["quantity"]) > 7


def test_hold_timeout_does_not_reset_on_buy_rehang(rig):
    r = rig
    tick(r)
    r.browser.partial("1")
    tick(r, 300)
    t = reconciled(r, 15)
    first = t["first_buy_at"]
    t = tick(r, 1500)
    assert t["exiting"]
    t = reconciled(r, 15)
    assert t["first_buy_at"] == first and t["pending"]["side"] == "sell"


def test_cancel_timeout_never_replayed(rig):
    r = rig
    tick(r)
    r.browser.cancel_fails = True
    tick(r, 300)
    assert not r.auto.running
    r.auto.control(r.body.request_id, "resume")
    tick(r, 60)
    assert not r.auto.running and r.browser.calls.count("live_cancel") == 1


def test_cancel_control_not_clicked_is_retried_after_fifteen_seconds(rig):
    r = rig
    tick(r)
    r.browser.cancel_not_clicked = 1

    t = tick(r, 300)
    assert r.auto.running
    assert "15 秒后重试" in t["message"]
    assert r.browser.calls.count("live_cancel") == 1
    record = r.live.get()
    assert record.get("cancel_requested_at") is None
    assert record["cancel_discovery_attempts"] == 1

    tick(r, 14)
    assert r.browser.calls.count("live_cancel") == 1
    tick(r, 1)
    assert r.browser.calls.count("live_cancel") == 2
    assert r.live.get()["cancel_state"] == "confirmed"


def test_restart_reconciles_but_never_submits_without_resume(rig):
    r = rig
    tick(r)
    r.browser.finish()
    restarted = Automatic(r.auto.engine, r.live, r.auto.market, r.auto.clock)
    r.auto = restarted
    reconciled(r)
    assert not restarted.running and D(restarted.get()["cost"]) == 0
    tick(r)
    assert r.browser.calls.count("live_submit") == 1
    restarted.control(r.body.request_id, "resume")
    reconciled(r, 5)
    assert D(restarted.get()["cost"]) > 0
    assert r.auto.get()["pending"]["side"] == "sell"


def test_intent_counter_survives_crash_between_submit_and_task_save(rig):
    r = rig
    tick(r)
    t = r.auto.get()
    t.update(buy_attempts=0, buy_rehangs=0, counted_attempt=None)
    r.auto.save(t)
    restarted = Automatic(r.auto.engine, r.live, r.auto.market, r.auto.clock)
    restarted.tick()
    restarted.tick()
    assert restarted.get()["buy_attempts"] == 1
    assert r.browser.calls.count("live_submit") == 1


def test_start_retry_and_manual_submit_block(rig):
    r = rig
    r.auto.control(r.body.request_id, "pause")
    r.auto.create(r.body)
    assert not r.auto.running
    with pytest.raises(ValueError, match="自动任务"):
        r.live.submit(
            SubmitOrder(**r.auto.probe(r.body.model_dump()), request_id=uuid4())
        )


def test_pause_never_cancels_finish_cancels_buy(rig):
    r = rig
    tick(r)
    r.auto.control(r.body.request_id, "pause")
    tick(r, 301)
    assert "live_cancel" not in r.browser.calls
    r.auto.control(r.body.request_id, "finish")
    tick(r)
    assert r.browser.calls.count("live_cancel") == 1
    reconciled(r, 15)
    tick(r)
    assert r.auto.get() is None


def test_force_restart_releases_local_task_but_never_touches_platform_order(rig):
    r = rig
    tick(r)
    task = r.auto.get()
    order_id = task["pending"]["request_id"]
    calls = list(r.browser.calls)

    result = r.auto.control(r.body.request_id, "force_restart")

    assert result["active"] is False
    assert result["phase"] == "restarted"
    assert r.auto.get() is None
    assert r.live.get() is None
    assert r.live.get(order_id)["state"] == "abandoned_for_restart"
    assert r.browser.calls == calls
    assert r.browser.pending is not None


def test_quote_expiry_stops_before_submission(rig):
    r = rig
    original = r.browser.execute

    def slow(action, payload=None):
        result = original(action, payload)
        if action == "live_prepare":
            r.now[0] += 16
        return result

    r.browser.execute = slow
    tick(r)
    assert not r.auto.running and "live_submit" not in r.browser.calls


def test_target_and_budget_stop_new_buys(rig):
    r = rig
    t = r.auto.get()
    t["request"]["config"]["target_points"] = "120"
    r.auto.save(t)
    tick(r)
    r.browser.finish()
    t = reconciled(r)
    # Rounding can leave a small shortfall; don't invent buy volume.
    assert t["pending"]["side"] == "sell"
    r.auto.control(r.body.request_id, "finish")
    r.browser.finish()
    reconciled(r)
    tick(r)
    assert r.auto.get() is None


def test_existing_points_reduce_required_buy_amount_and_stop_threshold(rig):
    r = rig
    t = r.auto.get()
    t["request"]["config"].update(target_points="120", current_points="80")
    r.auto.save(t)

    t = tick(r)
    assert t["round_plan"] == "10"
    assert D(t["pending"]["quote_amount"]) == 10

    # The stopping comparison includes the fixed starting points.
    t["buy_total"] = "10"
    t.update(pending=None, round_stage="idle", round_start_quote=None)
    r.browser.pending = None
    with r.auto.engine.begin() as connection:
        connection.execute(intents.delete())
    r.auto.save(t)
    tick(r)
    t = r.auto.get(str(r.body.request_id))
    assert t["active"] is False
    assert t["stop_buying"] is True


def test_existing_points_cannot_exceed_target():
    with pytest.raises(ValueError, match="不能大于目标积分"):
        StartTask(
            request_id=uuid4(),
            url="https://www.binance.com/zh-CN/alpha/bsc/0x1",
            expected_symbol="TEST",
            config={"target_points": "100", "current_points": "101"},
        )


def test_target_and_existing_points_have_no_fixed_upper_limit():
    body = StartTask(
        request_id=uuid4(),
        url="https://www.binance.com/zh-CN/alpha/bsc/0x1",
        expected_symbol="TEST",
        config={"target_points": "1000000", "current_points": "500000"},
    )
    assert body.config.target_points == D(1000000)
    assert body.config.current_points == D(500000)


def test_new_task_buy_estimate_interval_defaults_to_twenty_seconds():
    body = StartTask(
        request_id=uuid4(),
        url="https://www.binance.com/zh-CN/alpha/bsc/0x1",
        expected_symbol="TEST",
    )
    assert body.config.buy_check_seconds == 20


def test_live_task_accepts_2000_u_but_rejects_more():
    payload = {
        "request_id": uuid4(),
        "url": "https://www.binance.com/zh-CN/alpha/bsc/0x1",
        "expected_symbol": "TEST",
    }
    assert StartTask(**payload, config={"amount": "2000"}).config.amount == D(2000)
    with pytest.raises(ValueError):
        StartTask(**payload, config={"amount": "2000.01"})


def test_status_explains_next_scheduler_action(rig):
    r = rig
    assert r.auto.get()["task_start_quote"] == "100"
    tick(r)
    status = r.auto.status()
    assert status["current"]["task_start_quote"] == "100"
    assert status["current"]["schedule"]["kind"] == "minute_check"
    assert "损耗" in status["current"]["schedule"]["reason"]

    r.auto.control(r.body.request_id, "pause")
    status = r.auto.status()
    assert status["current"]["schedule"]["kind"] == "read_only_check"
    assert "不撤单、不下单" in status["current"]["schedule"]["reason"]


def test_status_recovers_task_start_quote_from_initial_decision(rig):
    r = rig
    task = r.auto.get()
    del task["task_start_quote"]
    task["balances"]["quote_available"] = "75"
    r.auto.save(task)

    status = r.auto.status()

    assert status["current"]["task_start_quote"] == "100"
    assert status["current"]["balances"]["quote_available"] == "75"


def test_legacy_cannot_resume_but_can_retire_without_fake_balances(rig):
    r = rig
    t = r.auto.get()
    del t["accounting_version"]
    r.auto.save(t)
    t = tick(r)
    assert not r.auto.running and "旧任务" in t["message"]
    r.auto.control(r.body.request_id, "retire_legacy")
    assert r.auto.get() is None and "live_submit" not in r.browser.calls


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
        assert browser.calls == ["order_readiness"]
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
                json={
                    **body,
                    "request_id": str(uuid4()),
                    "config": {"amount": "2000.01"},
                },
                headers=headers,
            ).status_code
            == 422
        )


def test_reconcile_missing_frozen_rechecks_loss_before_any_rebuy(rig):
    r = rig
    tick(r)
    r.browser.partial("1")
    r.browser.hide_frozen = True
    r.market.candles[-1].close = D(7)
    t = tick(r, 60)
    assert t["risk_reconcile"] and not t["exiting"]
    assert r.browser.calls.count("live_cancel") == 0
    tick(r, 240)
    t = reconciled(r, 15)
    assert t["exiting"] and t["pending"]["side"] == "sell"
    assert r.browser.calls.count("live_submit") == 2


def test_partially_filled_buy_over_half_cancels_before_five_minutes(rig):
    r = rig
    tick(r)
    r.browser.partial("3")
    tick(r, 60)
    assert r.browser.calls.count("live_cancel") == 1
    t = reconciled(r, 15)
    assert t["pending"]["side"] == "sell"


def test_partial_sell_dust_cancels_remaining_order_before_settlement(rig):
    r = rig
    tick(r)
    r.browser.finish()
    reconciled(r)
    r.browser.partial(str(r.browser.balance - D("0.2")))
    tick(r, 60)
    assert r.browser.calls.count("live_cancel") == 1
    t = reconciled(r, 15)
    assert t["rounds"] == 1 and t["pending"] is None
