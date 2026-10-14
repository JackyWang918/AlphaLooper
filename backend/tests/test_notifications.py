import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_diagnostics import DeepSeekAnalyzer
from app.notifications import WeComNotifier, task_stopped_payload
from tests.test_ai_diagnostics import diagnosis


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

    notifier = WeComNotifier(
        "https://example.invalid/hook", sender=sender, sleep=lambda _: None
    )
    notifier._send_with_retry(task_stopped_payload(task(), "测试"))
    assert len(calls) == 3
    notifier.close()

    disabled = WeComNotifier(None, sender=sender)
    assert disabled.enqueue_task_stopped(task(), "不会发送") is False
    disabled.close()


def abnormal_task():
    return {
        **task(),
        "phase": "attention",
        "balances": {"quote_available": "50"},
        "_diagnostic_context": {
            "order": {"state": "settling", "last_check_error": "余额慢"},
            "recent_events": [],
        },
    }


def drain(notifier):
    notifier.executor.submit(lambda: None).result(timeout=3)
    notifier.close()


def test_ai_result_is_combined_with_stop_notification_and_never_reanalyzed_on_send_retry():
    analyzer = SimpleNamespace(
        api_key="test",
        analyze=Mock(return_value={"status": "completed", **diagnosis()}),
    )
    sender = Mock(side_effect=[TimeoutError("retry"), None])
    saved = Mock()
    notifier = WeComNotifier(
        "https://example.invalid/hook",
        analyzer=analyzer,
        sender=sender,
        sleep=lambda _: None,
        on_analysis=saved,
    )
    assert notifier.enqueue_task_stopped(abnormal_task(), "订单核对失败")
    drain(notifier)
    analyzer.analyze.assert_called_once()
    saved.assert_called_once()
    assert sender.call_count == 2
    content = sender.call_args.args[1]["text"]["content"]
    assert "订单核对失败" in content and "AI 分析" in content
    assert "USDT 余额尚未同步" in content and "人工查看平台" in content
    assert "AI 不执行恢复或交易" in content
    assert len(content.encode("utf-8")) <= 2048


@pytest.mark.parametrize("value", [None, {}, {"status": "completed"}])
def test_invalid_analyzer_result_does_not_suppress_original_notice(value):
    analyzer = SimpleNamespace(api_key=None, analyze=Mock(return_value=value))
    sender = Mock()
    notifier = WeComNotifier(
        "https://example.invalid", analyzer=analyzer, sender=sender
    )
    notifier.enqueue_task_stopped(abnormal_task(), "原始停止原因")
    drain(notifier)
    content = sender.call_args.args[1]["text"]["content"]
    assert "原始停止原因" in content and "AI 分析失败" in content


def test_analyzer_exception_and_storage_failure_do_not_suppress_notification():
    analyzer = SimpleNamespace(
        api_key="sk-private", analyze=Mock(side_effect=RuntimeError("sk-private"))
    )
    sender = Mock()
    saved = Mock(side_effect=RuntimeError("database private details"))
    notifier = WeComNotifier(
        "https://example.invalid", analyzer=analyzer, sender=sender, on_analysis=saved
    )
    notifier.enqueue_task_stopped(abnormal_task(), "异常暂停")
    drain(notifier)
    sender.assert_called_once()
    content = sender.call_args.args[1]["text"]["content"]
    assert "sk-private" not in content and "异常暂停" in content


def test_snapshot_is_frozen_and_ai_does_not_block_caller():
    started, release = threading.Event(), threading.Event()
    received = []

    def analyze(context):
        started.set()
        assert release.wait(3)
        received.append(context)
        return {"status": "completed", **diagnosis()}

    sender = Mock()
    analyzer = SimpleNamespace(api_key=None, analyze=analyze)
    notifier = WeComNotifier(
        "https://example.invalid", analyzer=analyzer, sender=sender
    )
    snapshot = abnormal_task()
    try:
        assert notifier.enqueue_task_stopped(snapshot, "读取失败")
        assert started.wait(3)
        snapshot["balances"]["quote_available"] = "999"
        snapshot["phase"] = "running"
        release.set()
        drain(notifier)
        assert received[0]["balances"]["quote_available"] == "50"
        assert received[0]["state"]["phase"] == "attention"
        assert snapshot["phase"] == "running"
    finally:
        release.set()
        notifier.close()


@pytest.mark.parametrize("phase", ["paused", "completed", "running"])
def test_normal_stop_or_restart_notice_does_not_call_ai(phase):
    analyzer = SimpleNamespace(api_key=None, analyze=Mock())
    sender = Mock()
    notifier = WeComNotifier(
        "https://example.invalid", analyzer=analyzer, sender=sender
    )
    notifier.enqueue_task_stopped({**task(), "phase": phase}, "普通通知")
    drain(notifier)
    analyzer.analyze.assert_not_called()
    sender.assert_called_once()


def test_missing_api_key_still_sends_notice_with_configuration_hint():
    sender = Mock()
    notifier = WeComNotifier(
        "https://example.invalid", analyzer=DeepSeekAnalyzer(), sender=sender
    )
    notifier.enqueue_task_stopped(abnormal_task(), "加载超时")
    drain(notifier)
    content = sender.call_args.args[1]["text"]["content"]
    assert "加载超时" in content and "未配置 DeepSeek API Key" in content


def test_utf8_notification_limit_keeps_ai_summary_and_manual_boundary():
    report = {"status": "completed", **diagnosis()}
    report["summary"] = "推测" + "长" * 158
    report["suggested_steps"] = ["查" * 160] * 3
    payload = task_stopped_payload(abnormal_task(), "错误" * 3000, report)
    content = payload["text"]["content"]
    assert len(content.encode("utf-8")) <= 2048
    assert "AI 分析" in content and "AI 不执行恢复或交易" in content


def test_unknown_risk_does_not_fall_back_to_stale_loss_snapshot():
    snapshot = abnormal_task()
    snapshot["risk"] = {"session_loss": None}
    snapshot["session_loss"] = "38"
    assert (
        "整体盈亏估值：待核对"
        in task_stopped_payload(snapshot, "余额待核对")["text"]["content"]
    )


def test_ai_report_is_persisted_as_log_without_mutating_task(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app import decision_log, notifications

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    decision_log.decisions.create(engine)
    monkeypatch.setattr(notifications, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("ALPHALOOPER_WECOM_WEBHOOK_URL", "https://example.invalid")
    notifier = WeComNotifier.from_env(engine)
    notifier.sender = Mock()
    notifier.analyzer = SimpleNamespace(
        api_key=None, analyze=Mock(return_value={"status": "completed", **diagnosis()})
    )
    snapshot = abnormal_task()
    original = json.dumps(snapshot)
    notifier.enqueue_task_stopped(snapshot, "余额读取失败")
    drain(notifier)
    rows = decision_log.read(engine, snapshot["id"], kind="ai_analysis")["items"]
    assert len(rows) == 1 and rows[0]["details"]["status"] == "completed"
    assert rows[0]["evidence"]["stop_reason"] == "余额读取失败"
    assert json.dumps(snapshot) == original
    engine.dispose()
