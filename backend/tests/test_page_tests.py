from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.browser.diagnostics import run_test
from app.main import browser_action
from tests.test_alpha import PAYLOAD
from tests.test_live_browser import page as base_page  # noqa: F401
from tests.test_recovery_open import services as base_services  # noqa: F401


@pytest.fixture(name="page")
def diagnostic_page(request):
    return request.getfixturevalue("base_page")


@pytest.fixture(name="services")
def diagnostic_services(request):
    return request.getfixturevalue("base_services")


def payload(action, **changes):
    return {**PAYLOAD, "action": action, "request_id": str(uuid4()), **changes}


def install_orders(page, confirm=False):
    page.locator("#current").evaluate(
        r"""(e, confirm) => {
      e.innerHTML=`<button id="cancel-all">全部取消</button><table><thead><tr><th>代币</th><th>方向</th><th>价格</th></tr></thead>
      <tbody><tr><td>DGAI</td><td>买入</td><td>1.05 USDT</td></tr><tr><td>OTHER</td><td>卖出</td><td>2.5 USDT</td></tr></tbody></table>`;
      window.cancelAllClicks=0;
      document.getElementById('cancel-all').onclick=()=>{
        window.cancelAllClicks++;
        if(confirm){setTimeout(()=>document.body.insertAdjacentHTML('beforeend', '<div role="dialog">确认取消全部订单？<button onclick="document.getElementById(\'current\').innerHTML=\'<p>暂无订单</p>\';this.parentNode.remove()">确认</button></div>'),200);}
        else e.innerHTML='<p>暂无订单</p>';
      };
    }""",
        confirm,
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_fill_total_returns_platform_values_and_validation_without_trading(page, side):
    page.locator("#limitTotal").evaluate("""e=>e.addEventListener('blur',()=>{
        document.getElementById('limitAmount').value='9.52';e.value='9.99711383';
    })""")
    page.locator("#limitPrice").evaluate("e=>e.type='number'")
    result = run_test(
        page, payload("fill_total", side=side, price="1.050001", quote_amount="10")
    )
    assert result["side"] == side
    assert result["values"]["quote_amount"] == "9.99711383"
    assert result["values"]["quantity"] == "9.52"
    assert not result["validation"]["price"]["valid"]
    assert page.evaluate("window.submits") == 0 and not result["submitted"]


def test_individual_balances_read_only_correct_side_and_precision(page):
    page.locator("#cash").evaluate("e=>e.textContent='可用 123.987 USDT'")
    page.locator("#coins").evaluate("e=>e.textContent='可用 9.52998765 DGAI'")
    assert run_test(page, payload("buy_balance")) == {
        "available": "123.9",
        "currency": "USDT",
    }
    assert (
        page.get_by_role("tab", name="买入", exact=True).get_attribute("aria-selected")
        == "true"
    )
    assert run_test(page, payload("sell_balance")) == {
        "available": "9.52998765",
        "currency": "DGAI",
    }
    assert page.locator("#limitPrice").input_value() == ""
    assert page.evaluate("window.submits") == 0


def test_current_orders_reads_multiple_symbols_and_directions(page):
    install_orders(page)
    assert run_test(page, payload("orders"))["orders"] == [
        {"symbol": "DGAI", "side": "买入", "price": "1.05", "quote": "USDT"},
        {"symbol": "OTHER", "side": "卖出", "price": "2.5", "quote": "USDT"},
    ]
    assert page.evaluate("window.cancelAllClicks") == 0


@pytest.mark.parametrize("confirm", [False, True])
def test_cancel_all_scoped_to_current_panel_once_and_verifies_empty(page, confirm):
    install_orders(page, confirm)
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<button onclick=\"window.wrong=true\">全部取消</button>')"
    )
    result = run_test(page, payload("cancel_all"))
    assert result["completed"] and result["clicked"]
    assert page.evaluate("window.cancelAllClicks") == 1
    assert page.evaluate("window.wrong||false") is False
    assert page.evaluate("window.submits") == 0


def test_cancel_all_verification_prompt_is_not_confirmed(page):
    install_orders(page, True)
    page.evaluate("""() => {document.getElementById('cancel-all').onclick=()=>{
      document.body.insertAdjacentHTML('beforeend','<div role="dialog">安全验证：取消订单<button onclick="window.verified=true">确认</button></div>');
    };}""")
    with pytest.raises(ValueError, match="手动处理"):
        run_test(page, payload("cancel_all"))
    assert page.evaluate("window.verified||false") is False


def test_empty_current_orders_cancel_is_noop(page):
    result = run_test(page, payload("cancel_all"))
    assert result["completed"] and not result["clicked"]


def test_native_sell_slider_drag_reads_maximum_without_submitting(page):
    page.locator(
        "#limitAmount"
    ).evaluate(r"""e=>e.parentNode.insertAdjacentHTML('afterend',
      '<input id="range" type="range" min="0" max="100" value="0" style="width:300px" oninput="document.getElementById(\'limitAmount\').value=this.value;document.getElementById(\'limitTotal\').value=\'100\'">')""")
    result = run_test(page, payload("sell_slider", price="1.05"))
    assert result["slider_value"] == result["slider_max"] == "100"
    assert result["values"]["price"] == "1.05"
    assert result["side"] == "sell" and page.evaluate("window.submits") == 0


def test_no_slider_does_not_use_text_percent_or_submit(page):
    with pytest.raises(ValueError, match="进度条"):
        run_test(page, payload("sell_slider"))
    assert page.evaluate("window.percents||0") == 0
    assert page.evaluate("window.submits") == 0


def test_graphical_slider_thumb_drags_to_end_without_percent_text(page):
    page.locator("#percent").evaluate("e=>e.remove()")
    page.locator("#limitAmount").evaluate(r"""e=>{
      const wrapper=document.createElement('div');wrapper.className='bn-slider';
      wrapper.style='position:relative;width:300px;height:24px;margin:20px';
      wrapper.innerHTML='<div class="bn-slider-handle" role="slider" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="position:absolute;left:0;width:18px;height:18px;background:red;transform:translateX(-50%)"></div>';
      e.parentNode.after(wrapper);
      const thumb=wrapper.firstChild;let dragging=false;
      thumb.onmousedown=()=>dragging=true;
      document.addEventListener('mousemove',event=>{
        if(!dragging)return;
        const rect=wrapper.getBoundingClientRect();
        const value=Math.max(0,Math.min(100,Math.round((event.clientX-rect.left)/rect.width*100)));
        thumb.style.left=value+'%';thumb.setAttribute('aria-valuenow',String(value));
        document.getElementById('limitAmount').value='9.52';
        document.getElementById('limitTotal').value='9.99711383';
      });
      document.addEventListener('mouseup',()=>dragging=false);
    }""")
    result = run_test(page, payload("sell_slider", price="1.05011700"))
    assert result["slider_value"] == "100"
    assert result["values"]["quantity"] == "9.52"
    assert result["values"]["price"] == "1.05011700"
    assert page.evaluate("window.submits") == 0


def test_running_automatic_task_blocks_tests_but_paused_same_coin_allows(services):
    live, browser = services
    body = payload("cancel_all", url="https://www.binance.com/zh-CN/alpha/bsc/0x123")
    live.automatic.running = True
    with pytest.raises(HTTPException, match="暂停"):
        browser_action("page_test", payload=body)
    browser.execute.assert_not_called()
    live.automatic.running = False
    assert browser_action("page_test", payload=body)["ok"]
    browser.execute.assert_called_once_with("page_test", "", body)


def test_paused_task_cannot_test_other_coin(services):
    _, browser = services
    with pytest.raises(HTTPException, match="原币种"):
        browser_action("page_test", payload=payload("buy_balance"))
    browser.execute.assert_not_called()


def test_page_test_api_requires_local_header_and_known_action(services):
    from fastapi.testclient import TestClient

    from app.main import app

    _, browser = services
    body = payload("orders", url="https://www.binance.com/zh-CN/alpha/bsc/0x123")
    client = TestClient(app)
    assert client.post("/api/browser/page-test", json=body).status_code == 403
    browser.execute.assert_not_called()
    headers = {"X-AlphaLooper-Client": "local-ui"}
    assert (
        client.post("/api/browser/page-test", headers=headers, json=body).status_code
        == 200
    )
    browser.execute.reset_mock()
    assert (
        client.post(
            "/api/browser/page-test",
            headers=headers,
            json={**body, "action": "live_submit"},
        ).status_code
        == 422
    )
    browser.execute.assert_not_called()
