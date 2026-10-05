"""Общие фрагменты схем инструментов (одинаковые для всех провайдеров).

Схемы без unions/default/$ref: так их одинаково принимают Claude, Gemini и GPT. Каждый токен
схемы оплачивается в каждом запросе с инструментами — описания короткие."""

# Одно предложение о содержимом файла: попадает в текст ответа после шаблона
# («📊 Готово! Презентация «X» — 12 слайдов. <summary>»).
SUMMARY_PROP = {
    "type": "string",
    "description": (
        "One short sentence, in the same language as the user's request, describing what "
        "this file contains (the key idea or scope). Do NOT repeat the title or mention "
        "slide/page/row counts — the system adds those. No markdown."
    ),
}

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}

# Секция документа (docx и pdf): заголовок, абзацы, списки, таблица.
SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {"type": "string", "description": "Section heading; omit for none"},
        "paragraphs": {**_STR_LIST, "description": "Body paragraphs"},
        "bullets": {**_STR_LIST, "description": "Bullet list after the paragraphs"},
        "numbered": {**_STR_LIST, "description": "Numbered list (steps)"},
        "table": {
            "type": "object",
            "properties": {
                "headers": _STR_LIST,
                "rows": {"type": "array", "items": _STR_LIST},
            },
        },
    },
}
