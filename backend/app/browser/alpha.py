"""Read and fill observed Alpha controls. No submit or confirmation selectors."""

from decimal import Decimal, InvalidOperation, localcontext

from playwright.sync_api import Page, expect

from app.browser.schemas import FillForm, token_identity

TABS = {"buy": "买入", "sell": "卖出"}


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
    if price.count() != 1 or amount.count() != 1:
        return result
    if not price.is_visible() or not amount.is_visible():
        return result
    symbol = amount.locator("..").locator(".bn-textField-suffix").inner_text().strip()
    quote = price.locator("..").locator(".bn-textField-suffix").inner_text().strip()
    if not symbol or quote not in {"USDT", "USDC"}:
        result["reason"] = "币种或计价币尚未加载，当前仅适配 USDT/USDC 表单。"
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
        fill_supported=price.is_editable() and amount.is_editable(),
        reason="表单已识别；仅填写，不提交。",
    )
    return result


def verify_identity(page: Page, command: FillForm):
    if token_identity(page.url) != token_identity(command.url):
        raise ValueError("当前页面的链或合约地址与指定链接不一致，已停止。")
    state = inspect_form(page)
    if not state["fill_supported"]:
        raise ValueError(state["reason"])
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


def fill_form(page: Page, payload: dict):
    command = FillForm(**payload)
    initial = verify_identity(page, command)
    check_step(command.price, initial["price_step"])
    check_step(command.quantity, initial["quantity_step"])
    tab = page.get_by_role("tab", name=TABS[command.side], exact=True)
    # fill() alone can write behind an overlay; verify pointer actionability first.
    tab.click(trial=True, timeout=3000)
    if initial["side"] != command.side:
        tab.click(timeout=5000)
    expect(tab).to_have_attribute("aria-selected", "true")
    state = verify_identity(page, command)
    check_step(command.price, state["price_step"])
    check_step(command.quantity, state["quantity_step"])
    price, amount = page.locator("#limitPrice"), page.locator("#limitAmount")
    price.click(trial=True, timeout=3000)
    price.fill(command.price)
    price.press("Tab")
    verify_identity(page, command)
    expect(tab).to_have_attribute("aria-selected", "true")
    amount.click(trial=True, timeout=3000)
    amount.fill(command.quantity)
    amount.press("Tab")
    # Recheck after blur because controlled inputs may normalize or reject values.
    expect(price).to_have_value(command.price)
    expect(amount).to_have_value(command.quantity)
    state = verify_identity(page, command)
    if state["side"] != command.side:
        raise ValueError("填表过程中买卖方向发生变化，请人工检查。")
    return {
        "side": state["side"],
        "symbol": state["symbol"],
        "quote": state["quote"],
        "price": price.input_value(),
        "quantity": amount.input_value(),
        "submitted": False,
    }
