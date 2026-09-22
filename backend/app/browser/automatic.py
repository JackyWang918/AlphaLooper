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

CURRENT_PLATFORM_HEADERS = (
    "时间",
    "代币",
    "类型",
    "方向",
    "价格",
    "数量",
    "状态",
    "反向订单",
    "条件",
    "止盈/止损",
    "操作",
)


def no_dialog(page):
    if page.locator(MODALS).filter(visible=True).count():
        raise ValueError("页面有弹窗，请手动处理后恢复自动任务。")


def row_columns(row):
    """Pair a platform order row with its header, including split-table layouts."""
    table = row.locator("xpath=ancestor::table[1]")
    if table.count() != 1:
        raise ValueError("当前委托订单行没有唯一所属表格。")
    cell_locator = row.locator("td")
    cells = [cell_locator.nth(index) for index in range(cell_locator.count())]
    values = [value.strip() for value in cell_locator.all_inner_texts()]

    candidates = []

    def add_candidate(locator):
        headers = [value.strip() for value in locator.all_inner_texts()]
        # Binance may render decoration/action spacer headers without a data cell.
        # Remove only surplus empty edges; never discard a named or interior column.
        while len(headers) > len(values) and headers and not headers[-1]:
            headers.pop()
        while len(headers) > len(values) and headers and not headers[0]:
            headers.pop(0)
        if headers and headers not in candidates:
            candidates.append(headers)

    for header_row in table.locator("thead tr").filter(visible=True).all():
        add_candidate(header_row.locator("th, [role=columnheader]"))
    panel = row.locator("xpath=ancestor::*[@role='tabpanel'][1]")
    if panel.count() == 1:
        for header_row in panel.locator("thead tr").filter(visible=True).all():
            add_candidate(header_row.locator("th, [role=columnheader]"))
        add_candidate(panel.get_by_role("columnheader").filter(visible=True))

    if (
        values
        and not values[0]
        and any(len(headers) == len(values) - 1 for headers in candidates)
    ):
        values = values[1:]
        cells = cells[1:]
    matching = [headers for headers in candidates if len(headers) == len(values)]
    if len(matching) == 1:
        headers = matching[0]
    elif len(matching) > 1:
        raise ValueError("当前委托存在多组不同的可见表头，无法确定订单列含义。")
    elif (
        not candidates
        and len(values) == len(CURRENT_PLATFORM_HEADERS)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", values[0])
    ):
        # Observed Binance layout: its fixed header and scrollable body may be
        # rendered in separate, non-semantic containers. Validate the semantic
        # fields below before trusting these positions.
        headers = list(CURRENT_PLATFORM_HEADERS)
    else:
        headers = candidates[0] if candidates else []
    if len(headers) != len(values):
        observed = "、".join(value or "（空）" for value in headers)
        raise ValueError(
            f"当前委托表头有 {len(headers)} 列、订单行有 {len(values)} 列，"
            f"无法一一对应；实际表头：{observed}。"
        )
    return headers, values, cells


def unique_field(headers, values, names):
    matched = [index for index, header in enumerate(headers) if header in names]
    if len(matched) != 1:
        label = "/".join(names)
        raise ValueError(f"当前委托字段“{label}”匹配到 {len(matched)} 列。")
    return values[matched[0]]


def current_detail(page, payload):
    command = FillForm(**payload)
    no_dialog(page)
    if current_order(page, payload)["empty"]:
        return None, None
    panel = select_panel(page, "当前委托")
    rows = panel.locator("tbody tr").filter(visible=True)
    rows = [row for row in rows.all() if row.locator("td").count() >= 5]
    if len(rows) != 1:
        raise ValueError("必须唯一识别当前系统委托，不能批量撤单。")
    row = rows[0]
    headers, values, _ = row_columns(row)

    def numeric(names, unit):
        number, currency = amount(unique_field(headers, values, names))
        if currency != unit:
            raise ValueError(
                f"当前委托字段“{'/'.join(names)}”的单位应为 {unit}，实际为 {currency or '空'}。"
            )
        return Decimal(number)

    price = numeric(("委托价格", "委托价", "价格"), command.expected_quote)
    quantity = numeric(("数量", "委托数量"), command.expected_symbol)
    progress_headers = {"已成交", "成交数量", "累计成交量"} & set(headers)
    gross_headers = {"成交额", "累计成交额"} & set(headers)
    if progress_headers or gross_headers:
        if not progress_headers or not gross_headers:
            raise ValueError(
                "当前委托只显示了成交数量或成交额中的一项，无法核对累计成交。"
            )
        filled = numeric(tuple(progress_headers), command.expected_symbol)
        gross = numeric(tuple(gross_headers), command.expected_quote)
    else:
        status = unique_field(headers, values, ("状态",))
        if status in {"新订单", "未成交", "等待成交"}:
            # The current Binance layout omits progress columns for an untouched order.
            filled = gross = Decimal(0)
        else:
            raise ValueError(
                f"当前委托状态为“{status}”，但页面没有已成交和成交额列，"
                "无法确认是否发生部分成交。"
            )
    if (
        unique_field(headers, values, ("代币", "币种")) != command.expected_symbol
        or unique_field(headers, values, ("方向",)) != TABS[command.side]
        or unique_field(headers, values, ("类型", "订单类型")) != "限价"
        or price != Decimal(command.price)
        or quantity != Decimal(command.quantity)
        or not 0 <= filled <= quantity
        or (filled == 0) != (gross == 0)
        or (command.side == "buy" and gross > filled * price)
        or (command.side == "sell" and gross < filled * price)
    ):
        raise ValueError("当前委托币种、方向、限价或数量与系统记录不符。")
    return row, {"quantity": str(filled), "gross": str(gross)}


def cancel_button(row):
    button = row.get_by_role("button", name=re.compile(r"^(撤单|撤销|取消)$"))
    if button.count() == 1:
        return button
    headers, _, cells = row_columns(row)
    action = [
        index for index, header in enumerate(headers) if header in {"操作", "全部取消"}
    ]
    if len(action) == 1:
        candidate = (
            cells[action[0]].locator("button, [role=button]").filter(visible=True)
        )
        if candidate.count() == 1:
            return candidate
    raise ValueError("未找到当前委托行内唯一撤单按钮，未点击。")


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
    button = cancel_button(row)
    button.click(trial=True, timeout=3000)
    row, _ = current_detail(page, payload)
    if row is None:
        return {"cancel_clicked": False, "already_absent": True}
    button = cancel_button(row)
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
