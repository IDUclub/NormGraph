"""Grounding keeps real quotes despite typography and list joins, and drops only invented ones."""

from types import SimpleNamespace

import langextract as lx
import pytest

from src.pipeline.extractor import RestrictionExtractor
from src.providers.langextract_backend import InvalidExtractionOutput


def _extraction(quote, number=None, unit=None):
    attrs = dict(subject="s", object="o", kind="k")
    if number is not None:
        attrs.update(value_operator=">=", value_number=number, value_unit=unit)
    return lx.data.Extraction(
        extraction_class="ограничение", extraction_text=quote, attributes=attrs
    )


def _extract(monkeypatch, clause, *extractions):
    monkeypatch.setattr(
        lx, "extract", lambda **kwargs: SimpleNamespace(extractions=list(extractions))
    )
    return RestrictionExtractor(None).extract_clause_sync(clause)


@pytest.mark.parametrize(
    "clause, quote",
    [
        (
            "Размещение складов горюче-смазочных материалов не допускается.",
            "Размещение складов горюче\u2011смазочных материалов не допускается.",
        ),
        (
            "Удельный размер площадок — 0,7 м2 на 1 человека.",
            "Удельный размер площадок - 0,7 м² на 1 человека",
        ),
        (
            "Зона «Ж-2»:\u202fвысота не более 9 этажей",
            'зона "Ж–2": высота не более 9 этажей',
        ),
        ("Расстояние до объектов — не менее 1\u00a0000 м.", "не менее 1000 м"),
        (
            "Ёмкость резервуаров не должна превышать 50 м3.",
            "емкость резервуаров не должна превышать",
        ),
    ],
)
def test_typographic_variants_of_a_real_quote_are_grounded(monkeypatch, clause, quote):
    assert len(_extract(monkeypatch, clause, _extraction(quote))) == 1


def test_list_item_joined_with_its_lead_in_is_grounded(monkeypatch):
    clause = (
        "Ширина проездов должна составлять не менее: 3,5 м — при высоте зданий до 13 м; "
        "4,2 м — при высоте здания от 13 м до 46 м."
    )
    quote = (
        "Ширина проездов должна составлять не менее: 4,2 м — при высоте здания от 13 м"
    )
    assert _extract(monkeypatch, clause, _extraction(quote, "4.2", "м"))


def test_inflected_quote_of_an_enumeration_is_grounded(monkeypatch):
    clause = "В границах зон запрещаются использование сточных вод, размещение кладбищ."
    assert _extract(monkeypatch, clause, _extraction("запрещается размещение кладбищ"))


def test_only_invented_restrictions_are_dropped(monkeypatch):
    clause = "Вместимость спортивного зала — не менее 1 зала на 300 учащихся."
    kept = _extraction("не менее 1 зала на 300 учащихся", "300", "учащихся")
    copied_from_prompt = _extraction(
        "полосу насаждений шириной не менее 50 м", "50", "м"
    )
    result = _extract(monkeypatch, clause, kept, copied_from_prompt)
    assert [r.extraction_text for r in result] == [kept.extraction_text]


def test_words_from_the_clause_with_an_invented_number_are_rejected(monkeypatch):
    clause = "Расстояние от школы до дороги следует принимать не менее 25 м."
    with pytest.raises(InvalidExtractionOutput, match="ungrounded_extraction_text"):
        _extract(monkeypatch, clause, _extraction("расстояние до дороги не менее 30 м"))


def test_value_missing_from_the_quote_is_rejected(monkeypatch):
    clause = "Расстояние от школы до дороги не менее 25 м, до парковки не менее 10 м."
    with pytest.raises(InvalidExtractionOutput, match="ungrounded_extraction_quantity"):
        _extract(monkeypatch, clause, _extraction("до дороги не менее 25 м", "10", "м"))


def test_both_ends_of_a_range_carry_its_unit(monkeypatch):
    clause = "Для занятий физкультурой — не менее 10–40 м в зависимости от шума."
    quote = "не менее 10–40 м"
    assert _extract(monkeypatch, clause, _extraction(quote, "10", "м"))
    assert _extract(monkeypatch, clause, _extraction(quote, "40", "м"))


def test_value_after_a_dash_is_not_read_as_negative(monkeypatch):
    clause = "Минимальный отступ от красной линии до зданий — 5 м."
    quote = "Минимальный отступ от красной линии до зданий — 5 м."
    assert _extract(monkeypatch, clause, _extraction(quote, "5", "м"))
