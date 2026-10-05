"""Ошибки генерации, которые API отдаёт клиенту как 502 с машинным кодом:

    {"detail": {"code": "model_refusal", "message": "..."}}

Сообщения короткие, без внутренних деталей (причина — только в
ai_request_logs.error_message). Остальные ошибки сервиса остаются как были.
"""

MODEL_REFUSAL = "model_refusal"  # отказ модели по правилам безопасности
OUTPUT_TRUNCATED = "output_truncated"  # обрыв по max_tokens
GENERATION_FAILED = "generation_failed"  # инструмент не вызван после двух попыток

MESSAGES = {
    MODEL_REFUSAL: "Модель отклонила запрос по соображениям безопасности. Переформулируйте запрос.",
    OUTPUT_TRUNCATED: "Ответ получился слишком длинным и был оборван. Сократите запрос или разбейте его на части.",
    GENERATION_FAILED: "Не удалось создать файл: модель не выполнила задачу. Попробуйте ещё раз.",
}


class GenerationError(Exception):
    """Базовый класс; internal — подробности только для лога."""

    code: str

    def __init__(self, internal: str = ""):
        super().__init__(internal or self.code)
        self.internal = internal or self.code

    @property
    def detail(self) -> dict:
        return {"code": self.code, "message": MESSAGES[self.code]}


class ModelRefusal(GenerationError):
    code = MODEL_REFUSAL


class OutputTruncated(GenerationError):
    code = OUTPUT_TRUNCATED


class GenerationFailed(GenerationError):
    code = GENERATION_FAILED
