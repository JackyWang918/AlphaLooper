"""Read visible table evidence only; never click, submit, fetch or infer fills."""

import time

from playwright.sync_api import Page

from app.browser.schemas import token_identity

# Deliberately return a bounded set of visible table cells and labels, not HTML,
# request headers, storage, cookies, or the full page text. These are observations,
# not verified account orders: adapters require a separately verified schema.
VISIBLE_TABLES = r"""() => {
  const visible = el => !!el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  let truncated = false;
  let remaining = 30000;
  const text = el => {
    let value = (el.innerText || '').trim();
    // Details are used only for the order ID, never for individual fills.
    const orderId = value.match(/^订单ID[:：]\s*(\d+)\b/);
    if (orderId) value = '订单ID: ' + orderId[1];
    const size = Math.min(500, remaining);
    if (value.length > size) truncated = true;
    remaining -= Math.min(size, value.length);
    return value.slice(0, size);
  };
  const all = [...document.querySelectorAll('table, [role="table"], [role="grid"]')]
    .filter(el => visible(el) && !el.parentElement?.closest('table, [role="table"], [role="grid"]'));
  if (all.length > 10) truncated = true;
  const tables = all.slice(0, 10).map(table => {
    const headers = [...table.querySelectorAll('th, [role="columnheader"]')].filter(visible).slice(0,31).map(text);
    const source = [...table.querySelectorAll('tr, [role="row"]')].filter(visible);
    if (source.length > 201) truncated = true;
    const rows = source.slice(0,201).map(row => [...row.querySelectorAll('td, [role="cell"], [role="gridcell"]')]
      .filter(visible).slice(0,31).map(text)).filter(row => row.length);
    if (headers.length > 30 || rows.length > 200 || rows.some(row=>row.length>30)) truncated = true;
    return {headers: headers.slice(0,30), rows: rows.slice(0,200).map(row=>row.slice(0,30))};
  });
  const tabs = [...document.querySelectorAll('[role="tab"]')].filter(visible).slice(0,40)
    .map(el => ({label:text(el), selected:el.getAttribute('aria-selected')==='true'}));
  const needsLogin = [...document.querySelectorAll('a, button')].filter(visible)
    .some(el => /^(登录|立即登录|Log In|Login|Sign In)$/i.test((el.innerText || '').trim()));
  const emptyLabels = [...document.querySelectorAll('[role="tabpanel"]')].filter(visible)
    .some(el => /暂无(?:订单|委托|成交|记录|数据)|无(?:订单|委托|成交)记录|No (?:orders|records|trades)/i.test(el.innerText || ''));
  return {tables, tabs, login_prompt_visible:needsLogin, empty_label_visible:emptyLabels, truncated};
}"""

ORDER_HEADERS = {
    "订单编号",
    "订单号",
    "委托编号",
    "委托时间",
    "成交时间",
    "成交编号",
    "成交额",
    "手续费",
    "状态",
    "Order ID",
    "Trade ID",
    "Fee",
    "Status",
}


def read_records(page: Page, expected_url: str):
    expected = token_identity(expected_url)
    if token_identity(page.url) != expected:
        raise ValueError("当前页面币种与控制台链接不一致，请先核对交易页面。")
    observed = page.evaluate(VISIBLE_TABLES)
    if token_identity(page.url) != expected:
        raise ValueError("读取期间页面已切换，本次结果已丢弃。")
    tables = observed["tables"]
    candidates = [
        i
        for i, table in enumerate(tables)
        if any(h in ORDER_HEADERS for h in table["headers"])
    ]
    if observed["login_prompt_visible"]:
        status = "login_required"
        message = (
            "页面显示登录入口，请手动登录并打开订单记录区域。不能将此状态视为空账户。"
        )
    elif candidates:
        status = "needs_mapping"
        message = "已读取订单候选表格；订单账本按汇总行及其展开的订单 ID 导入，手续费按成交额估算。"
    elif observed["empty_label_visible"]:
        status = "empty_view_unverified"
        message = "当前记录区域显示空提示；登录状态、筛选范围和分页尚未核验，不能认定账户没有成交。"
    else:
        status = "unsupported_view"
        message = (
            "尚未识别订单表格。请手动切换历史订单/成交记录；当前页面结构可能需要适配。"
        )
    return {
        "source": "visible_page_observation",
        "read_only": True,
        "verified_for_accounting": False,
        "status": status,
        "message": message,
        "chain": expected[0],
        "address": expected[1],
        "captured_at": int(time.time()),
        "candidate_tables": candidates,
        "scope": "current_visible_tables_only",
        **observed,
    }
