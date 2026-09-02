"""
Валидатор SQL для db_query.

Модель генерирует SQL по схеме БД — этот модуль решает, можно ли его
выполнить. Ничего не выполняется до прохождения всех проверок. Читать
вместе с whitelist.yaml.

Философия: при любой неоднозначности — REJECT, не пытаться "угадать"
безопасную интерпретацию. Более полное покрытие (подзапросы, CTE, UNION)
можно добавлять по мере необходимости, но каждый добавленный случай должен
быть явно проверен, а не разрешён по умолчанию.
"""

from dataclasses import dataclass
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

_WHITELIST_PATH = Path(__file__).parent / "whitelist.yaml"


class SQLValidationError(Exception):
    """Запрос отклонён валидатором. Сообщение безопасно показывать
    пользователю — не содержит внутренних деталей схемы."""


@dataclass
class ValidatedQuery:
    sql: str  # переписанный безопасный SQL, готовый к выполнению
    tables_used: list[str]


class SQLValidator:
    def __init__(self, whitelist_path: Path = _WHITELIST_PATH):
        with open(whitelist_path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        self._tables = self._config["tables"]
        self._blocked = set(self._config.get("blocked_tables", []))
        self._defaults = self._config["defaults"]

    def validate(self, sql: str, *, role: str, user_id: int) -> ValidatedQuery:
        statements = sqlglot.parse(sql, read="postgres")

        if len(statements) != 1:
            raise SQLValidationError("Разрешён ровно один SQL-запрос за раз.")

        tree = statements[0]

        if not isinstance(tree, exp.Select):
            raise SQLValidationError("Разрешены только SELECT-запросы.")

        # Явно отклоняем конструкции, для которых row-level инъекция и
        # column-whitelisting неоднозначны, вместо того чтобы разрешать их
        # "как получится".
        if tree.find(exp.With):
            raise SQLValidationError("CTE (WITH) пока не поддерживаются валидатором.")
        if list(tree.find_all(exp.Union)):
            raise SQLValidationError("UNION пока не поддерживается валидатором.")
        if tree.find(exp.Subquery):
            raise SQLValidationError("Подзапросы в FROM пока не поддерживаются валидатором.")

        tables = self._extract_tables(tree)
        self._check_tables_allowed(tables, role)
        self._check_functions(tree)
        self._expand_and_check_columns(tree, tables, role)
        self._inject_row_level(tree, tables, role, user_id)
        self._enforce_limit(tree)

        return ValidatedQuery(sql=tree.sql(dialect="postgres"), tables_used=sorted(tables))

    # ------------------------------------------------------------------

    def _extract_tables(self, tree: exp.Select) -> set[str]:
        tables = {t.name for t in tree.find_all(exp.Table)}
        if not tables:
            raise SQLValidationError("Не удалось определить таблицы запроса.")
        return tables

    def _check_tables_allowed(self, tables: set[str], role: str) -> None:
        for table in tables:
            if table in self._blocked:
                raise SQLValidationError(f"Таблица '{table}' недоступна.")
            rule = self._tables.get(table)
            if rule is None:
                raise SQLValidationError(f"Таблица '{table}' не входит в whitelist.")
            allowed_roles = rule["roles"]
            if "*" not in allowed_roles and role not in allowed_roles:
                raise SQLValidationError(f"Роль '{role}' не имеет доступа к таблице '{table}'.")

    def _check_functions(self, tree: exp.Select) -> None:
        deny = set(self._defaults.get("deny_functions", []))
        for func in tree.find_all(exp.Anonymous, exp.Func):
            func_name = (func.name or "").lower()
            if func_name in {f.lower() for f in deny}:
                raise SQLValidationError(f"Функция '{func_name}' запрещена.")

    def _expand_and_check_columns(self, tree: exp.Select, tables: set[str], role: str) -> None:
        # SELECT * -> явно раскрываем в разрешённый список колонок.
        # Это самое частое место утечки запрещённого поля — раскрытие
        # обязательно, простого пост-фильтра недостаточно.
        star_expressions = [e for e in tree.expressions if isinstance(e, exp.Star)]
        if star_expressions:
            if len(tables) != 1:
                raise SQLValidationError(
                    "SELECT * разрешён только для запроса к одной таблице — "
                    "перечислите колонки явно при использовании JOIN."
                )
            table = next(iter(tables))
            allowed_columns = self._allowed_columns_for_role(table, role)
            tree.set(
                "expressions",
                [exp.column(col) for col in allowed_columns],
            )
            return

        # Явно перечисленные колонки — проверяем каждую по её таблице,
        # если таблица одна (для multi-table запросов требуем квалификацию
        # table.column, иначе тоже reject, чтобы не гадать).
        for col in tree.find_all(exp.Column):
            col_name = col.name
            table_alias = col.table

            if table_alias:
                target_table = table_alias
            elif len(tables) == 1:
                target_table = next(iter(tables))
            else:
                raise SQLValidationError(
                    f"Колонка '{col_name}' должна быть указана с именем таблицы (table.column) "
                    "при запросе к нескольким таблицам."
                )

            if target_table not in tables:
                # алиас, не совпадающий с именем таблицы — не поддерживаем,
                # чтобы не терять точность role-проверки
                raise SQLValidationError(
                    f"Не удалось сопоставить алиас '{target_table}' с таблицей whitelist."
                )

            allowed_columns = self._allowed_columns_for_role(target_table, role)
            if col_name not in allowed_columns:
                raise SQLValidationError(
                    f"Колонка '{target_table}.{col_name}' недоступна для роли '{role}'."
                )

    def _allowed_columns_for_role(self, table: str, role: str) -> list[str]:
        rule = self._tables[table]
        columns = list(rule.get("columns", []))
        column_roles = rule.get("column_roles", {})
        deny_columns = set(rule.get("deny_columns", []))

        result = []
        for col in columns:
            if col in deny_columns:
                continue
            if col in column_roles and role not in column_roles[col]:
                continue
            result.append(col)
        return result

    def _inject_row_level(self, tree: exp.Select, tables: set[str], role: str, user_id: int) -> None:
        for table in tables:
            rule = self._tables[table]
            row_level = rule.get("row_level", {})
            predicate_template = row_level.get(role)
            if not predicate_template:
                continue

            predicate_sql = predicate_template.replace(":current_user_id", str(user_id))
            predicate = sqlglot.condition(predicate_sql, dialect="postgres")
            tree.where(predicate, copy=False)

    def _enforce_limit(self, tree: exp.Select) -> None:
        max_rows = self._defaults["max_rows"]
        existing_limit = tree.args.get("limit")

        if existing_limit is None:
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
            return

        try:
            current_value = int(existing_limit.expression.this)
        except (AttributeError, ValueError):
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
            return

        if current_value > max_rows:
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
