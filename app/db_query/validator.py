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
        alias_map = self._build_alias_map(tree)
        self._expand_and_check_columns(tree, tables, alias_map, role)
        self._inject_row_level(tree, tables, role, user_id)
        self._enforce_limit(tree)

        return ValidatedQuery(sql=tree.sql(dialect="postgres"), tables_used=sorted(tables))

    # ------------------------------------------------------------------

    def _extract_tables(self, tree: exp.Select) -> set[str]:
        tables = {t.name for t in tree.find_all(exp.Table)}
        if not tables:
            raise SQLValidationError("Не удалось определить таблицы запроса.")
        return tables

    def _build_alias_map(self, tree: exp.Select) -> dict[str, str]:
        """Отображение "квалификатор -> реальное имя таблицы" из FROM/JOIN.

        sqlglot различает exp.Table.name (настоящее имя таблицы) и
        exp.Table.alias (алиас, если он есть, иначе пустая строка) — это
        структурная информация из уже распарсенного AST, а не догадка, так
        что она согласуется с философией "не угадывать": мы используем то,
        что sqlglot уже однозначно определил при разборе синтаксиса.

        Каждой таблице сопоставляем И алиас (если есть), И настоящее имя —
        так запросы без алиасов (table.column) продолжают работать как
        раньше. Если один и тот же квалификатор смотрит на две разные
        таблицы — это может произойти только в невалидном/подозрительном
        SQL, поэтому REJECT вместо выбора одной из них.
        """
        alias_map: dict[str, str] = {}
        for table_expr in tree.find_all(exp.Table):
            real_name = table_expr.name
            for qualifier in filter(None, [table_expr.alias, real_name]):
                if qualifier in alias_map and alias_map[qualifier] != real_name:
                    raise SQLValidationError(
                        f"Неоднозначный алиас '{qualifier}' в запросе."
                    )
                alias_map[qualifier] = real_name
        return alias_map

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

    def _expand_and_check_columns(
        self, tree: exp.Select, tables: set[str], alias_map: dict[str, str], role: str
    ) -> None:
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
        # table.column, иначе тоже reject, чтобы не гадать). Квалификатор
        # может быть как алиасом (ws), так и настоящим именем таблицы
        # (warehouse_stocks) — оба случая разрешает alias_map.
        for col in tree.find_all(exp.Column):
            col_name = col.name
            qualifier = col.table

            if qualifier:
                target_table = alias_map.get(qualifier)
                if target_table is None:
                    # Квалификатор не соответствует ни одной таблице из
                    # FROM/JOIN этого запроса — не пытаемся угадать.
                    raise SQLValidationError(
                        f"Не удалось сопоставить алиас '{qualifier}' с таблицей в запросе."
                    )
            elif len(tables) == 1:
                target_table = next(iter(tables))
            else:
                raise SQLValidationError(
                    f"Колонка '{col_name}' должна быть указана с именем таблицы (table.column) "
                    "при запросе к нескольким таблицам."
                )

            if target_table not in tables:
                raise SQLValidationError(f"Таблица '{target_table}' не входит в whitelist.")

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
