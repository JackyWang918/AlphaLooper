import json
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from app import account_ledger, decision_log
from app.automatic import Automatic, StartTask
from app.browser.manager import BrowserBusy, BrowserManager
from app.browser.schemas import FillForm, OpenPage, ReadRecords, token_identity
from app.database import make_engine
from app.ledger_api import router as ledger_router
from app.live_orders import LiveOrders, SubmitOrder
from app.research_api import router as research_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = make_engine()
    app.state.browser = BrowserManager()
    app.state.live = LiveOrders(app.state.engine, app.state.browser)
    app.state.automatic = Automatic(app.state.engine, app.state.live)
    app.state.live.start()
    try:
        yield
    finally:
        app.state.live.close()
        app.state.browser.close()
        app.state.engine.dispose()


app = FastAPI(title="AlphaLooper", version="0.1.0", lifespan=lifespan)
app.include_router(research_router)
app.include_router(ledger_router)


@app.middleware("http")
async def local_control(request: Request, call_next):
    # A foreign web page must not be able to control the local browser via POST.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("x-alphalooper-client") != "local-ui":
            return JSONResponse({"detail": "缺少本地控制台请求标识。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://127.0.0.1:18761",
            "http://localhost:18761",
            "http://127.0.0.1:18760",
        }:
            return JSONResponse({"detail": "不允许的请求来源。"}, status_code=403)
    return await call_next(request)


def browser_action(action: str, url: str = "", payload: dict | None = None):
    try:
        with app.state.live.lock:
            if action in {"open", "fill", "read_records", "order_readiness"}:
                order = app.state.live.get()
                automatic = app.state.live.automatic
                task = automatic.get() if automatic else None
                recovery_open = False
                if action == "open" and task and not automatic.running and not order:
                    try:
                        recovery_open = token_identity(url) == token_identity(
                            task["request"]["url"]
                        )
                    except ValueError:
                        recovery_open = False
                if (order or task) and not recovery_open:
                    detail = "有实盘委托或自动任务占用流程，请先处理原任务。"
                    if action == "open" and task and not order:
                        detail = (
                            "自动任务正在运行，请先暂停，再打开该任务原来的币种页面。"
                            if automatic.running
                            else "当前任务已暂停，只能重新打开原任务的币种页面，不能切换币种。"
                        )
                    raise HTTPException(status_code=409, detail=detail)
            result = app.state.browser.execute(action, url, payload)
    except BrowserBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["message"])
    return result


@app.get("/api/browser/status")
def browser_status():
    return browser_action("status")


@app.post("/api/browser/launch")
def browser_launch():
    return browser_action("launch")


@app.post("/api/browser/open")
def browser_open(body: OpenPage):
    return browser_action("open", body.url)


@app.get("/api/health")
def health():
    with app.state.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "database": "connected",
        "trading_enabled": app.state.live.enabled,
    }


@app.post("/api/browser/fill")
def browser_fill(body: FillForm):
    return browser_action("fill", payload=body.model_dump())


@app.post("/api/browser/order-readiness")
def order_readiness_check(body: FillForm):
    return browser_action("order_readiness", payload=body.model_dump())


@app.post("/api/account/orders/read")
def account_orders_read(body: ReadRecords):
    result = browser_action("read_records", url=body.url)
    observation = result["observation"]
    return account_ledger.update(app.state.engine, body.book, observation)


@app.get("/api/account/orders")
def account_orders(book: str = "本机账户"):
    return account_ledger.read(app.state.engine, book)


class LiveSwitch(BaseModel):
    enabled: bool


@app.get("/api/live/orders")
def live_status():
    return app.state.live.status()


@app.post("/api/live/enabled")
def live_switch(body: LiveSwitch):
    with app.state.live.lock:
        if app.state.live.automatic and app.state.live.automatic.get():
            raise HTTPException(
                status_code=409, detail="请使用自动任务的暂停或卖完结束操作。"
            )
        app.state.live.enabled = body.enabled
    return {"enabled": body.enabled}


@app.post("/api/live/orders")
def live_submit(body: SubmitOrder):
    try:
        return app.state.live.submit(body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/live/check")
def live_check():
    return app.state.live.check()


@app.post("/api/live/confirmation-preview")
def live_confirmation_preview():
    with app.state.live.lock:
        record = app.state.live.get()
        if not record:
            raise HTTPException(status_code=409, detail="当前没有待确认的系统委托。")
        return browser_action("confirmation_preview", payload=record["request"])


class ResolveUnsubmitted(BaseModel):
    request_id: UUID
    confirmed_not_submitted: Literal[True]


@app.post("/api/live/resolve-unsubmitted")
def live_resolve_unsubmitted(body: ResolveUnsubmitted):
    try:
        return app.state.live.resolve_unsubmitted(body.request_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/automatic")
def automatic_status():
    return app.state.automatic.status()


@app.post("/api/automatic/start")
def automatic_start(body: StartTask):
    try:
        return app.state.automatic.create(body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class TaskControl(BaseModel):
    task_id: UUID
    action: Literal["pause", "resume", "finish"]


@app.post("/api/automatic/control")
def automatic_control(body: TaskControl):
    try:
        return app.state.automatic.control(body.task_id, body.action)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


DecisionKind = Literal[
    "control",
    "buy_wait",
    "buy",
    "sell",
    "risk",
    "cancel",
    "execution",
    "order_check",
    "fill",
    "completed",
    "error",
]


@app.get("/api/automatic/{task_id}/decisions")
def automatic_decisions(
    task_id: UUID,
    before: int | None = Query(None, ge=1),
    kind: DecisionKind | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    task = app.state.automatic.get(str(task_id))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return {
        "task_id": str(task_id),
        "config": task["request"]["config"],
        **decision_log.read(app.state.engine, str(task_id), before, kind, limit),
    }


@app.get("/api/automatic/{task_id}/decisions/export")
def automatic_decisions_export(task_id: UUID, kind: DecisionKind | None = None):
    task = app.state.automatic.get(str(task_id))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    engine = app.state.engine

    def lines():
        yield (
            json.dumps(
                {"type": "task", "task_id": str(task_id), "request": task["request"]},
                ensure_ascii=False,
            )
            + "\n"
        )
        yield from decision_log.export(engine, str(task_id), kind)

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="decisions-{task_id}.jsonl"'
        },
    )
