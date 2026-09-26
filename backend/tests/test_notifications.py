from app.notifications import WeComNotifier, task_stopped_payload


def task():
    return {
        "id": "12345678-abcd",
        "request": {"expected_symbol": "TEST", "expected_quote": "USDT"},
        "buy_total": "123.45",
        "realized_pnl": "1.25",
        "session_loss": "9",
        "risk": {"session_loss": "-2.5"},
        "pending": {"side": "sell"},
    }


def test_stop_payload_contains_reason_volume_and_pnl_without_url():
    payload = task_stopped_payload(task(), "订单核对失败：页面结构变化")
    content = payload["text"]["content"]

    assert payload["msgtype"] == "text"
    assert "订单核对失败：页面结构变化" in content
    assert "累计买入：123.45 USDT" in content
    assert "整体盈亏估值：+2.5 USDT" in content
    assert "已结束轮次现金盈亏：1.25 USDT" in content
    assert "有待处理卖出委托" in content
    assert "webhook" not in content and "binance.com" not in content


def test_notifier_retries_and_does_not_run_when_unconfigured():
    calls = []

    def sender(url, payload):
        calls.append((url, payload))
        if len(calls) < 3:
            raise TimeoutError("temporary")

    notifier = WeComNotifier("https://example.invalid/hook", sender=sender, sleep=lambda _: None)
    notifier._send_with_retry(task_stopped_payload(task(), "测试"))
    assert len(calls) == 3
    notifier.close()

    disabled = WeComNotifier(None, sender=sender)
    assert disabled.enqueue_task_stopped(task(), "不会发送") is False
    disabled.close()
