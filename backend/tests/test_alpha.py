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
    assert result["submitted"] is False
    assert page.evaluate("window.submits") == 0


def test_fill_accepts_equivalent_value_with_trimmed_trailing_zeroes(page):
    page.locator("#limitAmount").evaluate(
        "e=>e.addEventListener('blur',()=>e.value=String(Number(e.value)))"
    )
    result = fill_form(page, {**PAYLOAD, "quantity": "47.82000000"})
    assert result["quantity"] == "47.82"
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


def test_fill_reports_actual_normalized_mismatch(page):
    page.locator("#limitAmount").evaluate(
        "e=>e.addEventListener('blur',()=>e.value='47.81')"
    )
    with pytest.raises(ValueError, match="计划数量 47.82000000、页面数量 47.81"):
        fill_form(page, {**PAYLOAD, "quantity": "47.82000000"})
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
    page.locator("#limitAmount").evaluate(
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
