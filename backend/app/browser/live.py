"""Single-order execution adapter. Only invoked by the local live-order service."""

import re
from decimal import Decimal

from playwright.sync_api import expect

from app.account_ledger import extract
from app.browser.alpha import TABS, fill_form, verify_identity
from app.browser.records import read_records
from app.browser.schemas import FillForm, token_identity


def select_panel(page, name):
    tab = page.get_by_role("tab", name=name, exact=True)
    if tab.count() != 1:
        raise ValueError(f"无法唯一识别{name}标签，请人工检查页面。")
    tab.click(timeout=3000)
    expect(tab).to_have_attribute("aria-selected", "true")
    panel_id = tab.get_attribute("aria-controls")
    if not panel_id or not re.fullmatch(r"[A-Za-z0-9_:-]+", panel_id):
        raise ValueError("委托区域缺少可核对的面板关联，当前页面尚不支持自动执行。")
    panel = page.locator('[role="tabpanel"]').filter(visible=True)
    panel = panel.and_(page.locator(f'[id="{panel_id}"]'))
    if panel.count() != 1 or not panel.is_visible():
        raise ValueError("委托区域尚未加载。")
    if panel.get_attribute("aria-busy") == "true":
        raise ValueError("委托区域正在加载，请稍后检查。")
    return panel


def current_order(page, payload):
    command = FillForm(**payload)
    verify_identity(page, command)
    panel = select_panel(page, "当前委托")
    # Empty requires an explicit visible message in the selected account panel.
    empty = panel.get_by_text(re.compile(r"^(暂无订单|暂无委托|暂无数据|无订单记录)$"))
    rows = panel.locator("tbody tr").filter(visible=True)
    data = [r for r in rows.all() if r.locator("td").count() >= 5]
    if not data and empty.count() == 1 and empty.is_visible():
        return {"empty": True}
    if len(data) != 1:
        raise ValueError("当前委托不是可确认的单笔订单，暂停自动处理。")
    cells = [s.strip() for s in data[0].locator("td").all_inner_texts()]
    side = TABS[command.side]
    if command.expected_symbol not in cells or side not in cells:
        raise ValueError("当前委托币种或方向与系统记录不同。")
    # No disappearance inference from missing/hidden/loading rows.
    return {"empty": False}


def latest_history(page, payload):
    command = FillForm(**payload)
    verify_identity(page, command)
    panel = select_panel(page, "历史委托")
    rows = panel.locator("tbody tr").filter(visible=True)
    if not rows.count():
        empty = panel.get_by_text(
            re.compile(r"^(暂无订单|暂无委托|暂无数据|无订单记录)$")
        )
        if empty.count() == 1 and empty.is_visible():
            return None
        raise ValueError("历史委托尚未加载，不能确认结果。")
    observation = read_records(page, command.url)
    orders, _ = extract(observation)
    if not orders:
        # Expand only the first row via a labelled control or the observed empty chevron cell.
        expand = rows.first.get_by_role(
            "button", name=re.compile(r"^(展开|展开详情|Expand)$")
        )
        if expand.count() != 1:
            cells = rows.first.locator("td")
            if cells.count() != 14 or cells.first.inner_text().strip():
                raise ValueError(
                    "无法识别第一笔历史委托的展开控件，请手动展开后再检查。"
                )
            expand = cells.first
        expand.click(timeout=3000)
        expect(panel.get_by_text(re.compile(r"^订单ID[:：]")).first).to_be_visible()
        orders, _ = extract(read_records(page, command.url))
    if len(orders) != 1:
        raise ValueError("无法读取第一笔历史委托汇总。")
    return orders[0]


def preflight(page, payload):
    if not current_order(page, payload)["empty"]:
        raise ValueError("平台已有挂单，不能叠加新单。")
    previous = latest_history(page, payload)
    fill_form(page, payload)
    command = FillForm(**payload)
    button = submit_button(page, command)
    button.click(trial=True, timeout=3000)
    return {"baseline_id": previous["order_id"] if previous else None}


def submit_button(page, command):
    # Exact accessible button name, never buy/sell tabs or a generic confirmation.
    button = page.get_by_role(
        "button", name=f"{TABS[command.side]} {command.expected_symbol}", exact=True
    )
    if button.count() != 1 or not button.is_enabled():
        raise ValueError("未找到唯一可用的下单按钮，当前页面尚不支持自动提交。")
    return button


def submit_once(page, payload):
    command = FillForm(**payload)
    if not current_order(page, payload)["empty"]:
        raise ValueError("提交前发现平台已有挂单，已停止。")
    fill_form(page, payload)
    state = verify_identity(page, command)
    if state["side"] != command.side:
        raise ValueError("下单方向发生变化。")
    button = submit_button(page, command)
    button.click(
        timeout=3000
    )  # Exactly one attempt. Never retry a possibly sent order.
    return {"clicked": True}


def inspect_order(page, payload):
    if token_identity(page.url) != token_identity(payload["url"]):
        raise ValueError("当前交易页面已切换。")
    if not current_order(page, payload)["empty"]:
        return {"pending": True}
    return {"pending": False, "order": latest_history(page, payload)}


def matches(order, payload, baseline_id):
    if not order or order["order_id"] == baseline_id:
        return False
    return (
        order["symbol"] == payload["expected_symbol"]
        and order["quote"] == payload["expected_quote"]
        and order["side"] == TABS[payload["side"]]
        and (order["chain"], order["address"]) == token_identity(payload["url"])
        and Decimal(order.get("requested_quantity", "-1"))
        == Decimal(payload["quantity"])
        and Decimal(order.get("limit_price", "-1")) == Decimal(payload["price"])
        and Decimal(order["quantity"]) <= Decimal(payload["quantity"])
    )
