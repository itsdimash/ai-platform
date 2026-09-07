"""Общий парсинг PDF — pdfplumber, страница за страницей.

Единственное место, где вызывается pdfplumber для извлечения текста:
и text-extraction роут (document_extract), и мультимодальный чат
(chat_multimodal, эвристика «скан vs текстовый PDF») используют эту
функцию, чтобы не дублировать логику открытия/обхода документа.
"""
from __future__ import annotations

import io


def extract_pdf_pages(data: bytes) -> list[str]:
    """Текст каждой страницы PDF отдельным элементом списка.

    Страницы без извлекаемого текстового слоя (например, сканы) в список
    НЕ попадают — поведение унаследовано от прежнего document_extract:
    берём только непустые `page.extract_text()`. Длина списка поэтому
    может быть меньше реального числа страниц документа.

    pdfplumber импортируется лениво — он тяжёлый, тянем только при
    реальном вызове (как в остальном коде).
    """
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return pages
