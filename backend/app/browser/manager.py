import multiprocessing
import threading
import time

from app.browser.owner import endpoint_available, run_browser_owner
from app.browser.runtime import RESPONSE_TIMEOUT_SECONDS
from app.browser.worker import run_worker
from app.database import PROJECT_ROOT

ACTION_NAMES = {
    "launch": "连接浏览器",
    "open": "打开交易页",
    "status": "读取浏览器状态",
    "fill": "仅填写表单",
    "page_test": "页面测试",
    "order_readiness": "下单前核对委托和余额",
    "live_prepare": "准备订单表单",
    "live_submit": "提交订单",
    "live_progress": "巡检当前订单",
    "live_inspect": "核对当前订单",
    "live_unsubmitted": "核对是否未提交",
    "live_cancel": "撤销当前委托",
    "live_balance": "读取账户余额",
    "confirmation_preview": "读取确认弹窗",
}


class BrowserBusy(Exception):
    pass


class BrowserManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._pipe = None
        self._owner = None
        self._owner_pipe = None
        self._endpoint = None
        self._fault = None

    def _ensure_browser(self, context):
        if (
            self._owner
            and self._owner.is_alive()
            and endpoint_available(self._endpoint)
        ):
            return {"ok": True}
        self._close_owner()
        self._owner_pipe, child = context.Pipe()
        self._owner = context.Process(
            target=run_browser_owner,
            args=(child, str(PROJECT_ROOT / "data" / "browser-profile")),
            daemon=True,
        )
        self._owner.start()
        child.close()
        try:
            if not self._owner_pipe.poll(35):
                return {
                    "ok": False,
                    "message": "Chrome 启动超过 35 秒，请检查旧浏览器是否占用用户目录。",
                }
            result = self._owner_pipe.recv()
            if result.get("ok"):
                self._endpoint = result["endpoint"]
            return result
        except (EOFError, OSError):
            return {"ok": False, "message": "Chrome 保持进程已退出，请重新启动。"}

    def execute(self, action: str, url: str = "", payload: dict | None = None):
        if not self._lock.acquire(blocking=False):
            raise BrowserBusy("浏览器正在执行操作，请稍后重试。")
        try:
            if self._fault and action != "launch":
                if action == "status":
                    return {
                        "ok": True,
                        "connected": False,
                        "url": "",
                        "title": "",
                        "fill_supported": False,
                        "executor_blocked": True,
                        "reason": self._fault,
                    }
                return {"ok": False, "message": self._fault}
            deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
            if action == "launch":
                # Explicit reconnect discards the old channel, including late
                # replies. It never resends a previous command.
                self._dispose()
            if not self._process or not self._process.is_alive():
                self._dispose()
                if action != "launch":
                    if action != "status":
                        return {"ok": False, "message": "请先启动浏览器。"}
                    return {
                        "ok": True,
                        "connected": False,
                        "url": "",
                        "title": "",
                        "fill_supported": False,
                    }
                context = multiprocessing.get_context("spawn")
                ready = self._ensure_browser(context)
                if not ready.get("ok"):
                    self._close_owner()
                    return ready
                self._pipe, child = context.Pipe()
                self._process = context.Process(
                    target=run_worker,
                    args=(child, self._endpoint),
                    daemon=True,
                )
                self._process.start()
                child.close()
            self._fault = None
            try:
                self._pipe.send({"action": action, "url": url, "payload": payload})
                step = "等待执行器开始"
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._pipe.poll(remaining):
                        self._dispose()
                        self._fault = (
                            f"执行器响应超过 {RESPONSE_TIMEOUT_SECONDS} 秒，已停止页面操作；"
                            f"动作：{ACTION_NAMES.get(action, action)}，最后步骤：{step}。"
                            "已保留浏览器供人工检查；如有风控请手动校验。"
                            "点击“启动 Chrome”重新连接，再核对后恢复任务；不会自动重试买卖或撤单。"
                        )
                        return {
                            "ok": False,
                            "message": self._fault,
                            "action": action,
                            "step": step,
                        }
                    result = self._pipe.recv()
                    if result.get("event") == "progress":
                        step = result["step"]
                        continue
                    if not result.get("ok"):
                        result.update(action=action, step=step)
                        result["message"] = (
                            result.get("message", "浏览器操作失败")
                            + f"（动作：{ACTION_NAMES.get(action, action)}；步骤：{step}）"
                        )
                    return result
            except (EOFError, OSError):
                self._dispose()
                self._fault = "浏览器执行器已退出，已停止页面操作并保留浏览器；点击“启动 Chrome”重新连接，再核对后恢复任务。"
                return {"ok": False, "message": self._fault}
        finally:
            self._lock.release()

    def _dispose(self):
        if self._process:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=3)
            self._process = None
        if self._pipe:
            self._pipe.close()
            self._pipe = None

    def close(self):
        with self._lock:
            if self._process and self._process.is_alive():
                try:
                    self._pipe.send({"action": "stop"})
                    self._process.join(timeout=8)
                except (EOFError, OSError):
                    pass
            self._dispose()
            self._close_owner()

    def _close_owner(self):
        if self._owner:
            if self._owner.is_alive():
                try:
                    self._owner_pipe.send({"action": "stop"})
                    self._owner.join(timeout=8)
                except (EOFError, OSError):
                    pass
                if self._owner.is_alive():
                    self._owner.terminate()
            self._owner.join(timeout=3)
        if self._owner_pipe:
            self._owner_pipe.close()
        self._owner = self._owner_pipe = self._endpoint = None
