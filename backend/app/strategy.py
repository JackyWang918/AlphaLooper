"""Decimal-only research model and explicit-event simulator. No execution adapter."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from itertools import pairwise
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.market import MarketError, Snapshot

D = Decimal


class Config(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    amount: Decimal = Field(default=D("50"), gt=0, le=10000)
    window: int = Field(default=15, ge=3, le=240)
    buy_offset: Decimal = Field(default=D("0.5"), ge=0, le=5)
    sell_offset: Decimal = Field(default=D("0.5"), ge=0, le=5)
    fee_bps: Decimal = Field(
        ge=0, le=100
    )  # Required assumption, never inferred from market data.
    stop_pct: Decimal = Field(default=D("2"), gt=0, le=20)
    budget: Decimal = Field(default=D("10"), gt=0, le=1000)
    reserve: Decimal = Field(default=D("2"), ge=0, le=100)
    wait_seconds: int = Field(default=300, ge=10, le=3600)
    exit_seconds: int = Field(default=15, ge=5, le=300)
    exit_level: int = Field(default=6, ge=1, le=100)
    max_hold_seconds: int = Field(default=1800, ge=60, le=86400)
    target_points: Decimal = Field(default=D("32768"), gt=0)
    points_per_u: Decimal = Field(default=D("4"), gt=0)


def align(value, step, up=False):
    return (value / step).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR
    ) * step


def estimate(m: Snapshot, c: Config):
    rows = sorted(
        [r for r in m.candles if r.close_time < m.fetched_at], key=lambda r: r.time
    )[-c.window :]
    if len(rows) != c.window or any(
        b.time - a.time != 60000 for a, b in pairwise(rows)
    ):
        raise MarketError("已收盘的一分钟 K 线数量不足或时间不连续。")
    if m.fetched_at - rows[-1].close_time > 90000:
        raise MarketError("最近已收盘 K 线过期。")
    if any(
        r.high < max(r.open, r.close, r.low) or r.low > min(r.open, r.close)
        for r in rows
    ):
        raise MarketError("K 线高低价关系异常。")
    volume = sum((r.volume for r in rows), D(0))
    if volume <= 0:
        raise MarketError("观察窗口无成交量。")
    with localcontext() as ctx:
        ctx.prec = 40
        center = sum((r.quote_volume for r in rows), D(0)) / volume
        mean = sum((r.close for r in rows), D(0)) / len(rows)
        volatility = (
            sum(((r.close - mean) ** 2 for r in rows), D(0)) / len(rows)
        ).sqrt()
        buy = align(center - volatility * c.buy_offset, m.tick)
        sell = align(center + volatility * c.sell_offset, m.tick, True)
        if buy <= 0:
            raise MarketError("计算买价非正数，请调整试验参数。")
        quantity = align(c.amount / (buy * (1 + c.fee_bps / 10000)), m.step)
        warnings = ["参数为试验值，成交和损耗未获验证；手续费按手动假设计算。"]
        crossed = m.bids[0][0] >= m.asks[0][0]
        if crossed:
            warnings.append(
                "竞价盘口交叉/锁定；暂停模拟新买入，深度估值不是保证成交报价。"
            )
        if buy >= m.asks[0][0]:
            warnings.append(
                "历史买价已触及卖一，可能主动成交；模拟器暂停新买单，需调整参数。"
            )
        if sell <= m.bids[0][0]:
            warnings.append("历史卖价低于或等于买一，可能主动成交。")
        if quantity < m.min_qty or buy * quantity < m.min_notional:
            raise MarketError("按步长取整后不足最低下单量/金额。")
        return {
            "center": center,
            "volatility": volatility,
            "buy": buy,
            "sell": sell,
            "quantity": quantity,
            "window": c.window,
            "last_closed": rows[-1].close_time,
            "warnings": warnings,
            "buy_allowed": not crossed and buy < m.asks[0][0],
        }


class Order(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: str = Field(default_factory=lambda: str(uuid4()))
    side: Literal["buy", "sell"]
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    filled: Decimal = Field(default=D(0), ge=0)
    placed_at: int
    cancel_requested: bool = False


class State(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str = ""
    clock: int = 0
    last_check_minute: int = 0
    cost: Decimal = Field(default=D(0), ge=0)
    inventory: Decimal = Field(default=D(0), ge=0)
    proceeds: Decimal = Field(default=D(0), ge=0)
    session_loss: Decimal = Field(default=D(0), ge=0)
    exiting: bool = False
    budget_stopped: bool = False
    buy_total: Decimal = Field(default=D(0), ge=0)
    first_buy_at: int | None = None
    target_reached: bool = False
    completed: bool = False
    order: Order | None = None
    event_ids: list[str] = Field(default_factory=list, max_length=2000)
    message: str = "空仓，尚未创建模拟订单。"


class Event(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["tick", "fill", "cancel_confirm"]
    advance_seconds: int = Field(default=0, ge=0, le=3600)
    quantity: Decimal = Field(default=D(0), ge=0)
    price: Decimal = Field(default=D(0), ge=0)


def exposure(s: State, m: Snapshot, c: Config):
    remaining, gross = s.inventory, D(0)
    for price, size in m.bids:
        taken = min(remaining, size)
        gross += taken * price
        remaining -= taken
        if remaining == 0:
            break
    if remaining > 0:
        return {"covered": False, "loss": None, "loss_pct": None, "exit_net": None}
    net = gross * (1 - c.fee_bps / 10000)
    loss = max(D(0), s.cost - s.proceeds - net)
    return {
        "covered": True,
        "loss": loss,
        "loss_pct": loss / s.cost * 100 if s.cost else D(0),
        "exit_net": net,
    }


def advance(state: State, event: Event, m: Snapshot, c: Config):
    s = state.model_copy(deep=True)
    if event.id in s.event_ids:
        return s
    if len(s.event_ids) >= 2000:
        raise ValueError("沙盒事件达到上限，请导出记录后重置实验。")
    if s.symbol and s.symbol != m.symbol:
        raise ValueError("模拟会话不能中途换币，请重置沙盒。")
    s.symbol = m.symbol
    if not s.clock:
        s.clock = (m.fetched_at // 60000) * 60
        s.last_check_minute = s.clock // 60
    s.clock += event.advance_seconds
    s.event_ids.append(event.id)
    fee = c.fee_bps / 10000
    if event.kind == "fill":
        order = s.order
        if not order or event.quantity <= 0 or event.price <= 0:
            raise ValueError("成交事件需要有效订单、正数量及正价格。")
        if event.quantity > order.quantity - order.filled:
            raise ValueError("模拟成交量超过该订单剩余数量。")
        if (order.side == "buy" and event.price > order.price) or (
            order.side == "sell" and event.price < order.price
        ):
            raise ValueError("模拟成交价格违反限价约束。")
        order.filled += event.quantity
        if order.side == "buy":
            if s.first_buy_at is None:
                s.first_buy_at = s.clock
            s.buy_total += event.quantity * event.price
            s.cost += event.quantity * event.price * (1 + fee)
            s.inventory += event.quantity
        else:
            if event.quantity > s.inventory:
                raise ValueError("模拟卖出超过持仓。")
            s.inventory -= event.quantity
            s.proceeds += event.quantity * event.price * (1 - fee)
        if order.filled == order.quantity:
            s.order = None
        s.message = "已记录人工指定的模拟成交；不是市场成交推断。"
    if event.kind == "cancel_confirm":
        if not s.order or not s.order.cancel_requested:
            raise ValueError("没有待确认的模拟撤单。")
        s.order = None
        s.message = "已确认模拟撤单，按最终成交量处理剩余持仓。"
    s.target_reached = (
        s.target_reached or s.buy_total * c.points_per_u >= c.target_points
    )
    if s.inventory and (
        s.target_reached
        or (
            s.first_buy_at is not None
            and s.clock - s.first_buy_at >= c.max_hold_seconds
        )
    ):
        s.exiting = True
    # Settle fully exited rounds; gains do not replenish the loss budget (conservative trial rule).
    if s.cost and s.inventory == 0 and s.order is None:
        s.session_loss += max(D(0), s.cost - s.proceeds)
        s.cost = s.proceeds = D(0)
        s.exiting = False
        s.first_buy_at = None
        s.message = "本轮结束；再次推进时才评估下一轮。"
        s.budget_stopped = s.budget_stopped or s.session_loss >= c.budget
        s.completed = s.target_reached or s.budget_stopped
        if s.completed:
            s.message = "任务已清仓结束；不再创建新买单。"
        return s
    risk = exposure(s, m, c)
    # Mark-to-exit losses are checked only on a crossed natural-minute boundary.
    if s.clock // 60 > s.last_check_minute:
        s.last_check_minute = s.clock // 60
        if s.inventory and not risk["covered"]:
            s.exiting = True
            s.message = "买盘深度不足，亏损未知；进入主动退出，不视为零亏损。"
        elif s.inventory and risk["loss_pct"] >= c.stop_pct:
            s.exiting = True
        if s.session_loss + (risk["loss"] or D(0)) >= c.budget:
            s.budget_stopped = True
            if s.inventory:
                s.exiting = True
    if s.order:
        if s.order.cancel_requested:
            s.message = "等待撤单确认；期间允许记录迟到成交，不创建重复订单。"
            return s
        timeout = (
            c.exit_seconds if s.exiting and s.order.side == "sell" else c.wait_seconds
        )
        if (
            s.clock - s.order.placed_at >= timeout
            or (s.exiting and not state.exiting)
            or ((s.budget_stopped or s.target_reached) and s.order.side == "buy")
        ):
            s.order.cancel_requested = True
            s.message = "请求模拟撤单；请先记录撤单期间成交，再确认撤单。"
        else:
            s.message = "等待模拟成交或挂单超时。"
        return s
    if s.inventory:
        if s.exiting:
            price = m.bids[min(c.exit_level, len(m.bids)) - 1][0]
            reason = "主动退出（不足目标档位时使用最深可见买档），不保证全部成交。"
        elif risk["covered"] and risk["loss"] == 0:
            price, reason = m.asks[0][0], "预计可回本，参考卖一。"
        else:
            price, reason = estimate(m, c)["sell"], "尚未回本，按历史模型挂卖价。"
        quantity = align(s.inventory, m.step)
        if quantity <= 0 or quantity < m.min_qty or quantity * price < m.min_notional:
            s.message = "剩余数量不足最小委托要求，需要人工处理；不算作已清仓。"
            return s
        s.order = Order(side="sell", price=price, quantity=quantity, placed_at=s.clock)
        s.message = "模拟卖单：" + reason
    else:
        if s.completed or s.target_reached:
            s.completed = True
            s.message = "任务已清仓结束；不再创建新买单。"
            return s
        reserve = max(c.reserve, c.amount * (c.stop_pct / 100 + 2 * fee))
        if s.budget_stopped or s.session_loss + reserve >= c.budget:
            s.budget_stopped = True
            s.completed = True
            s.message = "停止新买入：损耗预算或退出预留不足。"
            return s
        e = estimate(m, c)
        if not e["buy_allowed"]:
            s.message = (
                "盘口交叉/锁定或历史买价已触及卖一，暂停新买单；等待新行情或调整参数。"
            )
            return s
        remaining = c.target_points / c.points_per_u - s.buy_total
        # Smallest valid final order, still capped by the per-round budget.
        quantity = min(
            e["quantity"],
            max(
                align(remaining / e["buy"], m.step, True),
                align(m.min_qty, m.step, True),
                align(m.min_notional / e["buy"], m.step, True),
            ),
        )
        s.order = Order(
            side="buy", price=e["buy"], quantity=quantity, placed_at=s.clock
        )
        s.message = "已创建模拟买单；不根据 K 线触价自动判定成交。"
    return s
