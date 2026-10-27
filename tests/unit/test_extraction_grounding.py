"""Grounding keeps real quotes despite typography and list joins, and drops only invented ones."""

from types import SimpleNamespace

import langextract as lx
import pytest

from src.pipeline.clause_context import RelatedClause
from src.pipeline.extractor import (
    RestrictionExtractor,
    _ground,
    _normalize_for_grounding,
    _numerals_to_digits,
    to_restrictions,
)
from src.providers.langextract_backend import (
    InvalidExtractionOutput,
    ProviderLanguageModel,
)


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
    return RestrictionExtractor(
        ProviderLanguageModel(None, model_id="fake")
    ).extract_clause_sync(clause)


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


@pytest.mark.parametrize(
    "quote, number, unit",
    [
        ("не может быть более одного месяца", "1", "месяц"),
        ("не может быть более чем один месяц", "1", "месяц"),
        ("не позднее чем за семь дней до дня размещения", "7", "дн."),
        (
            "составляет не менее двух и не более четырех месяцев со дня опубликования",
            "2",
            "мес",
        ),
        (
            "составляет не менее двух и не более четырех месяцев со дня опубликования",
            "4",
            "мес",
        ),
        ("в срок не позднее трех рабочих дней после завершения", "3", "рабочих дней"),
        ("не позднее, чем по истечении десяти дней с даты утверждения", "10", "дн."),
        ("не ранее чем по истечении одного года со дня включения", "1", "год"),
        (
            "не менее одной единицы мест на погрузочно-разгрузочных площадках",
            "1",
            "ед.",
        ),
    ],
)
def test_word_numeral_restriction_is_returned(monkeypatch, quote, number, unit):
    result = _extract(monkeypatch, quote + ".", _extraction(quote, number, unit))
    assert len(result) == 1
    assert result[0].value.number == float(number)
    assert result[0].extraction_text == quote


@pytest.mark.parametrize("separator", [" x ", "х", " × ", " * "])
def test_each_dimension_carries_the_trailing_unit(monkeypatch, separator):
    quote = (
        "Минимальные размеры 1 машино-места для легковых автомобилей - "
        f"2,5{separator}5 м"
    )
    result = _extract(
        monkeypatch, quote, _extraction(quote, "2.5", "м"), _extraction(quote, "5", "м")
    )
    assert [r.value.number for r in result] == [2.5, 5]


@pytest.mark.parametrize(
    "words, number",
    [
        ("нуля", "0"),
        ("одна", "1"),
        ("одно", "1"),
        ("одному", "1"),
        ("одним", "1"),
        ("одну", "1"),
        ("одном", "1"),
        ("двумя", "2"),
        ("трём", "3"),
        ("четырьмя", "4"),
        ("восьмью", "8"),
        ("девятнадцати", "19"),
        ("двадцать пять", "25"),
        ("тридцатью шестью", "36"),
        ("сорока", "40"),
        ("пятьюдесятью", "50"),
        ("шестидесяти", "60"),
        ("семидесяти", "70"),
        ("восемьюдесятью", "80"),
        ("девяноста", "90"),
        ("сто двадцать", "120"),
        ("двумястами тридцатью пятью", "235"),
        ("трёхсот", "300"),
        ("четырёхстах", "400"),
        ("пятистам", "500"),
        ("шестьюстами", "600"),
        ("семисот", "700"),
        ("восьмьюстами", "800"),
        ("девятьюстами", "900"),
        ("тысячу", "1000"),
        ("двух тысяч ста двадцати пяти", "2125"),
        ("тысяча один", "1001"),
        ("полтора", "1.5"),
        ("полторы", "1.5"),
        ("полутора", "1.5"),
    ],
)
def test_cardinal_forms_ground_in_their_unit(monkeypatch, words, number):
    quote = f"Расстояние не менее {words} метров."
    assert _extract(monkeypatch, quote, _extraction(quote, number, "м"))


@pytest.mark.parametrize("separator", ["и", "и не более", "и не менее", "и до"])
@pytest.mark.parametrize("first, second", [("двух", "четырех"), ("2", "4")])
def test_conjoined_bounds_share_the_unit(monkeypatch, separator, first, second):
    quote = f"Расстояние от {first} {separator} {second} метров."
    result = _extract(
        monkeypatch, quote, _extraction(quote, "2", "м"), _extraction(quote, "4", "м")
    )
    assert [r.value.number for r in result] == [2, 4]


@pytest.mark.parametrize(
    "quote, number, unit",
    [
        ("не более одного месяца", "2", "месяц"),
        ("одного из участков", "1", "м"),
        ("до третьего этажа", "3", "эт"),
        ("не менее двадцати пяти метров", "5", "м"),
        ("не менее двух тысяч метров", "2", "м"),
        ("не менее двух дней", "2", "м"),
        ("не менее двух и высота четырех метров", "2", "м"),
        ("размеры 2,5 x 5 м", "3", "м"),
    ],
)
def test_unsupported_word_quantity_is_rejected(monkeypatch, quote, number, unit):
    with pytest.raises(InvalidExtractionOutput, match="ungrounded_extraction_quantity"):
        _extract(monkeypatch, quote, _extraction(quote, number, unit))


def test_ordinal_is_grounded_only_when_a_cardinal_states_the_value(monkeypatch):
    quote = "До третьего этажа, высота не более трех этажей."
    assert _extract(monkeypatch, quote, _extraction(quote, "3", "эт"))


def test_word_numeral_in_a_related_clause_supplies_the_value():
    quote = "Расстояние принимается по таблице."
    restriction = to_restrictions(
        SimpleNamespace(extractions=[_extraction(quote, "25", "м")])
    )[0]
    related = RelatedClause(
        "table", "Расстояние не менее двадцати пяти метров.", "table_ref"
    )
    assert _ground(
        restriction,
        _normalize_for_grounding(quote),
        [(related, _normalize_for_grounding(related.text))],
    ) == (None, related.source())


def test_quantity_conversion_leaves_ordinals_and_invalid_compounds_alone():
    text = "до третьего этажа; пять семь метров; двадцать десять метров"
    assert _numerals_to_digits(text) == text
