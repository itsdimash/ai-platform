import pytest

from app.utils.docx_cells import (
    CellNotFoundError,
    extract_description_cell,
    is_target_header,
    normalize,
)
from tests import docx_fixtures as fx


@pytest.mark.parametrize(
    "build", [fx.layout_a, fx.layout_b, fx.merged_horizontal, fx.merged_vertical, fx.nested]
)
def test_each_layout_returns_exactly_the_content_once(build):
    res = extract_description_cell(build())
    assert res.text == fx.LONG  # без дублей от объединённых ячеек и без соседних подписей
    assert res.warnings == [] and res.cells_used == 1


def test_header_wording_variants_are_matched():
    variants = [
        "ОПИСАНИЕ  ТРЕБУЕМЫХ\nХАРАКТЕРИСТИК товара",
        "Описание\xa0требуемых технических характеристик",
        "«Описание требуемых характеристик»:",
    ]
    for header in variants:
        assert is_target_header(header)
        assert extract_description_cell(fx.layout_a(header=header)).text == fx.LONG


def test_long_cell_quoting_the_header_is_not_a_header():
    quoted = "Участник обязан соблюдать описание требуемых характеристик. " * 20
    assert not is_target_header(quoted)


def test_several_cells_are_joined_in_document_order_with_warning():
    res = extract_description_cell(fx.two_tables())
    assert res.text == "Первое описание. Мышь - 3 шт.\n\nВторое описание. Клавиатура - 4 шт."
    assert res.cells_used == 2 and "несколько ячеек" in res.warnings[0]


def test_no_cell_raises_without_fallback_to_whole_document():
    with pytest.raises(CellNotFoundError) as exc:
        extract_description_cell(fx.no_cell())
    assert "Описание требуемых" in str(exc.value)


def test_header_without_content_raises():
    with pytest.raises(CellNotFoundError):
        extract_description_cell(fx.layout_a(content=""))


def test_normalize():
    assert normalize("  Привет\xa0\n Мир:  ") == "привет мир"
