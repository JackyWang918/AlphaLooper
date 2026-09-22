from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select

from app import account_ledger
from app.live_orders import LiveOrders, SubmitOrder, intents
from tests.test_alpha import PAYLOAD


@pytest.fixture
def service():
    engine = create_engine("sqlite://")
    intents.create(engine)
    account_ledger.orders.create(engine)
    browser = Mock()
    instance = LiveOrders(engine, browser)
    instance.enabled = True
    return instance


def request():
    return SubmitOrder(**PAYLOAD, request_id=uuid4())


def final_order():
    from app.browser.schemas import token_identity

    chain, address = token_identity(PAYLOAD["url"])
    return {
        "order_id": "123",
        "created_at": "2026-09-22 12:00:00",
        "symbol": "DGAI",
        "quote": "USDT",
        "side": "买入",
        "quantity": "1",
        "gross": "0.9",
        "average_price": "0.9 USDT",
        "limit_price": "0.9",
        "requested_quantity": "1",
        "status": "已成交",
        "captured_at": 100,
        "chain": chain,
        "address": address,
    }


def test_disabled_and_budget_fail_before_browser(service):
    service.enabled = False
    with pytest.raises(ValueError, match="开启"):
        service.submit(request())
    service.enabled = True
    with pytest.raises(ValueError, match="50"):
        service.submit(
            SubmitOrder(**{**PAYLOAD, "quantity": "100"}, request_id=uuid4())
        )
    service.browser.execute.assert_not_called()


def test_one_click_dedup_single_active_and_final_accounting(service):
    service.browser.execute.side_effect = [
        {"ok": True, "baseline_id": "122"},
        {"ok": True, "clicked": True},
        {"ok": True, "pending": True},
        {"ok": True, "pending": False, "order": final_order()},
    ]
    body = request()
    assert service.submit(body)["state"] == "waiting"
    assert service.submit(body)["state"] == "waiting"
    with pytest.raises(ValueError, match="上一笔"):
        service.submit(request())
    assert service.check()["state"] == "waiting"
    assert service.check()["state"] == "completed"
    assert service.check() is None
    assert service.submit(body)["state"] == "completed"
    assert [c.args[0] for c in service.browser.execute.call_args_list].count(
        "live_submit"
    ) == 1
    stats = account_ledger.read(service.engine, "本机账户")["stats"][0]
    assert stats["buy_total"] == "0.9" and stats["estimated_points"] == "3.6"
    assert stats["estimated_fee"] == "0.00009"


def test_timeout_restart_does_not_resubmit_and_old_history_not_completion(service):
    service.browser.execute.side_effect = [
        {"ok": True, "baseline_id": "123"},
        TimeoutError("timeout"),
    ]
    body = request()
    assert service.submit(body)["state"] == "submission_unknown"
    restarted = LiveOrders(service.engine, Mock())
    assert not restarted.enabled
    restarted.browser.execute.return_value = {
        "ok": True,
        "pending": False,
        "order": final_order(),
    }
    assert restarted.check()["active"]
    assert restarted.submit(body)["state"] == "submission_unknown"
    assert [c.args[0] for c in restarted.browser.execute.call_args_list] == [
        "live_inspect"
    ]
    with service.engine.connect() as c:
        assert c.execute(select(account_ledger.orders)).all() == []


def test_wrong_order_or_partial_open_does_not_finalize(service):
    service.browser.execute.side_effect = [
        {"ok": True, "baseline_id": None},
        {"ok": True, "clicked": True},
    ]
    service.submit(request())
    service.browser.execute.side_effect = None
    for changes in (
        {"side": "卖出"},
        {"requested_quantity": "2"},
        {"limit_price": "0.8"},
        {"status": "部分成交"},
    ):
        service.browser.execute.return_value = {
            "ok": True,
            "pending": False,
            "order": {**final_order(), **changes},
        }
        assert service.check()["active"]
    service.browser.execute.return_value = {
        "ok": True,
        "pending": False,
        "order": {
            **final_order(),
            "quantity": "0.4",
            "gross": "0.36",
            "status": "已取消",
        },
    }
    assert not service.check()["active"]
    assert (
        account_ledger.read(service.engine, "本机账户")["stats"][0]["buy_total"]
        == "0.36"
    )


def test_preflight_failure_never_clicks(service):
    service.browser.execute.return_value = {"ok": False, "message": "尚未加载"}
    assert service.submit(request())["state"] == "not_submitted"
    assert [c.args[0] for c in service.browser.execute.call_args_list] == [
        "live_prepare"
    ]


def test_manual_unsubmitted_resolution_keeps_audit_and_never_replays(service):
    service.browser.execute.side_effect = [
        {"ok": True, "baseline_id": "123"},
        {"ok": True, "clicked": True},
        {"ok": True, "pending": False, "order": final_order()},
    ]
    body = request()
    service.submit(body)
    result = service.resolve_unsubmitted(body.request_id)
    assert result["state"] == "not_submitted" and not result["active"]
    assert result["resolution"] == "user_confirmed_not_submitted"
    assert service.get() is None
    assert service.submit(body)["state"] == "not_submitted"
    assert [c.args[0] for c in service.browser.execute.call_args_list] == [
        "live_prepare",
        "live_submit",
        "live_unsubmitted",
    ]
    assert account_ledger.read(service.engine, "本机账户")["stats"] == []


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True, "pending": True},
        {"ok": True, "pending": False, "order": {"order_id": "124"}},
        {"ok": False, "message": "确认弹窗未关闭"},
    ],
)
def test_resolution_rejects_pending_changed_history_or_dialog(service, response):
    service.browser.execute.side_effect = [
        {"ok": True, "baseline_id": "123"},
        {"ok": True, "clicked": True},
        response,
    ]
    body = request()
    service.submit(body)
    with pytest.raises(ValueError):
        service.resolve_unsubmitted(body.request_id)
    assert service.get()["active"]


def test_account_conflict_keeps_pending(service):
    import json

    service.browser.execute.side_effect = [
        {"ok": True, "baseline_id": None},
        {"ok": True, "clicked": True},
    ]
    service.submit(request())
    with service.engine.begin() as c:
        c.execute(
            account_ledger.orders.insert().values(
                book="本机账户",
                order_id="123",
                payload=json.dumps({**final_order(), "gross": "9"}),
            )
        )
    service.browser.execute.side_effect = None
    service.browser.execute.return_value = {
        "ok": True,
        "pending": False,
        "order": final_order(),
    }
    assert service.check()["active"]
    assert service.get()["state"] == "waiting"


def test_api_requires_local_enable_and_never_replays(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    from app import database
    from app.main import app

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "live.db")
    command.upgrade(Config("alembic.ini"), "head")
    with TestClient(app) as client:
        execute = Mock(
            side_effect=[
                {"ok": True, "baseline_id": None},
                {"ok": True, "clicked": True},
            ]
        )
        monkeypatch.setattr(app.state.browser, "execute", execute)
        headers = {"X-AlphaLooper-Client": "local-ui"}
        body = request().model_dump(mode="json")
        assert (
            client.post("/api/live/enabled", json={"enabled": True}).status_code == 403
        )
        assert (
            client.post("/api/live/orders", json=body, headers=headers).status_code
            == 409
        )
        assert execute.call_count == 0
        execute.side_effect = None
        execute.return_value = {"ok": True, "baseline_id": "122"}
        assert (
            client.post("/api/browser/order-readiness", json=PAYLOAD).status_code == 403
        )
        assert (
            client.post(
                "/api/browser/order-readiness", json=PAYLOAD, headers=headers
            ).json()["baseline_id"]
            == "122"
        )
        assert execute.call_args.args[0] == "order_readiness"
        assert client.get("/api/live/orders").json()["recent"] == []
        execute.reset_mock()
        execute.side_effect = [
            {"ok": True, "baseline_id": None},
            {"ok": True, "clicked": True},
        ]
        client.post("/api/live/enabled", json={"enabled": True}, headers=headers)
        assert (
            client.post("/api/live/orders", json=body, headers=headers).json()["state"]
            == "waiting"
        )
        assert (
            client.post("/api/live/orders", json=body, headers=headers).json()["state"]
            == "waiting"
        )
        assert (
            client.post("/api/browser/fill", json=PAYLOAD, headers=headers).status_code
            == 409
        )
        assert execute.call_count == 2
        resolution = {"request_id": body["request_id"], "confirmed_not_submitted": True}
        assert (
            client.post("/api/live/resolve-unsubmitted", json=resolution).status_code
            == 403
        )
        assert (
            client.post(
                "/api/live/resolve-unsubmitted",
                json={**resolution, "confirmed_not_submitted": False},
                headers=headers,
            ).status_code
            == 422
        )
        assert execute.call_count == 2
        assert (
            client.post(
                "/api/browser/order-readiness", json=PAYLOAD, headers=headers
            ).status_code
            == 409
        )
        assert execute.call_count == 2
        client.post("/api/live/enabled", json={"enabled": False}, headers=headers)
        assert client.get("/api/live/orders").json()["current"]["active"]
