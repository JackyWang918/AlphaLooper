"""Keep Chrome alive independently of a disposable automation worker."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import sync_playwright


def endpoint_available(endpoint):
    if not endpoint:
        return False
    parts = urlsplit(endpoint)
    if parts.scheme != "ws" or parts.hostname != "127.0.0.1":
        return False
    try:
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{parts.port}/json/version", timeout=1
        ) as response:
            version = json.load(response)
        observed = urlsplit(version["webSocketDebuggerUrl"])
        return observed.port == parts.port and observed.path == parts.path
    except (OSError, ValueError, KeyError):
        return False


def run_browser_owner(pipe, profile, headless=False):
    context = None
    try:
        with sync_playwright() as playwright:
            try:
                Path(profile).mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    profile,
                    channel="chrome",
                    headless=headless,
                    no_viewport=True,
                    args=[
                        "--start-maximized",
                        "--remote-debugging-address=127.0.0.1",
                        "--remote-debugging-port=0",
                    ],
                )
                lines = (Path(profile) / "DevToolsActivePort").read_text().splitlines()
                if (
                    len(lines) != 2
                    or not lines[0].isdigit()
                    or not 0 < int(lines[0]) < 65536
                    or not re.fullmatch(r"/devtools/browser/[a-f0-9-]+", lines[1])
                ):
                    raise ValueError("浏览器调试连接信息无效。")
                pipe.send(
                    {"ok": True, "endpoint": f"ws://127.0.0.1:{lines[0]}{lines[1]}"}
                )
                # No page automation in this process. Only explicit shutdown
                # closes the browser; worker timeout cannot reach this owner.
                pipe.recv()
            finally:
                if context is not None:
                    context.close()
    except Exception:  # noqa: BLE001 -- do not expose profile/page data
        try:
            pipe.send(
                {
                    "ok": False,
                    "message": "Chrome 启动或保持进程失败；请检查是否有旧版浏览器占用用户目录。",
                }
            )
        except (EOFError, OSError):
            pass
    finally:
        pipe.close()
