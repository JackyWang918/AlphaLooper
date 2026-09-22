import pytest

from app.browser.confirmation import validate_confirmation
from app.browser.schemas import FillForm
from tests.test_alpha import PAYLOAD

TEXT = """DGAI
DGrid AI
类型
限价 / 买入
委托价
0.90000000 USDT
数量
1.00 DGAI
成交额
0.90000000 USDT
预计手续费
0.0001 DGAI
付款账户
资金账户
限价单通常会以限价或更优价格执行，但不保证一定会成交。
继续"""


def test_decimal_equivalence_and_token_fee():
    result = validate_confirmation(TEXT, FillForm(**PAYLOAD))
    assert result["fee_currency"] == "DGAI"
    assert result["estimated_fee"] == "0.0001"


def test_observed_live_dialog_uses_estimated_fee_label():
    # Read-only evidence from the real confirmation dialog, no account identifiers.
    observed = """DGAI
DGrid AI
类型
限价 / 买入
委托价
1 USDT
数量
20 DGAI
成交额
20.00000000 USDT
预估手续费
0.002 DGAI
付款账户
资金账户
限价单通常会以限价或更优价格执行，但不保证一定会成交。
继续"""
    command = FillForm(**{**PAYLOAD, "price": "1", "quantity": "20"})
    assert validate_confirmation(observed, command)["estimated_fee"] == "0.002"
    with pytest.raises(ValueError, match="数量"):
        validate_confirmation(observed, command.model_copy(update={"quantity": "23"}))
    with pytest.raises(ValueError, match="多个手续费"):
        validate_confirmation(observed + "\n预计手续费\n0.002 DGAI", command)


@pytest.mark.parametrize(
    "old,new",
    [
        ("DGAI\nDGrid", "OTHER\nDGrid"),
        ("限价 / 买入", "限价 / 卖出"),
        ("限价 / 买入", "市价 / 买入"),
        ("委托价\n0.90000000", "委托价\n0.91"),
        ("数量\n1.00", "数量\n10.00"),
        ("成交额\n0.90000000", "成交额\n9.00"),
        ("USDT", "USDC"),
        ("预计手续费\n0.0001 DGAI", "预计手续费\n0.1 BNB"),
        ("继续", "安全验证\n继续"),
        ("继续", "委托价\n0.90000000 USDT\n继续"),
        ("继续", "委托价\n无法读取\n继续"),
    ],
)
def test_mismatched_or_ambiguous_confirmation_is_rejected(old, new):
    with pytest.raises(ValueError):
        validate_confirmation(TEXT.replace(old, new), FillForm(**PAYLOAD))


def test_buy_budget_uses_displayed_fee():
    text = TEXT.replace("0.90000000", "49.99").replace("0.0001 DGAI", "0.02 USDT")
    with pytest.raises(ValueError, match="50 U"):
        validate_confirmation(text, FillForm(**{**PAYLOAD, "price": "49.99"}))


def test_grouped_numbers_and_sell_are_supported():
    text = TEXT.replace("限价 / 买入", "限价 / 卖出").replace(
        "1.00 DGAI", "1,000.00 DGAI"
    )
    text = text.replace("成交额\n0.90000000", "成交额\n900.00")
    validate_confirmation(
        text, FillForm(**{**PAYLOAD, "side": "sell", "quantity": "1000"})
    )
