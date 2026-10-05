from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


class AttachmentOut(BaseModel):
    """Вложение сообщения. В БД хранится всё, кроме url: presigned-ссылка
    вычисляется при каждом ответе и живёт R2_PRESIGN_EXPIRES секунд — для
    свежей ссылки используйте GET /v1/files/{key}/url."""

    type: Literal["file", "image"]
    name: str
    key: str
    mime: str
    size: int
    url: str | None = None


class ChatRequest(BaseModel):
    prompt: str
    session_id: int | None = None  # None -> создать новую сессию
    # None -> авто-роутинг через классификатор + config.yaml (как раньше).
    # Явное значение (например "claude-sonnet") -> обходит авто-роутинг,
    # но флаги задачи (web_search, require_human_review) из routing_rules
    # всё равно применяются — см. Router.rule_for() в app/router/route.py.
    # Допустимые значения — ключи секции `models` в app/router/config.yaml.
    model: str | None = None
    # Ключи файлов, ранее загруженных через POST /v1/documents/extract (поле
    # file_key). Привязываются к сообщению пользователя в истории. Только
    # собственные ключи (ai/{user_id}/...), до 10 штук.
    attachment_keys: list[str] = Field(default_factory=list, max_length=10)


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
    # Файлы/изображения, созданные ассистентом в этом ответе.
    attachments: list[AttachmentOut] = Field(default_factory=list)
    # Реальный id модели картинок, если в ответе есть созданное изображение
    # (model_used — всегда модель текста).
    image_model_used: str | None = None


class SessionRename(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
