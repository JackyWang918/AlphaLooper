import multiprocessing
import threading

from app.browser.worker import run_worker
from app.database import PROJECT_ROOT


class BrowserBusy(Exception):
    pass


class BrowserManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._pipe = None

    def execute(self, action: str, url: str = "", payload: dict | None = None):
        if not self._lock.acquire(blocking=False):
            raise BrowserBusy("浏览器正在执行操作，请稍后重试。")
        try:
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
                self._pipe, child = context.Pipe()
                self._process = context.Process(
                    target=run_worker,
                    args=(child, str(PROJECT_ROOT / "data" / "browser-profile")),
                    daemon=True,
                )
                self._process.start()
                child.close()
            try:
                self._pipe.send({"action": action, "url": url, "payload": payload})
                if not self._pipe.poll(45):
                    self._dispose()
                    return {
                        "ok": False,
                        "message": "执行器响应超时，已停止接收操作。请检查浏览器后重新启动。",
                    }
                return self._pipe.recv()
            except (EOFError, OSError):
                self._dispose()
                return {"ok": False, "message": "浏览器执行器已退出，请重新启动。"}
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
