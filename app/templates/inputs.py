"""Валидация входа запуска: параметры, слоты файлов, require_any, ключи в R2.

Файлы приходят только ключами (file_key из /v1/documents/extract). Форму проверяем
синхронно (422), ключи — по владельцу (403), существованию (404), расширению и размеру (422).
Содержимое (magic bytes) проверяется позже, при чтении файла внутри run.
"""

import re
from typing import Any

from app.auth import CurrentUser
from app.limits import MAX_ATTACHMENT_KEYS, MAX_DOCUMENT_BYTES, MB
from app.templates.errors import TemplateHTTPError, input_invalid
from app.templates.spec import ParamSpec, TemplateSpec
from app.templates.storage import TemplateStorage
from app.utils.r2 import StorageNotConfiguredError
from app.utils.storage import can_access_key, is_valid_key, make_record
from app.utils.uploads import EXTENSIONS, extension_of

DEFAULT_TEXT_MAX = 500
DEFAULT_TEXTAREA_MAX = 60_000
USER_MESSAGE_VALUE_LIMIT = 300
_UUID_PREFIX = re.compile(r"^[0-9a-f]{32}_")


def _clean_param(spec: ParamSpec, value: Any) -> Any:
    """Приводит значение параметра к типу или бросает 422. Пустое -> None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if spec.type in ("text", "textarea"):
        if not isinstance(value, str):
            raise input_invalid(f"Параметр «{spec.label}» должен быть строкой", spec.key)
        limit = spec.max_length or (
            DEFAULT_TEXTAREA_MAX if spec.type == "textarea" else DEFAULT_TEXT_MAX
        )
        if len(value) > limit:
            raise input_invalid(
                f"Параметр «{spec.label}» слишком длинный (максимум {limit} символов)", spec.key
            )
        return value.strip()
    if spec.type == "number":
        if isinstance(value, bool):
            raise input_invalid(f"Параметр «{spec.label}» должен быть числом", spec.key)
        try:
            number = float(str(value).replace(",", ".")) if isinstance(value, str) else value
            number = float(number)
        except (TypeError, ValueError) as exc:
            raise input_invalid(f"Параметр «{spec.label}» должен быть числом", spec.key) from exc
        if (spec.min is not None and number < spec.min) or (
            spec.max is not None and number > spec.max
        ):
            raise input_invalid(f"Параметр «{spec.label}» вне допустимого диапазона", spec.key)
        return int(number) if number.is_integer() else number
    # select
    allowed = {o.value for o in spec.options or []}
    if value not in allowed:
        raise input_invalid(f"Недопустимое значение параметра «{spec.label}»", spec.key)
    return value


def validate_shape(
    spec: TemplateSpec, files: dict[str, Any] | None, params: dict[str, Any] | None
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Форма входа без обращения к хранилищу. Возвращает (ключи по слотам, очищенные параметры)."""
    files = files or {}
    params = params or {}
    if not isinstance(files, dict) or not isinstance(params, dict):
        raise input_invalid("files и params должны быть объектами")

    slots = {f.slot: f for f in spec.files}
    for name in files:
        if name not in slots:
            raise input_invalid(f"Неизвестный слот файла «{name}»", name)
    spec_params = {p.key: p for p in spec.params}
    for name in params:
        if name not in spec_params:
            raise input_invalid(f"Неизвестный параметр «{name}»", name)

    clean_files: dict[str, list[str]] = {}
    for slot in spec.files:
        keys = files.get(slot.slot) or []
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list) or not all(isinstance(k, str) and k for k in keys):
            raise input_invalid(f"Слот «{slot.label}»: ожидается список ключей файлов", slot.slot)
        keys = list(dict.fromkeys(keys))
        if len(keys) > MAX_ATTACHMENT_KEYS:
            raise input_invalid(f"Слот «{slot.label}»: слишком много файлов", slot.slot)
        if len(keys) > 1 and not slot.multiple:
            raise input_invalid(f"Слот «{slot.label}»: допускается один файл", slot.slot)
        if not keys and slot.required:
            raise input_invalid(f"Не приложен обязательный файл: {slot.label}", slot.slot)
        clean_files[slot.slot] = keys

    clean_params: dict[str, Any] = {}
    for param in spec.params:
        value = _clean_param(param, params.get(param.key))
        if value is None and param.required:
            raise input_invalid(f"Не заполнен обязательный параметр: {param.label}", param.key)
        if value is not None:
            clean_params[param.key] = value

    if spec.require_any:
        filled = any(clean_files.get(n) or n in clean_params for n in spec.require_any)
        if not filled:
            labels = {f.slot: f.label for f in spec.files} | {p.key: p.label for p in spec.params}
            names = ", ".join(f"«{labels[n]}»" for n in spec.require_any)
            raise input_invalid(f"Заполните хотя бы одно из полей: {names}")
    return clean_files, clean_params


async def resolve_files(
    spec: TemplateSpec,
    user: CurrentUser,
    keys_by_slot: dict[str, list[str]],
    storage: TemplateStorage,
) -> dict[str, list[dict]]:
    """Проверяет ключи в хранилище и возвращает записи вложений по слотам."""
    result: dict[str, list[dict]] = {}
    for slot in spec.files:
        records: list[dict] = []
        for key in keys_by_slot.get(slot.slot, []):
            if not is_valid_key(key) or not can_access_key(key, user.user_id, user.role):
                raise TemplateHTTPError(
                    403, "ai_file_forbidden", "Файл не принадлежит пользователю", slot.slot
                )
            ext = extension_of(key)
            if ext not in slot.accept:
                raise input_invalid(
                    f"Слот «{slot.label}»: допустимые форматы {', '.join(slot.accept)}", slot.slot
                )
            try:
                info = await storage.head(key)
            except StorageNotConfiguredError as exc:
                raise TemplateHTTPError(
                    503, "storage_unavailable", "Хранилище файлов не настроено"
                ) from exc
            if info is None:
                raise TemplateHTTPError(404, "file_not_found", "Файл не найден", slot.slot)
            if info["size"] > MAX_DOCUMENT_BYTES:
                raise input_invalid(
                    f"Файл слишком большой (максимум {MAX_DOCUMENT_BYTES // MB} МБ)", slot.slot
                )
            mime = info["mime"]
            if mime == "application/octet-stream":
                mime = EXTENSIONS[ext][0]
            name = info["name"] or _UUID_PREFIX.sub("", key.rsplit("/", 1)[-1])
            records.append(make_record(name=name, key=key, mime=mime, size=info["size"]))
        result[slot.slot] = records
    return result


def _shorten(value: str) -> str:
    value = value.strip()
    if len(value) > USER_MESSAGE_VALUE_LIMIT:
        return value[:USER_MESSAGE_VALUE_LIMIT].rstrip() + "…"
    return value


def format_user_message(spec: TemplateSpec, params: dict[str, Any]) -> str:
    """Текст сообщения пользователя в историю: «Шаблон «X»» + строки «Параметр: значение»."""
    lines = [f"Шаблон «{spec.title}»"]
    for param in spec.params:
        if param.key not in params:
            continue
        value = params[param.key]
        if param.type == "select":
            value = next((o.label for o in param.options or [] if o.value == value), value)
        lines.append(f"{param.label}: {_shorten(str(value))}")
    return "\n".join(lines)
