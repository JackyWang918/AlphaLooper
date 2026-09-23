"""Read and fill observed Alpha controls. No submit or confirmation selectors."""

import re
import time
from decimal import Decimal, InvalidOperation, localcontext

from playwright.sync_api import Page, expect

from app.browser.schemas import FillForm, token_identity

TABS = {"buy": "买入", "sell": "卖出"}
FORM_READY_TIMEOUT = 5


class PageNotReady(ValueError):
    """The expected trade page is open, but required UI is still rendering."""


class FormNotReady(PageNotReady):
    """The expected trade page is open, but its form is still rendering."""


def total_inputs(page: Page):
    """Use the field's meaning as well as the legacy id; never input order."""
    label = re.compile(r"^成交额(?:\s*[（(]?(?:USDT|USDC)[）)]?)?$")
    grouped = page.locator(
        "xpath=//*[count(.//input)=1 and "
        ".//*[not(*) and normalize-space(.)='成交额']]//input"
    )
    return (
        page.locator("input#limitTotal")
        .or_(page.get_by_label(label))
        .or_(page.get_by_placeholder(label))
        .or_(grouped)
        .and_(page.locator("input:visible"))
    )


def wait_total_input(page: Page):
    deadline = time.monotonic() + 5
    while True:
        total = total_inputs(page)
        count = total.count()
        if count > 1:
            raise ValueError(f"成交额输入框识别到 {count} 个可见候选，无法唯一定位。")
        if count == 1 and total.is_editable():
            return total
        if time.monotonic() >= deadline:
            reason = "没有可见候选" if count == 0 else "输入框只读或禁用"
            raise ValueError(
                f"成交额输入框等待 5 秒后仍不可填写：{reason}"
                "（按成交额标签及 #limitTotal 定位）。"
            )
        time.sleep(0.1)


def inspect_form(page: Page):
    result = {
        "fill_supported": False,
        "symbol": "",
        "quote": "",
        "side": "",
        "reason": "请打开中文 Alpha 交易页面并等待表单加载。",
    }
    try:
        chain, address = token_identity(page.url)
    except ValueError:
        return result
    result.update(chain=chain, address=address)
    price, amount = page.locator("#limitPrice"), page.locator("#limitAmount")
    total = total_inputs(page)
    if price.count() != 1 or amount.count() != 1:
        return result
    if not price.is_visible() or not amount.is_visible():
        return result
    symbol = amount.locator("..").locator(".bn-textField-suffix").inner_text().strip()
    quote = price.locator("..").locator(".bn-textField-suffix").inner_text().strip()
    result.update(symbol=symbol, quote=quote)
    if not symbol or not quote:
        result["reason"] = "交易表单仍在加载币种和计价币。"
        return result
    if quote not in {"USDT", "USDC"}:
        result["reason"] = f"页面计价币为 {quote}，当前仅适配 USDT/USDC 表单。"
        return result
    active = []
    for side, label in TABS.items():
        tab = page.get_by_role("tab", name=label, exact=True)
        if tab.count() != 1:
            return result
        if tab.get_attribute("aria-selected") == "true":
            active.append(side)
    if len(active) != 1:
        return result
    result.update(
        symbol=symbol,
        quote=quote,
        side=active[0],
        price_step=price.get_attribute("step"),
        quantity_step=amount.get_attribute("step"),
        total_supported=total.count() == 1
        and total.is_visible()
        and total.is_editable(),
        fill_supported=price.is_editable() and amount.is_editable(),
        reason="表单已识别；仅填写，不提交。",
    )
    return result


def verify_identity(page: Page, command: FillForm):
    if token_identity(page.url) != token_identity(command.url):
        raise ValueError("当前页面的链或合约地址与指定链接不一致，已停止。")
    deadline = time.monotonic() + FORM_READY_TIMEOUT
    while True:
        state = inspect_form(page)
        if state["fill_supported"]:
            break
        if state.get("quote") and state["quote"] not in {"USDT", "USDC"}:
            raise ValueError(state["reason"])
        if time.monotonic() >= deadline:
            raise FormNotReady(state["reason"])
        time.sleep(0.1)
    if (
        state["symbol"] != command.expected_symbol
        or state["quote"] != command.expected_quote
    ):
        raise ValueError("页面币种或计价币发生变化，请刷新状态并重新核对。")
    return state


def check_step(value: str, step: str | None):
    try:
        with localcontext() as context:
            context.prec = 80
            size = Decimal(step or "0")
            if not size.is_finite() or size <= 0 or Decimal(value) % size != 0:
                raise ValueError("价格或数量不符合页面精度，请按页面步长填写。")
    except InvalidOperation as exc:
        raise ValueError("无法确认页面输入精度，已停止。") from exc


def set_sell_slider_to_max(page: Page, amount):
    """Drag the unique sell-form percentage slider to its maximum endpoint."""
    total = wait_total_input(page)
    scope = total.locator(
        "xpath=ancestor::*[.//input[@id='limitAmount'] and .//input[@id='limitPrice']][1]"
    )
    track = scope.locator('.bn-slider, .rc-slider, input[type="range"]').filter(
        visible=True
    )
    if track.count() != 1:
        track = scope.get_by_role("slider").filter(visible=True)
    if track.count() != 1:
        raise ValueError("卖出表单中没有唯一可见的数量进度条，未设置卖出数量。")

    handle = track.locator(
        '.bn-slider-handle, .rc-slider-handle, [role="slider"]'
    ).filter(visible=True)
    if handle.count() > 1:
        raise ValueError("卖出数量进度条存在多个滑块，未执行拖动。")
    handle = handle if handle.count() == 1 else track
    track_box, handle_box = track.bounding_box(), handle.bounding_box()
    # A role=slider may identify only the thumb. Walk up to its visible track.
    for _ in range(3):
        if track_box and track_box["width"] >= 80:
            break
        track = track.locator("..")
        track_box = track.bounding_box()
    if not track_box or not handle_box or track_box["width"] < 80:
        raise ValueError("无法确定卖出数量进度条的可拖动范围。")

    handle.click(trial=True, timeout=3000)
    page.mouse.move(
        handle_box["x"] + handle_box["width"] / 2,
        handle_box["y"] + handle_box["height"] / 2,
    )
    page.mouse.down()
    try:
        page.mouse.move(
            track_box["x"] + track_box["width"] + 2,
            track_box["y"] + track_box["height"] / 2,
            steps=20,
        )
    finally:
        page.mouse.up()
    page.wait_for_timeout(100)

    semantic, maximum = (
        handle.get_attribute("aria-valuenow"),
        handle.get_attribute("aria-valuemax"),
    )
    if handle.evaluate("e => e.matches('input[type=range]')"):
        semantic = handle.input_value()
        maximum = handle.get_attribute("max") or "100"
    if (
        semantic is not None
        and maximum is not None
        and Decimal(semantic) != Decimal(maximum)
    ):
        raise ValueError("卖出数量进度条没有到达最大值，未提交订单。")

    deadline = time.monotonic() + 3
    while True:
        value = amount.input_value().replace(",", "")
        try:
            if value and Decimal(value) > 0:
                return value
        except InvalidOperation:
            pass
        if time.monotonic() >= deadline:
            raise ValueError("进度条拉满后平台没有生成可卖数量，未提交订单。")
        page.wait_for_timeout(100)


def fill_form(page: Page, payload: dict):
    command = FillForm(**payload)
    initial = verify_identity(page, command)
    check_step(command.price, initial["price_step"])
    if not command.sell_all and command.quote_amount is None:
        check_step(command.quantity, initial["quantity_step"])
    tab = page.get_by_role("tab", name=TABS[command.side], exact=True)
    # fill() alone can write behind an overlay; verify pointer actionability first.
    tab.click(trial=True, timeout=3000)
    if initial["side"] != command.side:
        tab.click(timeout=5000)
    expect(tab).to_have_attribute("aria-selected", "true")
    state = verify_identity(page, command)
    check_step(command.price, state["price_step"])
    if not command.sell_all and command.quote_amount is None:
        check_step(command.quantity, state["quantity_step"])
    price, amount = page.locator("#limitPrice"), page.locator("#limitAmount")
    # Resolve all required fields before changing the order price.
    total = wait_total_input(page) if command.side == "buy" else None
    price.click(trial=True, timeout=3000)
    price.fill(command.price)
    if command.side == "buy":
        with localcontext() as context:
            context.prec = 80
            quote_amount = command.quote_amount or format(
                Decimal(command.price) * Decimal(command.quantity), "f"
            )
        total.click(trial=True, timeout=3000)
        total.fill(quote_amount)
        total.press("Tab")
    elif command.sell_all:
        actual_quantity = set_sell_slider_to_max(page, amount)
        quote_amount = None
        # Slider handlers can reset the limit price. Set the requested price last.
        price.fill(command.price)
    else:
        amount.click(trial=True, timeout=3000)
        amount.fill(command.quantity)
        amount.press("Tab")
        quote_amount = None
    state = verify_identity(page, command)
    if state["side"] != command.side:
        raise ValueError("填表过程中买卖方向发生变化，请人工检查。")
    return {
        "side": state["side"],
        "symbol": state["symbol"],
        "quote": state["quote"],
        "price": command.price,
        "quantity": actual_quantity if command.sell_all else command.quantity,
        "quote_amount": quote_amount,
        "submitted": False,
    }
