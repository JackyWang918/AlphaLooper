import pytest

from app.browser.automatic import available_balance, cancel_once, inspect_progress
from tests.test_alpha import PAYLOAD
from tests.test_live_browser import page as base_page  # noqa: F401


@pytest.fixture(name="page")
def isolated_page(request):
    return request.getfixturevalue("base_page")


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
        "<table><thead><tr>"
        + "".join(f"<th>{v}</th>" for v in headers)
        + "</tr></thead><tbody><tr>"
        + "".join(f"<td>{v}</td>" for v in fields)
        + "</tr></tbody></table>"
    )
    page.locator("#current").evaluate("(e,html)=>e.innerHTML=html", html)
    page.evaluate("window.cancels=0")


def test_partial_progress_and_single_scoped_cancel(page):
    install_order(page)
    assert inspect_progress(page, PAYLOAD)["progress"] == {
        "quantity": "0.2",
        "gross": "0.18",
    }
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<button onclick=\"window.wrong=true\">撤单</button>')"
    )
    assert cancel_once(page, PAYLOAD)["cancel_clicked"]
    assert page.evaluate("window.cancels") == 1
    assert page.evaluate("window.wrong||false") is False


@pytest.mark.parametrize(
    "changes", [{"price": "0.8 USDT"}, {"quantity": "2 DGAI"}, {"direction": "卖出"}]
)
def test_cancel_rejects_wrong_order_without_click(page, changes):
    install_order(page, **changes)
    with pytest.raises(ValueError):
        cancel_once(page, PAYLOAD)
    assert page.evaluate("window.cancels") == 0


def test_balance_read_switches_side_but_never_submits(page):
    page.locator("body").evaluate(
        "e=>e.insertAdjacentHTML('beforeend','<p>可用余额： 1.9998 DGAI</p>')"
    )
    assert available_balance(page, PAYLOAD) == {"available": "1.9998"}
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
