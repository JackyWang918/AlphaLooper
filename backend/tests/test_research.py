import time
from decimal import Decimal as D
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.market import Candle, MarketError, resolve_pair, validate_snapshot
from app.strategy import Event, Order, State, advance, estimate, exposure

URL = (
    "https://www.binance.com/zh-CN/alpha/bsc/0x10d4183389e99233db3cc981c43443ebd28ebd5e"
)



def event(kind="tick", advance_seconds=0, **kwargs):
    return Event(
        id=str(uuid4()),
        kind=kind,
        advance_seconds=advance_seconds,
        **kwargs,
    )


def held(m):
    clock = m.fetched_at // 60000 * 60
    return State(
        symbol=m.symbol,
        clock=clock,
        last_check_minute=clock // 60,
        cost="50",
        inventory="5",
        order=Order(side="sell", price="10.1", quantity="5", placed_at=clock),
    )


def test_estimate_closed_candles_and_rounding(market, config):
    market.candles.append(
        Candle(
            time=market.fetched_at - 1000,
            close_time=market.fetched_at + 58999,
            open="999",
            high="999",
            low="999",
            close="999",
            volume="1",
            quote_volume="999",
        )
    )
    e = estimate(market, config)
    assert e["center"] == D(10)
    assert e["buy"] == D("9.95") and e["sell"] == D("10.05")
    assert e["quantity"] * e["buy"] * (1 + D("0.001")) <= 50


def test_bad_candles_and_stale_book(market, config):
    with pytest.raises(MarketError):
        validate_snapshot(market, market.fetched_at + 16000)
    market.candles[1].time += 1
    with pytest.raises(MarketError):
        estimate(market, config)


def test_crossed_auction_book_blocks_new_buy(market, config):
    market.asks = [(D("9.9"), D(100))]
    assert not estimate(market, config)["buy_allowed"]
    assert advance(State(), event(), market, config).order is None


def test_timeout_cancel_ack_and_late_partial_fill(market, config):
    s = advance(State(), event(), market, config)
    original = s.order.price
    s = advance(s, event(advance_seconds=300), market, config)
    assert s.order.cancel_requested
    same = advance(s, event(), market, config)
    assert same.order == s.order
    s = advance(s, event("fill", quantity="1", price=str(original)), market, config)
    assert s.inventory == 1 and s.order.cancel_requested
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.order.side == "sell" and s.order.quantity == 1
    assert s.order.placed_at == s.clock


def test_empty_buy_reprices_after_confirm(market, config):
    s = advance(State(), event(), market, config)
    s = advance(s, event(advance_seconds=300), market, config)
    old = s.order.price
    for candle in market.candles:
        candle.quote_volume = D("100.5")
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.order.side == "buy" and s.order.price > old


def test_minute_only_stop_and_sticky_exit(market, config):
    s = held(market)
    market.bids = [(D("9.7") - D("0.01") * i, D(100)) for i in range(6)]
    s = advance(s, event(advance_seconds=59), market, config)
    assert not s.exiting
    s = advance(s, event(advance_seconds=1), market, config)
    assert s.exiting and s.order.cancel_requested
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.order.price == D("9.65")
    market.bids = [(D("10.2") - D("0.01") * i, D(100)) for i in range(6)]
    s = advance(s, event(advance_seconds=15), market, config)
    assert s.exiting and s.order.cancel_requested
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.order.price == D("10.15")


def test_break_even_uses_ask_and_includes_fees(market, config):
    s = held(market)
    s.cost = D(49)
    s.order = None
    result = advance(s, event(), market, config)
    assert result.order.price == D("10.1")
    s.cost = D(50)
    assert exposure(s, market, config)["loss"] == D("0.050")
    s.proceeds = D(20)
    s.inventory = D(3)
    assert exposure(s, market, config)["loss"] == D("0.030")


def test_budget_with_unrealized_loss_cancels_and_exits(market, config):
    s = held(market)
    s.session_loss = D("9.99")
    s = advance(s, event(advance_seconds=60), market, config)
    assert s.budget_stopped and s.exiting and s.order.cancel_requested
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.order.side == "sell"
    result = advance(State(session_loss="8"), event(), market, config)
    assert result.budget_stopped and result.order is None


def test_shallow_book_is_unknown_not_zero(market, config):
    s = held(market)
    market.bids = [(D("9.9"), D("0.1"))]
    assert exposure(s, market, config)["loss"] is None
    s = advance(s, event(advance_seconds=60), market, config)
    s = advance(s, event("cancel_confirm"), market, config)
    assert s.exiting and s.order.price == D("9.9")


def test_duplicate_and_invalid_fills(market, config):
    s = advance(State(), event(), market, config)
    fill = event("fill", quantity="1", price="9.95")
    s = advance(s, fill, market, config)
    assert advance(s, fill, market, config) == s
    with pytest.raises(ValueError):
        advance(s, event("fill", quantity="99", price="9"), market, config)
    with pytest.raises(ValueError):
        advance(s, event("fill", quantity="1", price="100"), market, config)


def test_round_settlement_keeps_loss_and_no_automatic_new_order(market, config):
    s = held(market)
    s.session_loss = D(1)
    s.order.price = D("9.9")
    s = advance(s, event("fill", quantity="5", price="9.9"), market, config)
    assert s.inventory == 0 and s.order is None
    assert s.session_loss == D("1.5495")


def test_pair_mapping_uses_chain_and_contract():
    tokens = [
        {
            "chainId": "56",
            "contractAddress": URL.split("/")[-1],
            "alphaId": "ALPHA_1",
            "symbol": "TEST",
        },
        {
            "chainId": "1",
            "contractAddress": URL.split("/")[-1],
            "alphaId": "ALPHA_2",
            "symbol": "TEST",
        },
    ]
    exchange = {
        "symbols": [
            {
                "symbol": "ALPHA_1USDT",
                "baseAsset": "ALPHA_1",
                "quoteAsset": "USDT",
                "status": "TRADING",
            }
        ]
    }
    assert resolve_pair(tokens, exchange, URL, "USDT")[1]["symbol"] == "ALPHA_1USDT"
    with pytest.raises(MarketError):
        resolve_pair(tokens, exchange, URL, "USDC")


def test_api_rejects_stale_snapshot_without_touching_browser(market, config):
    market.book_time = 1
    with TestClient(app) as client:
        result = client.post(
            "/api/research/simulate",
            headers={"X-AlphaLooper-Client": "local-ui"},
            json={
                "state": {},
                "event": event().model_dump(mode="json"),
                "snapshot": market.model_dump(mode="json"),
                "config": config.model_dump(mode="json"),
            },
        )
        assert result.status_code == 422
        assert app.state.browser._process is None


def test_api_prices_remain_decimal_strings(market, config, monkeypatch):
    from app import research_api

    market.fetched_at = int(time.time() * 1000)
    market.book_time = market.fetched_at
    monkeypatch.setattr(research_api.market, "snapshot", lambda *args: market)
    with TestClient(app) as client:
        headers = {"X-AlphaLooper-Client": "local-ui"}
        result = client.post(
            "/api/research/preview",
            headers=headers,
            json={
                "url": URL,
                "quote": "USDT",
                "config": config.model_dump(mode="json"),
            },
        )
        assert result.status_code == 200
        assert result.json()["estimate"]["buy"] == "9.95"
        result = client.post(
            "/api/research/simulate",
            headers=headers,
            json={
                "state": {},
                "event": event().model_dump(mode="json"),
                "snapshot": market.model_dump(mode="json"),
                "config": config.model_dump(mode="json"),
            },
        )
        assert result.status_code == 200
        assert result.json()["state"]["order"]["price"] == "9.95"
        assert app.state.browser._process is None
