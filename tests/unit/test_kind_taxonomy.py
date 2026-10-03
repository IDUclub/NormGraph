"""The closed list of kinds and the mapping of model labels onto it."""

from __future__ import annotations

import pytest

from src.pipeline.kind_taxonomy import KINDS, OTHER, canonical_kind, classify_kind
from src.pipeline.models import RestrictionMeasurement, RestrictionValue
from src.pipeline.prompts import PROMPT_DESCRIPTION, SEED_KINDS


def _value(operator, number, unit):
    return RestrictionValue(operator=operator, number=number, unit=unit)


def test_the_prompt_offers_every_listed_kind_and_no_new_ones():
    assert SEED_KINDS == list(KINDS)
    for code in KINDS:
        assert f"  {code} — " in PROMPT_DESCRIPTION
    assert "предложи короткий новый" not in PROMPT_DESCRIPTION


def test_former_names_map_to_listed_kinds():
    assert canonical_kind("минимальная_ширина") == "минимальный_размер"
    assert canonical_kind("требование_размещения") == "требование_размещения"
    assert canonical_kind("требование_качества") is None


@pytest.mark.parametrize(
    ("label", "value", "measurement", "expected"),
    [
        # The operator decides the direction whatever the label says.
        (
            "минимальное_расстояние",
            _value("<=", 500, "м"),
            None,
            "максимальное_расстояние",
        ),
        (
            "наличие_в_радиусе",
            _value("<=", 300, "м"),
            "distance",
            "максимальное_расстояние",
        ),
        ("минимальная_ширина", _value(">=", 6, "м"), None, "минимальный_размер"),
        ("максимальный_диаметр", _value("<=", 20, "м"), None, "максимальный_размер"),
        ("требование_размещения", _value("<=", 15, "мин"), None, "срок"),
        (
            "максимальная_доступность_времени",
            _value("<=", 20, "мин"),
            None,
            "время_доступности",
        ),
        ("предельная_высота", _value("<=", 9, "эт."), None, "предельная_высота"),
        (
            "требование_размещения",
            _value(">=", 25, "%"),
            "area_share",
            "минимальная_доля_площади",
        ),
        ("процент_застройки", _value("<=", 60, "%"), None, "максимальная_доля_площади"),
        (
            "минимальный_уровень_обеспечения",
            _value(">=", 90, "%"),
            "provision",
            "обеспеченность",
        ),
        (
            "минимальная_освещенность",
            _value(">=", 400, "лк"),
            None,
            "физический_параметр",
        ),
        ("max_count", _value("<=", 17, "детей"), None, "количество"),
        (
            "предельная_площадь_на_1_га",
            _value("<=", 4000, None),
            None,
            "плотность_застройки",
        ),
    ],
)
def test_a_norm_with_a_number_gets_the_kind_of_its_quantity(
    label, value, measurement, expected
):
    m = RestrictionMeasurement(kind=measurement) if measurement else None
    assert classify_kind(label, value, m) == expected


def test_a_number_without_a_known_quantity_keeps_the_listed_label():
    value = _value(">", 300, "м")
    assert classify_kind("требование_размещения", value) == "требование_размещения"


def test_the_quote_decides_only_when_label_and_unit_do_not():
    value = _value(">=", 50, "м")
    assert (
        classify_kind("требование_x", value, text="расстояние до жилых домов")
        == "минимальное_расстояние"
    )
    # A listed label is not overruled by a word in the quote.
    assert (
        classify_kind(
            "требование_размещения", value, text="многоэтажной жилой застройки"
        )
        == "требование_размещения"
    )


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("запрет_размещения", "запрет_размещения"),
        ("запрет_строительства_жилья", "запрет_размещения"),
        ("запрет_пересмотра", "запрет_использования"),
        ("не_требуется_согласование", "допустимость"),
        ("разрешение_пересечения", "допустимость"),
        ("требование_наличия_документа", "требование_размещения"),
        ("требование_качества", "требование_к_объекту"),
        ("требование_схемы", "процедурное_требование"),
        ("требование_времени", "срок"),
        ("требование_соблюдения_акустических_условий", "физический_параметр"),
        ("минимальный_уровень_обеспечения", "обеспеченность"),
        ("предельный_показатель_этажности", "предельная_высота"),
        ("превалирование_конституции", OTHER),
        ("", OTHER),
    ],
)
def test_a_norm_without_a_number_is_mapped_by_its_label(label, expected):
    assert classify_kind(label) == expected
    assert expected in KINDS


def test_a_prohibition_without_a_label_is_read_from_the_quote():
    assert classify_kind("", text="Не допускается стоянка") == "запрет_использования"
