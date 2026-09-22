import pytest
from playwright.sync_api import sync_playwright

from app.browser.live import (
    inspect_order,
    inspect_unsubmitted,
    matches,
    order_readiness,
    preflight,
    submit_once,
)
from tests.test_alpha import PAYLOAD


def history_order(**changes):
    order = {
        "order_id": "new-order",
        "symbol": "DGAI",
        "quote": "USDT",
        "side": "买入",
        "chain": "bsc",
        "address": "0x10d4183389e99233db3cc981c43443ebd28ebd5e",
        "requested_quantity": "1",
        "quantity": "1",
        "limit_price": "0.9",
    }
    order.update(changes)
    return order


def test_buy_history_allows_platform_quantity_rounding_by_one_step():
    payload = {**PAYLOAD, "quantity": "47.88000000", "price": "1.04397948"}
    order = history_order(
        requested_quantity="47.87",
        quantity="47.87",
        limit_price="1.04397948",
        quantity_step="0.01",
    )
    assert matches(order, payload, "old-order")
    assert not matches({**order, "requested_quantity": "47.86"}, payload, "old-order")
    assert not matches({**order, "requested_quantity": "47.89"}, payload, "old-order")


def test_sell_history_still_requires_exact_requested_quantity():
    payload = {**PAYLOAD, "side": "sell", "quantity": "47.88"}
    order = history_order(
        side="卖出",
        requested_quantity="47.87",
        quantity="47.87",
        quantity_step="0.01",
    )
    assert not matches(order, payload, "old-order")


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body="""
        <div role="tab" aria-selected="true" onclick="side(this)">买入</div>
        <div role="tab" aria-selected="false" onclick="side(this)">卖出</div>
        <div><input id="limitPrice" step="0.00001"><span class="bn-textField-suffix">USDT</span></div>
        <div><input id="limitAmount" step="0.01"><span class="bn-textField-suffix">DGAI</span></div>
        <div><input id="limitTotal" step="0.00001"><span class="bn-textField-suffix">USDT</span></div>
        <button onclick="submitOrder()">买入 DGAI</button><button onclick="submitOrder()">卖出 DGAI</button>
        <div role="tab" aria-selected="true" aria-controls="current" onclick="panel(this)">当前委托</div>
        <div role="tab" aria-selected="false" aria-controls="history" onclick="panel(this)">历史委托</div>
        <div role="tabpanel" id="current"><p>暂无订单</p></div>
        <div role="tabpanel" id="history" hidden><p>暂无订单</p></div>
        <script>
        window.submits=0; window.confirmations=0;
        function side(el){document.querySelectorAll('[role=tab]:not([aria-controls])').forEach(t=>t.setAttribute('aria-selected',String(t===el)));}
        function panel(el){document.querySelectorAll('[aria-controls]').forEach(t=>{t.setAttribute('aria-selected',String(t===el));document.getElementById(t.getAttribute('aria-controls')).hidden=t!==el;});}
        function submitOrder(){
          window.submits++;
          const side=document.querySelector('[role=tab]:not([aria-controls])[aria-selected=true]').textContent;
          document.body.insertAdjacentHTML('beforeend',`<div role="dialog" id="confirmation">
            <h2>DGAI</h2><p>DGrid AI</p>
            <div><span>类型</span><span>限价 / ${side}</span></div>
            <div><span>委托价</span><span>0.90000000 USDT</span></div>
            <div><span>数量</span><span>1.00 DGAI</span></div>
            <div><span>成交额</span><span>0.90000000 USDT</span></div>
            <div><span>预估手续费</span><span>0.0001 DGAI</span></div>
            <div><span>付款账户</span><span>资金账户</span></div>
            <button onclick="confirmOrder()">继续</button>
          </div>`);
        }
        function confirmOrder(){window.confirmations++;document.getElementById('confirmation').remove();document.getElementById('current').innerHTML='<table><tbody><tr><td>DGAI</td><td>买入</td><td>0.9 USDT</td><td>1 DGAI</td><td>0 DGAI</td></tr></tbody></table>';}
        </script>""",
            ),
        )
        page.goto(PAYLOAD["url"])
        yield page
        browser.close()


def test_submit_once_pending_then_latest_final(page):
    assert preflight(page, PAYLOAD) == {"baseline_id": None}
    assert page.evaluate("window.submits") == 0
    assert submit_once(page, PAYLOAD)["clicked"]
    assert page.evaluate("window.submits") == 1
    assert page.evaluate("window.confirmations") == 1
    assert inspect_order(page, PAYLOAD) == {"pending": True}
    with pytest.raises(ValueError, match="已有挂单"):
        submit_once(page, PAYLOAD)
    assert page.evaluate("window.submits") == 1
    from app.account_ledger import HEADERS

    cells = [
        "2026-09-22 12:00:00",
        "DGAI",
        "限价",
        "买入",
        "0.9 USDT",
        "0.9 USDT",
        "1 DGAI",
        "1 DGAI",
        "0.9 USDT",
        "-",
        "-",
        "-",
        "已成交",
    ]
    html = (
        "<table><thead><tr>"
        + "".join(f"<th>{h}</th>" for h in HEADERS)
        + "</tr></thead><tbody><tr>"
        + "".join(f"<td>{v}</td>" for v in cells)
        + "</tr><tr><td>订单ID: 123</td></tr></tbody></table>"
    )
    page.locator("#current").evaluate("e=>e.innerHTML='<p>暂无订单</p>'")
    page.locator("#history").evaluate("(e,html)=>e.innerHTML=html", html)
    result = inspect_order(page, PAYLOAD)
    assert not result["pending"] and result["order"]["order_id"] == "123"


def test_unloaded_panel_is_not_empty_and_no_click(page):
    page.locator("#current").evaluate("e=>e.innerHTML='' ")
    with pytest.raises(ValueError):
        preflight(page, PAYLOAD)
    assert page.evaluate("window.submits") == 0


def test_unsubmitted_check_blocks_lingering_confirmation(page):
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<div role=dialog>确认买入</div>')"
    )
    with pytest.raises(ValueError, match="关闭"):
        inspect_unsubmitted(page, PAYLOAD)
    assert page.evaluate("window.submits") == 0


def test_observed_current_empty_message_allows_preflight_without_submit(page):
    page.locator("#current p").evaluate("e=>e.textContent='无进行中的订单'")
    assert preflight(page, PAYLOAD) == {"baseline_id": None}
    assert page.evaluate("window.submits") == 0


def test_order_readiness_only_reads_and_returns_to_current_panel(page):
    page.locator("#limitPrice").fill("0.8")
    page.locator("#limitAmount").fill("2")
    assert order_readiness(page, PAYLOAD) == {"baseline_id": None}
    assert page.locator("#limitPrice").input_value() == "0.8"
    assert page.locator("#limitAmount").input_value() == "2"
    assert (
        page.get_by_role("tab", name="当前委托").get_attribute("aria-selected")
        == "true"
    )
    assert page.evaluate("window.submits") == 0


@pytest.mark.parametrize("separate_column", [False, True])
@pytest.mark.parametrize("delayed", [False, True])
def test_history_disclosure_layouts_read_without_trading(
    page, separate_column, delayed
):
    from app.account_ledger import HEADERS

    fields = [
        "2026-09-22 12:00:00",
        "DGAI",
        "限价",
        "买入",
        "0.9 USDT",
        "0.9 USDT",
        "1 DGAI",
        "1 DGAI",
        "0.9 USDT",
        "-",
        "-",
        "-",
        "已成交",
    ]
    icon = '<svg width="16" height="16" onclick="expandHistory(this)"><path d="M0 0 L16 16"/></svg>'
    if separate_column:
        fields.insert(0, icon)
    else:
        fields[0] = icon + fields[0]
    html = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in HEADERS)
    html += "</tr></thead><tbody><tr>" + "".join(f"<td>{v}</td>" for v in fields)
    html += "</tr></tbody></table>"
    if delayed:
        page.locator("#history").evaluate(
            """(e,html)=>{
              e.innerHTML='<table><tbody><tr><td>加载中</td></tr></tbody></table>';
              setTimeout(()=>e.innerHTML=html, 400);
            }""",
            html,
        )
    else:
        page.locator("#history").evaluate("(e,html)=>e.innerHTML=html", html)
    page.evaluate("""() => { window.expandHistory = icon => {
      icon.closest('tr').insertAdjacentHTML('afterend','<tr><td colspan="14">订单ID: 123</td></tr>');
    }; }""")
    assert order_readiness(page, PAYLOAD) == {"baseline_id": "123"}
    assert page.evaluate("window.submits") == 0
    assert page.locator("#limitPrice").input_value() == ""


@pytest.mark.parametrize("location", ["outside", "hidden"])
def test_current_empty_message_must_be_visible_inside_panel(page, location):
    page.locator("#current").evaluate("e=>e.innerHTML=''")
    page.locator("#current").evaluate("e=>e.style.minHeight='100px'")
    if location == "outside":
        page.locator("body").evaluate(
            "e=>e.insertAdjacentHTML('beforeend','<p>无进行中的订单</p>')"
        )
    else:
        page.locator("#current").evaluate(
            "e=>e.innerHTML='<p hidden>无进行中的订单</p>'"
        )
    with pytest.raises(ValueError, match="等待当前委托列表加载"):
        preflight(page, PAYLOAD)
    assert page.evaluate("window.submits") == 0


def test_sell_direction_and_duplicate_button_stop(page):
    assert preflight(page, {**PAYLOAD, "side": "sell"})["baseline_id"] is None
    page.get_by_role("button", name="卖出 DGAI").evaluate(
        "e=>e.after(e.cloneNode(true))"
    )
    with pytest.raises(ValueError, match="唯一"):
        submit_once(page, {**PAYLOAD, "side": "sell"})
    assert page.evaluate("window.submits") == 0


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_confirm_both_directions_exactly_once(page, side):
    result = submit_once(page, {**PAYLOAD, "side": side})
    assert result["confirmation_clicked"]
    assert result["confirmation"] == {"unchecked_confirmation": True}
    assert page.evaluate("[window.submits,window.confirmations]") == [1, 1]


@pytest.mark.parametrize(
    "mutation",
    [
        "dialog.insertAdjacentHTML('beforeend','<button>继续</button>')",
        "dialog.insertAdjacentHTML('beforeend','<p>安全验证</p>')",
        "dialog.after(dialog.cloneNode(true))",
        "dialog.querySelector('button').disabled=true",
        "dialog.remove()",
    ],
)
def test_confirmation_failures_never_click_continue(page, mutation):
    page.evaluate(
        """mutation => {
      const show=window.submitOrder;
      window.submitOrder=()=>{show();const dialog=document.querySelector('#confirmation');
        new Function('dialog',mutation)(dialog);
      };
    }""",
        mutation,
    )
    with pytest.raises(ValueError):
        submit_once(page, PAYLOAD)
    assert page.evaluate("[window.submits,window.confirmations]") == [1, 0]


def test_confirmation_content_is_not_checked(page):
    page.evaluate("""() => {
      const show=window.submitOrder;
      window.submitOrder=()=>{show();const dialog=document.querySelector('#confirmation');
        dialog.querySelector('button').addEventListener('pointerover',()=>{
          const spans=dialog.querySelectorAll('span');
          spans[5].textContent='2.00 DGAI';
        },{once:true});
      };
    }""")
    assert submit_once(page, PAYLOAD)["confirmation_clicked"]
    assert page.evaluate("window.confirmations") == 1


def test_confirmation_nested_wrappers_and_unrelated_continue(page):
    page.evaluate("""() => {
      document.body.insertAdjacentHTML('beforeend','<button onclick="window.wrong=true">继续</button>');
      const show=window.submitOrder;
      window.submitOrder=()=>{show();const dialog=document.querySelector('#confirmation');
        const wrapper=document.createElement('div');wrapper.className='bn-modal';
        dialog.replaceWith(wrapper);wrapper.append(dialog);
      };
    }""")
    assert submit_once(page, PAYLOAD)["confirmation_clicked"]
    assert page.evaluate("window.wrong||false") is False
    assert page.evaluate("window.confirmations") == 1


@pytest.mark.parametrize("partial", [False, True])
def test_waits_for_dialog_and_delayed_field_values(page, partial):
    page.evaluate(
        """partial => {
      const show=window.submitOrder;
      window.submitOrder=()=>{
        if(!partial){setTimeout(show,300);return;}
        show();const dialog=document.querySelector('#confirmation');
        const original=dialog.innerHTML;
        dialog.innerHTML='<h2>DGAI</h2><div>类型</div>';
        setTimeout(()=>dialog.innerHTML=original,300);
      };
    }""",
        partial,
    )
    assert submit_once(page, PAYLOAD)["confirmation_clicked"]
    assert page.evaluate("window.confirmations") == 1


def test_confirmation_preview_never_clicks(page):
    from app.browser.confirmation import confirmation_preview

    page.evaluate("submitOrder()")
    result = confirmation_preview(page, PAYLOAD)
    assert result["valid"] and result["read_only"]
    assert result["dialog_count"] == 1
    assert page.evaluate("window.confirmations") == 0
