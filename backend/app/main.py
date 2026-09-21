from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.browser.manager import BrowserBusy, BrowserManager
from app.browser.schemas import FillForm, OpenPage
from app.database import make_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = make_engine()
    app.state.browser = BrowserManager()
    try:
        yield
    finally:
        app.state.browser.close()
        app.state.engine.dispose()


app = FastAPI(title="AlphaLooper", version="0.1.0", lifespan=lifespan)


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
    return {"status": "ok", "database": "connected", "trading_enabled": False}


@app.post("/api/browser/fill")
def browser_fill(body: FillForm):
    return browser_action("fill", payload=body.model_dump())
