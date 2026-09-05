from pathlib import Path

import yaml

_WHITELIST_PATH = Path(__file__).parent / "whitelist.yaml"


def build_schema_context(role: str, whitelist_path: Path = _WHITELIST_PATH) -> str:
    """Строит текстовое описание доступных роли таблиц/колонок для промпта
    генерации SQL. Не включает row_level — эти предикаты инжектит
    validator.py после генерации, модели их видеть не нужно (может начать
    дублировать фильтр или путать синтаксис)."""

    with open(whitelist_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    lines = []
    for table, rule in config["tables"].items():
        allowed_roles = rule["roles"]
        if "*" not in allowed_roles and role not in allowed_roles:
            continue

        column_roles = rule.get("column_roles", {})
        deny_columns = set(rule.get("deny_columns", []))
        columns = [
            col
            for col in rule.get("columns", [])
            if col not in deny_columns and (col not in column_roles or role in column_roles[col])
        ]
        if not columns:
            continue

        lines.append(f"- {table}({', '.join(columns)})")

    if not lines:
        return "Нет доступных таблиц для этой роли."

    return (
        "Доступные таблицы и колонки (используй только их, без JOIN на другие таблицы):\n"
        + "\n".join(lines)
    )
