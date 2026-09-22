"""Own Playwright in a dedicated process; explicit live actions are separate from fill."""

from multiprocessing.connection import Connection
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

from app.browser.alpha import fill_form, inspect_form
from app.browser.automatic import available_balance, cancel_once, inspect_progress
from app.browser.confirmation import confirmation_preview
from app.browser.live import (
    inspect_order,
    inspect_unsubmitted,
    order_readiness,
    preflight,
    submit_once,
)
from app.browser.records import read_records


def run_worker(pipe: Connection, profile: str):
    with sync_playwright() as playwright:
        context = None
        page = None
        try:
            while True:
                try:
                    command = pipe.recv()
                except EOFError:
                    break
                action = command["action"]
                try:
                    filled = None
                    if action == "stop":
                        break
                    if action == "launch":
                        if context is None or not context.browser.is_connected():
                            Path(profile).mkdir(parents=True, exist_ok=True)
                            context = playwright.chromium.launch_persistent_context(
                                profile,
                                channel="chrome",
                                headless=False,
                                no_viewport=True,
                                args=["--start-maximized"],
                            )
                            context.set_default_timeout(5000)
                            page = (
                                context.pages[0]
                                if context.pages
                                else context.new_page()
                            )
                        if page is None or page.is_closed():
                            page = context.new_page()
                        page.bring_to_front()
                    elif action == "open":
                        if context is None or not context.browser.is_connected():
                            raise ValueError("请先启动浏览器。")
                        if page is None or page.is_closed():
                            page = context.new_page()
                        page.goto(
                            command["url"], wait_until="domcontentloaded", timeout=30000
                        )
                        page.bring_to_front()
                    elif action == "fill":
                        if page is None or page.is_closed():
                            raise ValueError("请先打开交易页面。")
                        filled = fill_form(page, command["payload"])
                    elif action in {
                        "live_prepare",
                        "live_submit",
                        "live_inspect",
                        "order_readiness",
                        "live_unsubmitted",
                        "confirmation_preview",
                        "live_progress",
                        "live_cancel",
                        "live_balance",
                    }:
                        if page is None or page.is_closed():
                            raise ValueError("请先打开交易页面。")
                        operation = {
                            "live_prepare": preflight,
                            "live_submit": submit_once,
                            "live_inspect": inspect_order,
                            "order_readiness": order_readiness,
                            "live_unsubmitted": inspect_unsubmitted,
                            "confirmation_preview": confirmation_preview,
                            "live_progress": inspect_progress,
                            "live_cancel": cancel_once,
                            "live_balance": available_balance,
                        }[action]
                        result = operation(page, command["payload"])
                        pipe.send({"ok": True, **result})
                        continue
                    elif action == "read_records":
                        if page is None or page.is_closed():
                            raise ValueError("请先打开交易页面。")
                        observation = read_records(page, command["url"])
                        pipe.send({"ok": True, "observation": observation})
                        continue
                    elif action != "status":
                        raise ValueError("不支持的浏览器操作。")

                    connected = context is not None and context.browser.is_connected()
                    current = (
                        page if connected and page and not page.is_closed() else None
                    )
                    pipe.send(
                        {
                            "ok": True,
                            "connected": connected,
                            "url": current.url if current else "",
                            "title": current.title() if current else "",
                            **(
                                inspect_form(current)
                                if current
                                else {"fill_supported": False}
                            ),
                            "filled": filled,
                        }
                    )
                except (Error, ValueError, AssertionError) as exc:
                    # Avoid returning page contents or credentials in errors.
                    message = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else (
                            "页面操作或回读核对失败，请人工检查。若正在实盘提交，结果可能未知，不会自动重试下单。"
                        )
                    )
                    pipe.send({"ok": False, "message": message})
        finally:
            if context is not None:
                try:
                    context.close()
                except Error:
                    pass
            pipe.close()
