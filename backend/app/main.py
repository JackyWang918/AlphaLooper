from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.database import make_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = make_engine()
    yield
    app.state.engine.dispose()


app = FastAPI(title="AlphaLooper", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
def health():
    with app.state.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected", "trading_enabled": False}
