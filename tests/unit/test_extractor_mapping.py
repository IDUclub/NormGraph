"""Mapping a langextract result to restriction triples."""

from __future__ import annotations

from types import SimpleNamespace

import langextract as lx

from src.pipeline.extractor import to_restrictions
from src.pipeline.prompts import RESTRICTION_CLASS


def _ext(attrs, text="span", start=3, end=10, cls=RESTRICTION_CLASS):
    return lx.data.Extraction(
        extraction_class=cls,
        extraction_text=text,
        attributes=attrs,
        char_interval=lx.data.CharInterval(start_pos=start, end_pos=end),
    )


def test_maps_triple_and_value():
    annotated = SimpleNamespace(
        extractions=[
            _ext(
                {
                    "subject": "санитарно-защитная зона",
                    "object": "полоса насаждений",
                    "kind": "минимальная_ширина",
                    "value_operator": ">=",
                    "value_number": "50",
                    "value_unit": "м",
                }
            )
        ]
    )
    out = to_restrictions(annotated)
    assert len(out) == 1
    r = out[0]
    assert r.subject == "санитарно-защитная зона"
    assert r.object == "полоса насаждений"
    assert r.kind == "минимальная_ширина"
    assert r.value.operator == ">=" and r.value.number == 50.0 and r.value.unit == "м"
    assert r.char_start == 3 and r.char_end == 10


def test_drops_incomplete_and_wrong_class():
    annotated = SimpleNamespace(
        extractions=[
            _ext({"subject": "x", "object": "y"}),  # no kind
            _ext({"subject": "x", "kind": "k"}),  # no object
            _ext(
                {"subject": "a", "object": "b", "kind": "k"}, cls="something_else"
            ),  # wrong class
        ]
    )
    assert to_restrictions(annotated) == []


def test_no_value_when_absent():
    annotated = SimpleNamespace(
        extractions=[
            _ext(
                {"subject": "СЗЗ", "object": "участки", "kind": "запрет_использования"}
            )
        ]
    )
    out = to_restrictions(annotated)
    assert out[0].value is None


def test_list_valued_attrs_are_flattened():
    annotated = SimpleNamespace(
        extractions=[
            _ext(
                {
                    "subject": "автомобильная дорога",
                    "object": ["ширина полосы движения", "число полос"],
                    "kind": "минимальная_ширина",
                    "value_operator": [">="],
                    "value_number": "3,75",
                    "value_unit": ["м"],
                }
            )
        ]
    )
    out = to_restrictions(annotated)
    assert len(out) == 1
    r = out[0]
    assert r.object == "ширина полосы движения, число полос"
    assert r.value.operator == ">=" and r.value.number == 3.75 and r.value.unit == "м"


def test_comma_decimal_parsed():
    annotated = SimpleNamespace(
        extractions=[
            _ext(
                {
                    "subject": "a",
                    "object": "b",
                    "kind": "k",
                    "value_number": "3,5",
                    "value_unit": "м",
                }
            )
        ]
    )
    assert to_restrictions(annotated)[0].value.number == 3.5
