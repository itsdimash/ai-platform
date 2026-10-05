import pytest
from pydantic import ValidationError

from app.routers.schemas import AttachmentOut, ChatRequest, ChatResponse, SessionRename

REC = {"type": "file", "name": "a.pptx", "key": "ai/1/2/x_a.pptx", "mime": "m", "size": 5}


def test_attachment_out_shape_and_optional_url():
    att = AttachmentOut(**REC)
    assert att.url is None
    assert AttachmentOut(**REC, url="https://x").model_dump()["url"] == "https://x"


def test_attachment_type_is_restricted():
    with pytest.raises(ValidationError):
        AttachmentOut(**{**REC, "type": "video"})


def test_chat_response_backward_compatible_defaults():
    resp = ChatResponse(
        session_id=1,
        text="t",
        task_type="general_qa",
        model_used="m",
        confidence=0.5,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    dumped = resp.model_dump()
    assert dumped["table"] is None and dumped["needs_review"] is False
    assert dumped["attachments"] == []


def test_chat_response_with_attachments():
    resp = ChatResponse(
        session_id=1,
        text="t",
        task_type="g",
        model_used="m",
        confidence=0,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        attachments=[REC],
    )
    assert resp.model_dump()["attachments"][0]["key"] == "ai/1/2/x_a.pptx"


def test_chat_request_attachment_keys_limit():
    assert ChatRequest(prompt="p").attachment_keys == []
    assert len(ChatRequest(prompt="p", attachment_keys=["k"] * 10).attachment_keys) == 10
    with pytest.raises(ValidationError):
        ChatRequest(prompt="p", attachment_keys=["k"] * 11)


def test_session_rename_validation():
    assert SessionRename(title="  Новое имя  ").title == "Новое имя"
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(ValidationError):
            SessionRename(title=bad)
