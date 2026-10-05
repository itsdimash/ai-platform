import pytest

from app.router.route import Router, load_config

router = Router()
CONFIG = load_config()

FILE_TOOL = {
    "presentation": "generate_presentation",
    "document": "generate_document",
    "spreadsheet": "generate_spreadsheet",
    "pdf": "generate_pdf",
    "image": "generate_image",
}


@pytest.mark.parametrize("task", sorted(CONFIG["routing_rules"]))
def test_auto_routing_follows_the_rule_for_every_category(task):
    rule = CONFIG["routing_rules"][task]
    d = router.decide(task, 0.95)
    assert d.model == rule["model"] and not d.used_fallback_confidence
    assert d.max_tokens == min(
        rule["max_tokens"], CONFIG["models"][rule["model"]]["caps"]["max_output_tokens"]
    )
    assert d.forced_tool == FILE_TOOL.get(task)
    assert d.timeout_s == 600


def test_file_categories_use_sonnet_with_32k_and_medium_thinking():
    for task in ("presentation", "document", "spreadsheet", "pdf", "creative", "code"):
        d = router.decide(task, 0.9)
        assert (d.model, d.max_tokens, d.thinking) == ("claude-sonnet", 32000, "medium")


def test_contracts_use_opus_medium_thinking_and_need_review():
    for task in ("contract_generation", "contract_analysis"):
        d = router.decide(task, 0.9)
        assert (d.model, d.thinking, d.max_tokens) == ("claude-opus", "medium", 32000)
        assert d.require_human_review is True and d.forced_tool is None


def test_cheap_categories_use_haiku_and_general_qa_uses_sonnet_low():
    for task in ("db_query", "translation", "rewriting"):
        assert router.decide(task, 0.9).model == "claude-haiku"
    d = router.decide("general_qa", 0.9)
    assert (d.model, d.thinking, d.max_tokens) == ("claude-sonnet", "low", 8192)


def test_web_search_flag():
    d = router.decide("web_search", 0.9)
    assert d.web_search is True and d.model == "claude-sonnet"
    assert router.decide("general_qa", 0.9).web_search is False


@pytest.mark.parametrize(
    "task", ["presentation", "image", "translation", "db_query", "contract_generation"]
)
def test_low_confidence_falls_back_to_strong_model_never_flash(task):
    d = router.decide(task, 0.69)
    assert d.used_fallback_confidence is True
    assert d.model == "claude-sonnet" and "gemini" not in d.model
    assert d.forced_tool is None and d.web_search is False and d.require_human_review is False
    assert d.thinking == "low" and d.max_tokens == 8192


def test_threshold_boundary():
    assert router.decide("presentation", 0.7).used_fallback_confidence is False
    assert router.decide("presentation", 0.6999).used_fallback_confidence is True


def test_unknown_category_uses_strong_default():
    d = router.decide("telepathy", 0.99)
    assert d.model == "claude-sonnet" and d.used_fallback_confidence is True


def test_explicit_model_is_respected_but_category_rules_still_apply():
    d = router.decide_explicit("presentation", 0.9, "gemini-flash")
    assert d.model == "gemini-flash"
    assert d.forced_tool == "generate_presentation"  # принудительный tool остаётся
    assert d.thinking == "medium" and not d.used_fallback_confidence
    d = router.decide_explicit("contract_generation", 0.9, "gpt-4o")
    assert d.model == "gpt-4o" and d.require_human_review is True
    d = router.decide_explicit("web_search", 0.9, "claude-opus")
    assert d.web_search is True


def test_explicit_model_max_tokens_are_clamped_to_model_limit():
    d = router.decide_explicit("presentation", 0.9, "gpt-4o")
    assert d.max_tokens == 16384  # потолок gpt-4o < 32000 из правила


def test_explicit_model_with_low_confidence_uses_low_confidence_params():
    d = router.decide_explicit("presentation", 0.2, "claude-opus")
    assert d.model == "claude-opus" and d.forced_tool is None and d.used_fallback_confidence is True


def test_fable_is_selectable_only_explicitly():
    assert router.decide_explicit("general_qa", 0.9, "claude-fable").model == "claude-fable"
    auto_models = {router.decide(t, 0.95).model for t in CONFIG["routing_rules"]}
    assert "claude-fable" not in auto_models | {router.decide("x", 0.1).model}
