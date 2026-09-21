"""Server-owned simulation state and atomic ledger endpoints."""

import json
import time
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import ledger
from app.browser.schemas import token_identity
from app.market import Snapshot, validate_snapshot
from app.research_api import PreviewRequest
from app.strategy import Event, State

router = APIRouter(prefix="/api/research/tasks", tags=["持久化模拟账本"])


class CreateTask(PreviewRequest):
    id: UUID
    snapshot: Snapshot


class TaskEvent(BaseModel):
    expected_version: int = Field(ge=0)
    event: Event
    snapshot: Snapshot


@contextmanager
def transaction(request):
    try:
        with request.app.state.engine.connect() as connection:
            # Serialize read/modify/write across tabs and processes, not just one worker.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    except ledger.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def response(value):
    return JSONResponse(json.loads(ledger.dump(value)))


@router.post("")
def create(body: CreateTask, request: Request):
    with transaction(request) as connection:
        task_id = str(body.id)
        existing = (
            connection.execute(select(ledger.tasks).where(ledger.tasks.c.id == task_id))
            .mappings()
            .first()
        )
        if existing:
            if (
                existing["url"] != body.url
                or existing["config"] != body.config.model_dump_json()
                or json.loads(existing["snapshot"])["quote"] != body.quote
            ):
                raise ledger.Conflict("任务编号已存在且参数不同。")
        else:
            validate_snapshot(body.snapshot, int(time.time() * 1000))
            chain, address = token_identity(body.url)
            if (chain, address, body.quote) != (
                body.snapshot.chain,
                body.snapshot.address,
                body.snapshot.quote,
            ):
                raise ValueError("行情快照与任务链接或计价币不符。")
            connection.execute(
                ledger.tasks.insert().values(
                    id=task_id,
                    created_at=int(time.time()),
                    updated_at=int(time.time()),
                    url=body.url,
                    config=body.config.model_dump_json(),
                    state=State().model_dump_json(),
                    snapshot=body.snapshot.model_dump_json(),
                    version=0,
                )
            )
        return response(ledger.detail(connection, task_id))


@router.get("")
def listing(request: Request):
    with request.app.state.engine.connect() as connection:
        rows = connection.execute(
            select(ledger.tasks).order_by(
                ledger.tasks.c.created_at.desc(), ledger.tasks.c.id
            )
        ).mappings()
        return response(
            [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "mode": "simulation",
                    "symbol": json.loads(row["snapshot"])["symbol"],
                    "completed": json.loads(row["state"])["completed"],
                }
                for row in rows
            ]
        )


@router.get("/{task_id}")
def read(task_id: UUID, request: Request):
    with transaction(request) as connection:
        return response(ledger.detail(connection, str(task_id)))


@router.post("/{task_id}/events")
def record(task_id: UUID, body: TaskEvent, request: Request):
    with transaction(request) as connection:
        return response(
            ledger.record_event(
                connection,
                str(task_id),
                body.expected_version,
                body.event,
                body.snapshot,
            )
        )
