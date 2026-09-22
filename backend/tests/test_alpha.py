from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from app.browser.alpha import check_step, fill_form
from app.browser.schemas import FillForm, token_identity
from app.browser.worker import select_target_page

URL = (
    "https://www.binance.com/zh-CN/alpha/bsc/0x10d4183389e99233db3cc981c43443ebd28ebd5e"
)
PAYLOAD = {
    "url": URL,
    "side": "buy",
    "price": "0.9",
    "quantity": "1",
    "expected_symbol": "DGAI",
    "expected_quote": "USDT",
}


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e-8", "0,5", " "])
def test_bad_numbers(value):
    with pytest.raises(ValidationError):
        FillForm(**{**PAYLOAD, "price": value})


def test_step_and_identity():
    check_step("0.00000001", "1e-8")
    with pytest.raises(ValueError):
        check_step("1.001", "0.01")
    assert token_identity(URL) == token_identity(URL.replace("zh-CN", "en") + "?x=1")
    with pytest.raises(ValueError):
        token_identity("https://www.binance.com/zh-CN/alpha")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        instance = p.chromium.launch(channel="chrome", headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    # An isolated fixture based on observed controls; no external requests.
    page.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body="""
      <div role="tab" aria-selected="true" onclick="select(this)">买入</div>
      <div role="tab" aria-selected="false" onclick="select(this)">卖出</div>
      <div><input id="limitPrice" step="1e-8"><span class="bn-textField-suffix">USDT</span></div>
      <div><input id="limitAmount" step="0.01"><span class="bn-textField-suffix">DGAI</span></div>
      <div><input id="limitTotal" step="1e-8"><span class="bn-textField-suffix">USDT</span></div>
      <button onclick="window.submits++">买入 DGAI</button>
      <script>
        window.submits=0;
        function select(el) {
          document.querySelectorAll('[role=tab]').forEach(t=>t.setAttribute('aria-selected',String(t===el)));
        }
      </script>""",
        ),
    )
    page.goto(URL)
    yield page
    page.close()


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_fill_both_directions_without_submission(page, side):
    result = fill_form(page, {**PAYLOAD, "side": side})
    assert result["side"] == side
    assert result["price"] == "0.9"
    assert result["quantity"] == "1"
    assert result["quote_amount"] == ("0.9" if side == "buy" else None)
    assert result["submitted"] is False
    assert page.evaluate("window.submits") == 0


def test_fill_accepts_equivalent_value_with_trimmed_trailing_zeroes(page):
    page.locator("#limitAmount").evaluate(
        "e=>e.addEventListener('blur',()=>e.value=String(Number(e.value)))"
    )
    result = fill_form(page, {**PAYLOAD, "quantity": "47.82000000"})
    assert result["quantity"] == "47.82000000"
    assert page.locator("#limitTotal").input_value() == "43.038000000"
    assert page.evaluate("window.submits") == 0


def test_worker_adopts_unique_matching_trade_tab():
    class Candidate:
        def __init__(self, url):
            self.url = url

        def is_closed(self):
            return False

    target = Candidate(URL)
    other = Candidate("about:blank")
    context = SimpleNamespace(pages=[other, target])
    assert select_target_page(context, other, URL) is target


def test_buy_fills_strategy_price_and_quote_amount_not_token_quantity(page):
    result = fill_form(page, {**PAYLOAD, "price": "1.04718870", "quantity": "47.74"})
    assert result["price"] == "1.04718870"
    assert page.locator("#limitPrice").input_value() == "1.04718870"
    assert page.locator("#limitTotal").input_value() == "49.9927885380"
    assert page.locator("#limitAmount").input_value() == ""
    assert page.evaluate("window.submits") == 0


@pytest.mark.parametrize("label_kind", ["label", "group", "placeholder"])
def test_buy_total_is_located_by_meaning_without_legacy_id(page, label_kind):
    page.locator("#limitTotal").evaluate(
        """(el, kind) => {
      el.id='quoteValue';
      if (kind==='placeholder') el.placeholder='成交额';
      else {
        const label=document.createElement(kind==='label'?'label':'div');
        label.textContent='成交额';
        if (kind==='label') label.htmlFor=el.id;
        el.parentElement.prepend(label);
      }
    }""",
        label_kind,
    )
    fill_form(page, PAYLOAD)
    assert page.locator("#quoteValue").input_value() == "0.9"
    assert page.locator("#limitAmount").input_value() == ""
    assert page.evaluate("window.submits") == 0


def test_buy_total_ignores_hidden_responsive_copy(page):
    page.locator("#limitTotal").evaluate("""el => {
      const copy=el.cloneNode(); copy.hidden=true;
      document.body.appendChild(copy);
    }""")
    fill_form(page, PAYLOAD)
    assert page.locator("#limitTotal:visible").input_value() == "0.9"


def test_buy_waits_for_total_after_switching_from_sell(page):
    page.get_by_role("tab", name="卖出", exact=True).click()
    page.locator("#limitTotal").evaluate("el => el.disabled=true")
    page.get_by_role("tab", name="买入", exact=True).evaluate("""el => {
      el.addEventListener('click', () => setTimeout(() => {
        document.querySelector('#limitTotal').disabled=false;
      }, 250));
    }""")
    fill_form(page, PAYLOAD)
    assert page.locator("#limitTotal").input_value() == "0.9"


def test_ambiguous_total_stops_before_price_write(page):
    page.locator("#limitTotal").evaluate(
        "el => document.body.appendChild(el.cloneNode())"
    )
    with pytest.raises(ValueError, match="2 个可见候选"):
        fill_form(page, PAYLOAD)
    assert page.locator("#limitPrice").input_value() == ""
    assert page.evaluate("window.submits") == 0


def test_missing_total_does_not_fall_back_to_quantity(page):
    page.locator("#limitTotal").evaluate("el => el.remove()")
    with pytest.raises(ValueError, match="没有可见候选"):
        fill_form(page, PAYLOAD)
    assert page.locator("#limitPrice").input_value() == ""
    assert page.locator("#limitAmount").input_value() == ""
    assert page.evaluate("window.submits") == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": URL[:-1] + "f"},
        {"expected_symbol": "OTHER"},
        {"expected_quote": "USDC"},
        {"quantity": "1.001"},
    ],
)
def test_mismatch_stops_before_filling(page, overrides):
    with pytest.raises(ValueError):
        fill_form(page, {**PAYLOAD, **overrides})
    assert page.locator("#limitPrice").input_value() == ""
    assert page.evaluate("window.submits") == 0


def test_duplicate_input_is_rejected(page):
    page.evaluate(
        "document.body.appendChild(document.querySelector('#limitPrice').cloneNode())"
    )
    with pytest.raises(ValueError):
        fill_form(page, PAYLOAD)


def test_direction_changes_during_fill_is_rejected(page):
    page.locator("#limitTotal").evaluate(
        "e=>e.oninput=()=>select(document.querySelectorAll('[role=tab]')[1])"
    )
    with pytest.raises(ValueError, match="方向"):
        fill_form(page, PAYLOAD)
    assert page.evaluate("window.submits") == 0


def test_overlay_prevents_filling(page):
    page.evaluate("""() => {
      const overlay=document.createElement('div');
      overlay.style='position:fixed;inset:0;z-index:999;background:white';
      overlay.textContent='Manual verification required';
      document.body.appendChild(overlay);
    }""")
    with pytest.raises(BrowserTimeout):
        fill_form(page, PAYLOAD)
    assert page.locator("#limitPrice").input_value() == ""
    assert page.evaluate("window.submits") == 0
