import time
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from app.browser.live import submit_once
from app.browser.manager import BrowserManager
from app.browser.owner import endpoint_available, run_browser_owner
from app.browser.runtime import checkpoint, command_runtime
from app.browser.worker import run_worker
from tests.test_alpha import PAYLOAD


def headless_owner(pipe, profile):
    run_browser_owner(pipe, profile, headless=True)


def stalled_worker(pipe, endpoint):
    with sync_playwright() as playwright:
        playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
        while True:
            command = pipe.recv()
            if command["action"] == "launch":
                pipe.send({"ok": True})
            else:
                pipe.send({"event": "progress", "step": "读取卖出侧余额"})
                time.sleep(60)


def test_outer_deadline_is_not_extended_by_progress(monkeypatch):
    manager = BrowserManager()
    manager._process = Mock()
    manager._pipe = Mock()
    manager._owner = Mock()
    process, pipe, owner = manager._process, manager._pipe, manager._owner
    monkeypatch.setattr(
        "app.browser.manager.time.monotonic", Mock(side_effect=[0, 0, 44, 46])
    )
    pipe.poll.return_value = True
    pipe.recv.return_value = {"event": "progress", "step": "读取卖出侧余额"}
    result = manager.execute("order_readiness")
    assert not result["ok"]
    assert result["step"] == "读取卖出侧余额"
    assert "已保留浏览器" in result["message"]
    process.terminate.assert_called_once()
    owner.terminate.assert_not_called()
    assert manager.execute("live_submit", payload=PAYLOAD)["ok"] is False
    assert pipe.send.call_count == 1  # no replay or newly accepted command


def test_expired_command_never_reaches_submit_click(monkeypatch):
    now = [0]
    monkeypatch.setattr("app.browser.runtime.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "app.browser.live.current_order", Mock(return_value={"empty": True})
    )
    monkeypatch.setattr("app.browser.live.fill_form", Mock())
    monkeypatch.setattr(
        "app.browser.live.verify_identity", Mock(return_value={"side": "buy"})
    )
    button = Mock()
    monkeypatch.setattr("app.browser.live.submit_button", Mock(return_value=button))

    def report(step):
        if "第一次点击" in step:
            now[0] = 35

    with command_runtime(report), pytest.raises(ValueError, match="总耗时"):
        submit_once(Mock(), PAYLOAD)
    button.click.assert_not_called()
    checkpoint("runtime reset after error")


def test_timeout_retains_isolated_chrome_and_reconnects_without_replay(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.browser.manager.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("app.browser.manager.run_browser_owner", headless_owner)
    monkeypatch.setattr("app.browser.manager.run_worker", stalled_worker)
    manager = BrowserManager()
    try:
        assert manager.execute("launch")["ok"]
        endpoint, owner_pid = manager._endpoint, manager._owner.pid
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
            page = browser.contexts[0].pages[0]
            page.set_content("<p>isolated manual inspection marker</p>")
        monkeypatch.setattr("app.browser.manager.RESPONSE_TIMEOUT_SECONDS", 0.3)
        result = manager.execute("order_readiness", payload=PAYLOAD)
        assert not result["ok"] and "读取卖出侧余额" in result["message"]
        assert manager._owner.is_alive() and endpoint_available(endpoint)
        blocked = manager.execute("status")
        assert blocked["executor_blocked"] and not blocked["connected"]
        monkeypatch.setattr("app.browser.manager.RESPONSE_TIMEOUT_SECONDS", 45)
        monkeypatch.setattr("app.browser.manager.run_worker", run_worker)
        assert manager.execute("launch")["ok"]
        assert manager._owner.pid == owner_pid and manager._endpoint == endpoint
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
            assert "isolated manual inspection marker" in browser.contexts[0].pages[
                0
            ].inner_text("body")
    finally:
        manager.close()
    assert not endpoint_available(endpoint)
