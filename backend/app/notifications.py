"""Out-of-band task notifications. Webhook secrets stay in backend/.env."""

import copy
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import dotenv_values

from app import decision_log
from app.ai_diagnostics import (
    DeepSeekAnalyzer,
    Diagnosis,
    analysis_text,
    clip_utf8,
    diagnostic_context,
    safe_text,
)
from app.database import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / "backend" / ".env"
WEBHOOK_ENV = "ALPHALOOPER_WECOM_WEBHOOK_URL"
logger = logging.getLogger(__name__)


def _setting(name: str) -> str | None:
    local = dotenv_values(ENV_FILE, encoding="utf-8-sig", interpolate=False)
    return os.environ.get(name, local.get(name)) or None


def _decimal(value) -> Decimal | None:
    try:
        number = Decimal(str(value)) if value is not None else None
        return number if number is not None and number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _display(value: Decimal | None, *, signed=False) -> str:
    if value is None:
        return "待核对"
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return f"+{text}" if signed and value > 0 else text


def task_stopped_payload(
    task: dict, reason: str, analysis: dict | None = None, *, secrets=()
) -> dict:
    """Build a concise text message without exposing the webhook or page URL."""
    risk = task.get("risk") or {}
    session_loss = _decimal(risk.get("session_loss"))
    if (
        session_loss is None
        and task.get("phase") != "attention"
        and not task.get("risk_reconcile")
    ):
        session_loss = _decimal(task.get("session_loss"))
    overall_pnl = -session_loss if session_loss is not None else None
    request = task.get("request") or {}
    symbol = request.get("expected_symbol") or "未知币种"
    quote = request.get("expected_quote") or "USDT"
    task_id = str(task.get("id") or "未知")[:8]
    pending = task.get("pending") or {}
    order_state = "无本地待处理委托"
    if pending:
        side = "买入" if pending.get("side") == "buy" else "卖出"
        order_state = f"有待处理{side}委托"
    content = "\n".join(
        [
            "AlphaLooper 自动任务已停止",
            f"币种：{symbol} / {quote}",
            f"任务：{task_id}",
            f"停止原因：{safe_text(reason, secrets, limit=300)}",
            f"累计买入：{task.get('buy_total', '0')} {quote}",
            f"整体盈亏估值：{_display(overall_pnl, signed=True)} {quote}",
            f"已结束轮次现金盈亏：{task.get('realized_pnl', '0')} {quote}",
            f"订单状态：{order_state}",
        ]
    )
    content = clip_utf8(
        safe_text(content, secrets, limit=1800), 900 if analysis else 1800
    )
    if analysis:
        content += "\n\n" + clip_utf8(
            safe_text(analysis_text(analysis), secrets, limit=1200), 1000
        )
    content += "\n请打开控制台核对挂单、余额和持仓后再决定是否恢复。"
    if analysis:
        content += "AI 不执行恢复或交易，结论来自暂停时快照。"
    # The text-message limit is bytes, not Python characters.
    return {"msgtype": "text", "text": {"content": clip_utf8(content, 2048)}}


Sender = Callable[[str, dict], None]


class WeComNotifier:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        sender: Sender | None = None,
        sleep: Callable[[float], None] = time.sleep,
        analyzer=None,
        on_analysis: Callable | None = None,
    ):
        self.webhook_url = webhook_url
        self.sender = sender or self._http_send
        self.sleep = sleep
        self.analyzer = analyzer
        self.on_analysis = on_analysis
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="alphalooper-wecom"
        )

    @classmethod
    def from_env(cls, engine=None):
        analyzer = DeepSeekAnalyzer.from_env(_setting)

        def record_analysis(task, reason, report, context):
            if engine is not None:
                with engine.begin() as connection:
                    decision_log.append(
                        connection,
                        task["id"],
                        "ai_analysis",
                        time.time(),
                        {
                            "reason": "AI 异常分析（仅供人工核对）",
                            "state": {
                                "phase": task.get("phase"),
                                "stopped_at": task.get("updated_at"),
                            },
                            "evidence": {
                                "stop_reason": reason,
                                "diagnostic_context": context,
                            },
                            "details": report,
                        },
                    )

        return cls(
            _setting(WEBHOOK_ENV), analyzer=analyzer, on_analysis=record_analysis
        )

    @property
    def configured(self):
        return bool(self.webhook_url)

    def enqueue_task_stopped(self, task: dict, reason: str) -> bool:
        if not self.webhook_url:
            return False
        # Freeze at enqueue time; the user may resume while AI is still working.
        snapshot = copy.deepcopy(task)
        secrets = (self.webhook_url, getattr(self.analyzer, "api_key", None))
        try:
            context = diagnostic_context(snapshot, reason, secrets)
        except Exception:  # noqa: BLE001 -- partial diagnostics cannot suppress the original alert
            context = {
                "reason": safe_text(reason, secrets, limit=800),
                "limitations": "暂停时诊断快照收集不完整，需人工补充订单和余额证据。",
            }
        future = self.executor.submit(
            self._send_stopped, snapshot, safe_text(reason, secrets, limit=800), context
        )
        future.add_done_callback(self._report_failure)
        return True

    def _send_stopped(self, task, reason, context):
        report = None
        if task.get("phase") == "attention" and self.analyzer:
            try:
                report = self.analyzer.analyze(context)
                if not isinstance(report, dict) or report.get("status") not in {
                    "completed",
                    "unavailable",
                    "disabled",
                    "unconfigured",
                }:
                    raise ValueError("Invalid diagnostic report")
                if report["status"] == "completed":
                    Diagnosis.model_validate(
                        {key: report[key] for key in Diagnosis.model_fields}
                    )
                elif not isinstance(report.get("message"), str):
                    raise ValueError("Missing diagnostic error message")
            except Exception:  # noqa: BLE001 -- always send the original stop notification
                report = {
                    "status": "unavailable",
                    "message": "AI 分析失败，原停止通知照常发送。",
                }
            secrets = (self.webhook_url, getattr(self.analyzer, "api_key", None))
            # Apply redaction to injected analyzers as well as the HTTP client.
            report = {
                key: [safe_text(item, secrets, limit=600) for item in value]
                if isinstance(value, list)
                else safe_text(value, secrets, limit=600)
                if isinstance(value, str)
                else value
                for key, value in report.items()
            }
            if self.on_analysis:
                try:
                    self.on_analysis(task, reason, report, context)
                except Exception:  # noqa: BLE001 -- diagnostic storage must not suppress notification
                    logger.error(
                        "AI 分析记录保存失败；仍发送停止通知，任务状态不受影响。"
                    )
        self._send_with_retry(
            task_stopped_payload(
                task,
                reason,
                report,
                secrets=(self.webhook_url, getattr(self.analyzer, "api_key", None)),
            )
        )

    def _send_with_retry(self, payload: dict):
        last_error = None
        for attempt in range(3):
            try:
                self.sender(self.webhook_url, payload)
                return
            except Exception as exc:  # noqa: BLE001 - notification must not stop trading
                last_error = exc
                if attempt < 2:
                    self.sleep(attempt + 1)
        raise RuntimeError("企业微信停止通知发送失败。") from last_error

    @staticmethod
    def _http_send(webhook_url: str, payload: dict):
        proxy = _setting("ALPHALOOPER_HTTP_PROXY")
        with httpx.Client(proxy=proxy, timeout=8, trust_env=False) as client:
            response = client.post(webhook_url, json=payload)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or body.get("errcode") != 0:
            raise RuntimeError("企业微信机器人返回失败。")

    @staticmethod
    def _report_failure(future: Future):
        try:
            future.result()
        except Exception:  # noqa: BLE001 -- never log exception URLs containing webhook keys
            logger.error("企业微信停止通知发送失败；任务状态不受影响。")

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=False)
