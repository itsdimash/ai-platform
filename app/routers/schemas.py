from pydantic import BaseModel


class ChatRequest(BaseModel):
    prompt: str
    session_id: int | None = None  # None -> создать новую сессию


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
