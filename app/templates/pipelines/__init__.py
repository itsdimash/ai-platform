"""Пайплайны шаблонов. Пайплайн — модуль app/templates/pipelines/<name>.py с функцией
`async def run(ctx: PipelineContext) -> PipelineResult`. Тесты могут зарегистрировать
пайплайн вручную через register_pipeline."""

import importlib
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.templates.context import PipelineContext, PipelineResult

PipelineFn = Callable[["PipelineContext"], Awaitable["PipelineResult"]]

_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_REGISTERED: dict[str, PipelineFn] = {}


def register_pipeline(name: str, fn: PipelineFn) -> None:
    _REGISTERED[name] = fn


def unregister_pipeline(name: str) -> None:
    _REGISTERED.pop(name, None)


def resolve_pipeline(name: str) -> PipelineFn:
    """Зарегистрированный пайплайн или функция run из app.templates.pipelines.<name>.
    Бросает LookupError, если такого нет."""
    if name in _REGISTERED:
        return _REGISTERED[name]
    if not _NAME.match(name):
        raise LookupError(f"недопустимое имя пайплайна {name!r}")
    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise
        raise LookupError(
            f"пайплайн {name!r} не найден (app/templates/pipelines/{name}.py)"
        ) from exc
    run = getattr(module, "run", None)
    if run is None:
        raise LookupError(f"в пайплайне {name!r} нет функции run(ctx)")
    return run
