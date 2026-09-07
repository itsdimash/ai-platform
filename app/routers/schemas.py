from pydantic import BaseModel


class ChatRequest(BaseModel):
    prompt: str
    session_id: int | None = None  # None -> создать новую сессию
    # None -> авто-роутинг через классификатор + config.yaml (как раньше).
    # Явное значение (например "claude-sonnet") -> обходит авто-роутинг,
    # но флаги задачи (web_search, require_human_review) из routing_rules
    # всё равно применяются — см. Router.rule_for() в app/router/route.py.
    # Допустимые значения — ключи MODEL_FACTORY в app/adapters/registry.py.
    model: str | None = None


class ChatResponse(BaseModel):
    session_id: int
    text: str
    task_type: str
    model_used: str
    confidence: float
    tokens_in: int
    tokens_out: int
    latency_ms: int
    table: list[dict] | None = None  # заполняется для db_query
