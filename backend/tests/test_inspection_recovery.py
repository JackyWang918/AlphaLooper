from unittest.mock import Mock

import pytest
from playwright.sync_api import Error, TimeoutError

from app.browser.automatic import inspect_progress
from tests.test_alpha import PAYLOAD


@pytest.mark.parametrize(
    "step,label",
    [
        ("reload", "刷新交易页"),
        ("current_detail", "首次读取当前委托"),
        ("wallet_snapshot", "读取买卖两侧余额"),
    ],
)
def test_read_timeout_discards_snapshot_and_identifies_step(monkeypatch, step, label):
    page = Mock()
    detail = Mock(return_value=(None, None))
    wallet = Mock(return_value={"base_available": "71.2286"})
    monkeypatch.setattr("app.browser.automatic.current_detail", detail)
    monkeypatch.setattr("app.browser.automatic.wallet_snapshot", wallet)
    {"reload": page.reload, "current_detail": detail, "wallet_snapshot": wallet}[
        step
    ].side_effect = TimeoutError("secret page content")
    result = inspect_progress(
        page, PAYLOAD | {"refresh_before_check": step == "reload"}
    )
    assert result["page_loading"] and result["settling"]
    assert result["pending"] is None
    assert "balances" not in result
    assert label in result["message"]
    assert "secret" not in result["message"]
    assert result["page_refreshed"] is False


def test_second_order_read_timeout_discards_balance(monkeypatch):
    monkeypatch.setattr(
        "app.browser.automatic.current_detail",
        Mock(side_effect=[(None, None), TimeoutError("timeout")]),
    )
    monkeypatch.setattr("app.browser.automatic.wallet_snapshot", Mock(return_value={}))
    result = inspect_progress(Mock(), PAYLOAD)
    assert "再次读取当前委托" in result["message"]
    assert "balances" not in result


@pytest.mark.parametrize("error", [ValueError("币种冲突"), Error("disconnected")])
def test_identity_conflicts_and_browser_errors_are_not_loading(monkeypatch, error):
    monkeypatch.setattr("app.browser.automatic.current_detail", Mock(side_effect=error))
    with pytest.raises(ValueError):
        inspect_progress(Mock(), PAYLOAD)
