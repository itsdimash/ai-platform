import copy

import pytest

from app.classifier.classify import KNOWN_TASK_TYPES
from app.router.route import load_config, normalize_thinking, validate_config
from app.tools import ALL_TOOLS

TOOLS = {t["name"] for t in ALL_TOOLS}


@pytest.fixture
def config():
    return copy.deepcopy(load_config())


def test_real_config_is_valid():
    validate_config(load_config(), TOOLS)


def test_every_logical_model_has_provider_and_id_and_caps(config):
    for name, spec in config["models"].items():
        assert spec["provider"] in ("openai", "anthropic", "gemini"), name
        assert isinstance(spec["id"], str) and spec["id"], name
        assert spec["caps"]["max_output_tokens"] > 0, name


def test_public_logical_names_are_kept(config):
    # контракт API: эти имена принимает параметр `model`
    for name in (
        "gpt-4o-mini",
        "gpt-4o",
        "claude-haiku",
        "claude-sonnet",
        "claude-opus",
        "gemini-flash",
        "gemini-pro",
    ):
        assert name in config["models"]


def test_every_category_has_a_rule_and_nothing_extra(config):
    assert set(config["routing_rules"]) == KNOWN_TASK_TYPES


def test_router_never_picks_explicit_only_models(config):
    explicit = {n for n, s in config["models"].items() if s.get("explicit_only")}
    assert "claude-fable" in explicit
    used = {r["model"] for r in config["routing_rules"].values()}
    used |= {config["low_confidence"]["model"], config["classifier"]["model"]}
    assert not used & explicit


def test_forced_tools_exist_and_cover_file_categories(config):
    rules = config["routing_rules"]
    expected = {
        "presentation": "generate_presentation",
        "document": "generate_document",
        "spreadsheet": "generate_spreadsheet",
        "pdf": "generate_pdf",
        "image": "generate_image",
    }
    for task, tool in expected.items():
        assert rules[task]["forced_tool"] == tool
    assert {r["forced_tool"] for r in rules.values() if r.get("forced_tool")} <= TOOLS


def test_policy_from_decisions(config):
    rules = config["routing_rules"]
    strong = {"presentation", "document", "spreadsheet", "pdf", "creative", "code"}
    assert all(
        rules[t]["model"] == "claude-sonnet" and rules[t]["max_tokens"] == 32000 for t in strong
    )
    assert rules["general_qa"]["model"] == "claude-sonnet"  # не haiku
    assert normalize_thinking(rules["general_qa"]["thinking"]) == "low"
    for t in ("db_query", "translation", "rewriting"):
        assert rules[t]["model"] == "claude-haiku"
    for t in ("contract_generation", "contract_analysis"):
        assert rules[t]["model"] == "claude-opus"
        assert normalize_thinking(rules[t]["thinking"]) == "medium"  # high — после SSE
        assert rules[t]["require_human_review"] is True
    assert config["low_confidence"]["model"] == "claude-sonnet"


def test_yaml_off_is_parsed_as_string(config):
    # голое off в YAML 1.1 -> False; в конфиге оно в кавычках, а нормализация подстраховывает
    assert normalize_thinking(False) == "off" and normalize_thinking(None) == "off"
    assert normalize_thinking("OFF") == "off"
    assert normalize_thinking(config["routing_rules"]["translation"]["thinking"]) == "off"


def test_image_and_price_sections(config):
    assert config["image"]["quality"] == "medium"
    assert config["image"]["model"] != config["image"]["fallback_model"]
    assert config["image"]["timeout_s"] == 300 and config["limits"]["provider_timeout_s"] == 600
    for name, price in config["prices"].items():
        assert name in config["models"]
        assert set(price) <= {"input", "output", "cache_read"}


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda c: c["models"]["gpt-4o"].pop("id"), "не задан id"),
        (lambda c: c["models"]["gpt-4o"].update(provider="mistral"), "provider"),
        (lambda c: c["models"]["claude-sonnet"]["caps"].update(forced_tool="maybe"), "forced_tool"),
        (
            lambda c: c["routing_rules"]["presentation"].update(forced_tool="generate_nothing"),
            "неизвестный инструмент",
        ),
        (
            lambda c: c["routing_rules"]["presentation"].update(model="no-such"),
            "не описана в models",
        ),
        (
            lambda c: c["routing_rules"]["presentation"].update(model="claude-fable"),
            "explicit_only",
        ),
        (lambda c: c["routing_rules"]["code"].update(thinking="extreme"), "thinking"),
        (lambda c: c["routing_rules"]["code"].update(max_tokens=0), "max_tokens"),
        (lambda c: c["classifier"].update(model="nope"), "classifier.model"),
        (lambda c: c["prices"].update(ghost={"input": 1}), "prices.ghost"),
        (lambda c: c["image"].pop("fallback_model"), "image.fallback_model"),
    ],
)
def test_invalid_config_is_rejected_with_clear_message(config, mutate, fragment):
    mutate(config)
    with pytest.raises(ValueError) as exc:
        validate_config(config, TOOLS)
    assert fragment in str(exc.value)
