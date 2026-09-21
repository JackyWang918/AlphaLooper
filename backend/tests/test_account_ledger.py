from sqlalchemy import create_engine

from app import account_ledger as ledger


def observation(
    side="买入",
    quantity="10",
    gross="20",
    order_id="000000000000001",
    time="2026-01-01 01:00:00",
    status="已成交",
):
    return {
        "chain": "bsc",
        "address": "0xtest",
        "captured_at": 10,
        "tables": [
            {"headers": ["", *ledger.HEADERS, ""], "rows": []},
            {
                "headers": [],
                "rows": [
                    [
                        "",
                        time,
                        "TEST",
                        "限价",
                        side,
                        "2 USDT",
                        "2 USDT",
                        quantity + " TEST",
                        quantity + " TEST",
                        gross + " USDT",
                        "-",
                        "-",
                        "-",
                        status,
                    ],
                    [
                        "订单ID: "
                        + order_id
                        + " 更新时间: "
                        + time
                        + " 忽略逐笔明细" * 1000
                    ],
                ],
            },
        ],
    }


def engine():
    e = create_engine("sqlite://")
    ledger.orders.create(e)
    return e


def test_order_id_upsert_partial_cancel_and_account_isolation():
    e = engine()
    first = observation(quantity="2", gross="4", status="部分成交")
    ledger.update(e, "a", first)
    ledger.update(e, "a", first)
    assert len(ledger.read(e, "a")["orders"]) == 1
    final = observation(quantity="3", gross="6", status="已取消")
    final["captured_at"] = 11
    result = ledger.update(e, "a", final)
    assert result["stats"][0]["buy_total"] == "6"
    assert result["stats"][0]["estimated_fee"] == "0.0006"
    assert result["stats"][0]["estimated_points"] == "24"
    assert ledger.update(e, "a", first)["rejected"] == 1
    assert ledger.read(e, "b")["orders"] == []
    ledger.update(e, "b", first)
    assert ledger.read(e, "a")["stats"][0]["buy_total"] == "6"


def test_platform_gross_used_pnl_sell_points_and_missing_cost():
    e = engine()
    ledger.update(
        e, "a", observation(gross="21")
    )  # Deliberately differs from qty * avg.
    result = ledger.update(
        e,
        "a",
        observation(side="卖出", gross="22", order_id="2", time="2026-01-01 01:01:00"),
    )
    stat = result["stats"][0]
    assert stat["buy_total"] == "21" and stat["estimated_points"] == "84"
    assert stat["estimated_realized_pnl"] == "0.9957"
    assert stat["recorded_quantity"] == "0"
    unknown = ledger.update(e, "b", observation(side="卖出"))["stats"][0]
    assert unknown["estimated_realized_pnl"] is None
    assert unknown["estimated_points"] == "0"


def test_no_id_wrong_schema_and_login_never_import():
    sample = observation()
    sample["tables"][1]["rows"].pop()
    assert ledger.extract(sample) == ([], 1)
    sample = observation()
    sample["tables"][0]["headers"] = ["成交时间"]
    assert ledger.extract(sample) == ([], 0)
    sample = observation()
    sample["login_prompt_visible"] = True
    assert ledger.extract(sample) == ([], 0)


def test_same_time_ordering_unknown_and_decimal_precision():
    e = engine()
    ledger.update(e, "a", observation(gross="0.123456789012345678"))
    result = ledger.update(e, "a", observation(side="卖出", gross="0.2", order_id="2"))
    assert result["stats"][0]["estimated_realized_pnl"] is None
    assert result["orders"][1]["estimated_fee"] == "0.0000123456789012345678"


def test_only_first_history_order_and_no_fallback_to_older_order():
    # Numeric IDs remain text; creation order is supplied by the history page.
    current = observation(order_id="20")
    older = observation(order_id="19")["tables"][1]["rows"]
    current["tables"][1]["rows"].extend(older)
    result, skipped = ledger.extract(current)
    assert [order["order_id"] for order in result] == ["20"]
    assert skipped == 0
    current["tables"][1]["rows"].pop(1)  # First order is not expanded.
    assert ledger.extract(current) == ([], 1)
