import pytest
from pydantic import ValidationError

from src.dto.check_plan import CheckPlanReviewRequest, validate_check_plan
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)


class FailingLLM:
    async def complete(self, *args, **kwargs):
        raise RuntimeError("LLM is unavailable")


async def test_metric_minimum_distance_is_planned_as_t1():
    plan = await CheckPlanPlanner().plan(
        "r1",
        ExtractedRestriction(
            subject="Школа",
            object="Жилой дом",
            kind="минимальное_расстояние",
            value=RestrictionValue(operator=">=", number=50, unit="м"),
            extraction_text="Не менее 50 м",
        ),
    )
    assert plan.template == "distance_from_source"
    assert plan.params["distance_m"] == 50
    assert plan.planner_status == "auto"
    assert [item.role for item in plan.declared_requirements.layers] == [
        "source",
        "targets",
    ]


async def test_maximum_distance_is_planned_as_presence_t3():
    plan = await CheckPlanPlanner().plan(
        "r2",
        ExtractedRestriction(
            subject="Парк",
            object="Жилой дом",
            kind="доступность",
            value=RestrictionValue(operator="<=", number=500, unit="м"),
        ),
    )
    assert plan.template == "presence_within"
    assert plan.params["distance_m"] == 500


@pytest.mark.parametrize(
    "number,unit,condition",
    [
        (500, "м", None),
        (
            800,
            "м",
            "в условиях стесненной городской застройки и труднодоступной местности",
        ),
        (1, "км", "в сельских поселениях"),
    ],
)
async def test_education_distance_only_blocks_unresolved_conditions(
    number, unit, condition
):
    ex = ExtractedRestriction(
        subject="организации, реализующие программы дошкольного, начального общего, основного общего и среднего общего образования",
        object="расстояние до жилых зданий",
        kind="минимальное_расстояние",
        value=RestrictionValue(
            operator="<=", number=number, unit=unit, condition=condition
        ),
        extraction_text=f"Расстояние до жилых зданий не более {number} {unit}.",
    )
    plan = await CheckPlanPlanner().plan("education", ex)

    if condition:
        assert plan.template == plan.planner_status == "unsupported"
        assert plan.params["blocked_reasons"] == ["applicability_not_verified"]
        assert plan.params["condition"] == condition
        candidate = validate_check_plan(plan.params["candidate_plan"])
    else:
        assert plan.planner_status == "auto"
        candidate = plan
    assert candidate.template == "presence_within"
    assert candidate.params["distance_m"] == (1000 if unit == "км" else number)
    assert candidate.params["objects_layer"] == "objects"
    # Each category is mandatory: a nearby school cannot substitute for a kindergarten.
    assert candidate.params["required_neighbor_layers"] == ["kindergartens", "schools"]
    assert [
        (layer.role, layer.entity, layer.entity_type)
        for layer in candidate.declared_requirements.layers
    ] == [
        ("objects", "Жилой дом", "physical_object"),
        ("kindergartens", "Детский сад", "service"),
        ("schools", "Школа", "service"),
    ]
    assert plan.source.extraction_text == ex.extraction_text
    assert ex.value.number == number and ex.value.unit == unit


@pytest.mark.parametrize(
    "subject,roles",
    [
        ("Школа", ["schools"]),
        ("Дошкольная организация", ["kindergartens"]),
    ],
)
async def test_single_education_category_does_not_add_another_requirement(
    subject, roles
):
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject=subject,
            object="Жилой дом",
            kind="доступность",
            value=RestrictionValue(operator="<=", number=500, unit="м"),
        ),
    )
    assert plan.planner_status == "auto"
    assert plan.params["required_neighbor_layers"] == roles


@pytest.mark.parametrize(
    "text",
    [
        "Пешеходная доступность школы от жилых зданий не более 500 м.",
        "Расстояние от школы до жилых зданий по пешеходному маршруту не более 500 м.",
        "Транспортная доступность школы от жилых зданий не более 500 м.",
    ],
)
async def test_explicit_education_route_still_blocks_geometric_candidate(text):
    plan = await CheckPlanPlanner().plan(
        "education-route",
        ExtractedRestriction(
            subject="Школа",
            object="Жилой дом",
            kind="доступность",
            value=RestrictionValue(operator="<=", number=500, unit="м"),
            extraction_text=text,
        ),
    )
    assert plan.template == plan.planner_status == "unsupported"
    assert plan.params["blocked_reasons"] == ["walking_route_required"]
    candidate = validate_check_plan(plan.params["candidate_plan"])
    assert candidate.template == "presence_within"
    assert candidate.params["distance_m"] == 500


@pytest.mark.parametrize("unit", ["км", "KM", " километра "])
async def test_kilometers_are_normalized_for_metric_distance(unit):
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="Промышленное предприятие",
            object="Жилой дом",
            kind="расстояние",
            value=RestrictionValue(operator=">=", number=1, unit=unit),
        ),
    )
    assert plan.template == "distance_from_source"
    assert plan.params["distance_m"] == 1000


@pytest.mark.parametrize("number", [0, -1, 100001, float("inf"), float("nan")])
async def test_invalid_radius_does_not_escape_or_generate_a_plan(number):
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="Парк",
            object="Жилой дом",
            kind="расстояние",
            value=RestrictionValue(operator="<=", number=number, unit="м"),
        ),
    )
    assert plan.planner_status == "unsupported"
    assert plan.params["blocked_reasons"] == ["distance_out_of_range"]


async def test_strict_maximum_is_not_replaced_with_inclusive_radius():
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="Парк",
            object="Жилой дом",
            kind="расстояние",
            value=RestrictionValue(operator="<", number=500, unit="м"),
        ),
    )
    assert plan.planner_status == "unsupported"
    assert "strict_distance_not_supported" in plan.params["blocked_reasons"]


@pytest.mark.parametrize(
    "condition,text,reason",
    [
        ("для сельской местности", "Норма", "applicability_not_verified"),
        (None, "Пешеходная доступность до остановки", "walking_route_required"),
    ],
)
async def test_unresolved_semantics_cannot_be_bypassed_by_llm(condition, text, reason):
    class RecordingLLM:
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            return "{}"

    llm = RecordingLLM()
    plan = await CheckPlanPlanner(llm).plan(
        "r",
        ExtractedRestriction(
            subject="Объект",
            object="Территория",
            kind="неизвестная",
            value=RestrictionValue(condition=condition),
            extraction_text=text,
        ),
    )
    assert reason in plan.params["blocked_reasons"]
    assert plan.planner_status == "unsupported"
    assert llm.calls == 0


async def test_percent_equality_is_valid_for_extracted_single_equals():
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="Озеленение",
            object="Участок",
            kind="доля",
            value=RestrictionValue(operator="=", number=20, unit="%"),
            measurement=RestrictionMeasurement(
                kind="area_share",
                indicator="доля площади озеленения",
                basis="площадь участка",
                numerator_entity="Озеленение",
                denominator_entity="Участок",
            ),
            extraction_text="Площадь озеленения составляет 20% площади участка.",
        ),
    )
    assert plan.template == "zonal_ratio"
    assert plan.params["operator"] == "=="
    assert [
        (layer.role, layer.entity) for layer in plan.declared_requirements.layers
    ] == [("zones", "Участок"), ("numerator", "Озеленение")]
    assert plan.declared_requirements.layers[0].entity_type == "functional_zone"


async def test_unmapped_restriction_is_explicitly_unsupported_without_llm():
    plan = await CheckPlanPlanner().plan(
        "r3",
        ExtractedRestriction(
            subject="Объект",
            object="Территория",
            kind="неизвестное_ограничение",
        ),
    )
    assert plan.planner_status == "unsupported"


async def test_llm_failure_falls_back_to_explicitly_unsupported_plan():
    plan = await CheckPlanPlanner(FailingLLM()).plan(
        "r4",
        ExtractedRestriction(
            subject="Объект",
            object="Территория",
            kind="неизвестное_ограничение",
        ),
    )

    assert plan.template == "unsupported"
    assert plan.planner_status == "unsupported"


def test_contract_forbids_unknown_schema_and_extra_fields():
    with pytest.raises(ValidationError):
        validate_check_plan(
            {
                "schema_version": "2.0",
                "template": "distance_from_source",
                "template_version": 1,
                "params": {},
                "source": {"restriction_id": "r"},
                "planner_status": "auto",
                "python": "eval('x')",
            }
        )


def test_replace_review_requires_a_plan():
    with pytest.raises(ValidationError):
        CheckPlanReviewRequest(action="replace")


def test_distance_table_rejects_overlapping_bands():
    with pytest.raises(ValidationError):
        validate_check_plan(
            {
                "schema_version": "1.0",
                "template": "distance_table",
                "template_version": 1,
                "params": {
                    "source_layer": "source",
                    "attribute_role": "floors",
                    "bands": [
                        {"min": 0, "max": 5, "distance_m": 10},
                        {"min": 5, "max": 10, "distance_m": 20},
                    ],
                    "targets": ["targets"],
                },
                "declared_requirements": {
                    "layers": [
                        {
                            "role": "source",
                            "entity": "source",
                            "entity_type": "physical_object",
                        },
                        {
                            "role": "targets",
                            "entity": "target",
                            "entity_type": "physical_object",
                        },
                    ],
                    "attributes": [
                        {
                            "role": "floors",
                            "on": "source",
                            "accepts": [
                                {
                                    "field": "floors",
                                    "unit": "floor",
                                    "quality": "direct",
                                }
                            ],
                        }
                    ],
                },
                "source": {"restriction_id": "r1"},
                "planner_status": "auto",
            }
        )


def test_contract_rejects_params_referencing_undeclared_layer_role():
    with pytest.raises(ValueError, match="unknown layer roles"):
        validate_check_plan(
            {
                "schema_version": "1.0",
                "template": "distance_from_source",
                "template_version": 1,
                "params": {
                    "source_layer": "ghost",
                    "targets": ["targets"],
                    "geometry_mode": "buffered",
                    "predicate": "intersects",
                    "violation_when": "matched",
                    "distance_m": 50,
                },
                "declared_requirements": {
                    "layers": [
                        {
                            "role": "source",
                            "entity": "source",
                            "entity_type": "physical_object",
                        },
                        {
                            "role": "targets",
                            "entity": "target",
                            "entity_type": "physical_object",
                        },
                    ]
                },
                "source": {"restriction_id": "r5"},
                "planner_status": "reviewed",
            }
        )
