import json
from dataclasses import dataclass

from ..adapters.base import ModelAdapter

CLASSIFIER_SYSTEM_PROMPT = """Ты классификатор задач для внутренней AI-платформы компании.
Верни ТОЛЬКО валидный JSON, без пояснений, без markdown-разметки.

Категории:
- translation — перевод текста
- summarization — резюме/суммаризация документа
- rewriting — рерайт/редактура текста
- contract_generation — генерация договора
- data_analysis — анализ данных (не запрос к БД, а анализ присланных данных)
- db_query — вопрос о данных компании (склад, проекты, поставщики, счета)
- web_search — вопрос, требующий актуальной информации из интернета
- general_qa — общий вопрос, не относящийся к перечисленному выше

Схема ответа:
{"task_type": "...", "confidence": 0.0-1.0, "reasoning": "коротко, почему"}
"""


@dataclass
class Classification:
    task_type: str
    confidence: float
    reasoning: str


KNOWN_TASK_TYPES = {
    "translation",
    "summarization",
    "rewriting",
    "contract_generation",
    "data_analysis",
    "db_query",
    "web_search",
    "general_qa",
}


async def classify(prompt: str, adapter: ModelAdapter) -> Classification:
    result = await adapter.generate(
        prompt=prompt,
        system=CLASSIFIER_SYSTEM_PROMPT,
        json_mode=True,
        max_tokens=200,
    )

    try:
        data = json.loads(result.text)
        task_type = data["task_type"]
        confidence = float(data["confidence"])
        reasoning = data.get("reasoning", "")
    except (json.JSONDecodeError, KeyError, ValueError):
        # Классификатор не вернул валидный JSON — безопасный дефолт,
        # не роняем запрос целиком.
        return Classification(task_type="general_qa", confidence=0.0, reasoning="classifier_parse_error")

    if task_type not in KNOWN_TASK_TYPES:
        return Classification(task_type="general_qa", confidence=0.0, reasoning=f"unknown_task_type:{task_type}")

    return Classification(task_type=task_type, confidence=confidence, reasoning=reasoning)
