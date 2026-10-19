"""Ошибки шаблонов.

HTTP-ошибки запуска: {"detail": {"code", "message", "field"?}}.
Ошибки внутри run (status=failed): RunError(code, message) -> run.error.
"""

from fastapi import HTTPException

# Коды ошибок внутри run.error (контракт).
TENDER_CELL_NOT_FOUND = "tender_cell_not_found"
TOO_MANY_ITEMS = "too_many_items"
XLSX_TEMPLATE_INVALID = "xlsx_template_invalid"
SEARCH_UNAVAILABLE = "search_unavailable"
OUTPUT_TRUNCATED = "output_truncated"
GENERATION_FAILED = "generation_failed"
INTERRUPTED = "interrupted"
TIMEOUT = "timeout"

GENERIC_FAILURE_MESSAGE = "Не удалось выполнить шаблон. Попробуйте ещё раз."


class TemplateHTTPError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, field: str | None = None):
        detail: dict = {"code": code, "message": message}
        if field:
            detail["field"] = field
        super().__init__(status_code=status_code, detail=detail)


def input_invalid(message: str, field: str | None = None) -> TemplateHTTPError:
    return TemplateHTTPError(422, "template_input_invalid", message, field)


class RunError(Exception):
    """Контролируемая ошибка пайплайна: run получает status=failed с этим кодом.
    message показывается пользователю — без стеков и значений ключей."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
