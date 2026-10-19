"""Диагностические скрипты: только читают файл, ничего не пишут."""

import os

from scripts import inspect_tender_docx, inspect_xlsx_template
from tests import docx_fixtures as fx
from tests import xlsx_fixtures as xfx


def snapshot(path):
    return os.path.getmtime(path), path.read_bytes()


def test_inspect_docx_reports_cell(tmp_path, capsys):
    path = tmp_path / "tender.docx"
    path.write_bytes(fx.layout_a())
    before = snapshot(path)
    assert inspect_tender_docx.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "таблица 1" in out and "Раскладка: A" in out
    assert f"Символов: {len(fx.LONG)}" in out and "Маркеров количества" in out and ": 2" in out
    assert "Частей перед этапом 1: 1" in out and "Ноутбук - 2 шт." in out
    assert snapshot(path) == before and list(tmp_path.iterdir()) == [path]


def test_inspect_docx_layout_b_nested_and_missing(tmp_path, capsys):
    for name, build, marker in (
        ("b", fx.layout_b, "Раскладка: B"),
        ("n", fx.nested, "вложенная таблица"),
    ):
        path = tmp_path / f"{name}.docx"
        path.write_bytes(build())
        assert inspect_tender_docx.main([str(path)]) == 0
        assert marker in capsys.readouterr().out
    path = tmp_path / "none.docx"
    path.write_bytes(fx.no_cell())
    assert inspect_tender_docx.main([str(path)]) == 1
    assert "НЕ найдена" in capsys.readouterr().out
    assert inspect_tender_docx.main([]) == 2
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"nope")
    assert inspect_tender_docx.main([str(bad)]) == 1


def test_inspect_docx_reports_chunks_for_long_text(tmp_path, capsys):
    big = " ".join(f"Товар{i} - 1 шт. " + "описание. " * 300 for i in range(20))
    path = tmp_path / "big.docx"
    path.write_bytes(fx.layout_a(big))
    assert inspect_tender_docx.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "Маркеров количества («- N шт.» и т.п.): 20" in out and "размеры частей" in out


def test_inspect_xlsx_ok_and_warnings(tmp_path, capsys):
    path = tmp_path / "t.xlsx"
    path.write_bytes(xfx.with_existing_rows())
    before = snapshot(path)
    assert inspect_xlsx_template.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "Лист: «Спец»" in out and "Строка заголовков: 3" in out
    assert "Запись начнётся со строки: 7" in out and "продолжится с номера: 3" in out
    assert "Фото/ссылка товаров: колонка G" in out and "потерях нет" in out
    assert snapshot(path) == before and list(tmp_path.iterdir()) == [path]

    path.write_bytes(xfx.with_loss_objects())
    assert inspect_xlsx_template.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "диаграммы" in out and "макросы" in out and "изображения" in out


def test_inspect_xlsx_missing_columns_and_bad_input(tmp_path, capsys):
    path = tmp_path / "m.xlsx"
    path.write_bytes(xfx.missing_columns())
    assert inspect_xlsx_template.main([str(path)]) == 1
    assert "НЕ найдены колонки: Кол-во, Фото/ссылка товаров" in capsys.readouterr().out
    assert inspect_xlsx_template.main([]) == 2
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"nope")
    assert inspect_xlsx_template.main([str(bad)]) == 1
