"""Public Alpha market data only; no cookies, account endpoints or trading calls."""

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from app.browser.schemas import token_identity
from app.database import PROJECT_ROOT

BASE = "https://www.binance.com"
PREFIX = "/bapi/defi/v1/public/alpha-trade/"
TOKEN_LIST = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
ENV_FILE = PROJECT_ROOT / "backend" / ".env"


def network_settings():
    # Resolve against the repository, not the terminal's working directory.
    # Explicit process settings (including an empty proxy) take precedence.
    local = dotenv_values(ENV_FILE, encoding="utf-8-sig", interpolate=False)
    proxy = (
        os.environ.get("ALPHALOOPER_HTTP_PROXY", local.get("ALPHALOOPER_HTTP_PROXY"))
        or None
    )
    transport = (
        os.environ.get(
            "ALPHALOOPER_HTTP_TRANSPORT", local.get("ALPHALOOPER_HTTP_TRANSPORT")
        )
        or "httpx"
    )
    return proxy, transport


class MarketError(ValueError):
    pass


class Candle(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    time: int
    close_time: int
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    quote_volume: Decimal = Field(ge=0)


class Snapshot(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str
    token: str
    chain: str
    address: str
    quote: str
    fetched_at: int
    book_time: int
    latency_ms: int
    tick: Decimal = Field(gt=0)
    step: Decimal = Field(gt=0)
    min_qty: Decimal = Field(ge=0)
    min_notional: Decimal = Field(ge=0)
    candles: list[Candle]
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    ticker: dict


def public_get(path: str, params=None):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    proxy, transport = network_settings()
    try:
        if transport == "curl":
            args = [
                "curl.exe" if os.name == "nt" else "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "15",
            ]
            if proxy:
                args += ["--proxy", proxy]
            result = subprocess.run(
                args + [url], capture_output=True, timeout=18, check=True
            )
            body = json.loads(result.stdout)
        elif transport == "httpx":
            with httpx.Client(proxy=proxy, timeout=15, trust_env=False) as client:
                response = client.get(url)
                response.raise_for_status()
                body = response.json()
        else:
            raise MarketError("ALPHALOOPER_HTTP_TRANSPORT 必须为 httpx 或 curl。")
        if (
            not isinstance(body, dict)
            or body.get("code") != "000000"
            or body.get("success") is False
            or body.get("data") is None
        ):
            raise MarketError("Alpha 行情接口返回失败，未使用旧数据替代。")
        return body["data"]
    except (httpx.HTTPError, OSError, subprocess.SubprocessError, ValueError) as exc:
        if isinstance(exc, MarketError):
            raise
        route = "已配置代理" if proxy else "直连，未配置代理"
        endpoint = path.rstrip("/").rsplit("/", 1)[-1]
        raise MarketError(
            f"公开行情请求失败（接口 {endpoint}；{transport}；{route}；{type(exc).__name__}）。"
            "请检查 Clash 是否运行及 backend/.env 的代理端口。"
        ) from exc


def resolve_pair(tokens, exchange, url, quote):
    chain, address = token_identity(url)
    # Verified BSC mapping. Do not guess other chain IDs or normalize Solana addresses.
    if chain != "bsc":
        raise MarketError("本阶段行情映射仅验证了 BSC 链。")
    matches = [
        t
        for t in tokens
        if str(t.get("chainId")) == "56"
        and str(t.get("contractAddress", "")).lower() == address
    ]
    if len(matches) != 1:
        raise MarketError("无法按链与合约唯一匹配 Alpha 代币。")
    token = matches[0]
    pairs = [
        p
        for p in exchange["symbols"]
        if p["baseAsset"] == token["alphaId"]
        and p["quoteAsset"] == quote
        and p["status"] == "TRADING"
    ]
    if len(pairs) != 1:
        raise MarketError("该代币没有可用的指定计价交易对。")
    return token, pairs[0], chain, address


def validate_snapshot(m: Snapshot, now_ms: int, max_age=15000):
    if (
        not -5000 <= now_ms - m.book_time <= max_age
        or not -5000 <= now_ms - m.fetched_at <= max_age
    ):
        raise MarketError("盘口已过期或时间异常，请刷新行情后试算。")
    for rows, reverse in [(m.bids, True), (m.asks, False)]:
        if not rows or any(
            p <= 0 or q <= 0 or not p.is_finite() or not q.is_finite() for p, q in rows
        ):
            raise MarketError("盘口为空或存在非法档位。")
        prices = [p for p, _ in rows]
        if prices != sorted(set(prices), reverse=reverse):
            raise MarketError("盘口顺序或档位重复异常。")


class MarketClient:
    def __init__(self):
        self.metadata = None
        self.metadata_time = 0.0

    def snapshot(self, url: str, quote: str, window: int):
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=3) as pool:
            if self.metadata is None or time.monotonic() - self.metadata_time > 300:
                tokens = pool.submit(public_get, TOKEN_LIST)
                exchange = pool.submit(public_get, PREFIX + "get-exchange-info")
                self.metadata = tokens.result(), exchange.result()
                self.metadata_time = time.monotonic()
            try:
                token, pair, chain, address = resolve_pair(*self.metadata, url, quote)
            except (KeyError, TypeError) as exc:
                raise MarketError("代币列表或交易规则字段异常。") from exc
            symbol = pair["symbol"]
            futures = [
                pool.submit(public_get, PREFIX + name, params)
                for name, params in [
                    (
                        "klines",
                        {
                            "symbol": symbol,
                            "interval": "1m",
                            "limit": max(60, window + 2),
                        },
                    ),
                    ("fullDepth", {"symbol": symbol, "limit": 100}),
                    ("ticker", {"symbol": symbol}),
                ]
            ]
            rows, book, ticker = [f.result() for f in futures]
        now = int(time.time() * 1000)
        try:
            filters = {f["filterType"]: f for f in pair["filters"]}
            if book.get("symbol") != symbol or ticker.get("symbol") != symbol:
                raise MarketError("行情返回的交易对与请求不符。")
            pf, lot = filters["PRICE_FILTER"], filters["LOT_SIZE"]
            nf = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
            candles = [
                Candle(
                    time=int(r[0]),
                    close_time=int(r[6]),
                    open=r[1],
                    high=r[2],
                    low=r[3],
                    close=r[4],
                    volume=r[5],
                    quote_volume=r[7],
                )
                for r in rows
            ]
            result = Snapshot(
                symbol=symbol,
                token=token["symbol"],
                chain=chain,
                address=address,
                quote=quote,
                fetched_at=now,
                book_time=int(book.get("E", book.get("T", 0))),
                latency_ms=int((time.monotonic() - start) * 1000),
                tick=pf["tickSize"],
                step=lot["stepSize"],
                min_qty=lot["minQty"],
                min_notional=nf["minNotional"],
                candles=candles,
                bids=book["bids"],
                asks=book["asks"],
                ticker=ticker,
            )
            validate_snapshot(result, now)
            return result
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            if isinstance(exc, MarketError):
                raise
            raise MarketError("行情字段或交易规则缺失/异常，无法安全试算。") from exc
