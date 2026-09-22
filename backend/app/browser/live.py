"""Single-order execution adapter. Only invoked by the local live-order service."""

import re
import time
from contextlib import contextmanager
from decimal import Decimal

from playwright.sync_api import Error, expect

from app.account_ledger import HEADERS, extract
from app.browser.alpha import TABS, fill_form, verify_identity
from app.browser.confirmation import confirm_once
from app.browser.records import read_records
from app.browser.schemas import FillForm, token_identity


@contextmanager
def stage(name):
    try:
        yield
    except (Error, AssertionError) as exc:
        raise ValueError(f"{name}失败：控件未就绪、被遮挡或页面结构不匹配。") from exc
    except ValueError as exc:
        raise ValueError(f"{name}：{exc}") from exc


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
    expect(panel).to_be_visible(timeout=5000)
    if panel.get_attribute("aria-busy") == "true":
        raise ValueError("委托区域正在加载，请稍后检查。")
    return panel


def current_order(page, payload):
    command = FillForm(**payload)
    verify_identity(page, command)
    panel = select_panel(page, "当前委托")
    # Empty requires an explicit visible message in the selected account panel.
    empty = panel.get_by_text(
        re.compile(r"^(暂无订单|暂无委托|暂无数据|无订单记录|无进行中的订单)$")
    )
    rows = panel.locator("tbody tr").filter(visible=True)
    with stage("等待当前委托列表加载"):
        expect(rows.first.locator("td").nth(4).or_(empty).first).to_be_visible(
            timeout=5000
        )
    data = [r for r in rows.all() if r.locator("td").count() >= 5]
    if not data and empty.count() == 1 and empty.is_visible():
        return {"empty": True}
    if len(data) != 1:
        raise ValueError(
            "已找到当前委托区域，但未能确认无挂单或识别唯一订单；本次检查已停止，请核对平台列表。"
        )
    cells = [s.strip() for s in data[0].locator("td").all_inner_texts()]
    side = TABS[command.side]
    if command.expected_symbol not in cells or side not in cells:
        raise ValueError("当前委托币种或方向与系统记录不同。")
    # No disappearance inference from missing/hidden/loading rows.
    return {"empty": False}


def latest_history(page, payload):
    command = FillForm(**payload)
    verify_identity(page, command)
    with stage("切换历史委托标签"):
        panel = select_panel(page, "历史委托")
    rows = panel.locator("tbody tr").filter(visible=True)
    empty = panel.get_by_text(re.compile(r"^(暂无订单|暂无委托|暂无数据|无订单记录)$"))
    with stage("等待历史委托列表加载"):
        expect(rows.first.locator("td").nth(12).or_(empty).first).to_be_visible(
            timeout=5000
        )
    if not rows.count():
        if empty.count() == 1 and empty.is_visible():
            return None
        raise ValueError("历史委托尚未加载，不能确认结果。")
    if (
        rows.first.locator("td").count() < 13
        and empty.count() == 1
        and empty.is_visible()
    ):
        return None
    observation = read_records(page, command.url)
    orders, _ = extract(observation)
    if not orders:
        # Expand only the first row via a labelled control or the observed empty chevron cell.
        expand = rows.first.get_by_role(
            "button", name=re.compile(r"^(展开|展开详情|Expand)$")
        )
        if expand.count() != 1:
            cells = rows.first.locator("td")
            headers = [
                h.strip() for h in panel.locator("th").all_inner_texts() if h.strip()
            ]
            if headers != HEADERS:
                raise ValueError("历史委托表头不匹配，不能自动展开。")
            first_text = cells.first.inner_text().strip() if cells.count() else ""
            if cells.count() == 14 and not first_text:
                icons = cells.first.locator("svg").filter(visible=True)
                expand = icons if icons.count() == 1 else cells.first
            elif cells.count() == 13 and re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", first_text
            ):
                # Some layouts put the disclosure icon in the creation-time cell.
                expand = cells.first.locator("svg").filter(visible=True)
                if expand.count() != 1:
                    raise ValueError("第一笔历史委托的时间列中没有唯一展开图标。")
            else:
                raise ValueError(
                    f"无法识别第一笔历史委托的展开控件（{cells.count()} 列），请手动展开后再检查。"
                )
        with stage("展开第一笔历史委托"):
            expand.click(timeout=3000)
        with stage("等待第一笔历史委托的订单 ID"):
            expect(panel.get_by_text(re.compile(r"^订单ID[:：]")).first).to_be_visible()
        orders, _ = extract(read_records(page, command.url))
    if len(orders) != 1:
        raise ValueError("无法读取第一笔历史委托汇总。")
    return orders[0]


def order_readiness(page, payload):
    """Read orders only: no form writes, trading clicks, or ledger writes."""
    with stage("检查当前委托"):
        if not current_order(page, payload)["empty"]:
            raise ValueError("平台已有挂单，不能叠加新单。")
    with stage("读取提交前最新历史委托"):
        previous = latest_history(page, payload)
    with stage("返回当前委托"):
        select_panel(page, "当前委托")
    return {"baseline_id": previous["order_id"] if previous else None}


def preflight(page, payload):
    result = order_readiness(page, payload)
    with stage("提交前填表与回读"):
        fill_form(page, payload)
    with stage("检查提交按钮可点击性（未点击）"):
        command = FillForm(**payload)
        button = submit_button(page, command)
        button.click(trial=True, timeout=3000)
    return result


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
    deadline = payload.get("quote_valid_until")
    if deadline is not None and time.time() > deadline:
        raise ValueError("行情已过期，未点击买卖按钮。")
    with stage("第一次点击买卖按钮（打开确认弹窗）"):
        button.click(timeout=3000)  # Never retry a possibly sent order.
    with stage("核对订单确认弹窗并点击一次继续"):
        confirmation = confirm_once(page, command, deadline=deadline)
    return {"clicked": True, "confirmation_clicked": True, "confirmation": confirmation}


def inspect_order(page, payload):
    if token_identity(page.url) != token_identity(payload["url"]):
        raise ValueError("当前交易页面已切换。")
    if not current_order(page, payload)["empty"]:
        return {"pending": True}
    return {"pending": False, "order": latest_history(page, payload)}


def inspect_unsubmitted(page, payload):
    # A lingering confirmation could still send the old intent later.
    with stage("检查交易确认弹窗是否已关闭"):
        if (
            page.locator('[role="dialog"], [aria-modal="true"], .bn-modal')
            .filter(visible=True)
            .count()
        ):
            raise ValueError("请先手动关闭平台确认弹窗，再核对未提交状态。")
    result = inspect_order(page, payload)
    if (
        page.locator('[role="dialog"], [aria-modal="true"], .bn-modal')
        .filter(visible=True)
        .count()
    ):
        raise ValueError("平台仍有弹窗，请先手动关闭。")
    return result


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
