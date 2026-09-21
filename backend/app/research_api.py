import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.browser.schemas import token_identity
from app.market import MarketClient, MarketError, Snapshot, validate_snapshot
from app.strategy import Config, Event, State, advance, estimate, exposure

router = APIRouter(prefix="/api/research", tags=["行情与策略沙盒（不执行交易）"])
market = MarketClient()


class PreviewRequest(BaseModel):
    url: str
    quote: Literal["USDT", "USDC"] = "USDT"
    config: Config

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        token_identity(value)
        return value


class SimulateRequest(BaseModel):
    state: State
    event: Event
    snapshot: Snapshot
    config: Config


@router.post("/preview")
def preview(body: PreviewRequest):
    try:
        snapshot = market.snapshot(body.url, body.quote, body.config.window)
        return JSONResponse(
            json.loads(
                json.dumps(
                    {
                        "snapshot": snapshot.model_dump(mode="json"),
                        "estimate": estimate(snapshot, body.config),
                        "simulation_only": True,
                    },
                    default=str,
                )
            )
        )
    except (MarketError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/simulate")
def simulate(body: SimulateRequest):
    try:
        validate_snapshot(body.snapshot, int(time.time() * 1000))
        state = advance(body.state, body.event, body.snapshot, body.config)
        return JSONResponse(
            json.loads(
                json.dumps(
                    {
                        "state": state.model_dump(mode="json"),
                        "risk": exposure(state, body.snapshot, body.config),
                        "simulation_only": True,
                    },
                    default=str,
                )
            )
        )
    except (MarketError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
