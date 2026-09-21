import time
from decimal import Decimal as D

import pytest

from app.market import Candle, Snapshot
from app.strategy import Config


@pytest.fixture
def market():
    now = (int(time.time() * 1000) // 60000) * 60000 + 1000
    rows = [
        Candle(
            time=now - 1000 - i * 60000,
            close_time=now - 1001 - (i - 1) * 60000,
            open="10",
            high="10.2",
            low="9.8",
            close=str(D(10) + (i - 2) * D("0.1")),
            volume="10",
            quote_volume="100",
        )
        for i in (3, 2, 1)
    ]
    return Snapshot(
        symbol="ALPHA_TESTUSDT",
        token="TEST",
        chain="bsc",
        address="0x1",
        quote="USDT",
        fetched_at=now,
        book_time=now,
        latency_ms=5,
        tick="0.01",
        step="0.01",
        min_qty="0.01",
        min_notional="0.1",
        candles=rows,
        bids=[(str(D(10) - D("0.01") * i), "100") for i in range(6)],
        asks=[("10.1", "100")],
        ticker={},
    )


@pytest.fixture
def config():
    return Config(window=3, fee_bps="10")
