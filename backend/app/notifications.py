"""Out-of-band task notifications. Webhook secrets stay in backend/.env."""

import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import dotenv_values

from app.database import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / "backend" / ".env"
WEBHOOK_ENV = "ALPHALOOPER_WECOM_WEBHOOK_URL"
logger = logging.getLogger(__name__)


def _setting(name: str) -> str | None:
    local = dotenv_values(ENV_FILE, encoding="utf-8-sig", interpolate=False)
    return os.environ.get(name, local.get(name)) or None


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _display(value: Decimal | None, *, signed=False) -> str:
    if value is None:
        return "待核对"
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return f"+{text}" if signed and value > 0 else text


def task_stopped_payload(task: dict, reason: str) -> dict:
    """Build a concise text message without exposing the webhook or page URL."""
    risk = task.get("risk") or {}
    session_loss = _decimal(risk.get("session_loss"))
    if session_loss is None:
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
            f"停止原因：{reason}",
            f"累计买入：{task.get('buy_total', '0')} {quote}",
            f"整体盈亏估值：{_display(overall_pnl, signed=True)} {quote}",
            f"已结束轮次现金盈亏：{task.get('realized_pnl', '0')} {quote}",
            f"订单状态：{order_state}",
            "请打开控制台核对平台挂单、余额和持仓后再决定是否恢复。",
        ]
    )
    # Leave headroom below the robot text-message limit, including multi-byte text.
    return {"msgtype": "text", "text": {"content": content[:1800]}}


Sender = Callable[[str, dict], None]


class WeComNotifier:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        sender: Sender | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.webhook_url = webhook_url
        self.sender = sender or self._http_send
        self.sleep = sleep
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="alphalooper-wecom"
        )

    @classmethod
    def from_env(cls):
        return cls(_setting(WEBHOOK_ENV))

    @property
    def configured(self):
        return bool(self.webhook_url)

    def enqueue_task_stopped(self, task: dict, reason: str) -> bool:
        if not self.webhook_url:
            return False
        future = self.executor.submit(self._send_with_retry, task_stopped_payload(task, reason))
        future.add_done_callback(self._report_failure)
        return True

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
            detail = json.dumps(body, ensure_ascii=False)[:300]
            raise RuntimeError(f"企业微信机器人返回失败：{detail}")

    @staticmethod
    def _report_failure(future: Future):
        try:
            future.result()
        except Exception:
            logger.exception("企业微信停止通知发送失败；任务状态不受影响。")

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=False)
