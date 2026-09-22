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

from pydantic import Field
from sqlalchemy import Column, Integer, String, Table, Text, select

from app import decision_log
from app.browser.schemas import ReadRecords, token_identity
from app.database import Base
from app.live_orders import SubmitOrder
from app.market import MarketClient, validate_snapshot
from app.strategy import Config, State, align, estimate, exposure, reference_price

tasks = Table(
    "auto_tasks",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("active", Integer, nullable=True, unique=True),
    Column("payload", Text, nullable=False),
)


class LiveConfig(Config):
    buy_check_seconds: int = Field(default=5, ge=5, le=300, multiple_of=5)
    amount: D = Field(default=D(50), gt=0, le=50)
    budget: D = Field(default=D(10), gt=0, le=10)
    fee_bps: D = Field(default=D(1), ge=1, le=1)
    stop_pct: D = Field(default=D(2), ge=2, le=2)
    wait_seconds: int = Field(default=300, ge=300, le=300)
    max_hold_seconds: int = Field(default=1800, ge=1800, le=1800)
    target_points: D = Field(default=D(32768), gt=0, le=32768)
    points_per_u: D = Field(default=D(4), ge=4, le=4)


class StartTask(ReadRecords):
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
                                "fees",
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
            if current and current.get("pending"):
                record = self.live.get(current["pending"]["request_id"])
                if record:
                    current = dict(current)
                    current["pending_order"] = {
                        "state": record["state"],
                        "message": record["message"],
                        "submission_error": record.get("submission_error"),
                    }
            return {"running": self.running, "current": current, "recent": records[:10]}

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
            self.live.call("order_readiness", payload)
            balance = self.live.call("live_balance", payload)["available"]
            task = {
                "id": str(body.request_id),
                "request": request,
                "active": True,
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "phase": "running",
                "message": "自动任务已启动，等待最新行情。",
                "baseline_balance": balance,
                "inventory": "0",
                "cost": "0",
                "proceeds": "0",
                "buy_total": "0",
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
            if action == "pause":
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

    def pause(self, t, message):
        repeated = (
            not self.running and t["phase"] == "attention" and t["message"] == message
        )
        self.running = False
        t.update(phase="attention", message=message)
        self.save(t, None if repeated else "error")

    def snapshot(self, t, c):
        r = t["request"]
        m = self.market.snapshot(r["url"], r["expected_quote"], c.window)
        validate_snapshot(m, int(self.clock() * 1000))
        if (m.chain, m.address) != token_identity(r["url"]) or (
            m.token != r["expected_symbol"] or m.quote != r["expected_quote"]
        ):
            raise ValueError("行情与任务币种不匹配。")
        return m

    def position(self, t, record=None):
        """Include open-order partial fills in risk without booking them twice."""
        inventory, cost, proceeds = map(D, (t["inventory"], t["cost"], t["proceeds"]))
        if record and record.get("progress"):
            q, gross = map(
                D, (record["progress"]["quantity"], record["progress"]["gross"])
            )
            fee = D("0.0001")
            if record["request"]["side"] == "buy":
                base_fee = (
                    record.get("confirmation", {}).get("fee_currency")
                    == t["request"]["expected_symbol"]
                )
                inventory += q * (1 - fee) if base_fee else q
                cost += gross if base_fee else gross * (1 + fee)
            else:
                inventory -= q
                proceeds += gross * (1 - fee)
            if q and t["first_buy_at"] is None and record["request"]["side"] == "buy":
                # Order summaries do not provide the first individual fill timestamp.
                # Use submission time as a conservative lower bound; never restart it.
                t["first_buy_at"] = record["created_at"]
        if inventory < 0:
            raise ValueError("实际卖出数量超过任务持仓，停止并核对。")
        return State(inventory=inventory, cost=cost, proceeds=proceeds)

    def consume(self, t, record):
        """Task accounting and clearing its pending pointer commit together."""
        result = record["result"]
        q, gross = D(result["quantity"]), D(result["gross"])
        fee = gross * D("0.0001")
        if record["request"]["side"] == "buy":
            if q:
                t["first_buy_at"] = t["first_buy_at"] or record["created_at"]
            base_fee = (
                record.get("confirmation", {}).get("fee_currency")
                == t["request"]["expected_symbol"]
            )
            t["inventory"] = str(
                D(t["inventory"]) + (q * D("0.9999") if base_fee else q)
            )
            t["cost"] = str(D(t["cost"]) + gross + (D(0) if base_fee else fee))
            t["buy_total"] = str(D(t["buy_total"]) + gross)
        else:
            if q > D(t["inventory"]):
                raise ValueError("最终卖出量超过任务持仓。")
            t["inventory"] = str(D(t["inventory"]) - q)
            t["proceeds"] = str(D(t["proceeds"]) + gross - fee)
        t["fees"] = str(D(t["fees"]) + fee)
        t.update(last_order=record["id"], pending=None)
        self.save(
            t,
            "fill",
            "已核对订单最终结果并计入任务。",
            {"request_id": record["id"], "result": result},
        )

    def settle(self, t):
        if D(t["inventory"]) != 0 or not D(t["cost"]):
            return
        pnl = D(t["proceeds"]) - D(t["cost"])
        t.update(
            realized_pnl=str(D(t["realized_pnl"]) + pnl),
            session_loss=str(D(t["session_loss"]) + max(D(0), -pnl)),
            cost="0",
            proceeds="0",
            first_buy_at=None,
            exiting=False,
            rounds=t["rounds"] + 1,
        )

    def tick(self):
        with self.live.lock, localcontext() as context:
            context.prec = 80
            self.evidence = {}
            t = self.get()
            if not t:
                self.running = False
                return
            try:
                self._tick(t)
            except Exception as exc:  # noqa: BLE001 -- persisted actionable pause, no retry clicks
                self.pause(t, str(exc))

    def _tick(self, t):
        now = self.clock()
        c = LiveConfig(**t["request"]["config"])
        record = self.live.get(t["pending"]["request_id"]) if t["pending"] else None
        if record and record["active"]:
            interval = c.exit_seconds if record.get("cancel_requested_at") else 60
            if now - self.last_poll >= interval or (
                self.running and int(now) // 60 > t["last_minute"]
            ):
                record = self.live.check()
                self.last_poll = now
                self.save(
                    t,
                    "order_check",
                    record["message"],
                    {
                        "request_id": record["id"],
                        "status": record["state"],
                        "progress": record.get("progress"),
                        "error": record.get("last_check_error"),
                    },
                )
            if record.get("last_check_error"):
                raise ValueError("订单核对失败：" + record["last_check_error"])
        if record and not record["active"]:
            if record["state"] == "completed":
                self.consume(t, record)
                record = None
            else:
                t.update(pending=None, last_order=record["id"])
                self.pause(t, "上一笔未提交：" + record["message"] + "；检查后可恢复。")
                return
        self.settle(t)
        if not self.running:
            self.save(t)
            return
        if t["pending"] and record is None:
            # Persisted plan without a live intent: after restart discard its stale
            # quote and regenerate only after the explicit resume above.
            t["pending"] = None
            self.save(t)
        if record and record["state"] == "submission_unknown":
            raise ValueError("原订单提交结果未知；请核对平台，不能开始下一笔。")
        held = self.position(t, record)
        target = D(t["buy_total"]) + (
            D(record["progress"]["gross"])
            if record and record.get("progress") and record["request"]["side"] == "buy"
            else D(0)
        )
        if (
            target * c.points_per_u >= c.target_points
            or D(t["session_loss"]) >= c.budget
        ):
            t["stop_buying"] = True
        if t["stop_buying"] or (
            t["first_buy_at"] is not None
            and now - t["first_buy_at"] >= c.max_hold_seconds
        ):
            t["exiting"] = True
        minute_due = int(now) // 60 > t["last_minute"]
        if not record and not held.inventory:
            reserve = max(c.reserve, c.amount * (c.stop_pct / 100 + D("0.0002")))
            if t["stop_buying"] or D(t["session_loss"]) + reserve >= c.budget:
                t.update(
                    active=False,
                    phase="completed",
                    message="任务已结束，无待处理委托或任务持仓。",
                )
                self.running = False
                self.save(
                    t,
                    "completed",
                    "停止新买入：已要求收尾、已达标或损耗预算预留不足。",
                    {"reserve": str(reserve), "budget": str(c.budget)},
                )
                return
        # Fetch when an action is possible or on the minute risk boundary.
        if not record and not held.inventory and now < t.get("next_buy_check_at", 0):
            self.save(t)
            return
        m = self.snapshot(t, c) if not record or minute_due else None
        if m:
            risk = exposure(held, m, c)
            self.evidence = {
                "config": c.model_dump(mode="json"),
                "market_time": m.fetched_at,
                "symbol": m.symbol,
                "tick": m.tick,
                "step": m.step,
                "min_qty": m.min_qty,
                "min_notional": m.min_notional,
                "valuation_basis": "latest_closed_1m_candle",
                "candles": [
                    row.model_dump(mode="json")
                    for row in sorted(
                        [row for row in m.candles if row.close_time < m.fetched_at],
                        key=lambda row: row.time,
                    )[-c.window :]
                ],
                "position_including_partial": held.model_dump(mode="json"),
                "risk": risk,
                "checks": {
                    "minute_check_due": minute_due,
                    "loss_threshold_pct": c.stop_pct,
                    "loss_threshold_hit": risk["loss_pct"] is not None
                    and risk["loss_pct"] >= c.stop_pct,
                    "budget": c.budget,
                    "budget_hit": D(t["session_loss"]) + (risk["loss"] or D(0))
                    >= c.budget,
                    "points": target * c.points_per_u,
                    "target_points": c.target_points,
                    "target_hit": target * c.points_per_u >= c.target_points,
                    "held_seconds": None
                    if t["first_buy_at"] is None
                    else now - t["first_buy_at"],
                    "hold_limit_seconds": c.max_hold_seconds,
                },
            }
            t["risk"] = json.loads(json.dumps(risk, default=str))
            t["market_at"] = m.fetched_at
            if minute_due:
                t["last_minute"] = int(now) // 60
                if held.inventory and (risk["loss_pct"] >= c.stop_pct):
                    t["exiting"] = True
                if D(t["session_loss"]) + (risk["loss"] or D(0)) >= c.budget:
                    t.update(stop_buying=True, exiting=True)
            if held.inventory:
                self.save(
                    t,
                    "risk",
                    "持仓风险评估："
                    + (
                        "已进入主动退出。"
                        if t["exiting"]
                        else "继续按普通卖出规则处理。"
                    ),
                )
        if record:
            if record.get("cancel_requested_at"):
                self.save(t)
                if (
                    record.get("cancel_error")
                    or now - record["cancel_requested_at"] >= 60
                ):
                    raise ValueError(
                        "撤单尚未确认，不重放撤单或重挂；请核对平台后恢复。"
                    )
                return
            timeout = (
                c.exit_seconds
                if t["exiting"] and record["request"]["side"] == "sell"
                else c.wait_seconds
            )
            if now - record["created_at"] >= timeout or (
                t["exiting"] and not t.get("pending_exit")
            ):
                self.save(
                    t,
                    "cancel",
                    "决定撤单：挂单超时或进入主动退出；先核对最终成交再重挂。",
                    {
                        "request_id": record["id"],
                        "elapsed_seconds": now - record["created_at"],
                        "timeout_seconds": timeout,
                        "entering_exit": t["exiting"] and not t.get("pending_exit"),
                    },
                )
                canceled = self.live.cancel()
                t["message"] = "已请求撤单，等待最终成交结果，未重挂。"
                if canceled.get("cancel_error"):
                    raise ValueError(canceled["cancel_error"])
                self.save(
                    t,
                    "execution",
                    "撤单动作返回，等待平台最终结果。",
                    {
                        "request_id": record["id"],
                        "cancel_state": canceled.get("cancel_state"),
                    },
                )
            else:
                t["message"] = "等待当前真实委托成交；每分钟核对。"
            self.save(t)
            return
        if not held.inventory:
            e = estimate(m, c)
            t["estimate"] = json.loads(json.dumps(e, default=str))
            self.evidence["estimate"] = e
            if not e["buy_allowed"]:
                t["next_buy_check_at"] = self.clock() + c.buy_check_seconds
                t["message"] = (
                    "暂不买入："
                    + "；".join(e["buy_blockers"])
                    + f" 约 {c.buy_check_seconds} 秒后重新估价。"
                )
                self.save(t, "buy_wait")
                return
            remaining = c.target_points / c.points_per_u - D(t["buy_total"])
            price = e["buy"]
            quantity = min(
                e["quantity"],
                max(
                    align(remaining / price, m.step, True),
                    align(m.min_qty, m.step, True),
                    align(m.min_notional / price, m.step, True),
                ),
            )
            side = "buy"
            decision_reason = "采用 K 线模型买价；按预算、目标和步长计算数量。"
        else:
            if t["exiting"]:
                price = align(reference_price(m), m.tick)
                decision_reason = "主动退出：采用最新已收盘一分钟 K 线收盘价。"
            else:
                e = estimate(m, c)
                self.evidence["estimate"] = e
                price = e["sell"]
                decision_reason = "普通卖出：采用 K 线模型卖价。"
            available = D(
                self.live.call("live_balance", self.probe(t["request"]))["available"]
            )
            owned = available - D(t["baseline_balance"])
            self.evidence["balance"] = {
                "available": available,
                "baseline": t["baseline_balance"],
                "task_available": owned,
            }
            # Only small fee/precision differences may adjust the task's inventory.
            # Never consume tokens that existed before this task.
            tolerance = max(m.step, held.inventory * D("0.0002"))
            if owned < 0 or abs(owned - held.inventory) > tolerance:
                raise ValueError(
                    "可用余额与任务持仓不符，请核对代币费用、账户或人工交易。"
                )
            t["inventory"] = str(owned)
            quantity = align(owned, m.step)
            price = align(price, m.tick, not t["exiting"])
            side = "sell"
        if quantity <= 0 or quantity < m.min_qty or price * quantity < m.min_notional:
            raise ValueError("剩余数量不足最小委托量/金额，需人工处理；未标记清仓。")
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
        )
        pending = body.model_dump(mode="json")
        if side == "buy":
            pending["quote_amount"] = format(price * quantity, "f")
        t.update(
            pending=pending,
            pending_exit=t["exiting"],
            message=f"自动{side}：{body.quantity} @ {body.price}",
        )
        self.save(
            t,
            "buy" if side == "buy" else "sell",
            decision_reason,
            {
                "price": body.price,
                "quantity": body.quantity,
                "quote_amount": pending.get("quote_amount"),
                "request_id": str(body.request_id),
            },
        )
        record = self.live.submit(
            body,
            owner=t["id"],
            quote_valid_until=m.fetched_at / 1000 + 15,
        )
        self.last_poll = 0
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
