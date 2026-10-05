import json

import pytest

from app.adapters.base import ChatTurn, GenerationResult
from app.classifier.classify import (
    CLASSIFIER_SYSTEM_PROMPT,
    KNOWN_TASK_TYPES,
    build_classifier_input,
    classify,
    parse_classification,
)


def test_new_categories_are_known_and_described_in_prompt():
    for cat in (
        "presentation",
        "document",
        "spreadsheet",
        "pdf",
        "image",
        "code",
        "creative",
        "contract_analysis",
    ):
        assert cat in KNOWN_TASK_TYPES
        assert f"- {cat} —" in CLASSIFIER_SYSTEM_PROMPT
    # RU и EN примеры, в том числе формулировка из реального теста
    assert "Создай презентацию на тему: X" in CLASSIFIER_SYSTEM_PROMPT
    assert "pitch deck" in CLASSIFIER_SYSTEM_PROMPT


def test_parse_plain_and_fenced_json():
    c = parse_classification(
        '{"task_type": "presentation", "confidence": 0.92, "reasoning": "слайды"}'
    )
    assert (c.task_type, c.confidence, c.reasoning) == ("presentation", 0.92, "слайды")
    fenced = parse_classification('```json\n{"task_type": "pdf", "confidence": 0.8}\n```')
    assert fenced.task_type == "pdf" and fenced.confidence == 0.8


@pytest.mark.parametrize(
    "text",
    [
        "",
        "не json",
        "[]",
        '{"task_type": "presentation"}',
        '{"task_type": "presentation", "confidence": "high"}',
        '{"task_type": "weather", "confidence": 0.9}',
    ],
)
def test_garbage_falls_back_to_general_qa_with_zero_confidence(text):
    c = parse_classification(text)
    assert (c.task_type, c.confidence) == ("general_qa", 0.0)


def test_confidence_is_clamped():
    assert parse_classification('{"task_type": "code", "confidence": 7}').confidence == 1.0
    assert parse_classification('{"task_type": "code", "confidence": -1}').confidence == 0.0


def test_input_is_last_message_plus_two_exchanges_with_truncated_assistant():
    ctx = []
    for i in range(5):
        ctx += [ChatTurn("user", f"вопрос {i}"), ChatTurn("assistant", f"ответ {i} " + "x" * 600)]
    text = build_classifier_input("добавь слайд про цены", ctx, "attached: 1 pdf, 2 images")
    assert text.count("Пользователь:") == 2 and text.count("Ассистент:") == 2  # ровно 2 обмена
    assert "вопрос 3" in text and "вопрос 4" in text and "вопрос 2" not in text
    assistant_line = next(
        line for line in text.splitlines() if line.startswith("Ассистент: ответ 4")
    )
    assert len(assistant_line) <= len("Ассистент: ") + 300 + 1 and assistant_line.endswith("…")
    assert "Вложения: attached: 1 pdf, 2 images" in text
    assert text.rstrip().endswith("Последнее сообщение пользователя: добавь слайд про цены")


def test_input_without_context_or_attachments_is_minimal():
    assert build_classifier_input("привет") == "Последнее сообщение пользователя: привет"


class _Adapter:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return GenerationResult(self.text, tokens_in=777, tokens_out=33, latency_ms=1)


async def test_classify_calls_json_mode_without_tools_and_records_tokens():
    adapter = _Adapter(
        json.dumps({"task_type": "spreadsheet", "confidence": 0.9, "reasoning": "xlsx"})
    )
    c = await classify("сделай таблицу", adapter, attachment_kinds="attached: 1 xlsx")
    call = adapter.calls[0]
    assert call["json_mode"] is True and call["tools"] == [] and call["thinking"] == "off"
    assert call["system"] == CLASSIFIER_SYSTEM_PROMPT
    assert "Вложения: attached: 1 xlsx" in call["prompt"]
    assert (c.task_type, c.tokens_in, c.tokens_out) == ("spreadsheet", 777, 33)


async def test_classify_garbage_still_returns_tokens():
    c = await classify("x", _Adapter("oops"))
    assert (c.task_type, c.confidence, c.tokens_in) == ("general_qa", 0.0, 777)
