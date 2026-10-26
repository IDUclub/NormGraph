"""Precision guards against the defects found in the 2026-09-30 production audit."""

import pytest

from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)
from src.pipeline.norm_guards import direction_conflict, precision_reasons


def _distance(subject, object_, number, text, *, operator=">=", unit="м"):
    return ExtractedRestriction(
        subject=subject,
        object=object_,
        kind="минимальное_расстояние",
        value=RestrictionValue(operator=operator, number=number, unit=unit),
        extraction_text=text,
    )


async def test_measure_label_is_not_a_layer():
    # «отступ … от красной линии – 10 м» used to become targets=«отступ от красной линии».
    plan = await CheckPlanPlanner().plan(
        "r",
        _distance(
            "зона охраны объектов культурного наследия",
            "отступ от красной линии",
            10,
            "отступ зданий от красной линии – 10 м",
        ),
    )
    assert plan.planner_status == "unsupported"
    assert "non_spatial_entity" in plan.params["blocked_reasons"]


@pytest.mark.parametrize(
    "text,reason",
    [
        (
            "Пожарные гидранты следует размещать не реже чем через 100 м",
            "periodic_spacing_not_supported",
        ),
        (
            "Расстояние между опорами не чаще, чем через 60 м",
            "periodic_spacing_not_supported",
        ),
        ("Расстояние до сооружения — не более 300 м", "operator_direction_conflict"),
    ],
)
async def test_upper_bound_is_not_planned_as_minimum_distance(text, reason):
    plan = await CheckPlanPlanner().plan(
        "r", _distance("Пожарный гидрант", "Жилой дом", 100, text)
    )
    assert plan.planner_status == "unsupported"
    assert reason in plan.params["blocked_reasons"]
    # The blocked candidate stays available for an expert.
    assert plan.params["candidate_plan"]["template"] == "distance_from_source"


async def test_depth_is_not_a_planar_distance():
    plan = await CheckPlanPlanner().plan(
        "r",
        _distance(
            "Водопровод",
            "Канализация",
            9,
            "трубопроводы прокладывать не глубже 9 м от поверхности",
            operator="<=",
        ),
    )
    assert plan.planner_status == "unsupported"
    assert "depth_not_distance" in plan.params["blocked_reasons"]


@pytest.mark.parametrize("number,unit", [(0.5, "м"), (2.5, "м")])
async def test_in_building_scale_is_not_territorial(number, unit):
    plan = await CheckPlanPlanner().plan(
        "r",
        _distance(
            "Кроватка",
            "Отопительный прибор",
            number,
            f"Кроватки размещают на расстоянии не менее {number} {unit} от отопительных приборов",
            unit=unit,
        ),
    )
    assert plan.planner_status == "unsupported"
    assert "distance_below_territorial_scale" in plan.params["blocked_reasons"]


async def test_window_to_wall_share_is_not_a_zonal_ratio():
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="стена",
            object="окна",
            kind="минимальная_доля_площади",
            value=RestrictionValue(operator=">=", number=20, unit="%"),
            measurement=RestrictionMeasurement(
                kind="area_share",
                basis="площадь стены",
                numerator_entity="окна",
                denominator_entity="стена",
            ),
            extraction_text="Площадь окон должна составлять не менее 20 % площади стены.",
        ),
    )
    assert plan.planner_status == "unsupported"
    assert "ratio_basis_not_supported" in plan.params["blocked_reasons"]


async def test_healthy_minimum_distance_stays_executable():
    plan = await CheckPlanPlanner().plan(
        "r",
        _distance(
            "Резервуар",
            "Общественное здание",
            100,
            "Резервуары следует размещать на расстоянии не менее 100 м от общественных зданий",
        ),
    )
    assert plan.planner_status == "auto"
    assert plan.params["distance_m"] == 100


async def test_healthy_maximum_distance_stays_executable():
    plan = await CheckPlanPlanner().plan(
        "r",
        _distance(
            "детские дома-интернаты для детей-сирот",
            "общеобразовательные школы",
            1,
            "Расстояние от детских домов для детей-сирот до школ не более 1 км",
            operator="<=",
            unit="км",
        ),
    )
    assert plan.planner_status == "auto"
    assert plan.template == "presence_within"
    assert plan.params["distance_m"] == 1000


@pytest.mark.parametrize(
    "text,operator,expected",
    [
        ("не более 500 м", ">=", True),
        ("не менее 500 м", "<=", True),
        ("не менее 500 м", ">=", False),
        ("не менее 50 м и не более 100 м", ">=", False),  # a range: undecided here
        ("от 50 м, но не более 100 м", ">=", True),
        ("500 м", ">=", False),
    ],
)
def test_direction_conflict(text, operator, expected):
    assert direction_conflict(text, operator) is expected


def test_precision_reasons_for_labels():
    assert precision_reasons("не менее 20 м", labels=("Жилой дом", "стеллаж")) == [
        "non_territorial_entity"
    ]
