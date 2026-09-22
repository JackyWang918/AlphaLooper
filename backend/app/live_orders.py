"""One durable pending order; submissions are never replayed on retry or restart."""

import json
import logging
import threading
import time
from decimal import Decimal, localcontext
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import Column, Integer, String, Table, Text, select

from app.browser.schemas import FillForm
from app.database import Base

intents = Table(
    "live_orders",
    Base.metadata,
    Column("id", String, primary_key=True),
    Column("active", Integer, unique=True, nullable=True),
    Column("payload", Text, nullable=False),
)


class SubmitOrder(FillForm):
    request_id: UUID
    book: str = Field(
        default="本机账户", min_length=1, max_length=80, pattern=r".*\S.*"
    )

    @model_validator(mode="after")
    def platform_sell_percentage(self):
        if self.side == "sell":
            self.sell_all = True
        return self


class LiveOrders:
    def __init__(self, engine, browser):
        self.engine, self.browser = engine, browser
        self.enabled = False
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.automatic = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=50)

    def _loop(self):
        last_check = 0
        while not self.stop.wait(5):
            try:
                if self.automatic and self.automatic.get():
                    self.automatic.tick()
                elif time.time() - last_check >= (
                    5 if self.get() and self.get()["state"] == "settling" else 60
                ):
                    last_check = time.time()
                    self.check()
            except Exception:
                logging.getLogger(__name__).exception("Order monitor failed")

    def get(self, id=None):
        with self.engine.connect() as c:
            query = select(intents.c.payload)
            query = (
                query.where(intents.c.id == id)
                if id
                else query.where(intents.c.active == 1)
            )
            value = c.execute(query).scalar_one_or_none()
        return json.loads(value) if value else None

    def status(self):
        with self.lock, self.engine.connect() as c:
            records = [
                json.loads(p) for p in c.execute(select(intents.c.payload)).scalars()
            ]
            records.sort(key=lambda r: r["created_at"], reverse=True)
            return {
                "enabled": self.enabled,
                "current": next((r for r in records if r["active"]), None),
                "recent": records[:20],
            }

    def save(self, record):
        with self.engine.begin() as c:
            c.execute(
                intents.update()
                .where(intents.c.id == record["id"])
                .values(
                    active=1 if record["active"] else None, payload=json.dumps(record)
                )
            )

    def call(self, action, payload):
        result = self.browser.execute(action, payload=payload)
        if not result.get("ok"):
            raise ValueError(result.get("message", "浏览器执行失败"))
        return result

    def submit(self, body: SubmitOrder, *, owner=None, quote_valid_until=None):
        with self.lock:
            task = self.automatic.get() if self.automatic else None
            if task and owner != task["id"]:
                raise ValueError("自动任务占用交易流程，请在自动任务面板操作。")
            payload = body.model_dump(mode="json")
            id = str(body.request_id)
            old = self.get(id)
            if old:
                if old["request"] != payload:
                    raise ValueError("请求编号已使用，不能更换订单参数重试。")
                return old
            if not self.enabled and owner is None:
                raise ValueError("请先在本地控制台开启实盘单笔下单。")
            with localcontext() as context:
                context.prec = 80
                total = (
                    Decimal(body.quote_amount)
                    if body.quote_amount
                    else Decimal(body.price) * Decimal(body.quantity)
                )
                if body.side == "buy" and total > Decimal(50):
                    raise ValueError("单笔计划买入金额不得超过 50 U。")
            record = {
                "id": id,
                "request": payload,
                "created_at": time.time(),
                "active": True,
                "state": "preparing",
                "message": "正在核对页面",
                "accounting_version": 2,
                "quote_amount": str(total) if body.side == "buy" else None,
                "task_id": owner,
            }
            with self.engine.connect() as c:
                c.exec_driver_sql("BEGIN IMMEDIATE")
                if c.execute(select(intents.c.id).where(intents.c.active == 1)).first():
                    c.rollback()
                    raise ValueError("上一笔委托尚未确认结束，不能提交下一笔。")
                c.execute(
                    intents.insert().values(id=id, active=1, payload=json.dumps(record))
                )
                c.commit()
            try:
                result = self.call("live_prepare", payload)
                if quote_valid_until is not None and time.time() > quote_valid_until:
                    raise ValueError(
                        "提交准备期间行情已过期，未点击下单，请恢复后重新估价。"
                    )
            except Exception as exc:  # noqa: BLE001 -- preserve unresolved order on adapter failure
                record.update(active=False, state="not_submitted", message=str(exc))
                self.save(record)
                return record
            record.update(
                before_balances=result["balances"],
                state="submission_unknown",
                message="已记录提交意图，等待平台确认；不会自动重试。",
            )
            self.save(
                record
            )  # Durable BEFORE any click; a crash cannot trigger replay.
            try:
                result = self.call(
                    "live_submit",
                    payload
                    | (
                        {"quote_valid_until": quote_valid_until}
                        if quote_valid_until is not None
                        else {}
                    ),
                )
                if result.get("confirmation_clicked") is not True:
                    raise ValueError("执行器未确认已完成订单确认步骤，保留待核实状态。")
                record.update(
                    state="waiting",
                    confirmation_clicked=True,
                    confirmation=result.get("confirmation"),
                    message="已核对订单确认单并点击继续，等待平台订单结果。",
                )
            except Exception as exc:  # noqa: BLE001 -- preserve unresolved order on adapter failure
                record["submission_error"] = str(exc)
                record["message"] = "提交结果待核实：" + record["submission_error"]
            self.save(record)
            return record

    def check(self):
        with self.lock, localcontext() as context:
            context.prec = 80
            record = self.get()
            if not record:
                return None
            if record["state"] == "preparing":
                # Restart happened before the durable submit marker, so no click occurred.
                record.update(
                    active=False,
                    state="not_submitted",
                    message="准备阶段中断，未提交。",
                )
                self.save(record)
                return record
            if time.time() < record.get("cancel_check_after", 0):
                record["message"] = "已确认取消全部订单，等待平台状态和余额更新。"
                self.save(record)
                return record
            try:
                refresh_before_check = bool(record.get("cancel_refresh_pending"))
                result = self.call(
                    "live_progress" if record.get("task_id") else "live_inspect",
                    record["request"]
                    | ({"refresh_before_check": True} if refresh_before_check else {}),
                )
                if refresh_before_check and result.get("page_refreshed"):
                    record["cancel_refresh_pending"] = False
                    record["cancel_page_refreshed_at"] = time.time()
                record["checked_at"] = time.time()
                record.pop("last_check_error", None)
                if result.get("settling"):
                    record.pop("settlement_candidate", None)
                    record.update(
                        state="settling",
                        message="读取期间委托或冻结余额变化，等待余额稳定。",
                    )
                elif result["pending"]:
                    record.pop("settlement_candidate", None)
                    record.update(
                        state="waiting",
                        observed_pending=True,
                        balances=result["balances"],
                        current_order=result.get("current_order"),
                        message="当前委托仍在，按余额跟踪。",
                    )
                else:
                    if record.get("accounting_version") != 2:
                        raise ValueError(
                            "旧订单没有买入前余额基准，不能转换为新口径。请核对后结束旧任务。"
                        )
                    balances = result["balances"]
                    if not record.get("observed_pending") and balances == record.get(
                        "before_balances"
                    ):
                        raise ValueError(
                            "无挂单且余额未变化，提交结果仍未知；不会自动重发。"
                        )
                    previous = record.get("settlement_candidate")
                    if not previous or previous["balances"] != balances:
                        record["settlement_candidate"] = {
                            "balances": balances,
                            "at": time.time(),
                        }
                        record.update(
                            state="settling", message="当前无委托，等待下一次余额核对。"
                        )
                    elif time.time() - previous["at"] >= 2:
                        self.finish(record, balances)
                        return record
            except Exception as exc:  # noqa: BLE001 -- preserve unresolved order on adapter failure
                record.pop("settlement_candidate", None)
                record.update(last_check_error=str(exc), checked_at=time.time())
                if (
                    record.get("submission_error")
                    and record["state"] == "submission_unknown"
                ):
                    record["message"] = "提交结果待核实：" + record["submission_error"]
                else:
                    record["message"] = str(exc)
            self.save(record)
            return record

    def resolve_unsubmitted(self, request_id):
        with self.lock:
            record = self.get(str(request_id))
            if not record or not record["active"]:
                raise ValueError("该委托已结束或不存在，请刷新状态。")
            result = self.call("live_unsubmitted", record["request"])
            if result["pending"]:
                raise ValueError("平台仍有挂单，不能标记为未提交。")
            if result.get("settling") or result.get("balances") != record.get(
                "before_balances"
            ):
                raise ValueError("余额尚未稳定或已变化，不能标记为未提交。")
            record.update(
                active=False,
                state="not_submitted",
                resolved_at=time.time(),
                resolution="user_confirmed_not_submitted",
                message="用户确认未完成平台二次确认；核对无挂单且余额未变化，已结束本地等待。未重新提交。",
            )
            self.save(record)
            return record

    def finish(self, record, balances):
        before = record["before_balances"]
        cash_delta = Decimal(balances["quote_available"]) - Decimal(
            before["quote_available"]
        )
        token_delta = Decimal(balances["base_available"]) - Decimal(
            before["base_available"]
        )
        side = record["request"]["side"]
        if (side == "buy" and (cash_delta > 0 or token_delta < 0)) or (
            side == "sell" and (cash_delta < 0 or token_delta > 0)
        ):
            raise ValueError("余额变化与订单方向不符，等待核对。")
        result = {
            "balances": balances,
            "before_balances": before,
            "cash_delta": str(cash_delta),
            "token_delta": str(token_delta),
            "gross": str(abs(cash_delta)),
            "quantity": str(abs(token_delta)),
            "status": "余额已核对",
            "local_order_id": record["id"],
        }
        record.update(
            active=False,
            state="completed",
            result=result,
            message="当前无委托且余额稳定，已记录实际资金变化。",
        )
        self.save(record)

    def cancel(self):
        with self.lock:
            record = self.get()
            if not record or not record.get("task_id"):
                raise ValueError("没有自动任务可撤销的委托。")
            if record.get("cancel_requested_at"):
                return record
            if record["state"] != "waiting":
                raise ValueError("提交结果未明确，不能自动撤单。")
            record.update(cancel_requested_at=time.time(), cancel_state="unknown")
            self.save(record)  # Before click; never replay after timeout/restart.
            try:
                result = self.call("live_cancel", record["request"])
                if result.get("confirmation_clicked") is not True:
                    raise ValueError("执行器未确认已点击取消全部订单弹窗的确认按钮。")
                confirmed_at = time.time()
                record.update(
                    cancel_state="confirmed",
                    cancel_result=result,
                    cancel_confirmed_at=confirmed_at,
                    cancel_check_after=confirmed_at + 10,
                    cancel_timeout_at=confirmed_at + 120,
                    cancel_refresh_pending=True,
                    message="已确认取消全部订单；等待后刷新页面并核对委托与余额。",
                )
            except Exception as exc:  # noqa: BLE001
                record.update(
                    cancel_error=str(exc), message="撤单结果待核实：" + str(exc)
                )
            self.save(record)
            return record
