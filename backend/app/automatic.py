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

from pydantic import Field, model_validator
from sqlalchemy import Column, Integer, String, Table, Text, select

from app import decision_log
from app.browser.schemas import TradingAccount, token_identity
from app.database import Base
from app.limits import MAX_BUY_QUOTE_AMOUNT
from app.live_orders import (
    BACKGROUND_TICK_SECONDS,
    CANCEL_CONFIRM_TIMEOUT_SECONDS,
    ORDER_POLL_SECONDS,
    SETTLEMENT_POLL_SECONDS,
    SubmitOrder,
)
from app.market import MarketClient, validate_snapshot
from app.strategy import Config, align, estimate, reference_price

tasks = Table(
    "auto_tasks",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("active", Integer, nullable=True, unique=True),
    Column("payload", Text, nullable=False),
)


class LiveConfig(Config):
    buy_check_seconds: int = Field(default=20, ge=5, le=300, multiple_of=5)
    amount: D = Field(default=D(50), gt=0, le=MAX_BUY_QUOTE_AMOUNT)
    budget: D = Field(default=D(10), gt=0, le=10)
    fee_bps: D = Field(default=D(1), ge=1, le=1)
    stop_pct: D = Field(default=D(2), ge=2, le=2)
    wait_seconds: int = Field(default=300, ge=300, le=300)
    max_hold_seconds: int = Field(default=1800, ge=1800, le=1800)
    target_points: D = Field(default=D(32768), gt=0, le=32768)
    current_points: D = Field(default=D(0), ge=0, le=32768)
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
    def __init__(self, engine, live, market=None, clock=time.time):
        self.engine, self.live = engine, live
        self.market, self.clock = market or MarketClient(), clock
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
                        else ORDER_POLL_SECONDS
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
                    else "普通挂单到达五分钟后撤单核对。",
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
                        "持仓达到三十分钟后进入主动退出。",
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
        with self.live.lock:
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
            task = {
                "id": str(body.request_id),
                "request": request,
                "active": True,
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "phase": "running",
                "message": "自动任务已启动，等待最新行情。",
                "accounting_version": 2,
                "balances": balances,
                "task_start_quote": balances["quote_available"],
                "round_start_quote": None,
                "round_plan": "0",
                "round_stage": "idle",
                "buy_attempts": 0,
                "buy_rehangs": 0,
                "dust": "0",
                "inventory": "0",
                "cost": "0",
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
                if t.get("accounting_version") == 2:
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
                # tick must reconcile the persisted pending intent before any new action.
                self.running = True
                self.last_poll = 0
                t["next_buy_check_at"] = 0
                t.update(phase="running", message="正在核对原订单与持仓后恢复。")
            elif action == "finish":
                t.update(
                    stop_buying=True,
                    exiting=True,
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
            t["cost"] = str(D(t["round_start_quote"]) - D(wallet["quote_available"]))
            t["buy_end_quote"] = wallet["quote_available"]
            if paid > 0:
                t["first_buy_at"] = t["first_buy_at"] or record["created_at"]
            if D(t["cost"]) > D(t["round_plan"]) / 2:
                t["round_stage"] = "sell"
        else:
            t["proceeds"] = str(D(wallet["quote_available"]) - D(t["buy_end_quote"]))
        t.update(last_order=record["id"], pending=None)
        self.save(
            t,
            "fill" if log_event else None,
            "当前委托已结束，按余额差记录实际资金变化。",
            {"request_id": record["id"], "result": result},
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
            session_loss=str(D(t["session_loss"]) + max(D(0), -pnl)),
            last_round={
                "start_quote": t["round_start_quote"],
                "end_quote": t["balances"]["quote_available"],
                "cash_pnl": str(pnl),
                "residual_quantity": t["inventory"],
                "residual_value": str(residual),
                "buy_rehangs": t["buy_rehangs"],
            },
            cost="0",
            proceeds="0",
            first_buy_at=None,
            exiting=False,
            rounds=t["rounds"] + 1,
            round_stage="idle",
            round_start_quote=None,
            dust=t["inventory"],
            buy_attempts=0,
            buy_rehangs=0,
            risk=None,
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

    def risk(self, t, wallet, price):
        if wallet.get("quote_total") is None or wallet.get("base_total") is None:
            return {
                "loss": None,
                "loss_pct": None,
                "equity": None,
                "basis": "latest_closed_1m_candle",
                "reason": "页面未显示冻结余额或剩余委托量",
            }
        start = D(t["round_start_quote"])
        equity = D(wallet["quote_total"]) + D(wallet["base_total"]) * price
        loss = max(D(0), start - equity)
        return {
            "loss": str(loss),
            "loss_pct": str(loss / start * 100),
            "equity": str(equity),
            "denominator": str(start),
            "price": str(price),
            "basis": "latest_closed_1m_candle",
        }

    def _tick(self, t):
        now = self.clock()
        if t.get("accounting_version") != 2:
            raise ValueError(
                "旧任务缺少本轮起始 USDT 余额，不能套用新口径；请先处理平台挂单，再结束旧版记录。"
            )
        c = LiveConfig(**t["request"]["config"])
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
                    else ORDER_POLL_SECONDS
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
        if (
            c.current_points + D(t["buy_total"]) * c.points_per_u
            >= c.target_points
            or D(t["session_loss"]) >= c.budget
        ):
            t["stop_buying"] = True
        if t["stop_buying"] or (
            t["first_buy_at"] is not None
            and now - t["first_buy_at"] >= c.max_hold_seconds
        ):
            t["exiting"] = True
        if not record and t["round_stage"] == "idle":
            reserve = max(c.reserve, c.amount * c.stop_pct / 100)
            if t["stop_buying"] or D(t["session_loss"]) + reserve >= c.budget:
                t.update(
                    active=False,
                    phase="completed",
                    message="任务已结束，无待处理委托；余量按 2 U 规则保留。",
                )
                self.running = False
                self.save(t, "completed")
                return
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
        ):
            self.save(t)
            return
        m = self.snapshot(t, c)
        price_ref = reference_price(m)
        self.evidence = {
            "config": c.model_dump(mode="json"),
            "market_time": m.fetched_at,
            "valuation_basis": "latest_closed_1m_candle",
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
        else:
            wallet = t["balances"]
        if t["round_start_quote"] is not None and (
            minute_due or (not record and t.get("risk_reconcile"))
        ):
            risk = self.risk(t, wallet, price_ref)
            t["risk"] = risk
            self.evidence.update(balances=wallet, risk=risk)
            t["last_minute"] = int(now) // 60
            if risk["loss_pct"] is None:
                # Never label frozen funds as loss or churn an otherwise valid
                # order merely to obtain a valuation. Normal timeout and
                # explicit exit conditions still release the order.
                t["risk_reconcile"] = True
            else:
                t["risk_reconcile"] = False
                if D(risk["loss_pct"]) >= c.stop_pct:
                    t["exiting"] = True
            if (
                risk["loss"] is not None
                and D(t["session_loss"]) + D(risk["loss"]) >= c.budget
            ):
                t.update(stop_buying=True, exiting=True)
            self.save(t, "risk", "按本轮买入前总 USDT 检查资产损耗。")
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
                record.get("cancel_retry_after")
                and now >= record["cancel_retry_after"]
            )
            timeout = (
                c.exit_seconds
                if t["exiting"] and record["request"]["side"] == "sell"
                else c.wait_seconds
            )
            enough_bought = (
                record["request"]["side"] == "buy"
                and wallet.get("quote_total") is not None
                and D(t["round_start_quote"]) - D(wallet["quote_total"])
                > D(t["round_plan"]) / 2
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
                    reason = "累计买入超过计划金额 50%，撤销剩余买单后转卖。"
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
        wallet = self.live.call("order_readiness", self.probe(t["request"]))["balances"]
        t.update(balances=wallet, inventory=wallet["base_available"])
        if t["exiting"] and t["round_start_quote"] is not None:
            t["round_stage"] = "sell"
            t.setdefault("buy_end_quote", wallet["quote_available"])
        if self.settle(t, price_ref, now + c.buy_check_seconds):
            # Do not place the next round in the same tick as settlement.
            return
        if t["round_stage"] in {"idle", "buy"}:
            if t["buy_rehangs"] >= 10 and t["buy_attempts"] >= 11:
                raise ValueError(
                    "已完成 10 次撤单重挂，累计买入仍未超过计划金额 50%；请调整策略，或选择停止买入、卖完结束。"
                )
            e = estimate(m, c)
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
            price = e["buy"]
            quote_amount = min(
                D(t["round_plan"]) - D(t["cost"]), D(wallet["quote_available"])
            )
            quantity = align(quote_amount / price, m.step)
            side = "buy"
            reason = "按本轮剩余计划金额买入，累计投入超过 50% 后转卖。"
        else:
            price = align(price_ref, m.tick) if t["exiting"] else estimate(m, c)["sell"]
            quantity = D(wallet["base_available"])
            quote_amount = None
            side = "sell"
            reason = "只填写卖价并将平台卖出数量滑杆拉满，包含已有零头。"
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
