import pytest

from app.browser.automatic import available_balance, cancel_once, inspect_progress
from tests.test_alpha import PAYLOAD
from tests.test_live_browser import page as base_page  # noqa: F401


@pytest.fixture(name="page")
def isolated_page(request):
    return request.getfixturevalue("base_page")


def install_cancel_flow(page):
    page.evaluate("""() => {
      window.cancelRequests=0;
      window.cancelConfirmations=0;
      window.requestCancelAll=()=>{
        window.cancelRequests++;
        document.body.insertAdjacentHTML('beforeend', `<div role="dialog" id="cancel-confirmation">
          <p>确定取消全部订单？</p>
          <button onclick="window.cancelConfirmations++;this.closest('[role=dialog]').remove()">确认</button>
        </div>`);
      };
    }""")


def install_order(page, *, price="0.9 USDT", quantity="1 DGAI", direction="买入"):
    headers = ["代币", "类型", "方向", "委托价格", "数量", "已成交", "成交额", "操作"]
    fields = [
        "DGAI",
        "限价",
        direction,
        price,
        quantity,
        "0.2 DGAI",
        "0.18 USDT",
        '<button onclick="window.cancels++">撤单</button>',
    ]
    html = (
        '<button onclick="window.requestCancelAll()">全部取消</button>'
        "<table><thead><tr>"
        + "".join(f"<th>{v}</th>" for v in headers)
        + "</tr></thead><tbody><tr>"
        + "".join(f"<td>{v}</td>" for v in fields)
        + "</tr></tbody></table>"
    )
    page.locator("#current").evaluate("(e,html)=>e.innerHTML=html", html)
    install_cancel_flow(page)
    page.evaluate("window.cancels=0")


def install_current_layout(page, *, status="新订单", duplicate_blank_headers=False):
    headers = [
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
        "全部取消",
    ]
    if duplicate_blank_headers:
        headers[-2:] = ["", ""]
    fields = [
        "2026-09-22 18:04:26",
        "DGAI",
        "限价",
        "买入",
        "0.9 USDT",
        "1 DGAI",
        status,
        "-",
        "-",
        "-",
        '<button onclick="window.cancels++"><svg></svg></button>',
    ]
    html = (
        '<button onclick="window.requestCancelAll()">全部取消</button>'
        "<table><thead><tr>"
        + "".join(f"<th>{value}</th>" for value in headers)
        + "</tr></thead><tbody><tr>"
        + "".join(f"<td>{value}</td>" for value in fields)
        + "</tr></tbody></table>"
    )
    page.locator("#current").evaluate("(element,html)=>element.innerHTML=html", html)
    install_cancel_flow(page)
    page.evaluate("window.cancels=0")


def test_partial_progress_and_single_scoped_cancel(page):
    install_order(page)
    assert (
        inspect_progress(page, PAYLOAD)["current_order"]["remaining_quantity"] == "0.8"
    )
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<button onclick=\"window.wrong=true\">撤单</button>')"
    )
    assert cancel_once(page, PAYLOAD)["cancel_clicked"]
    assert page.evaluate("window.cancelRequests") == 1
    assert page.evaluate("window.cancelConfirmations") == 1
    assert page.evaluate("window.cancels") == 0
    assert page.evaluate("window.wrong||false") is False


def test_actual_current_layout_reports_zero_progress_for_new_order(page):
    install_current_layout(page)
    assert inspect_progress(page, PAYLOAD)["current_order"]["requested_quantity"] == "1"
    assert cancel_once(page, PAYLOAD)["cancel_clicked"]
    assert page.evaluate("window.cancelRequests") == 1
    assert page.evaluate("window.cancelConfirmations") == 1
    assert page.evaluate("window.cancels") == 0


def test_split_header_and_body_tables_use_the_platform_header(page):
    install_current_layout(page)
    page.locator("#current").evaluate("""panel => {
      const bodyTable=panel.querySelector('table');
      const headerTable=document.createElement('table');
      headerTable.appendChild(bodyTable.querySelector('thead'));
      panel.insertBefore(headerTable, bodyTable);
    }""")
    assert inspect_progress(page, PAYLOAD)["current_order"]["requested_quantity"] == "1"


def test_fixed_platform_layout_works_when_header_is_not_semantic_dom(page):
    install_current_layout(page)
    page.locator("#current thead").evaluate("element=>element.remove()")
    assert inspect_progress(page, PAYLOAD)["current_order"]["requested_quantity"] == "1"


def test_irrelevant_duplicate_blank_headers_do_not_break_field_mapping(page):
    install_current_layout(page, duplicate_blank_headers=True)
    assert inspect_progress(page, PAYLOAD)["current_order"]["requested_quantity"] == "1"


def test_current_layout_stops_if_status_may_include_a_partial_fill(page):
    install_current_layout(page, status="部分成交")
    assert inspect_progress(page, PAYLOAD)["pending"]


def test_surplus_empty_edge_header_is_ignored(page):
    install_current_layout(page)
    page.locator("#current thead tr").evaluate(
        "element=>element.appendChild(document.createElement('th'))"
    )
    assert inspect_progress(page, PAYLOAD)["current_order"]["requested_quantity"] == "1"


def test_named_header_cell_count_error_reports_observed_layout(page):
    install_current_layout(page)
    page.locator("#current thead tr").evaluate(
        """element => {
          const header=document.createElement('th');
          header.textContent='未知业务列';
          element.appendChild(header);
        }"""
    )
    with pytest.raises(ValueError, match="表头有 12 列、订单行有 11 列"):
        inspect_progress(page, PAYLOAD)


def test_current_order_keeps_platform_price_without_requiring_exact_match(page):
    install_order(page, price="0.899999 USDT")
    result = inspect_progress(page, PAYLOAD)
    assert result["current_order"]["price"] == "0.899999"


def test_cancel_rejects_wrong_direction_without_click(page):
    install_order(page, direction="卖出")
    with pytest.raises(ValueError):
        cancel_once(page, PAYLOAD)
    assert page.evaluate("window.cancelRequests") == 0


def test_balance_read_switches_side_but_never_submits(page):
    page.locator("#coins").evaluate("e=>e.textContent='可用余额： 1.9998 DGAI'")
    assert available_balance(page, PAYLOAD)["base_available"] == "1.9998"
    assert page.evaluate("window.submits") == 0
    assert page.locator("#limitAmount").input_value() == ""


def test_ambiguous_balance_or_modal_stops(page):
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<p>可用 1 DGAI</p><p>可用 2 DGAI</p>')"
    )
    with pytest.raises(ValueError, match="唯一"):
        available_balance(page, PAYLOAD)
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<div role=dialog>安全验证</div>')"
    )
    with pytest.raises(ValueError, match="弹窗"):
        cancel_once(page, PAYLOAD)
    assert page.evaluate("window.submits") == 0


def test_cancel_all_requires_exactly_one_ordinary_confirmation(page):
    install_order(page)
    page.evaluate("""() => { window.requestCancelAll=()=>{
      window.cancelRequests++;
      document.body.insertAdjacentHTML('beforeend','<div role="dialog"><p>安全验证</p><button>确认</button></div>');
    }; }""")
    with pytest.raises(ValueError, match="验证"):
        cancel_once(page, PAYLOAD)
    assert page.evaluate("window.cancelRequests") == 1
    assert page.evaluate("window.cancelConfirmations") == 0


def test_cancel_all_accepts_unique_styled_text_control(page):
    install_order(page)
    page.get_by_role("button", name="全部取消").evaluate(
        "element=>element.outerHTML='<span id=styled-cancel onclick=\"window.requestCancelAll()\">全部取消</span>'"
    )
    assert cancel_once(page, PAYLOAD)["cancel_clicked"]
    assert page.evaluate("window.cancelRequests") == 1
    assert page.evaluate("window.cancelConfirmations") == 1


def test_first_post_cancel_inspection_refreshes_page(page):
    loads = []
    page.on("load", lambda: loads.append(page.url))
    result = inspect_progress(page, {**PAYLOAD, "refresh_before_check": True})
    assert result["page_refreshed"]
    assert len(loads) == 1


def test_form_suffix_loading_is_retryable(page, monkeypatch):
    monkeypatch.setattr("app.browser.alpha.FORM_READY_TIMEOUT", 0.01)
    page.locator("#limitPrice").locator("..").locator(
        ".bn-textField-suffix"
    ).evaluate("element=>element.textContent='' ")

    result = inspect_progress(page, PAYLOAD)

    assert result["settling"] is True
    assert result["page_loading"] is True
    assert "仍在加载" in result["message"]


def test_loaded_unsupported_quote_is_not_retryable(page):
    page.locator("#limitPrice").locator("..").locator(
        ".bn-textField-suffix"
    ).evaluate("element=>element.textContent='BTC'")

    with pytest.raises(ValueError, match="页面计价币为 BTC"):
        inspect_progress(page, PAYLOAD)


def test_missing_current_order_tab_while_loading_is_retryable(page):
    page.get_by_role("tab", name="当前委托", exact=True).evaluate(
        "element=>element.remove()"
    )

    result = inspect_progress(page, PAYLOAD)

    assert result["settling"] is True
    assert result["page_loading"] is True
    assert "仍在加载" in result["message"]


def test_duplicate_current_order_tabs_remain_a_hard_error(page):
    page.get_by_role("tab", name="当前委托", exact=True).evaluate(
        "element=>element.parentElement.appendChild(element.cloneNode(true))"
    )

    with pytest.raises(ValueError, match="无法唯一识别当前委托标签"):
        inspect_progress(page, PAYLOAD)


def test_explicit_frozen_and_total_balances_include_locked_assets(page):
    install_current_layout(page, status="部分成交")
    page.locator("#cash").evaluate("e=>e.textContent='可用 50 USDT'")
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<p>冻结 20 USDT</p>')"
    )
    result = inspect_progress(page, PAYLOAD)
    assert result["balances"]["quote_total"] == "70"
    assert result["balances"]["base_total"] == "1"


def test_absent_order_but_frozen_funds_not_settled(page):
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<p>冻结 20 USDT</p>')"
    )
    assert inspect_progress(page, PAYLOAD)["settling"]


def test_unknown_frozen_is_explicit_not_zero(page):
    install_current_layout(page, status="部分成交")
    result = inspect_progress(page, PAYLOAD)
    assert result["balances"]["quote_total"] is None
    assert result["balances"]["base_total"] == "1"
