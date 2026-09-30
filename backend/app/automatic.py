"""Durable live task coordinator. Real fills come only from the order adapter.

One process, one shared RLock, one active task and one active order. A planned
request is committed before entering LiveOrders. After a crash an unsent plan
requires explicit resume; an existing intent is reconciled, never replayed.
"""

import json
import time
from decimal import Decimal as D
from decimal import localcontext
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Column, Integer, String, Table, Text, select

from app import decision_log
from app.browser.schemas import TradingAccount, token_identity
from app.database import Base
from app.limits import MAX_BUY_QUOTE_AMOUNT
from app.live_orders import (
    BACKGROUND_TICK_SECONDS,
    CANCEL_CONFIRM_TIMEOUT_SECONDS,
    SETTLEMENT_POLL_SECONDS,
    SubmitOrder,
)
from app.market import MarketClient, MarketError, validate_snapshot
from app.strategy import align

RISK_POLICY_VERSION = 3
STRATEGY_POLICY_VERSION = 2
AUTO_ORDER_POLL_SECONDS = 5


def trade_price_band(m):
    """Use the last public trade as reference and the last closed bar as width."""
    rows = [row for row in m.candles if row.close_time < m.fetched_at]
    if not rows:
        raise MarketError("没有已收盘的一分钟 K 线，不能计算报价边界。")
    row = max(rows, key=lambda item: item.close_time)
    if m.fetched_at - row.close_time > 90000:
        raise MarketError("最近已收盘 K 线过期，不能计算报价边界。")
    try:
        price = D(str(m.ticker["lastPrice"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise MarketError("公开行情缺少有效 lastPrice，不能计算报价边界。") from exc
    if not price.is_finite() or price <= 0:
        raise MarketError("公开行情 lastPrice 非正数或非有限值。")
    width_pct = (row.high - row.low) / row.close * 100
    if width_pct < 0 or not width_pct.is_finite():
        raise MarketError("上一根 K 线振幅无效。")
    return {"reference": price, "width_pct": width_pct, "last_closed": row.close_time}


def band_order_price(m, side):
    band = trade_price_band(m)
    width = band["width_pct"] / 100
    raw = band["reference"] * (1 + width if side == "buy" else 1 - width)
    price = align(raw, m.tick, up=side == "buy")
    if price <= 0:
        raise MarketError("按价格步长计算的限价非正数。")
    return price, band

tasks = Table(
    "auto_tasks",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("active", Integer, nullable=True, unique=True),
    Column("payload", Text, nullable=False),
)


class LiveConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    buy_check_seconds: int = Field(default=20, ge=5, le=300, multiple_of=5)
    window: int = Field(default=3, ge=3, le=3)
    amount: D = Field(default=D(50), gt=0, le=MAX_BUY_QUOTE_AMOUNT)
    budget: D = Field(default=D(10), gt=0, le=10)
    fee_bps: D = Field(default=D(1), ge=1, le=1)
    stop_pct: D = Field(default=D(2), ge=2, le=2)
    wait_seconds: int = Field(default=30, ge=30, le=30)
    exit_seconds: int = Field(default=15, ge=15, le=15)
    max_hold_seconds: int = Field(default=120, ge=120, le=120)
    target_points: D = Field(default=D(32768), gt=0)
    current_points: D = Field(default=D(0), ge=0)
    points_per_u: D = Field(default=D(4), ge=4, le=4)

    @model_validator(mode="after")
    def current_points_not_above_target(self):
        if self.current_points > self.target_points:
            raise ValueError("当前已有积分不能大于目标积分。")
        return self


class StartTask(TradingAccount):
    request_id: UUID
    expected_symbol: str = Field(min_length=1, max_length=30)
    expected_quote: Literal["USDT", "USDC"] = "USDT"
    config: LiveConfig = Field(default_factory=LiveConfig)


class Automatic:
    def __init__(self, engine, live, market=None, clock=time.time, notifier=None):
        self.engine, self.live = engine, live
        self.market, self.clock = market or MarketClient(), clock
        self.notifier = notifier
        self.running = False  # Never restored from disk.
        self.last_poll = 0
        self.evidence = {}
        live.automatic = self

    def get(self, id=None):
        with self.engine.connect() as c:
            query = (
                select(tasks.c.payload).where(tasks.c.id == id)
                if id
                else select(tasks.c.payload).where(tasks.c.active == 1)
            )
            value = c.execute(query).scalar_one_or_none()
        return json.loads(value) if value else None

    def save(self, task, event=None, reason=None, details=None):
        task["updated_at"] = self.clock()
        with self.engine.begin() as c:
            c.execute(
                tasks.update()
                .where(tasks.c.id == task["id"])
                .values(active=1 if task["active"] else None, payload=json.dumps(task))
            )
            if event:
                decision_log.append(
                    c,
                    task["id"],
                    event,
                    self.clock(),
                    {
                        "reason": reason or task["message"],
                        "state": {
                            key: task.get(key)
                            for key in (
                                "inventory",
                                "cost",
                                "proceeds",
                                "buy_total",
                                "session_loss",
                                "task_start_equity",
                                "round_bought_quantity",
                                "exit_reason",
                                "realized_pnl",
                                "round_start_quote",
                                "round_stage",
                                "buy_rehangs",
                                "balances",
                                "dust",
                                "rounds",
                                "first_buy_at",
                                "exiting",
                                "stop_buying",
                                "pending",
                            )
                        },
                        "evidence": self.evidence,
                        "details": details,
                    },
                )

    def status(self):
        with self.live.lock, self.engine.connect() as c:
            records = [
                json.loads(p) for p in c.execute(select(tasks.c.payload)).scalars()
            ]
            records.sort(key=lambda t: t["created_at"], reverse=True)
            current = next((t for t in records if t["active"]), None)
            record = None
            if current and current.get("pending"):
                record = self.live.get(current["pending"]["request_id"])
                if record:
                    current = dict(current)
                    current["pending_order"] = {
                        "state": record["state"],
                        "message": record["message"],
                        "submission_error": record.get("submission_error"),
                    }
            if current:
                current = dict(current)
                if current.get("task_start_quote") is None:
                    payload = c.execute(
                        select(decision_log.decisions.c.payload)
                        .where(
                            decision_log.decisions.c.task_id == current["id"],
                            decision_log.decisions.c.kind == "control",
                        )
                        .order_by(decision_log.decisions.c.id.asc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if payload:
                        starting_balances = (
                            json.loads(payload).get("state", {}).get("balances")
                        )
                        if starting_balances:
                            current["task_start_quote"] = starting_balances.get(
                                "quote_available"
                            )
                current["schedule"] = self.next_action(current, record)
            return {"running": self.running, "current": current, "recent": records[:10]}

    def next_action(self, task, record=None):
        """Describe the next meaningful scheduler action for the control panel."""
        now = self.clock()
        if record is None and task.get("pending"):
            record = self.live.get(task["pending"]["request_id"])
        if not self.running:
            if record and record.get("active"):
                interval = (
                    SETTLEMENT_POLL_SECONDS
                    if record.get("state") == "settling"
                    else (
                        task["request"]["config"].get("exit_seconds", 15)
                        if task.get("exiting")
                        or record.get("cancel_requested_at")
                        or record.get("cancel_retry_after")
                        else AUTO_ORDER_POLL_SECONDS
                    )
                )
                checked_at = record.get("checked_at")
                return {
                    "kind": "read_only_check",
                    "at": max(now, checked_at + interval) if checked_at else now,
                    "reason": "任务已暂停；只读核对已有委托和余额，不撤单、不下单。",
                }
            return {
                "kind": "manual",
                "at": None,
                "reason": "等待人工恢复任务；当前不会自动交易。",
            }
        if record and record.get("state") == "submission_unknown":
            return {
                "kind": "manual",
                "at": None,
                "reason": "订单提交结果未知，等待人工核对，不会自动重发。",
            }
        if record and record.get("active"):
            if record.get("cancel_retry_after"):
                return {
                    "kind": "cancel_retry",
                    "at": record["cancel_retry_after"],
                    "reason": "重新识别撤单控件；此前已确认没有点击。",
                }
            if record.get("cancel_requested_at"):
                return {
                    "kind": "cancel_check",
                    "at": max(
                        now,
                        record.get("cancel_check_after", now),
                        record.get("checked_at", now)
                        + task["request"]["config"].get("exit_seconds", 15),
                    ),
                    "reason": "核对撤单结果、当前委托和余额；两分钟不明确则暂停。",
                }
            if record.get("state") == "settling":
                return {
                    "kind": "settlement_check",
                    "at": max(
                        now,
                        record.get("checked_at", now) + SETTLEMENT_POLL_SECONDS,
                    ),
                    "reason": "等待页面、委托状态和余额稳定。",
                }
            config = LiveConfig(**task["request"]["config"])
            timeout = (
                config.exit_seconds
                if task.get("exiting") and record["request"]["side"] == "sell"
                else config.wait_seconds
            )
            candidates = [
                (
                    record["created_at"] + timeout,
                    "order_timeout",
                    "主动退出卖单到时后撤单重估。"
                    if timeout == config.exit_seconds
                    else "普通挂单到达三十秒后撤单核对。",
                )
            ]
            if task.get("round_start_quote") is not None:
                candidates.append(
                    (
                        (task["last_minute"] + 1) * 60,
                        "minute_check",
                        "跨过下一分钟后巡检订单并检查资产损耗。",
                    )
                )
            if task.get("first_buy_at") is not None:
                candidates.append(
                    (
                        task["first_buy_at"] + config.max_hold_seconds,
                        "hold_timeout",
                        "首次买入后满两分钟进入主动退出。",
                    )
                )
            at, kind, reason = min(candidates, key=lambda item: item[0])
            return {"kind": kind, "at": max(now, at), "reason": reason}
        next_buy = task.get("next_buy_check_at", 0)
        if next_buy > now:
            return {
                "kind": "empty_evaluation",
                "at": next_buy,
                "reason": "空仓等待结束后重新获取 K 线并评估买入。",
            }
        return {
            "kind": "scheduler_tick",
            "at": now + BACKGROUND_TICK_SECONDS,
            "reason": "下一个后台心跳将核对状态并继续当前阶段。",
        }

    def create(self, body: StartTask):
        with self.live.lock, localcontext() as context:
            context.prec = 80
            request = body.model_dump(mode="json")
            old = self.get(str(body.request_id))
            if old:
                if old["request"] != request:
                    raise ValueError("任务编号已使用，不能修改参数。")
                return old  # Retry never starts/resumes.
            if self.get() or self.live.get():
                raise ValueError("仍有任务或单笔委托待处理，请先完成原任务。")
            # Reading the balance and order baseline is required before allocating a task.
            payload = self.probe(request)
            balances = self.live.call("order_readiness", payload)["balances"]
            start_price = None
            start_equity = D(balances["quote_available"])
            if D(balances["base_available"]) > 0:
                start_price = trade_price_band(
                    self.snapshot({"request": request}, body.config)
                )["reference"]
                start_equity += D(balances["base_available"]) * start_price
            task = {
                "id": str(body.request_id),
                "request": request,
                "active": True,
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "phase": "running",
                "message": "自动任务已启动，等待最新行情。",
                "accounting_version": 2,
                "risk_policy_version": RISK_POLICY_VERSION,
                "strategy_policy_version": STRATEGY_POLICY_VERSION,
                "balances": balances,
                "task_start_quote": balances["quote_available"],
                "task_start_equity": str(start_equity),
                "task_start_base": balances["base_available"],
                "task_start_price": str(start_price)
                if start_price is not None
                else None,
                "round_start_quote": None,
                "round_plan": "0",
                "round_stage": "idle",
                "buy_attempts": 0,
                "buy_rehangs": 0,
                "dust": "0",
                "inventory": balances["base_available"],
                "cost": "0",
                "round_bought_quantity": "0",
                "proceeds": "0",
                "buy_total": "0",
                "starting_points": str(body.config.current_points),
                "required_points": str(
                    body.config.target_points - body.config.current_points
                ),
                "fees": "0",
                "realized_pnl": "0",
                "session_loss": "0",
                "rounds": 0,
                "first_buy_at": None,
                "exiting": False,
                "stop_buying": False,
                "last_minute": int(self.clock()) // 60,
                "pending": None,
                "risk": None,
                "estimate": None,
                "last_order": None,
            }
            with self.engine.begin() as c:
                c.execute(
                    tasks.insert().values(
                        id=task["id"], active=1, payload=json.dumps(task)
                    )
                )
            self.running = True
            self.last_poll = 0
            self.evidence = {}
            self.save(
                task, "control", "用户启动自动任务。", {"config": request["config"]}
            )
            return task

    @staticmethod
    def probe(request):
        return {k: request[k] for k in ("url", "expected_symbol", "expected_quote")} | {
            "side": "sell",
            "price": "1",
            "quantity": "1",
        }

    def control(self, id, action):
        with self.live.lock:
            self.evidence = {}
            t = self.get()
            if not t or t["id"] != str(id):
                raise ValueError("当前任务已变化，请刷新。")
            if action == "force_restart":
                old_order = self.live.get()
                if old_order:
                    if old_order.get("task_id") != t["id"]:
                        raise ValueError("存在不属于当前任务的实盘委托，不能强制重开。")
                    old_order.update(
                        active=False,
                        state="abandoned_for_restart",
                        message=(
                            "用户强制重开自动任务；本地停止跟踪此委托。"
                            "平台订单未自动撤销，新任务启动前仍会检查当前委托。"
                        ),
                    )
                    self.live.save(old_order)
                self.running = False
                t.update(
                    active=False,
                    pending=None,
                    phase="restarted",
                    message=(
                        "旧任务已强制结束，参数已解锁。"
                        "请先确认平台没有旧挂单，再配置并启动新任务。"
                    ),
                )
            elif action == "retire_legacy":
                if (
                    t.get("accounting_version") == 2
                    and t.get("risk_policy_version") == RISK_POLICY_VERSION
                    and t.get("strategy_policy_version") == STRATEGY_POLICY_VERSION
                ):
                    raise ValueError("新口径任务请使用停止买入、卖完结束。")
                self.live.call("order_readiness", self.probe(t["request"]))
                old_order = self.live.get()
                if old_order:
                    old_order.update(
                        active=False,
                        state="legacy_closed",
                        message="用户结束旧版记录；未按新口径补造盈亏。",
                    )
                    self.live.save(old_order)
                self.running = False
                t.update(
                    active=False,
                    pending=None,
                    phase="completed",
                    message="旧版记录已结束；平台无挂单，旧盈亏不转换。",
                )
            elif action == "pause":
                self.running = False
                t.update(
                    phase="paused",
                    message="已暂停自动操作；现有挂单不撤销，继续只读核对。",
                )
            elif action == "resume":
                self.require_risk_policy(t)
                # tick must reconcile the persisted pending intent before any new action.
                self.running = True
                self.last_poll = 0
                t["next_buy_check_at"] = 0
                t.update(phase="running", message="正在核对原订单与持仓后恢复。")
            elif action == "finish":
                self.require_risk_policy(t)
                t.update(
                    stop_buying=True,
                    exiting=True,
                    exit_reason="用户要求停止买入、卖完结束",
                    message="停止新买入，撤单核对后处理剩余持仓。",
                )
                self.running = True
                self.last_poll = 0
            else:
                raise ValueError("不支持的任务操作。")
            self.save(t, "control")
            return t

    def resolve_unsubmitted(self, id):
        """Verify a non-confirmed intent and release its automatic task."""
        with self.live.lock:
            self.evidence = {}
            t = self.get()
            if not t or t["id"] != str(id):
                raise ValueError("当前任务已变化，请刷新。")
            if not t.get("pending"):
                raise ValueError("当前任务没有待核实订单。")
            record = self.live.get(t["pending"]["request_id"])
            if not record or record["state"] != "submission_unknown":
                raise ValueError("当前订单不是二次确认未完成状态，不能按未提交结束。")
            resolved = self.live.resolve_unsubmitted(record["id"])
            t.update(
                pending=None,
                last_order=resolved["id"],
                stop_buying=True,
                exiting=False,
            )
            self.running = False
            if D(t["inventory"]) == 0:
                t.update(
                    active=False,
                    phase="completed",
                    message="已核对平台无本次订单；旧任务已结束，可以启动新任务。",
                )
            else:
                t.update(
                    phase="paused",
                    message="已核对本次订单未提交；任务仍有持仓，请恢复后继续卖出。",
                )
            self.save(
                t,
                "control",
                t["message"],
                {"request_id": resolved["id"], "resolution": resolved["resolution"]},
            )
            return t

    def pause(self, t, message, *, log_event=True):
        repeated = (
            not self.running and t["phase"] == "attention" and t["message"] == message
        )
        self.running = False
        t.update(phase="attention", message=message)
        self.save(t, "error" if log_event and not repeated else None)
        if log_event and not repeated:
            self.notify_stopped(t, message)

    def notify_stopped(self, task, reason):
        if self.notifier:
            self.notifier.enqueue_task_stopped(dict(task), reason)

    def snapshot(self, t, c):
        r = t["request"]
        m = self.market.snapshot(r["url"], r["expected_quote"], c.window)
        validate_snapshot(m, int(self.clock() * 1000))
        if (m.chain, m.address) != token_identity(r["url"]) or (
            m.token != r["expected_symbol"] or m.quote != r["expected_quote"]
        ):
            raise ValueError("行情与任务币种不匹配。")
        return m

    def consume(self, t, record, *, log_event=True):
        """Consume a local order exactly once together with the cleared pointer."""
        result = record["result"]
        wallet = result["balances"]
        t.update(balances=wallet, inventory=wallet["base_available"])
        if record["request"]["side"] == "buy":
            paid = -D(result["cash_delta"])
            t["buy_total"] = str(D(t["buy_total"]) + paid)
            t["round_bought_quantity"] = str(
                D(t.get("round_bought_quantity", "0")) + D(result["token_delta"])
            )
            t["cost"] = str(D(t["round_start_quote"]) - D(wallet["quote_available"]))
            t["buy_end_quote"] = wallet["quote_available"]
            if paid > 0:
                t["first_buy_at"] = t["first_buy_at"] or record["created_at"]
            if paid > 0 or D(t["round_bought_quantity"]) > 0:
                t["round_stage"] = "sell"
        else:
            t["proceeds"] = str(D(wallet["quote_available"]) - D(t["buy_end_quote"]))
        t.update(last_order=record["id"], pending=None)
        token_delta = D(result["token_delta"])
        effective_cash_per_token = (
            str(abs(D(result["cash_delta"]) / token_delta))
            if token_delta != 0
            else None
        )
        self.save(
            t,
            "fill" if log_event else None,
            "当前委托已结束，按余额差记录实际资金变化。",
            {
                "request_id": record["id"],
                "result": result,
                "effective_cash_per_token": effective_cash_per_token,
                "observed_settlement_seconds": max(0, self.clock() - record["created_at"]),
            },
        )

    def settle(self, t, price, next_buy_check_at):
        if t["round_stage"] != "sell" or t["pending"]:
            return False
        residual = D(t["balances"]["base_available"]) * price
        if residual > D(2):
            return False
        pnl = D(t["balances"]["quote_available"]) - D(t["round_start_quote"])
        t.update(
            realized_pnl=str(D(t["realized_pnl"]) + pnl),
            session_loss=str(
                D(t["task_start_equity"])
                - D(t["balances"]["quote_available"])
                - residual
            ),
            last_round={
                "start_quote": t["round_start_quote"],
                "end_quote": t["balances"]["quote_available"],
                "cash_pnl": str(pnl),
                "residual_quantity": t["inventory"],
                "residual_value": str(residual),
                "buy_rehangs": t["buy_rehangs"],
            },
            cost="0",
            round_bought_quantity="0",
            proceeds="0",
            first_buy_at=None,
            exiting=False,
            rounds=t["rounds"] + 1,
            round_stage="idle",
            round_start_quote=None,
            dust=t["inventory"],
            buy_attempts=0,
            buy_rehangs=0,
            next_buy_check_at=next_buy_check_at,
        )
        self.save(
            t,
            "completed",
            "本轮结束：无挂单且剩余代币价值不超过 2 U，记录 USDT 现金差额。",
            t["last_round"],
        )
        return True

    def tick(self):
        with self.live.lock, localcontext() as context:
            context.prec = 80
            self.evidence = {}
            t = self.get()
            if not t:
                self.running = False
                return
            was_running = self.running
            try:
                self._tick(t)
            except Exception as exc:  # noqa: BLE001
                self.pause(t, str(exc), log_event=was_running)

    @staticmethod
    def require_risk_policy(t):
        if (
            t.get("accounting_version") != 2
            or t.get("risk_policy_version") != RISK_POLICY_VERSION
            or t.get("strategy_policy_version") != STRATEGY_POLICY_VERSION
            or t.get("task_start_equity") is None
        ):
            raise ValueError(
                "旧任务缺少当前止损或短周期报价策略版本；"
                "请先处理平台挂单和持仓，再核对无挂单并结束旧版记录，重新启动任务。"
            )

    def risk(self, t, wallet, price, record=None):
        if wallet.get("quote_total") is None or wallet.get("base_total") is None:
            return {
                "loss": None,
                "session_loss": None,
                "loss_pct": None,
                "equity": None,
                "basis": "public_last_price",
                "reason": "页面未显示冻结余额或剩余委托量",
            }
        equity = D(wallet["quote_total"]) + D(wallet["base_total"]) * price
        # Buy cash/token deltas include fees. An open buy contributes only its
        # observed fills; quote_total includes the still-locked unfilled cash.
        cost = D(t["cost"])
        bought = D(t["round_bought_quantity"])
        if record and record["request"]["side"] == "buy":
            before = record["before_balances"]
            cost += D(before["quote_available"]) - D(wallet["quote_total"])
            bought += D(wallet["base_total"]) - D(before["base_available"])
        if cost < 0 or bought < 0:
            raise ValueError("买入资金或代币余额变化异常，无法确认持币成本。")
        # Partial sell proceeds do not change the remaining tokens' entry price.
        # Old dust has no invented acquisition cost and is excluded from basis.
        held = min(bought, D(wallet["base_total"]))
        unit_cost = cost / bought if bought > 0 and cost > 0 else None
        basis = held * unit_cost if unit_cost is not None else D(0)
        loss = max(D(0), basis - held * price)
        return {
            "loss": str(loss),
            "loss_pct": str(loss / basis * 100) if basis > 0 else "0",
            "session_loss": str(D(t["task_start_equity"]) - equity),
            "equity": str(equity),
            "denominator": str(basis),
            "unit_cost": str(unit_cost) if unit_cost is not None else None,
            "held_quantity": str(held),
            "price": str(price),
            "basis": "public_last_price",
        }

    def _tick(self, t):
        now = self.clock()
        if t.get("accounting_version") != 2:
            raise ValueError(
                "旧任务缺少本轮起始 USDT 余额，不能套用新口径；请先处理平台挂单，再结束旧版记录。"
            )
        if self.running:
            self.require_risk_policy(t)
        config_data = t["request"]["config"]
        if t.get("strategy_policy_version") != STRATEGY_POLICY_VERSION:
            # Legacy tasks remain read-only after restart, even when their
            # persisted 5-minute/30-minute parameters no longer validate.
            config_data = {
                **config_data,
                "window": 3,
                "wait_seconds": 30,
                "max_hold_seconds": 120,
            }
        c = LiveConfig(**config_data)
        minute_due = int(now) // 60 > t["last_minute"]
        record = self.live.get(t["pending"]["request_id"]) if t["pending"] else None
        if record:
            self.count_attempt(t, record)
        polled = False
        if record and record["active"]:
            interval = (
                SETTLEMENT_POLL_SECONDS
                if record["state"] == "settling"
                else (
                    c.exit_seconds
                    if record.get("cancel_requested_at")
                    or record.get("cancel_retry_after")
                    or t["exiting"]
                    else AUTO_ORDER_POLL_SECONDS
                )
            )
            if now - self.last_poll >= interval or (self.running and minute_due):
                record = self.live.check(allow_refresh=self.running)
                polled = True
                self.last_poll = now
                # Paused reconciliation is operational state, not a strategy
                # decision. Resume restores both actions and decision logs.
                if self.running:
                    self.save(
                        t,
                        "order_check",
                        record["message"],
                        {
                            "request_id": record["id"],
                            "status": record["state"],
                            "balances": record.get("balances"),
                            "error": record.get("last_check_error"),
                        },
                    )
            if record.get("last_check_error"):
                raise ValueError("订单核对失败：" + record["last_check_error"])
        if record and not record["active"]:
            if record["state"] == "completed":
                self.consume(t, record, log_event=self.running)
                record = None
            else:
                t.update(pending=None, last_order=record["id"])
                self.pause(
                    t,
                    "上一笔未提交：" + record["message"] + "；检查后可恢复。",
                    log_event=self.running,
                )
                return
        if not self.running:
            self.save(t)
            return
        if t["pending"] and record is None:
            # A persisted plan with no live intent has not submitted; regenerate it.
            t["pending"] = None
            self.save(t)
        if record and record["state"] == "submission_unknown":
            raise ValueError("原订单提交结果未知；不能重复提交。")
        if record and record["state"] == "settling":
            t["message"] = record["message"]
            self.save(t)
            return
        if record and record.get("cancel_discovery_exhausted"):
            raise ValueError(record["cancel_error"])
        if c.current_points + D(t["buy_total"]) * c.points_per_u >= c.target_points:
            t["stop_buying"] = True
            t.setdefault("exit_reason", "预计总积分达到目标")
        if t["stop_buying"] or (
            t["first_buy_at"] is not None
            and now - t["first_buy_at"] >= c.max_hold_seconds
        ):
            t["exiting"] = True
            t.setdefault("exit_reason", "首次买入后满 2 分钟")
        if (
            not record
            and t["round_stage"] in {"idle", "buy"}
            and not t["exiting"]
            and now < t.get("next_buy_check_at", 0)
        ):
            return
        # A paused task only reconciles. Running orders need fresh balances for risk.
        if (
            record
            and not minute_due
            and not t["exiting"]
            and now - record["created_at"] < c.wait_seconds
            and now < record.get("cancel_retry_after", float("inf"))
            and not (
                polled
                and record["request"]["side"] == "buy"
                and record.get("balances")
                and D(record["balances"]["base_available"])
                > D(record["before_balances"]["base_available"])
            )
        ):
            self.save(t)
            return
        m = self.snapshot(t, c)
        price_ref = trade_price_band(m)["reference"]
        self.evidence = {
            "config": c.model_dump(mode="json"),
            "market_time": m.fetched_at,
            "valuation_basis": "public_last_price",
            "candles": [
                row.model_dump(mode="json")
                for row in sorted(
                    [row for row in m.candles if row.close_time < m.fetched_at],
                    key=lambda row: row.time,
                )[-c.window :]
            ],
        }
        t["market_at"] = m.fetched_at
        if record and polled and record.get("balances"):
            wallet = record["balances"]
            # With an open buy the newly available tokens establish first fill.
            if record["request"]["side"] == "buy" and D(wallet["base_available"]) > D(
                record["before_balances"]["base_available"]
            ):
                t["first_buy_at"] = t["first_buy_at"] or record["created_at"]
            if (
                t["first_buy_at"] is not None
                and now - t["first_buy_at"] >= c.max_hold_seconds
            ):
                t["exiting"] = True
        elif not record:
            wallet = self.live.call("order_readiness", self.probe(t["request"]))[
                "balances"
            ]
        else:
            wallet = t["balances"]
        if minute_due or not record:
            risk = self.risk(t, wallet, price_ref, record)
            t["risk"] = risk
            self.evidence.update(balances=wallet, risk=risk)
            t["last_minute"] = int(now) // 60
            if risk["session_loss"] is None:
                # Never label frozen funds as loss or churn an otherwise valid
                # order merely to obtain a valuation. Normal timeout and
                # explicit exit conditions still release the order.
                t["risk_reconcile"] = True
            else:
                t["risk_reconcile"] = False
                t["session_loss"] = risk["session_loss"]
                if D(risk["loss_pct"]) > c.stop_pct:
                    t["exiting"] = True
                    if not t["stop_buying"]:
                        t["exit_reason"] = "持币相对买入成本亏损超过 2%"
            if risk["session_loss"] is not None and D(risk["session_loss"]) > c.budget:
                t.update(
                    stop_buying=True,
                    exiting=True,
                    exit_reason=f"启动总资产减当前资产超过 {c.budget} U，撤单并卖完后结束",
                )
            self.save(t, "risk", "分别检查持币成本亏损率和任务启动以来的净资产损耗。")
        if record:
            if record.get("cancel_requested_at"):
                self.save(t)
                if record.get("cancel_error") or now >= record.get(
                    "cancel_timeout_at",
                    record["cancel_requested_at"] + CANCEL_CONFIRM_TIMEOUT_SECONDS,
                ):
                    raise ValueError("撤单尚未确认，不重复撤单或重挂；请核对平台。")
                return
            retry_cancel = bool(
                record.get("cancel_retry_after") and now >= record["cancel_retry_after"]
            )
            timeout = (
                c.exit_seconds
                if t["exiting"] and record["request"]["side"] == "sell"
                else c.wait_seconds
            )
            enough_bought = (
                record["request"]["side"] == "buy"
                and D(wallet["base_available"])
                > D(record["before_balances"]["base_available"])
            )
            sell_dust = (
                record["request"]["side"] == "sell"
                and wallet.get("base_total") is not None
                and D(wallet["base_total"]) * price_ref <= 2
            )
            if (
                now - record["created_at"] >= timeout
                or enough_bought
                or sell_dust
                or (t["exiting"] and not t.get("pending_exit"))
                or retry_cancel
            ):
                reason = "挂单超时或进入主动退出，先撤单核对余额。"
                if enough_bought:
                    reason = "已观察到部分买入成交，撤销剩余买单后转卖。"
                elif sell_dust:
                    reason = "卖出剩余价值不超过 2 U，撤销余单后结束本轮。"
                elif retry_cancel:
                    reason = "上次确认未点击撤单控件，重新识别并尝试撤单。"
                self.save(
                    t,
                    "cancel",
                    reason,
                    {"request_id": record["id"], "timeout_seconds": timeout},
                )
                canceled = self.live.cancel()
                if canceled.get("cancel_retry_after"):
                    t["message"] = canceled["message"]
                    self.save(t, "execution", canceled["message"])
                    return
                if canceled.get("cancel_error"):
                    raise ValueError(canceled["cancel_error"])
                t["message"] = reason
                self.save(
                    t,
                    "execution",
                    "已确认取消全部订单；延迟后刷新页面，再等待当前委托消失及余额稳定。",
                )
            return
        # Once all locked funds are released, use a fresh wallet for the next action.
        t.update(balances=wallet, inventory=wallet["base_available"])
        if t["stop_buying"] and t["round_stage"] == "idle":
            if D(wallet["base_available"]) * price_ref <= 2:
                t.update(
                    active=False,
                    phase="completed",
                    message=(
                        f"任务已结束：{t.get('exit_reason', '停止买入')}；"
                        "已确认无挂单，剩余代币价值不超过 2 U。"
                    ),
                )
                self.running = False
                self.save(t, "completed")
                self.notify_stopped(t, t["message"])
                return
            # Finishing before a new round still liquidates existing holdings.
            t.update(round_start_quote=wallet["quote_available"], round_stage="sell")
            t["buy_end_quote"] = wallet["quote_available"]
        if t["exiting"] and t["round_start_quote"] is not None:
            t["round_stage"] = "sell"
            t.setdefault("buy_end_quote", wallet["quote_available"])
        if self.settle(t, price_ref, now + c.buy_check_seconds):
            # Do not place the next round in the same tick as settlement.
            return
        if t["round_stage"] in {"idle", "buy"}:
            if t["buy_rehangs"] >= 10 and t["buy_attempts"] >= 11:
                raise ValueError("已连续 10 次撤单重挂仍未买入；请检查币种流动性。")
            buy_price, band = band_order_price(m, "buy")
            sell_price, _ = band_order_price(m, "sell")
            e = {
                "source": "public_ticker_lastPrice",
                "reference": str(band["reference"]),
                "last_closed": band["last_closed"],
                "width_pct": str(band["width_pct"]),
                "buy": str(buy_price),
                "sell": str(sell_price),
                "buy_allowed": True,
                "buy_blockers": [],
            }
            t["estimate"] = json.loads(json.dumps(e, default=str))
            self.evidence["estimate"] = e
            if not e["buy_allowed"]:
                t["next_buy_check_at"] = now + c.buy_check_seconds
                t["message"] = "暂不买入：" + "；".join(e["buy_blockers"])
                self.save(t, "buy_wait")
                return
            if t["round_stage"] == "idle":
                start = D(wallet["quote_available"])
                plan = min(
                    c.amount,
                    start,
                    (c.target_points - c.current_points) / c.points_per_u
                    - D(t["buy_total"]),
                )
                if start <= 0 or plan <= 0:
                    raise ValueError("没有可用计价币可开始新一轮。")
                t.update(
                    round_start_quote=str(start),
                    round_plan=str(plan),
                    round_stage="buy",
                    buy_end_quote=str(start),
                )
                t.pop("exit_reason", None)
            price = buy_price
            quote_amount = min(
                D(t["round_plan"]) - D(t["cost"]), D(wallet["quote_available"])
            )
            quantity = align(quote_amount / price, m.step)
            side = "buy"
            reason = "以公开 lastPrice 加上一根 K 线振幅为限价买入；有实际成交即转卖。"
        else:
            price, band = band_order_price(m, "sell")
            quantity = D(wallet["base_available"])
            quote_amount = None
            side = "sell"
            reason = "只填写卖价并将平台卖出数量滑杆拉满，包含已有零头。"
            if t["exiting"]:
                reason = (
                    f"{t.get('exit_reason', '主动退出')}；按最新公开 lastPrice"
                    "减上一根 K 线实际振幅向下对齐价格步长，限价卖出全部可用代币。"
                )
                self.evidence["exit_quote"] = {
                    "reference_last_price": str(band["reference"]),
                    "width_pct": str(band["width_pct"]),
                    "price": str(price),
                }
        if quantity <= 0 or quantity < m.min_qty or quantity * price < m.min_notional:
            raise ValueError("本次金额或数量低于平台最小委托，停止并检查策略。")
        validate_snapshot(m, int(self.clock() * 1000))
        body = SubmitOrder(
            **{
                k: t["request"][k]
                for k in ("url", "book", "expected_symbol", "expected_quote")
            },
            request_id=uuid4(),
            side=side,
            price=format(price, "f"),
            quantity=format(quantity, "f"),
            quote_amount=format(quote_amount, "f")
            if quote_amount is not None
            else None,
            sell_all=side == "sell",
        )
        pending = body.model_dump(mode="json")
        t.update(
            pending=pending,
            pending_exit=t["exiting"],
            next_buy_check_at=0,
            message=f"自动{side}：{body.price}",
        )
        self.save(
            t,
            side,
            reason,
            {
                "request_id": str(body.request_id),
                "price": body.price,
                "quote_amount": body.quote_amount,
                "sell_all": body.sell_all,
            },
        )
        record = self.live.submit(
            body, owner=t["id"], quote_valid_until=m.fetched_at / 1000 + 15
        )
        self.last_poll = 0
        # Persisted intent is counted exactly once, including unknown submissions.
        self.count_attempt(t, record)
        self.save(
            t,
            "execution",
            record["message"],
            {"request_id": record["id"], "status": record["state"]},
        )
        if record["state"] in {"not_submitted", "submission_unknown"}:
            if record["state"] == "not_submitted":
                t.update(pending=None, last_order=record["id"])
            self.pause(t, record["message"])

    def count_attempt(self, t, record):
        if (
            record["request"]["side"] == "buy"
            and record["state"] not in {"not_submitted", "preparing"}
            and record["id"] != t.get("counted_attempt")
        ):
            t["buy_attempts"] += 1
            t["buy_rehangs"] = max(0, t["buy_attempts"] - 1)
            t["counted_attempt"] = record["id"]
