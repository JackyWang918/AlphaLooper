"""User-triggered page tests. No test submits a buy or sell order."""

import re
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Literal
from uuid import UUID

from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import expect

from app.browser.alpha import (
    TABS,
    confirm_slider_at_max,
    verify_identity,
    wait_total_input,
)
from app.browser.automatic import (
    amount,
    no_dialog,
    row_columns,
    side_balance,
    unique_field,
)
from app.browser.confirmation import MODALS
from app.browser.live import select_panel
from app.browser.schemas import FillForm


class PageTest(FillForm):
    action: Literal[
        "fill_total",
        "buy_balance",
        "sell_balance",
        "orders",
        "cancel_all",
        "sell_slider",
    ]
    request_id: UUID
    side: Literal["buy", "sell"] = "buy"
    price: str = "1"
    quantity: str = "1"


def select_side(page, command, side):
    no_dialog(page)
    verify_identity(page, command)
    tab = page.get_by_role("tab", name=TABS[side], exact=True)
    tab.click(timeout=3000)
    expect(tab).to_have_attribute("aria-selected", "true")
    verify_identity(page, command)


def form_result(page, command):
    state = verify_identity(page, command)
    controls = {
        "price": page.locator("#limitPrice"),
        "quantity": page.locator("#limitAmount"),
        "quote_amount": wait_total_input(page),
    }
    values = {key: field.input_value() for key, field in controls.items()}
    validation = {
        key: field.evaluate(
            "e => ({valid: e.validity.valid, message: e.validationMessage, ariaInvalid: e.getAttribute('aria-invalid')})"
        )
        for key, field in controls.items()
    }
    errors = (
        page.locator('[role="alert"], .bn-formItem-errMsg, .bn-textField-error')
        .filter(visible=True)
        .all_inner_texts()
    )
    return {
        "side": state["side"],
        "symbol": state["symbol"],
        "quote": state["quote"],
        "values": values,
        "validation": validation,
        "page_errors": errors,
        "submitted": False,
    }


def fill_total(page, command):
    if command.quote_amount is None:
        raise ValueError("请填写成交额。")
    select_side(page, command, command.side)
    total = wait_total_input(page)
    price = page.locator("#limitPrice")
    # Let the platform validate/normalize the inputs; report its actual values.
    price.click(trial=True, timeout=3000)
    total.click(trial=True, timeout=3000)
    price.fill(command.price)
    total.fill(command.quote_amount)
    total.press("Tab")
    return form_result(page, command)


def current_rows(page, command):
    no_dialog(page)
    verify_identity(page, command)
    panel = select_panel(page, "当前委托")
    empty = panel.get_by_text(
        re.compile(r"^(暂无订单|暂无委托|暂无数据|无订单记录|无进行中的订单)$")
    )
    rows = panel.locator("tbody tr").filter(visible=True)
    expect(rows.first.locator("td").nth(2).or_(empty).first).to_be_visible(timeout=5000)
    data = [row for row in rows.all() if row.locator("td").count() >= 3]
    if not data and not (empty.count() == 1 and empty.is_visible()):
        raise ValueError("当前委托尚未加载，不能判定为空。")
    orders = []
    for row in data:
        headers, values, _ = row_columns(row)
        symbol = unique_field(headers, values, ("代币", "币种"))
        side = unique_field(headers, values, ("方向",))
        price, quote = amount(
            unique_field(headers, values, ("价格", "委托价", "委托价格"))
        )
        if not symbol or side not in {"买入", "卖出"} or Decimal(price) <= 0:
            raise ValueError("当前委托名称、方向或价格无法识别。")
        orders.append({"symbol": symbol, "side": side, "price": price, "quote": quote})
    return panel, orders


def cancel_all(page, command):
    panel, orders = current_rows(page, command)
    if not orders:
        return {
            "clicked": False,
            "completed": True,
            "orders": [],
            "message": "当前没有委托，无需撤单。",
        }
    button = panel.get_by_text(
        re.compile(r"^(全部取消|全部撤单|撤销全部|撤销全部订单|取消全部订单)$")
    ).filter(visible=True)
    if button.count() != 1:
        raise ValueError("当前委托面板中没有唯一的全部撤单控件，未点击。")
    button.click(timeout=3000)
    dialogs = page.locator(MODALS).filter(visible=True)
    empty = panel.get_by_text(
        re.compile(r"^(暂无订单|暂无委托|暂无数据|无订单记录|无进行中的订单)$")
    )
    try:
        expect(dialogs.first.or_(empty).first).to_be_visible(timeout=5000)
    except (BrowserTimeout, AssertionError):
        return {
            "clicked": True,
            "completed": False,
            "message": "已点击全部撤单，尚未确认全部结束；请点击读取当前委托核对。",
        }
    if dialogs.count():
        if dialogs.count() != 1:
            raise ValueError(
                "已点击全部撤单，但出现多个弹窗，请手动核对；不会重复点击。"
            )
        text = dialogs.inner_text()
        if re.search(
            r"验证码|人机验证|安全验证|风险测评|身份验证|验证器", text
        ) or not re.search(r"撤单|撤销|取消.*(?:订单|委托)", text):
            raise ValueError(
                "已点击全部撤单，平台提示需手动处理；不会自动确认未知弹窗。"
            )
        confirm = dialogs.get_by_role(
            "button", name=re.compile(r"^(确认|确定|继续|全部撤单|全部取消)$")
        )
        if confirm.count() != 1:
            raise ValueError("未找到唯一撤单确认按钮，请手动核对。")
        confirm.click(timeout=3000)
    try:
        expect(empty).to_be_visible(timeout=5000)
        _, remaining = current_rows(page, command)
        return {
            "clicked": True,
            "completed": not remaining,
            "orders": remaining,
            "message": "已核对当前委托为空。"
            if not remaining
            else "仍有委托，请重新读取核对。",
        }
    except BrowserTimeout:
        return {
            "clicked": True,
            "completed": False,
            "message": "已点击全部撤单，尚未确认全部结束；请点击读取当前委托核对。",
        }
    except AssertionError:
        return {
            "clicked": True,
            "completed": False,
            "message": "已点击全部撤单，尚未确认全部结束；请点击读取当前委托核对。",
        }


def sell_slider(page, command):
    select_side(page, command, "sell")
    # Restrict discovery to the smallest trade form containing price/amount/total.
    total = wait_total_input(page)
    scope = total.locator(
        "xpath=ancestor::*[.//input[@id='limitAmount'] and .//input[@id='limitPrice']][1]"
    )
    tracks = scope.locator('.bn-slider, .rc-slider, input[type="range"]').filter(
        visible=True
    )
    if tracks.count() != 1:
        tracks = scope.get_by_role("slider").filter(visible=True)
    if tracks.count() != 1:
        raise ValueError("卖出表单中没有唯一可拖动的进度条，未操作数量。")
    track = tracks
    track.scroll_into_view_if_needed()
    handle = track.locator(
        '.bn-slider-handle, .rc-slider-handle, [role="slider"]'
    ).filter(visible=True)
    if handle.count() > 1:
        raise ValueError("进度条存在多个滑块，无法确定卖出比例。")
    handle = handle if handle.count() == 1 else track
    box, thumb = track.bounding_box(), handle.bounding_box()
    # A role=slider can describe the thumb; use its local track container.
    for _ in range(3):
        if box and box["width"] >= 80:
            break
        track = track.locator("..")
        box = track.bounding_box()
    if not box or not thumb or box["width"] < 80:
        raise ValueError("无法确定卖出进度条范围。")
    price = page.locator("#limitPrice")
    price.click(trial=True, timeout=3000)
    price.fill(command.price)
    handle.click(trial=True, timeout=3000)
    # Input validation and actionability scrolling may have moved the slider.
    box, thumb = track.bounding_box(), handle.bounding_box()
    if not box or not thumb:
        raise ValueError("填写价格后进度条不可见，未执行拖动。")
    page.mouse.move(thumb["x"] + thumb["width"] / 2, thumb["y"] + thumb["height"] / 2)
    page.mouse.down()
    try:
        page.mouse.move(
            box["x"] + box["width"] - 1, box["y"] + box["height"] / 2, steps=20
        )
    finally:
        page.mouse.up()
    # Native ranges/accessible thumbs expose the endpoint for direct verification.
    semantic, maximum = confirm_slider_at_max(page, handle)
    # Restore the requested limit after the platform's slider-change handler.
    price.fill(command.price)
    price.press("Tab")
    result = form_result(page, command)
    result.update(dragged_to_end=True, slider_value=semantic, slider_max=maximum)
    return result


def run_test(page, payload):
    command = PageTest(**payload)
    with localcontext() as context:
        context.prec = 80
        if command.action == "fill_total":
            return fill_total(page, command)
        if command.action in {"buy_balance", "sell_balance"}:
            side = "buy" if command.action == "buy_balance" else "sell"
            if side == "buy" and command.expected_quote != "USDT":
                raise ValueError("请切换到 USDT 交易对后测试买入可用 USDT。")
            free, _ = side_balance(page, command, side)
            return {
                "available": format(
                    free.quantize(Decimal("0.1"), rounding=ROUND_DOWN), "f"
                )
                if side == "buy"
                else format(free, "f"),
                "currency": command.expected_quote
                if side == "buy"
                else command.expected_symbol,
            }
        if command.action == "orders":
            _, orders = current_rows(page, command)
            return {"orders": orders}
        if command.action == "cancel_all":
            return cancel_all(page, command)
        return sell_slider(page, command)
