from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.live_orders import LiveOrders, SubmitOrder, intents
from tests.test_alpha import PAYLOAD


def wallet(cash="100", base="0"):
    return {
        "quote_available": cash,
        "base_available": base,
        "quote_total": cash,
        "base_total": base,
    }


@pytest.fixture
def service(monkeypatch):
    engine = create_engine("sqlite://")
    intents.create(engine)
    instance = LiveOrders(engine, Mock())
    instance.enabled = True
    now = [1000]
    monkeypatch.setattr("app.live_orders.time.time", lambda: now[0])
    instance.now = now
    yield instance
    engine.dispose()


def request():
    return SubmitOrder(**PAYLOAD, request_id=uuid4())


def submitted(service, *, fails=False):
    service.browser.execute.side_effect = [
        {"ok": True, "balances": wallet()},
        TimeoutError("timeout")
        if fails
        else {"ok": True, "confirmation_clicked": True},
    ]
    body = request()
    service.submit(body)
    service.browser.execute.side_effect = None
    return body


def observe(service, balance=None, pending=False):
    service.browser.execute.return_value = {
        "ok": True,
        "pending": pending,
        "balances": balance or wallet(),
    }
    return service.check()


def test_disabled_and_budget_no_browser(service):
    service.enabled = False
    with pytest.raises(ValueError, match="开启"):
        service.submit(request())
    service.enabled = True
    with pytest.raises(ValueError, match="50"):
        service.submit(SubmitOrder(**PAYLOAD, request_id=uuid4(), quote_amount="51"))
    service.browser.execute.assert_not_called()


def test_dedup_single_active_and_stable_balance_settlement(service):
    body = submitted(service)
    assert service.submit(body)["state"] == "waiting"
    with pytest.raises(ValueError, match="上一笔"):
        service.submit(request())
    assert observe(service, pending=True)["active"]
    assert observe(service, wallet("99.1", "0.9999"))["state"] == "settling"
    assert observe(service, wallet("99.1", "0.9999"))["active"]  # no time elapsed
    service.now[0] += 5
    result = observe(service, wallet("99.1", "0.9999"))
    assert not result["active"]
    assert result["result"]["cash_delta"] == "-0.9"
    assert result["result"]["quantity"] == "0.9999"
    assert service.submit(body)["state"] == "completed"
    assert service.check() is None
    assert [c.args[0] for c in service.browser.execute.call_args_list].count(
        "live_submit"
    ) == 1


def test_balance_updates_reset_settlement_candidate(service):
    submitted(service)
    observe(service, wallet("99.5", "0.5"))
    service.now[0] += 5
    assert observe(service, wallet("99.1", "0.9999"))["active"]
    service.now[0] += 5
    assert not observe(service, wallet("99.1", "0.9999"))["active"]


def test_unknown_empty_unchanged_does_not_invent_non_submission(service):
    body = submitted(service, fails=True)
    assert observe(service)["state"] == "submission_unknown"
    service.now[0] += 60
    assert observe(service)["active"]
    assert service.submit(body)["state"] == "submission_unknown"
    assert service.resolve_unsubmitted(body.request_id)["state"] == "not_submitted"


def test_unknown_can_reconcile_real_balance_changes_without_retry(service):
    submitted(service, fails=True)
    observe(service, wallet("99.1", "1"))
    service.now[0] += 5
    assert not observe(service, wallet("99.1", "1"))["active"]
    assert [c.args[0] for c in service.browser.execute.call_args_list].count(
        "live_submit"
    ) == 1


def test_changed_balance_prevents_manual_unsubmitted_release(service):
    body = submitted(service, fails=True)
    service.browser.execute.return_value = {
        "ok": True,
        "pending": False,
        "balances": wallet("99", "1"),
    }
    with pytest.raises(ValueError, match="余额"):
        service.resolve_unsubmitted(body.request_id)
    assert service.get()["active"]


def test_restart_does_not_replay_and_preserves_first_error(service):
    submitted(service, fails=True)
    restarted = LiveOrders(service.engine, Mock())
    restarted.browser.execute.return_value = {"ok": False, "message": "页面加载中"}
    record = restarted.check()
    assert record["submission_error"] == "timeout"
    assert record["last_check_error"] == "页面加载中"
    assert not restarted.enabled
    assert [c.args[0] for c in restarted.browser.execute.call_args_list] == [
        "live_inspect"
    ]


def test_wrong_direction_delta_does_not_complete(service):
    submitted(service)
    observe(service, wallet("101", "1"))
    service.now[0] += 5
    result = observe(service, wallet("101", "1"))
    assert result["active"] and "方向" in result["last_check_error"]


def test_api_guard_and_history_endpoints_removed(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    from app import database
    from app.main import app

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "live.db")
    command.upgrade(Config("alembic.ini"), "head")
    headers = {"X-AlphaLooper-Client": "local-ui"}
    with TestClient(app) as client:
        fake = Mock(return_value={"ok": True, "balances": wallet()})
        monkeypatch.setattr(app.state.browser, "execute", fake)
        assert (
            client.post("/api/browser/order-readiness", json=PAYLOAD).status_code == 403
        )
        assert (
            client.post(
                "/api/browser/order-readiness", json=PAYLOAD, headers=headers
            ).json()["balances"]
            == wallet()
        )
        assert client.get("/api/account/orders").status_code == 404
        assert (
            client.post(
                "/api/account/orders/read", json={}, headers=headers
            ).status_code
            == 404
        )
        fake.assert_called_once()


def test_live_sell_forces_platform_percentage_but_fill_only_does_not():
    from app.browser.schemas import FillForm

    assert SubmitOrder(**{**PAYLOAD, "side": "sell"}, request_id=uuid4()).sell_all
    assert not FillForm(**{**PAYLOAD, "side": "sell"}).sell_all
