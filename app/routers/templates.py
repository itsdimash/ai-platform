"""Шаблоны-промпты как фоновые запуски (runs).

GET  /v1/templates                       — шаблоны, доступные роли из JWT
POST /v1/templates/{id}/runs  -> 202     — создать запуск (быстро; работа идёт в фоне)
GET  /v1/runs/{run_id}                   — статус / результат / ошибка
POST /v1/runs/{run_id}/cancel            — отмена

Ошибки: {"detail": {"code", "message", "field"?}}.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import CurrentUser, get_current_user
from ..templates.errors import input_invalid
from ..templates.runner import RunManager, get_run_manager
from ..templates.store import RunRecord

router = APIRouter()


class RunCreate(BaseModel):
    # id сессии чата (в ai-platform это целое число); None -> новая сессия
    session_id: int | str | None = None
    files: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def run_to_api(record: RunRecord) -> dict:
    return {
        "run_id": record.id,
        "template_id": record.template_id,
        "session_id": record.session_id,
        "status": record.status,
        "progress": record.progress,
        "result": record.result if record.status == "done" else None,
        "error": record.error if record.status == "failed" else None,
        "created_at": _iso(record.created_at),
        "started_at": _iso(record.started_at),
        "finished_at": _iso(record.finished_at),
    }


def _parse_session_id(value: int | str | None) -> int | None:
    if value is None or (isinstance(value, str) and value.strip() in ("", "null")):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise input_invalid("session_id должен быть целым числом", "session_id") from exc


@router.get("/v1/templates")
async def list_templates(
    user: CurrentUser = Depends(get_current_user),
    manager: RunManager = Depends(get_run_manager),
) -> list[dict]:
    return [t.public() for t in manager.registry.for_role(user.role)]


@router.post("/v1/templates/{template_id}/runs", status_code=202)
async def create_run(
    template_id: str,
    body: RunCreate,
    user: CurrentUser = Depends(get_current_user),
    manager: RunManager = Depends(get_run_manager),
) -> dict:
    record = await manager.start_run(
        user,
        template_id,
        files=body.files,
        params=body.params,
        session_id=_parse_session_id(body.session_id),
    )
    return {"run_id": record.id, "session_id": record.session_id, "status": record.status}


@router.get("/v1/runs/{run_id}")
async def get_run(
    run_id: str,
    user: CurrentUser = Depends(get_current_user),
    manager: RunManager = Depends(get_run_manager),
) -> dict:
    return run_to_api(await manager.get_run(user, run_id))


@router.post("/v1/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    user: CurrentUser = Depends(get_current_user),
    manager: RunManager = Depends(get_run_manager),
) -> dict:
    return run_to_api(await manager.cancel_run(user, run_id))
