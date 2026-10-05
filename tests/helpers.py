"""Общие подставные объекты для тестов сервиса чата."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters.base import FINISH_STOP, GenerationResult, ModelAdapter, ModelCaps
from app.models.chat import ChatSession
from app.router.route import load_config


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDB:
    """AsyncSession-заглушка: add/flush/commit/rollback/execute/get."""

    def __init__(self, history_rows=None, existing_session=None):
        self.added: list = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.history_rows = history_rows or []  # от старых к новым
        self._existing = existing_session

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, ChatSession) and obj.id is None:
                obj.id = 99

    async def execute(self, stmt):
        # fetch_recent_rows читает desc и разворачивает — отдаём в обратном порядке
        return FakeResult(list(reversed(self.history_rows)))

    async def get(self, model, pk):
        return self._existing

    def of(self, cls):
        return [o for o in self.added if isinstance(o, cls)]


def result(
    tool=None,
    args=None,
    text="",
    finish=FINISH_STOP,
    model_used=None,
    tokens_in=10,
    tokens_out=5,
    detail=None,
) -> GenerationResult:
    calls = [{"name": tool, "args": args or {}}] if tool else []
    return GenerationResult(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=100,
        tool_calls=calls,
        model_used=model_used,
        finish=finish,
        finish_detail=detail,
    )


class FakeAdapter(ModelAdapter):
    """Адаптер со сценарием ответов; запоминает аргументы каждого вызова."""

    def __init__(self, logical_name: str, script=None):
        spec = load_config()["models"][logical_name]
        self.name = logical_name
        self.model = spec["id"]
        self.caps = ModelCaps.from_dict(spec["caps"])
        self.script = list(script or [])
        self.calls: list[dict] = []

    async def generate(self, prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.script:
            return result(text="ответ")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_adapters(**scripts) -> dict[str, FakeAdapter]:
    """По адаптеру на каждое логическое имя из config.yaml; scripts — {имя: [ответы]}
    (дефис в имени заменяется на _ в kwargs)."""
    return {
        name: FakeAdapter(name, scripts.get(name.replace("-", "_")))
        for name in load_config()["models"]
    }


def db_row(role, content, attachments=None):
    return SimpleNamespace(role=role, content=content, attachments=attachments or [])
