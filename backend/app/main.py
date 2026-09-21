from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app import account_ledger
from app.browser.manager import BrowserBusy, BrowserManager
from app.browser.schemas import FillForm, OpenPage, ReadRecords
from app.database import make_engine
from app.ledger_api import router as ledger_router
from app.live_orders import LiveOrders, SubmitOrder
from app.research_api import router as research_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = make_engine()
    app.state.browser = BrowserManager()
    app.state.live = LiveOrders(app.state.engine, app.state.browser)
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
            if action in {"open", "fill", "read_records"} and app.state.live.get():
                raise HTTPException(
                    status_code=409, detail="有一笔实盘委托待确认，请先完成巡检。"
                )
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
