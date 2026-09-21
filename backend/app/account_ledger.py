"""Order-summary ledger. Fees are estimates; never reconstruct individual fills."""

import json
import re
from collections import defaultdict
from decimal import Decimal, localcontext

from sqlalchemy import Column, String, Table, Text, select
from sqlalchemy.dialects.sqlite import insert

from app.database import Base

orders = Table(
    "account_orders",
    Base.metadata,
    Column("book", String, primary_key=True),
    Column("order_id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)
HEADERS = [
    "创建时间",
    "代币",
    "类型",
    "方向",
    "成交均价",
    "委托价格",
    "已成交",
    "数量",
    "成交额",
    "反向订单",
    "条件",
    "止盈/止损",
    "状态",
]
FEE = Decimal("0.0001")


def amount(value):
    match = re.fullmatch(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s+([A-Za-z0-9]+)", value)
    if not match:
        raise ValueError("无法识别成交金额或数量单位")
    return match[1].replace(",", ""), match[2]


def extract(observation):
    """Read the first history order only; never fall through to an older order."""
    if observation.get("login_prompt_visible"):
        return [], 0
    tables = observation.get("tables", [])
    for i, table in enumerate(tables):
        headers = table["headers"] or (tables[i - 1]["headers"] if i else [])
        if [h for h in headers if h] != HEADERS or not table["rows"]:
            continue
        rows = table["rows"]
        row = rows[0]
        if len(row) == 14 and row[0] == "":
            row = row[1:]
        if len(row) != 13 or len(rows) < 2 or len(rows[1]) != 1:
            return [], 1
        match = re.match(r"^订单ID[:：]\s*(\d+)\b", rows[1][0])
        if not match:
            return [], 1
        fields = dict(zip(HEADERS, row))
        result = []
        try:
            quantity, symbol = amount(fields["已成交"])
            gross, quote = amount(fields["成交额"])
            if (
                symbol != fields["代币"]
                or quote not in {"USDT", "USDC"}
                or fields["方向"] not in {"买入", "卖出"}
                or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", fields["创建时间"]
                )
            ):
                raise ValueError("订单字段不匹配")
            if (Decimal(quantity) == 0) != (Decimal(gross) == 0):
                raise ValueError("数量与成交额不一致")
            result.append(
                {
                    "order_id": match[1],
                    "created_at": fields["创建时间"],
                    "symbol": symbol,
                    "quote": quote,
                    "side": fields["方向"],
                    "quantity": quantity,
                    "gross": gross,
                    "average_price": fields["成交均价"],
                    "status": fields["状态"],
                    "captured_at": observation["captured_at"],
                    "chain": observation["chain"],
                    "address": observation["address"],
                }
            )
        except ValueError:
            return [], 1
        return result, 0
    return [], 0


def update(engine, book, observation):
    incoming, skipped = extract(observation)
    updated, rejected = 0, 0
    with engine.connect() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            for order in incoming:
                old = c.execute(
                    select(orders.c.payload).where(
                        orders.c.book == book, orders.c.order_id == order["order_id"]
                    )
                ).scalar_one_or_none()
                if old:
                    previous = json.loads(old)
                    # Reject stale/regressive observations and accidental ID collisions.
                    if (
                        any(
                            previous[k] != order[k]
                            for k in (
                                "symbol",
                                "quote",
                                "side",
                                "created_at",
                                "chain",
                                "address",
                            )
                        )
                        or previous["captured_at"] > order["captured_at"]
                        or Decimal(previous["quantity"]) > Decimal(order["quantity"])
                        or Decimal(previous["gross"]) > Decimal(order["gross"])
                    ):
                        rejected += 1
                        continue
                statement = insert(orders).values(
                    book=book, order_id=order["order_id"], payload=json.dumps(order)
                )
                c.execute(
                    statement.on_conflict_do_update(
                        index_elements=["book", "order_id"],
                        set_={"payload": statement.excluded.payload},
                    )
                )
                updated += 1
            c.commit()
        except Exception:
            c.rollback()
            raise
    return {
        "updated": updated,
        "skipped": skipped,
        "rejected": rejected,
        **read(engine, book),
    }


def read(engine, book):
    with engine.connect() as c:
        saved = [
            json.loads(p)
            for p in c.execute(
                select(orders.c.payload).where(orders.c.book == book)
            ).scalars()
        ]
    saved.sort(key=lambda r: (r["created_at"], r["order_id"]))
    with localcontext() as context:
        context.prec = 50
        groups = defaultdict(list)
        for order in saved:
            order["estimated_fee"] = str(Decimal(order["gross"]) * FEE)
            groups[
                (order["chain"], order["address"], order["symbol"], order["quote"])
            ].append(order)
        stats = []
        for (chain, address, symbol, quote), rows in groups.items():
            buy = sell = fees = quantity = cost = pnl = Decimal(0)
            unknown = False
            # Creation-time ordering cannot resolve interleaved partial fills.
            times = [r["created_at"] for r in rows if Decimal(r["quantity"]) > 0]
            ambiguous = len(times) != len(set(times)) or any(
                r["status"] not in {"已成交", "已取消", "已撤销", "已过期"}
                for r in rows
            )
            for row in rows:
                q, gross, fee = (
                    Decimal(row["quantity"]),
                    Decimal(row["gross"]),
                    Decimal(row["estimated_fee"]),
                )
                fees += fee
                if row["side"] == "买入":
                    buy += gross
                    quantity += q
                    cost += gross + fee
                else:
                    sell += gross
                    if q > quantity:
                        unknown = True
                        quantity = cost = Decimal(0)
                    elif q:
                        allocated = cost if q == quantity else cost * q / quantity
                        pnl += gross - fee - allocated
                        quantity -= q
                        cost -= allocated
            stats.append(
                {
                    "chain": chain,
                    "address": address,
                    "symbol": symbol,
                    "quote": quote,
                    "buy_total": str(buy),
                    "sell_total": str(sell),
                    "estimated_fee": str(fees),
                    "estimated_points": str(buy * 4),
                    "estimated_realized_pnl": None
                    if unknown or ambiguous
                    else str(pnl),
                    "recorded_quantity": None if unknown else str(quantity),
                    "cost_status": "成本待补齐"
                    if unknown
                    else "成交顺序待确认"
                    if ambiguous
                    else "按订单创建顺序估算",
                }
            )
    return {"book": book, "orders": list(reversed(saved)), "stats": stats}
