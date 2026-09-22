"""Strict current-order and available-balance adapter for automatic tasks.

Unknown layouts fail closed. Cancellation uses the page's cancel-all action and
confirms one ordinary cancel-all dialog. Its return value never establishes
final fills or permits a replacement order.
"""

import re
import time
from decimal import Decimal

from app.browser.alpha import TABS, verify_identity
from app.browser.confirmation import MODALS, NUMBER, confirmation_dialogs
from app.browser.live import current_order, select_panel
from app.browser.schemas import FillForm


def amount(value):
    match = re.fullmatch(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s+([A-Za-z0-9]+)", value)
    if not match:
        raise ValueError("无法识别金额或数量单位")
    return match[1].replace(",", ""), match[2]


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
    if (
        unique_field(headers, values, ("代币", "币种")) != command.expected_symbol
        or unique_field(headers, values, ("方向",)) != TABS[command.side]
        or unique_field(headers, values, ("类型", "订单类型")) != "限价"
        or quantity <= 0
    ):
        raise ValueError("当前委托币种、方向或订单类型与系统记录不符。")
    # Platform quantities are authoritative (quote buys and percentage sells).
    # Progress columns are optional; balances drive the ledger.
    detail = {
        "price": str(price),
        "requested_quantity": str(quantity),
        "side": command.side,
    }
    for names, key in [
        (("剩余数量", "未成交数量"), "remaining_quantity"),
        (("已成交", "成交数量", "累计成交量"), "filled_quantity"),
    ]:
        if set(names) & set(headers):
            detail[key] = str(numeric(names, command.expected_symbol))
    if "filled_quantity" in detail:
        detail["remaining_quantity"] = str(
            quantity - Decimal(detail["filled_quantity"])
        )
    if (
        "remaining_quantity" in detail
        and not 0 <= Decimal(detail["remaining_quantity"]) <= quantity
    ):
        raise ValueError("当前委托剩余数量无效。")
    return row, detail


def cancel_all_button(page, row):
    panel = select_panel(page, "当前委托")
    button = panel.get_by_role(
        "button", name=re.compile(r"^(全部取消|取消全部|撤销全部)$")
    ).filter(visible=True)
    if button.count() == 1:
        return button
    if button.count() > 1:
        raise ValueError("当前委托区域有多个“全部取消”按钮，未点击。")

    # Some observed layouts expose the sole cancel-all control as an icon in
    # the only order row. The task invariant still requires exactly one order.
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
    raise ValueError("未找到当前委托区域唯一的“全部取消”控件，未点击。")


def confirm_cancel_all(page):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        dialogs = confirmation_dialogs(page)
        if dialogs.count() > 1:
            raise ValueError("出现多个可见弹窗，未点击撤单确认。")
        if dialogs.count() == 1:
            text = dialogs.inner_text()
            if re.search(r"验证码|人机验证|安全验证|风险测评|身份验证|验证器", text):
                raise ValueError("撤单触发平台验证，请手动处理；未点击确认。")
            if re.search(r"确定取消全部订单\s*[?？]?", text):
                button = dialogs.get_by_role("button", name="确认", exact=True).filter(
                    visible=True
                )
                if button.count() != 1 or not button.is_enabled():
                    raise ValueError(
                        "取消全部订单弹窗没有唯一可用的“确认”按钮，未点击。"
                    )
                button.click(trial=True, timeout=3000)
                button.click(timeout=3000)
                return {"cancel_all_confirmed": True}
        page.wait_for_timeout(100)
    raise ValueError("取消全部订单的确认弹窗未出现；撤单结果未知，未继续点击。")


def inspect_progress(page, payload):
    refreshed = False
    if payload.get("refresh_before_check"):
        page.reload(wait_until="domcontentloaded", timeout=30000)
        refreshed = True
    _, detail = current_detail(page, payload)
    try:
        balances = wallet_snapshot(
            page, payload, pending=detail is not None, detail=detail
        )
    except ValueError as exc:
        if "冻结余额尚未释放" in str(exc):
            return {
                "pending": detail is not None,
                "settling": True,
                "page_refreshed": refreshed,
            }
        raise
    _, after = current_detail(page, payload)
    if detail != after:
        return {
            "pending": after is not None,
            "settling": True,
            "page_refreshed": refreshed,
        }
    return {
        "pending": detail is not None,
        "current_order": detail,
        "balances": balances,
        "page_refreshed": refreshed,
    }


def cancel_once(page, payload):
    row, progress = current_detail(page, payload)
    if row is None:
        return {"cancel_clicked": False, "already_absent": True}
    button = cancel_all_button(page, row)
    button.click(trial=True, timeout=3000)
    row, _ = current_detail(page, payload)
    if row is None:
        return {"cancel_clicked": False, "already_absent": True}
    button = cancel_all_button(page, row)
    button.click(timeout=3000)
    confirmation = confirm_cancel_all(page)
    return {
        "cancel_clicked": True,
        "cancel_all": True,
        "confirmation_clicked": True,
        "progress": progress,
        **confirmation,
    }


def side_balance(page, command, side):
    verify_identity(page, command)
    no_dialog(page)
    page.get_by_role("tab", name=TABS[side], exact=True).click(timeout=3000)
    if verify_identity(page, command)["side"] != side:
        raise ValueError("不能确认余额读取方向。")
    unit = re.escape(
        command.expected_quote if side == "buy" else command.expected_symbol
    )
    text = page.locator("body").inner_text()

    def read(label, required=False):
        pattern = (
            rf"(?:^|\n)[ \t]*(?:{label})\s*[:：]?\s*({NUMBER})\s+{unit}[ \t]*(?=\n|$)"
        )
        found = re.findall(pattern, text)
        if len(found) > 1 or (required and not found):
            raise ValueError(f"未能唯一识别{TABS[side]}侧{label}余额。")
        return Decimal(found[0].replace(",", "")) if found else None

    free = read("可用(?:余额)?", True)
    frozen = read("冻结(?:余额)?|锁定(?:余额)?")
    total = read("总余额|总资产数量")
    if total is None and frozen is not None:
        total = free + frozen
    if total is not None and total < free:
        raise ValueError("总余额小于可用余额，等待页面更新。")
    return free, total


def wallet_snapshot(page, payload, *, pending=False, detail=None):
    """Read both currencies. Missing frozen funds never become a fictitious loss."""
    command = FillForm(**payload)
    quote_free, quote_total = side_balance(page, command, "buy")
    base_free, base_total = side_balance(page, command, "sell")
    if not pending:
        # Explicit frozen funds after disappearance mean release is still settling.
        if (quote_total is not None and quote_total != quote_free) or (
            base_total is not None and base_total != base_free
        ):
            raise ValueError("委托已消失，但冻结余额尚未释放，等待更新。")
        quote_total, base_total = quote_free, base_free
    elif command.side == "buy":
        base_total = base_free if base_total is None else base_total
        if quote_total is None and detail and "remaining_quantity" in detail:
            quote_total = quote_free + Decimal(detail["remaining_quantity"]) * Decimal(
                detail["price"]
            )
    else:
        quote_total = quote_free if quote_total is None else quote_total
        if base_total is None and detail and "remaining_quantity" in detail:
            base_total = base_free + Decimal(detail["remaining_quantity"])
    no_dialog(page)
    verify_identity(page, command)
    return {
        key: None if value is None else str(value)
        for key, value in {
            "quote_available": quote_free,
            "base_available": base_free,
            "quote_total": quote_total,
            "base_total": base_total,
        }.items()
    }


def available_balance(page, payload):
    return wallet_snapshot(page, payload)
