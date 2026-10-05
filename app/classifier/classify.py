"""Классификатор запросов: по последнему сообщению (+ до 2 предыдущих обменов и виды
вложений) определяет категорию задачи. Результат определяет модель и параметры в
router/config.yaml.

Промпт проверен на 26 размеченных запросах RU/EN (с контекстом и вложениями):
gemini-3.1-flash-lite — 26/26.
"""

import json
import re
from dataclasses import dataclass

from ..adapters.base import ChatTurn, ModelAdapter

CLASSIFIER_SYSTEM_PROMPT = """Ты классификатор запросов для внутренней AI-платформы компании. Верни ТОЛЬКО валидный JSON без пояснений и markdown:
{"task_type": "<категория>", "confidence": 0.0-1.0, "reasoning": "коротко, почему"}

Классифицируй ПОСЛЕДНЕЕ сообщение пользователя; предыдущие реплики — только контекст (например, «добавь ещё слайд» после обсуждения презентации — это presentation). Если запрос просит создать файл или изображение, выбирай категорию файла, даже если тема — перевод, договор или данные. Выбирай самую специфичную категорию; general_qa — только если ничего другого не подходит.

Категории (RU / EN):
- presentation — презентация, слайды, pitch deck (.pptx). «Создай презентацию на тему: X», «сделай слайды про Q3», «make a pitch deck for…»
- document — Word-документ, отчёт, письмо, служебная записка, коммерческое предложение (.docx). «Напиши служебную записку о…», «prepare a report on… as a document»
- spreadsheet — таблица Excel, выгрузка, бюджет, расчёт в таблице (.xlsx). «Сделай таблицу бюджета», «build an Excel with monthly sales»
- pdf — файл именно в формате PDF. «Сделай PDF-памятку по технике безопасности», «export this as a PDF brochure»
- image — нарисовать/сгенерировать изображение, иллюстрацию, логотип, фото. «Сгенерируй изображение: the cat with toy», «нарисуй логотип», «generate a poster image»
- code — написать, исправить или объяснить код, SQL, скрипт, регулярку. «Напиши функцию на Python…», «fix this bash script»
- creative — творческое письмо: рассказ, стихи, слоган, сценарий, идеи названий, речь. «Придумай слоган для…», «write a short story about…»
- contract_generation — составить договор/соглашение с нуля. «Составь договор поставки», «draft an NDA»
- contract_analysis — проверить, разобрать, найти риски в существующем договоре/документе. «Проверь договор на риски», «review this contract and flag unusual clauses»
- translation — перевод текста. «Переведи на английский», «translate to Kazakh»
- summarization — краткое изложение/резюме присланного документа или текста. «Кратко перескажи этот отчёт», «summarize the attached PDF»
- rewriting — переписать, отредактировать, улучшить стиль текста. «Перепиши вежливее», «proofread this email»
- data_analysis — анализ присланных ТАБЛИЦ/чисел/CSV (не запрос к БД компании и не вопросы по изображениям). «Проанализируй продажи из этой таблицы», «find trends in the attached CSV»
- db_query — вопрос о данных компании в её БД (склад, проекты, поставщики, счета). «Сколько болтов на складе?», «покажи проекты менеджера Иванова»
- web_search — нужна актуальная информация из интернета. «Какой сегодня курс доллара?», «latest news about…»
- general_qa — общий вопрос или разговор, ничего из перечисленного; вопросы по приложенному изображению/фото («что на этой фотографии?», «опиши картинку») — тоже general_qa. «Что такое валовая маржа?», «explain how VAT works»
"""

KNOWN_TASK_TYPES = {
    "presentation",
    "document",
    "spreadsheet",
    "pdf",
    "image",
    "code",
    "creative",
    "contract_generation",
    "contract_analysis",
    "translation",
    "summarization",
    "rewriting",
    "data_analysis",
    "db_query",
    "web_search",
    "general_qa",
}

_USER_CONTEXT_CHARS = 500
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass
class Classification:
    task_type: str
    confidence: float
    reasoning: str
    tokens_in: int = 0
    tokens_out: int = 0


def build_classifier_input(
    last_message: str,
    context: list[ChatTurn] | None = None,
    attachment_kinds: str = "",
    *,
    max_exchanges: int = 2,
    assistant_chars: int = 300,
) -> str:
    """Вход классификатора: последнее сообщение + не более max_exchanges предыдущих
    обменов (ответы ассистента обрезаются) + виды вложений. Вся склеенная история
    в классификатор не попадает."""
    parts: list[str] = []
    turns = list(context or [])[-(max_exchanges * 2) :]
    if turns:
        lines = []
        for turn in turns:
            limit = assistant_chars if turn.role == "assistant" else _USER_CONTEXT_CHARS
            text = turn.content.strip()
            if len(text) > limit:
                text = text[:limit] + "…"
            speaker = "Ассистент" if turn.role == "assistant" else "Пользователь"
            lines.append(f"{speaker}: {text}")
        parts.append("Контекст:\n" + "\n".join(lines))
    if attachment_kinds:
        parts.append(f"Вложения: {attachment_kinds}")
    parts.append(f"Последнее сообщение пользователя: {last_message}")
    return "\n\n".join(parts)


def parse_classification(text: str) -> Classification:
    """Разбирает ответ модели; при мусоре/неизвестной категории — general_qa с
    уверенностью 0.0 (роутер отправит такой запрос на сильную модель по умолчанию)."""
    try:
        data = json.loads(_FENCE_RE.sub("", (text or "").strip()))
        task_type = data["task_type"]
        confidence = min(1.0, max(0.0, float(data["confidence"])))
        reasoning = str(data.get("reasoning", ""))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return Classification("general_qa", 0.0, "classifier_parse_error")

    if task_type not in KNOWN_TASK_TYPES:
        return Classification("general_qa", 0.0, f"unknown_task_type:{task_type}")
    return Classification(task_type, confidence, reasoning)


async def classify(
    last_message: str,
    adapter: ModelAdapter,
    *,
    context: list[ChatTurn] | None = None,
    attachment_kinds: str = "",
    max_tokens: int = 300,
    timeout_s: float = 60.0,
    max_exchanges: int = 2,
    assistant_chars: int = 300,
) -> Classification:
    # tools=[] обязателен: без него generate() цепляет файловые tools, а Gemini запрещает
    # сочетание json_mode + tools.
    result = await adapter.generate(
        prompt=build_classifier_input(
            last_message,
            context,
            attachment_kinds,
            max_exchanges=max_exchanges,
            assistant_chars=assistant_chars,
        ),
        system=CLASSIFIER_SYSTEM_PROMPT,
        json_mode=True,
        max_tokens=max_tokens,
        tools=[],
        thinking="off",
        timeout_s=timeout_s,
    )
    classification = parse_classification(result.text)
    classification.tokens_in, classification.tokens_out = result.tokens_in, result.tokens_out
    return classification
