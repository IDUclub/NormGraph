"""Regressions from the synthetic uploaded document, using clause text, not plans."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import langextract as lx
import pytest

from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.extractor import RestrictionExtractor
from src.pipeline.models import ExtractedRestriction, RestrictionMeasurement
from src.pipeline.spatial_rules import compile_spatial_rule
from src.providers.langextract_backend import InvalidExtractionOutput

FIXTURE = Path(__file__).parents[1] / "fixtures" / "spatial_norms.json"


async def test_document_extracts_twelve_whole_rules_without_llm_or_fake_heading(
    monkeypatch,
):
    llm = Mock(side_effect=AssertionError("Known grounded grammar should not need LLM"))
    monkeypatch.setattr(lx, "extract", llm)
    extractor = RestrictionExtractor(None)
    clauses = json.loads(FIXTURE.read_text())
    assert len(clauses) == 12
    plans = {}
    for row in clauses:
        extracted = await extractor.extract_clause(row["text"])
        assert len(extracted) == 1
        ex = extracted[0]
        assert ex.char_start == 0 and ex.char_end == len(row["text"])
        plan = await CheckPlanPlanner().plan(row["clause"], ex)
        assert plan.planner_status == "auto"
        assert plan.source.extraction_text == row["text"]
        assert (
            RestrictionMeasurement.from_storage(ex.measurement.model_dump_json())
            == ex.measurement
        )
        plans[row["clause"]] = plan
    assert [p.template for p in plans.values()] == ["distance_from_source"] * 4 + [
        "zonal_attribute_threshold"
    ] * 2 + ["presence_within"] * 2 + ["distance_table"] * 2 + ["zonal_ratio"] * 2
    presence = plans["4.2"]
    assert presence.params["distance_m"] == 100
    layers = {l.role: l for l in presence.declared_requirements.layers}
    assert layers[presence.params["objects_layer"]].entity == "Жилой дом"
    assert layers[presence.params["required_neighbor_layers"][0]].entity == "Школа"
    assert "Point" not in layers["neighbors"].geometry_types
    assert plans["5.2"].params["bands"] == [
        dict(min=1, max=5, distance_m=100),
        dict(min=6, max=9, distance_m=150),
        dict(min=10, max=None, distance_m=200),
    ]
    floors = plans["3.2"].declared_requirements.attributes[0]
    assert floors.accepts[0].field == "building.floors" and floors.min_fill_rate == 0
    assert plans["6.2"].declared_requirements.layers[0].entity == "functional_zones"
    llm.assert_not_called()


@pytest.mark.parametrize(
    "suffix",
    [
        " За исключением сельских поселений.",
        " При численности населения более 1000 человек.",
        " Расстояние измеряется по пешеходному маршруту.",
    ],
)
def test_unknown_qualifiers_are_not_discarded(suffix):
    for row in json.loads(FIXTURE.read_text()):
        assert compile_spatial_rule(row["text"] + suffix) is None


def test_changed_values_are_parsed_and_unknown_entity_qualifiers_preserved():
    text = "Расстояние от школ до жилых домов следует принимать не менее 0,3 км."
    assert compile_spatial_rule(text).params["distance_m"] == 300
    assert compile_spatial_rule(text.replace("школ", "специальных школ")) is None
    assert compile_spatial_rule(text.replace("не менее", "более")) is None
    assert compile_spatial_rule(text.replace("0,3", "0")) is None


async def test_saved_wrong_roles_are_recompiled_from_grounded_text():
    row = next(r for r in json.loads(FIXTURE.read_text()) if r["clause"] == "4.2")
    ex = ExtractedRestriction(
        subject="жилой дом",
        object="школа",
        kind="доступность",
        extraction_text=row["text"],
    )
    plan = await CheckPlanPlanner().plan("stored", ex)
    assert plan.declared_requirements.layers[0].entity == "Жилой дом"
    assert plan.source.restriction_id == "stored"


def test_heading_number_is_not_a_floor_limit(monkeypatch):
    heading = "3 Этажность жилых домов"
    ext = lx.data.Extraction(
        extraction_class="ограничение",
        extraction_text=heading,
        attributes=dict(
            subject="дома",
            object="этажность",
            kind="предельная_этажность",
            value_number="3",
            value_operator="<=",
            value_unit="эт.",
        ),
    )
    monkeypatch.setattr(
        lx, "extract", lambda **kwargs: SimpleNamespace(extractions=[ext])
    )
    assert RestrictionExtractor(None).extract_clause_sync(heading) == []


def test_hallucinated_quote_is_not_stored(monkeypatch):
    ext = lx.data.Extraction(
        extraction_class="ограничение",
        extraction_text="Не более 3 этажей",
        attributes=dict(subject="a", object="b", kind="k"),
    )
    monkeypatch.setattr(
        lx, "extract", lambda **kwargs: SimpleNamespace(extractions=[ext])
    )
    with pytest.raises(InvalidExtractionOutput, match="ungrounded_extraction_text"):
        RestrictionExtractor(None).extract_clause_sync("Этажность жилых домов")


async def test_source_exception_is_not_lost_by_legacy_llm_triple():
    from src.pipeline.models import RestrictionValue

    ex = ExtractedRestriction(
        subject="Школа",
        object="Жилой дом",
        kind="минимальное_расстояние",
        value=RestrictionValue(operator=">=", number=20, unit="м"),
        extraction_text="Расстояние от школ до жилых домов следует принимать не менее 20 м. За исключением сельских поселений.",
    )
    plan = await CheckPlanPlanner().plan("exception", ex)
    assert plan.planner_status == "unsupported"
    assert "applicability_not_verified" in plan.params["blocked_reasons"]


def test_presence_count_and_neighbor_are_grounded_not_assumed():
    text = (
        "Для каждого жилого дома должно быть обеспечено не менее 2 школ "
        "на расстоянии не более 0,5 км от контура дома до контура школы."
    )
    rule = compile_spatial_rule(text)
    assert rule.params["minimum_neighbors"] == 2
    assert rule.params["distance_m"] == 500
    assert rule.restriction.measurement.minimum_neighbors == 2
    assert (
        compile_spatial_rule(text.replace("контура школы", "контура детского сада"))
        is None
    )


def test_table_bands_cannot_overlap_or_change_checked_entity():
    row = next(r for r in json.loads(FIXTURE.read_text()) if r["clause"] == "5.2")
    assert compile_spatial_rule(row["text"].replace("от 6 до 9", "от 5 до 9")) is None
    assert (
        compile_spatial_rule(
            row["text"].replace("являются спортивные площадки", "являются школы")
        )
        is None
    )
    rule = compile_spatial_rule(row["text"].replace("100 м", "100,5 м"))
    assert rule.params["bands"][0]["distance_m"] == 100.5


def test_full_containment_is_not_changed_to_intersection():
    row = next(r for r in json.loads(FIXTURE.read_text()) if r["clause"] == "3.1")
    rule = compile_spatial_rule(
        row["text"].replace("полностью или частично", "полностью")
    )
    assert rule.params["join_predicate"] == "within"
