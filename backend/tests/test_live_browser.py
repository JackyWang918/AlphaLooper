import pytest
from playwright.sync_api import sync_playwright

from app.browser.live import inspect_order, preflight, submit_once
from tests.test_alpha import PAYLOAD


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
        <button onclick="submitOrder()">买入 DGAI</button><button onclick="submitOrder()">卖出 DGAI</button>
        <div role="tab" aria-selected="true" aria-controls="current" onclick="panel(this)">当前委托</div>
        <div role="tab" aria-selected="false" aria-controls="history" onclick="panel(this)">历史委托</div>
        <div role="tabpanel" id="current"><p>暂无订单</p></div>
        <div role="tabpanel" id="history" hidden><p>暂无订单</p></div>
        <script>
        window.submits=0;
        function side(el){document.querySelectorAll('[role=tab]:not([aria-controls])').forEach(t=>t.setAttribute('aria-selected',String(t===el)));}
        function panel(el){document.querySelectorAll('[aria-controls]').forEach(t=>{t.setAttribute('aria-selected',String(t===el));document.getElementById(t.getAttribute('aria-controls')).hidden=t!==el;});}
        function submitOrder(){window.submits++;document.getElementById('current').innerHTML='<table><tbody><tr><td>DGAI</td><td>买入</td><td>0.9 USDT</td><td>1 DGAI</td><td>0 DGAI</td></tr></tbody></table>';}
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


def test_sell_direction_and_duplicate_button_stop(page):
    assert preflight(page, {**PAYLOAD, "side": "sell"})["baseline_id"] is None
    page.get_by_role("button", name="卖出 DGAI").evaluate(
        "e=>e.after(e.cloneNode(true))"
    )
    with pytest.raises(ValueError, match="唯一"):
        submit_once(page, {**PAYLOAD, "side": "sell"})
    assert page.evaluate("window.submits") == 0
