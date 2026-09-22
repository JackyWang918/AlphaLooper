"""Strict current-order and available-balance adapter for automatic tasks.

Unknown layouts fail closed. Cancellation is a single row-scoped click; its
return value never establishes final fills or permits a replacement order.
"""

import re
from decimal import Decimal

from app.account_ledger import amount
from app.browser.alpha import TABS, verify_identity
from app.browser.confirmation import MODALS, NUMBER
from app.browser.live import current_order, select_panel
from app.browser.schemas import FillForm


def no_dialog(page):
    if page.locator(MODALS).filter(visible=True).count():
        raise ValueError("页面有弹窗，请手动处理后恢复自动任务。")


def current_detail(page, payload):
    command = FillForm(**payload)
    no_dialog(page)
    if current_order(page, payload)["empty"]:
        return None, None
    panel = select_panel(page, "当前委托")
    tables = panel.locator("table").filter(visible=True)
    rows = panel.locator("tbody tr").filter(visible=True)
    rows = [row for row in rows.all() if row.locator("td").count() >= 5]
    if len(rows) != 1:
        raise ValueError("必须唯一识别当前系统委托，不能批量撤单。")
    row = rows[0]
    headers = [h.strip() for h in tables.locator("th").all_inner_texts()]
    values = [v.strip() for v in row.locator("td").all_inner_texts()]
    if len(values) == len(headers) + 1 and not values[0]:
        values = values[1:]
    if len(headers) != len(values) or len(set(headers)) != len(headers):
        raise ValueError("当前委托表头与字段不能唯一对应。")
    fields = dict(zip(headers, values))

    def numeric(names, unit):
        keys = [name for name in names if name in fields]
        if len(keys) != 1:
            raise ValueError("当前委托缺少唯一字段：" + "/".join(names))
        number, currency = amount(fields[keys[0]])
        if currency != unit:
            raise ValueError("当前委托金额或数量单位不匹配。")
        return Decimal(number)

    price = numeric(("委托价格", "委托价"), command.expected_quote)
    quantity = numeric(("数量",), command.expected_symbol)
    filled = numeric(("已成交",), command.expected_symbol)
    gross = numeric(("成交额",), command.expected_quote)
    if (
        fields.get("代币") != command.expected_symbol
        or fields.get("方向") != TABS[command.side]
        or fields.get("类型") != "限价"
        or price != Decimal(command.price)
        or quantity != Decimal(command.quantity)
        or not 0 <= filled <= quantity
        or (filled == 0) != (gross == 0)
        or (command.side == "buy" and gross > filled * price)
        or (command.side == "sell" and gross < filled * price)
    ):
        raise ValueError("当前委托币种、方向、限价或数量与系统记录不符。")
    return row, {"quantity": str(filled), "gross": str(gross)}


def inspect_progress(page, payload):
    from app.browser.live import latest_history

    _, progress = current_detail(page, payload)
    if progress is None:
        return {"pending": False, "order": latest_history(page, payload)}
    return {"pending": True, "progress": progress}


def cancel_once(page, payload):
    row, progress = current_detail(page, payload)
    if row is None:
        return {"cancel_clicked": False, "already_absent": True}
    button = row.get_by_role("button", name=re.compile(r"^(撤单|撤销|取消)$"))
    if button.count() != 1:
        raise ValueError("未找到当前委托行内唯一撤单按钮，未点击。")
    button.click(trial=True, timeout=3000)
    row, _ = current_detail(page, payload)
    if row is None:
        return {"cancel_clicked": False, "already_absent": True}
    button = row.get_by_role("button", name=re.compile(r"^(撤单|撤销|取消)$"))
    if button.count() != 1:
        raise ValueError("撤单按钮发生变化，未点击。")
    button.click(timeout=3000)
    no_dialog(page)
    return {"cancel_clicked": True, "progress": progress}


def available_balance(page, payload):
    """Switch only the side tab; never fill inputs or submit an order."""
    command = FillForm(**payload)
    verify_identity(page, command)
    no_dialog(page)
    tab = page.get_by_role("tab", name="卖出", exact=True)
    tab.click(timeout=3000)
    state = verify_identity(page, command)
    if state["side"] != "sell":
        raise ValueError("不能确认卖出方向，未读取余额。")
    unit = re.escape(command.expected_symbol)
    # Read the complete visible line(s); duplicate balances are ambiguous.
    pattern = (
        rf"(?:^|\n)[ \t]*可用(?:余额)?\s*[:：]?\s*({NUMBER})\s+{unit}[ \t]*(?=\n|$)"
    )
    found = re.findall(pattern, page.locator("body").inner_text())
    if len(found) != 1:
        raise ValueError("未能唯一识别卖出侧的代币可用余额，请核对页面布局。")
    no_dialog(page)
    verify_identity(page, command)
    return {"available": str(Decimal(found[0].replace(",", "")))}
