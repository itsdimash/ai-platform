from datetime import datetime

from sqlalchemy import Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AIRequestLog(Base):
    """Лог каждого запроса к платформе — что решил классификатор, какая
    модель ответила, сколько это стоило. С первого дня, не 'потом'."""

    __tablename__ = "ai_request_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    task_type: Mapped[str] = mapped_column(String(50))
    confidence: Mapped[float] = mapped_column(Float)
    used_fallback_confidence: Mapped[bool] = mapped_column(default=False)

    model_used: Mapped[str] = mapped_column(String(50))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(20), default="success")  # success | error
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
