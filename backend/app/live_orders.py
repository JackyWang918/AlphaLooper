"""One durable pending order; submissions are never replayed on retry or restart."""

import json
import logging
import threading
import time
from decimal import Decimal, localcontext
from uuid import UUID

from pydantic import Field
from sqlalchemy import Column, Integer, String, Table, Text, select
from sqlalchemy.dialects.sqlite import insert

from app import account_ledger
from app.browser.live import matches
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


class LiveOrders:
    def __init__(self, engine, browser):
        self.engine, self.browser = engine, browser
        self.enabled = False
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=50)

    def _loop(self):
        while not self.stop.wait(60):
            try:
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

    def submit(self, body: SubmitOrder):
        with self.lock:
            payload = body.model_dump(mode="json")
            id = str(body.request_id)
            old = self.get(id)
            if old:
                if old["request"] != payload:
                    raise ValueError("请求编号已使用，不能更换订单参数重试。")
                return old
            if not self.enabled:
                raise ValueError("请先在本地控制台开启实盘单笔下单。")
            with localcontext() as context:
                context.prec = 80
                if body.side == "buy" and Decimal(body.price) * Decimal(
                    body.quantity
                ) * Decimal("1.0001") > Decimal(50):
                    raise ValueError("本阶段单笔买入预算含估算手续费不得超过 50 U。")
            record = {
                "id": id,
                "request": payload,
                "created_at": time.time(),
                "active": True,
                "state": "preparing",
                "message": "正在核对页面",
                "baseline_id": None,
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
            except Exception as exc:  # noqa: BLE001 -- preserve unresolved order on adapter failure
                record.update(active=False, state="not_submitted", message=str(exc))
                self.save(record)
                return record
            record.update(
                baseline_id=result["baseline_id"],
                state="submission_unknown",
                message="已记录提交意图，等待平台确认；不会自动重试。",
            )
            self.save(
                record
            )  # Durable BEFORE any click; a crash cannot trigger replay.
            try:
                self.call("live_submit", payload)
                record.update(state="waiting", message="已点击提交，等待平台订单结果。")
            except Exception as exc:  # noqa: BLE001 -- preserve unresolved order on adapter failure
                record["message"] = "提交结果待核实：" + str(exc)
            self.save(record)
            return record

    def check(self):
        with self.lock:
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
            try:
                result = self.call("live_inspect", record["request"])
                record["checked_at"] = time.time()
                if result["pending"]:
                    record.update(state="waiting", message="当前委托仍在，继续等待。")
                else:
                    order = result.get("order")
                    if not matches(order, record["request"], record["baseline_id"]):
                        raise ValueError(
                            "最新历史委托尚未匹配本次订单，继续保留待核实状态。"
                        )
                    if order["status"] not in {"已成交", "已取消", "已撤销", "已过期"}:
                        raise ValueError("平台订单尚未结束。")
                    self.finish(record, order)
                    return record
            except Exception as exc:  # noqa: BLE001 -- preserve unresolved order on adapter failure
                record.update(message=str(exc), checked_at=time.time())
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
            order = result.get("order")
            latest_id = order["order_id"] if order else None
            if latest_id != record["baseline_id"]:
                raise ValueError(
                    "平台历史订单已变化，请先核对实际订单结果，不能解除等待。"
                )
            record.update(
                active=False,
                state="not_submitted",
                resolved_at=time.time(),
                resolution="user_confirmed_not_submitted",
                message="用户确认未完成平台二次确认；核对无挂单且历史未变化，已结束本地等待。未重新提交。",
            )
            self.save(record)
            return record

    def finish(self, record, order):
        # Final result + accounting are committed together; never double-count on restart.
        finished = dict(
            record,
            active=False,
            state="completed",
            result=order,
            message="平台最终结果已确认，账本已更新。",
        )
        book = record["request"]["book"]
        with self.engine.connect() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            previous = c.execute(
                select(account_ledger.orders.c.payload).where(
                    account_ledger.orders.c.book == book,
                    account_ledger.orders.c.order_id == order["order_id"],
                )
            ).scalar_one_or_none()
            if previous:
                old = json.loads(previous)
                if any(
                    old[k] != order[k]
                    for k in ("symbol", "quote", "side", "quantity", "gross")
                ):
                    c.rollback()
                    raise ValueError("账本已有同编号但结果不同的订单，请人工核对。")
            statement = insert(account_ledger.orders).values(
                book=book, order_id=order["order_id"], payload=json.dumps(order)
            )
            c.execute(
                statement.on_conflict_do_nothing(index_elements=["book", "order_id"])
            )
            c.execute(
                intents.update()
                .where(intents.c.id == record["id"])
                .values(active=None, payload=json.dumps(finished))
            )
            c.commit()
        record.update(finished)
