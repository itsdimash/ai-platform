from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from app.utils.docx_builder import DOCUMENT_TOOL
from app.utils.image_builder import IMAGE_TOOL
from app.utils.pptx_builder import PRESENTATION_TOOL
from app.utils.xlsx_builder import SPREADSHEET_TOOL


@dataclass
class GenerationResult:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    raw: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class Attachment:
    mime_type: str
    data: bytes
    kind: Literal["image", "pdf_document"]


# ДОБАВЛЕНО: DOCUMENT_TOOL (.docx) и SPREADSHEET_TOOL (.xlsx) — раньше AI мог
# создавать только презентации и картинки, хотя вся инфраструктура чтения
# .docx/.xlsx (routers/document_extract.py) и заливки в R2 (utils/r2.py)
# уже была на месте.
ALL_TOOLS = [PRESENTATION_TOOL, IMAGE_TOOL, DOCUMENT_TOOL, SPREADSHEET_TOOL]


class ModelAdapter(ABC):
    name: str

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        web_search: bool = False,
        max_tokens: int = 2048,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        raise NotImplementedError
