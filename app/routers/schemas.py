from pydantic import BaseModel


class ChatRequest(BaseModel):
    prompt: str
    session_id: int | None = None  # None -> создать новую сессию
    # None -> авто-роутинг через классификатор + config.yaml (как раньше).
    # Явное значение (например "claude-sonnet") -> обходит авто-роутинг,
    # но флаги задачи (web_search, require_human_review) из routing_rules
    # всё равно применяются — см. Router.rule_for() в app/router/route.py.
    # Допустимые значения — ключи секции `models` в app/router/config.yaml.
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
    # True, если по правилам роутинга ответ требует проверки человеком
    # (например, сгенерированный договор). Раньше флаг вычислялся и терялся.
    needs_review: bool = False
