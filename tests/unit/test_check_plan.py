import pytest
from pydantic import ValidationError

from src.dto.check_plan import CheckPlanReviewRequest, validate_check_plan
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue


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
