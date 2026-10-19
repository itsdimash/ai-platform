"""Описание шаблона (prompts/<id>/template.yaml) и реестр шаблонов.

Загрузка и валидация — pydantic, при старте приложения: ошибка в template.yaml
поднимает TemplateConfigError с понятным списком проблем, приложение не стартует.
"""

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.router.route import load_config
from app.templates.pipelines import resolve_pipeline
from app.utils.uploads import ALL_EXTENSIONS

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TEST_ID = re.compile(r"^_[a-z0-9_]{1,63}$")  # тестовые шаблоны (_echo) — только из тестов


class TemplateConfigError(Exception):
    """Некорректные шаблоны; message — список всех проблем."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileSlot(_Strict):
    slot: str
    label: str
    accept: list[str]
    required: bool = False
    multiple: bool = False

    @model_validator(mode="after")
    def _check(self) -> "FileSlot":
        self.accept = [a.lower() for a in self.accept]
        bad = [a for a in self.accept if a not in ALL_EXTENSIONS]
        if not self.accept or bad:
            raise ValueError(f"slot {self.slot}: accept должен быть из {sorted(ALL_EXTENSIONS)}")
        return self


class ParamOption(_Strict):
    value: str
    label: str


class ParamSpec(_Strict):
    key: str
    label: str
    type: Literal["text", "textarea", "number", "select"]
    required: bool = False
    max_length: int | None = Field(default=None, gt=0)
    options: list[ParamOption] | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check(self) -> "ParamSpec":
        if self.type == "select" and not self.options:
            raise ValueError(f"param {self.key}: для select нужны options")
        if self.type != "select" and self.options:
            raise ValueError(f"param {self.key}: options только у select")
        return self


class Limits(_Strict):
    max_items: int = Field(default=60, gt=0)
    max_searches_per_item: int = Field(default=3, ge=0)
    concurrency: int = Field(default=4, gt=0)
    run_timeout_s: int = Field(default=1800, gt=0)
    max_runs_per_user: int = Field(default=2, gt=0)


class TemplateSpec(_Strict):
    id: str
    title: str
    description: str = ""
    icon: str = "file"
    allowed_roles: list[str]
    files: list[FileSlot] = Field(default_factory=list)
    params: list[ParamSpec] = Field(default_factory=list)
    require_any: list[str] = Field(default_factory=list)
    output: Literal["file", "table", "table+file"]
    pipeline: str
    limits: Limits = Field(default_factory=Limits)
    # этап -> логическое имя модели из router/config.yaml; ключ default — по умолчанию
    models: dict[str, str]
    max_tokens: int = Field(default=32000, gt=0)
    thinking: Literal["off", "low", "medium", "high", "max"] = "low"
    # свободные настройки конкретного пайплайна (например, порог разбиения входа)
    settings: dict[str, Any] = Field(default_factory=dict)

    # каталог шаблона (не из YAML)
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "TemplateSpec":
        if not (_ID.match(self.id) or _TEST_ID.match(self.id)):
            raise ValueError(f"id {self.id!r}: строчные латинские буквы, цифры, _")
        if not self.allowed_roles:
            raise ValueError("allowed_roles не может быть пустым")
        slots = [f.slot for f in self.files]
        keys = [p.key for p in self.params]
        if len(set(slots)) != len(slots) or len(set(keys)) != len(keys):
            raise ValueError("повторяющиеся slot или key")
        if set(slots) & set(keys):
            raise ValueError("slot и key не должны совпадать")
        for name in self.require_any:
            if name not in slots and name not in keys:
                raise ValueError(f"require_any: неизвестное поле {name!r}")
        if not self.models:
            raise ValueError("models: нужна хотя бы одна модель")
        known = load_config()["models"]
        for stage, model in self.models.items():
            if model not in known:
                raise ValueError(f"models.{stage}: модели {model!r} нет в router/config.yaml")
        try:
            resolve_pipeline(self.pipeline)
        except LookupError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def model_for(self, stage: str | None = None) -> str:
        if stage and stage in self.models:
            return self.models[stage]
        return self.models.get("default") or next(iter(self.models.values()))

    def read_prompt(self, filename: str) -> str:
        """Текст файла промпта из каталога шаблона."""
        if self.base_dir is None:
            raise FileNotFoundError(filename)
        path = (self.base_dir / filename).resolve()
        if self.base_dir.resolve() not in path.parents:
            raise ValueError("файл промпта вне каталога шаблона")
        return path.read_text(encoding="utf-8")

    def public(self) -> dict:
        """Описание для GET /v1/templates (без пайплайна, лимитов и моделей)."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "files": [f.model_dump() for f in self.files],
            "params": [p.model_dump(exclude_none=True) for p in self.params],
            "require_any": self.require_any,
            "output": self.output,
        }


class TemplateRegistry:
    def __init__(self, templates: dict[str, TemplateSpec]):
        self._templates = templates

    def get(self, template_id: str) -> TemplateSpec | None:
        return self._templates.get(template_id)

    def all(self) -> list[TemplateSpec]:
        return list(self._templates.values())

    def for_role(self, role: str) -> list[TemplateSpec]:
        return [t for t in self._templates.values() if role in t.allowed_roles]


def load_registry(root: Path = PROMPTS_DIR) -> TemplateRegistry:
    """Читает prompts/<id>/template.yaml. Любая ошибка — TemplateConfigError со списком."""
    errors: list[str] = []
    templates: dict[str, TemplateSpec] = {}
    if root.is_dir():
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if directory.name.startswith("."):
                continue
            path = directory / "template.yaml"
            where = f"{directory.name}/template.yaml"
            if not path.is_file():
                errors.append(f"{where}: файл не найден")
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                spec = TemplateSpec.model_validate(data)
            except yaml.YAMLError as exc:
                errors.append(f"{where}: невалидный YAML ({exc})")
                continue
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(x) for x in err["loc"]) or "<root>"
                    errors.append(f"{where}: {loc}: {err['msg']}")
                continue
            if spec.id != directory.name:
                errors.append(f"{where}: id {spec.id!r} не совпадает с именем каталога")
                continue
            spec.base_dir = directory
            templates[spec.id] = spec
    if errors:
        raise TemplateConfigError("Некорректные шаблоны:\n  - " + "\n  - ".join(errors))
    return TemplateRegistry(templates)


_registry: TemplateRegistry | None = None


def get_registry() -> TemplateRegistry:
    global _registry
    if _registry is None:
        _registry = load_registry()
    return _registry
