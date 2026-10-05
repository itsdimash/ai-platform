"""Общие фрагменты схем инструментов (одинаковые для всех провайдеров)."""

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
