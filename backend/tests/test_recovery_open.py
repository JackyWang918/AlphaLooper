import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.main import app, browser_action

URL = "https://www.binance.com/zh-CN/alpha/bsc/0x123"


@pytest.fixture
def services(monkeypatch):
    automatic = SimpleNamespace(running=False, get=lambda: {"request": {"url": URL}})
    live = SimpleNamespace(
        lock=threading.RLock(), automatic=automatic, get=lambda: None
    )
    browser = Mock()
    browser.execute.return_value = {"ok": True, "url": URL}
    monkeypatch.setattr(app.state, "live", live, raising=False)
    monkeypatch.setattr(app.state, "browser", browser, raising=False)
    return live, browser


def test_paused_task_can_open_original_coin_without_resuming(services):
    live, browser = services
    assert browser_action("open", URL)["ok"]
    browser.execute.assert_called_once_with("open", URL, None)
    assert not live.automatic.running


@pytest.mark.parametrize(
    "action,url",
    [
        ("open", URL.replace("0x123", "0x456")),
        ("open", "https://www.binance.com/alpha"),
        ("fill", URL),
        ("read_records", URL),
        ("order_readiness", URL),
    ],
)
def test_recovery_does_not_unlock_other_actions(services, action, url):
    _, browser = services
    with pytest.raises(HTTPException) as exc:
        browser_action(action, url)
    assert exc.value.status_code == 409
    browser.execute.assert_not_called()


@pytest.mark.parametrize("reason", ["running", "pending_order"])
def test_running_task_or_unresolved_order_still_blocks_navigation(services, reason):
    live, browser = services
    if reason == "running":
        live.automatic.running = True
    else:
        live.get = lambda: {"active": True}
    with pytest.raises(HTTPException):
        browser_action("open", URL)
    browser.execute.assert_not_called()
