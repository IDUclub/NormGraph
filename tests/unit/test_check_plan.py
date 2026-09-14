import json

import pytest
from pydantic import ValidationError

from src.dto.check_plan import CheckPlanReviewRequest, validate_check_plan
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue


class FailingLLM:
    async def complete(self, *args, **kwargs):
        raise RuntimeError("LLM is unavailable")


async def test_llm_can_explicitly_decline_a_plan_without_becoming_executable():
    class DecliningLLM:
        async def complete(self, prompt, **kwargs):
            contract = json.loads(prompt)
            assert "params" in contract["response_schema"]["required"]
            assert (
                "distance_m"
                in contract["template_params_schemas"]["presence_within"]["properties"]
            )
            return json.dumps(
                {
                    "schema_version": "1.0",
                    "template": "unsupported",
                    "template_version": 1,
                    "params": {},
                    "source": {"restriction_id": "model-invented-id"},
                    "planner_status": "unsupported",
                }
            )

    plan = await CheckPlanPlanner(DecliningLLM())._llm_fallback(
        "source-id",
        ExtractedRestriction(
            subject="участок", object="высота", kind="предельная_высота"
        ),
    )
    assert plan is not None
    assert plan.template == plan.planner_status == "unsupported"
    assert plan.source.restriction_id == "source-id"
    with pytest.raises(ValueError):
        validate_check_plan(plan.model_dump())


@pytest.mark.parametrize(
    ("subject", "expected_type"),
    [
        ("здание школы", "physical_object"),
        ("ЗДАНИЯ ДЕТСКОГО САДА", "physical_object"),
        ("корпус больницы", "physical_object"),
        ("сооружение спортивного комплекса", "physical_object"),
        ("Школа", "service"),
        ("Детский сад", "service"),
        ("Территория школы", "functional_zone"),
    ],
)
async def test_distance_plan_distinguishes_building_from_service(
    subject, expected_type
):
    plan = await CheckPlanPlanner().plan(
        "school-parking",
        ExtractedRestriction(
            subject=subject,
            object="открытая автомобильная стоянка",
            kind="минимальное_расстояние",
            value=RestrictionValue(operator=">=", number=50, unit="м"),
        ),
    )
    source, target = plan.declared_requirements.layers
    assert source.entity == subject
    assert source.entity_type == expected_type
    assert target.entity_type == "physical_object"
    assert plan.params["distance_m"] == 50


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
            subject="Школа",
            object="Жилой дом",
            kind="доступность",
            value=RestrictionValue(operator="<=", number=500, unit="м"),
        ),
    )
    assert plan.template == "presence_within"
    assert plan.params["distance_m"] == 500


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
