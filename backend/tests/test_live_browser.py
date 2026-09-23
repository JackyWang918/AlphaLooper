import pytest
from playwright.sync_api import sync_playwright

from app.browser.alpha import fill_form
from app.browser.live import (
    inspect_order,
    inspect_unsubmitted,
    order_readiness,
    preflight,
    submit_once,
)
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
        <div><input id="limitTotal" step="0.00001"><span class="bn-textField-suffix">USDT</span></div>
        <p id="cash">可用 100 USDT</p><p id="coins">可用 1 DGAI</p>
        <button id="percent">100%</button>
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
        function confirmOrder(){window.confirmations++;document.getElementById('confirmation').remove();document.getElementById('current').innerHTML='<table><thead><tr><th>代币</th><th>类型</th><th>方向</th><th>价格</th><th>数量</th><th>状态</th><th>操作</th></tr></thead><tbody><tr><td>DGAI</td><td>限价</td><td>买入</td><td>0.9 USDT</td><td>1 DGAI</td><td>新订单</td><td></td></tr></tbody></table>';}
        </script>""",
            ),
        )
        page.goto(PAYLOAD["url"])
        yield page
        browser.close()


def test_submit_once_pending_then_balance_reconciliation_without_history(page):
    assert preflight(page, PAYLOAD)["balances"]["quote_available"] == "100"
    assert page.evaluate("window.submits") == 0
    assert submit_once(page, PAYLOAD)["clicked"]
    assert page.evaluate("[window.submits,window.confirmations]") == [1, 1]
    assert inspect_order(page, PAYLOAD)["pending"]
    with pytest.raises(ValueError, match="已有挂单"):
        submit_once(page, PAYLOAD)
    page.locator("#current").evaluate("e=>e.innerHTML='<p>暂无订单</p>'")
    page.locator("#cash").evaluate("e=>e.textContent='可用 99.1 USDT'")
    page.locator("#coins").evaluate("e=>e.textContent='可用 1.9999 DGAI'")
    result = inspect_order(page, PAYLOAD)
    assert not result["pending"] and result["balances"]["quote_available"] == "99.1"
    assert (
        page.get_by_role("tab", name="历史委托").get_attribute("aria-selected")
        == "false"
    )


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
    assert preflight(page, PAYLOAD)["balances"]["quote_available"] == "100"
    assert page.evaluate("window.submits") == 0


def test_order_readiness_only_reads_and_returns_to_current_panel(page):
    page.locator("#limitPrice").fill("0.8")
    page.locator("#limitAmount").fill("2")
    assert order_readiness(page, PAYLOAD)["balances"]["quote_available"] == "100"
    assert page.locator("#limitPrice").input_value() == "0.8"
    assert page.locator("#limitAmount").input_value() == "2"
    assert (
        page.get_by_role("tab", name="当前委托").get_attribute("aria-selected")
        == "true"
    )
    assert page.evaluate("window.submits") == 0


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
    assert (
        preflight(page, {**PAYLOAD, "side": "sell"})["balances"]["base_available"]
        == "1"
    )
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


def test_platform_sell_all_uses_percentage_control_not_quantity_fill(page):
    from app.browser.alpha import fill_form

    page.locator("#percent").evaluate(r"""e=>e.outerHTML=
      '<input id="percent" type="range" min="0" max="100" value="0" style="width:300px" oninput="document.getElementById(\'limitAmount\').value=\'0.99\';window.percents=(window.percents||0)+1">'""")
    page.locator("#limitAmount").evaluate(
        "e=>e.addEventListener('input',()=>window.quantityTyped=true)"
    )
    result = fill_form(
        page, {**PAYLOAD, "side": "sell", "quantity": "1.000001", "sell_all": True}
    )
    assert not result["submitted"]
    assert page.evaluate("window.percents") > 0
    assert page.evaluate("window.quantityTyped||false") is False
    assert page.locator("#limitAmount").input_value() == "0.99"
    assert page.locator("#limitPrice").input_value() == "0.9"
    assert page.evaluate("window.submits") == 0


def test_sell_all_does_not_fallback_to_typing_when_control_missing(page):
    page.locator("#percent").evaluate("e=>e.remove()")
    with pytest.raises(ValueError, match="进度条"):
        submit_once(page, {**PAYLOAD, "side": "sell", "sell_all": True})
    assert page.evaluate("window.submits") == 0


def test_sell_all_drags_graphical_slider_without_percent_text(page):
    page.locator("#percent").evaluate("e=>e.remove()")
    page.locator("#limitAmount").evaluate(r"""amount=>{
      const track=document.createElement('div');
      track.className='bn-slider';
      track.style='position:relative;width:300px;height:20px';
      track.innerHTML='<div class="bn-slider-handle" role="slider" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="position:absolute;left:0;width:18px;height:18px"></div>';
      amount.parentElement.after(track);
      const handle=track.firstElementChild;
      handle.addEventListener('mousedown',()=>{
        const move=event=>{
          const box=track.getBoundingClientRect();
          const value=Math.max(0,Math.min(100,(event.clientX-box.left)/box.width*100));
          handle.style.left=value+'%';handle.setAttribute('aria-valuenow',String(value));
          if(value>99){amount.value='9.52';document.getElementById('limitPrice').value='1.01';}
        };
        const up=()=>{document.removeEventListener('mousemove',move);document.removeEventListener('mouseup',up)};
        document.addEventListener('mousemove',move);document.addEventListener('mouseup',up);
      });
    }""")
    result = fill_form(
        page,
        {
            **PAYLOAD,
            "side": "sell",
            "price": "1.05",
            "quantity": "999",
            "sell_all": True,
        },
    )
    assert result["quantity"] == "9.52"
    assert page.locator("#limitAmount").input_value() == "9.52"
    assert page.locator("#limitPrice").input_value() == "1.05"
    assert page.evaluate("window.submits") == 0


def test_sell_all_uses_end_key_when_drag_stops_before_max(page):
    page.locator("#percent").evaluate("e=>e.remove()")
    page.locator("#limitAmount").evaluate(r"""amount=>{
      const track=document.createElement('div');
      track.className='bn-slider';track.style='position:relative;width:300px;height:20px';
      track.innerHTML='<div class="bn-slider-handle" role="slider" tabindex="0" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="position:absolute;left:0;width:18px;height:18px"></div>';
      amount.parentElement.after(track);
      const handle=track.firstElementChild;
      handle.addEventListener('mousedown',()=>{
        const move=()=>handle.setAttribute('aria-valuenow','75');
        const up=()=>{document.removeEventListener('mousemove',move);document.removeEventListener('mouseup',up)};
        document.addEventListener('mousemove',move);document.addEventListener('mouseup',up);
      });
      handle.addEventListener('keydown',event=>{
        if(event.key==='End'){
          handle.setAttribute('aria-valuenow','100');handle.style.left='100%';amount.value='9.52';
        }
      });
    }""")
    result = fill_form(
        page,
        {**PAYLOAD, "side": "sell", "price": "1.05", "quantity": "999", "sell_all": True},
    )
    assert result["quantity"] == "9.52"
    assert page.get_by_role("slider").get_attribute("aria-valuenow") == "100"
    assert page.evaluate("window.submits") == 0


def test_explicit_quote_amount_preserves_fifty_usdt(page):
    from app.browser.alpha import fill_form

    fill_form(page, {**PAYLOAD, "quote_amount": "50"})
    assert page.locator("#limitTotal").input_value() == "50"
