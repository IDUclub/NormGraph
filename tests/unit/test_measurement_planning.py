"""Regression cases for provision, area ratios and invalid extracted entities."""

from unittest.mock import AsyncMock

import pytest

from src.pipeline.check_plan_backfill import CheckPlanBackfillService
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)
from src.pipeline.service import _restriction_id

PARKING_SUBJECT = (
    "минимально допустимая обеспеченность населения, проживающего в многоквартирных "
    "жилых домах, закрытыми и открытыми автостоянками для постоянного хранения "
    "индивидуальных легковых автомобилей, размещаемыми на территории населенного пункта"
)


def parking():
    return ExtractedRestriction(
        subject=PARKING_SUBJECT,
        object="обеспеченность населения",
        kind="минимальная_доля_показателя",
        value=RestrictionValue(
            operator=">=",
            number=90,
            unit="%",
            condition="расчетного показателя уровня автомобилизации населения муниципальных образований Ленинградской области",
        ),
        extraction_text="должна быть не менее 90% расчетного показателя уровня автомобилизации населения муниципальных образований Ленинградской области",
    )


async def test_actual_failed_parking_norm_is_retained_without_truncation_or_llm():
    ex = parking()
    llm = AsyncMock()
    plan = await CheckPlanPlanner(llm).plan(
        "883735ffd617ea249c7c9b94f31ebf36efc5e0f7", ex
    )
    assert plan.planner_status == "unsupported"
    assert set(plan.params["blocked_reasons"]) == {
        "non_spatial_entity",
        "entity_label_too_long",
        "ratio_basis_not_supported",
        "applicability_not_verified",
    }
    assert plan.params["candidate_plan"] is None
    assert plan.params["condition"] == ex.value.condition
    assert ex.subject == PARKING_SUBJECT and len(ex.subject) > 200
    assert plan.source.extraction_text == ex.extraction_text
    llm.complete.assert_not_awaited()


@pytest.mark.parametrize("kind", ["provision", "count_share", "area_share"])
async def test_parking_percentage_cannot_be_misclassified_as_area_even_with_short_labels(
    kind,
):
    ex = parking().model_copy(
        update={
            "subject": "микрорайон",
            "object": "автостоянки",
            "value": RestrictionValue(operator=">=", number=90, unit="%"),
            "measurement": RestrictionMeasurement(
                kind=kind,
                indicator="обеспеченность автостоянками",
                basis="площадь территории",
                numerator_entity="автостоянки",
                denominator_entity="микрорайон",
            ),
        }
    )
    llm = AsyncMock()
    plan = await CheckPlanPlanner(llm).plan("r", ex)
    assert plan.planner_status == "unsupported"
    assert "ratio_basis_not_supported" in plan.params["blocked_reasons"]
    assert plan.params["measurement"]["indicator"] == "обеспеченность автостоянками"
    llm.complete.assert_not_awaited()


@pytest.mark.parametrize(
    "kind", ["минимальная_ширина", "предельная_высота", "минимальная_длина"]
)
async def test_linear_sizes_are_not_spatial_buffers(kind):
    ex = ExtractedRestriction(
        subject="участок",
        object="проезд",
        kind=kind,
        value=RestrictionValue(operator=">=", number=3.5, unit="м"),
    )
    plan = await CheckPlanPlanner().plan("r", ex)
    assert plan.planner_status == "unsupported"
    assert "linear_size_not_distance" in plan.params["blocked_reasons"]


def area():
    return ExtractedRestriction(
        subject="микрорайон",
        object="озелененная территория",
        kind="минимальная_доля_площади",
        value=RestrictionValue(operator=">=", number=25, unit="%"),
        extraction_text="Площадь озелененной территории составляет не менее 25% площади микрорайона.",
        measurement=RestrictionMeasurement(
            kind="area_share",
            indicator="доля площади озеленения",
            basis="площади микрорайона",
            numerator_entity="озелененная территория",
            denominator_entity="микрорайон",
        ),
    )


@pytest.mark.parametrize(
    "update",
    [
        {"basis": None},
        {"numerator_entity": None},
        {"denominator_entity": None},
        {"denominator_entity": "озелененная территория"},
    ],
)
async def test_area_ratio_requires_an_unambiguous_basis(update):
    ex = area()
    ex.measurement = ex.measurement.model_copy(update=update)
    plan = await CheckPlanPlanner().plan("r", ex)
    assert "ratio_basis_not_supported" in plan.params["blocked_reasons"]


async def test_old_percentage_without_measurement_is_not_guessed_from_kind():
    ex = area().model_copy(update={"measurement": None})
    plan = await CheckPlanPlanner().plan("r", ex)
    assert plan.planner_status == "unsupported"


@pytest.mark.parametrize(
    "update", [{"number": 101}, {"operator": "approximately"}, {"operator": None}]
)
async def test_invalid_deterministic_parameters_are_nonfatal(update):
    ex = area()
    ex.value = ex.value.model_copy(update=update)
    plan = await CheckPlanPlanner().plan("r", ex)
    assert plan.planner_status == "unsupported"
    assert "invalid_plan_parameters" in plan.params["blocked_reasons"]


async def test_persisted_measurement_produces_the_same_plan_on_regeneration():
    ex = area()
    row = dict(
        subject=ex.subject,
        object=ex.object,
        kind=ex.kind,
        extraction_text=ex.extraction_text,
        measurement_json=ex.measurement.model_dump_json(),
        **ex.value.to_props(),
    )
    restored = CheckPlanBackfillService._as_extracted(row)
    assert restored == ex
    planner = CheckPlanPlanner()
    assert await planner.plan("r", restored) == await planner.plan("r", ex)


async def test_corrupt_measurement_is_not_treated_as_legacy_distance():
    ex = CheckPlanBackfillService._as_extracted(
        dict(
            subject="завод",
            object="дом",
            kind="расстояние",
            value_operator=">=",
            value_number=50,
            value_unit="м",
            measurement_json="invalid",
        )
    )
    plan = await CheckPlanPlanner().plan("r", ex)
    assert plan.planner_status == "unsupported"


def test_different_calculation_bases_have_distinct_stable_restriction_ids():
    ex = area()
    m2 = ex.measurement.model_copy(
        update={"basis": "площадь застроенной части микрорайона"}
    )
    args = ("c", ex.subject, ex.object, ex.kind, ex.value)
    assert _restriction_id(*args, ex.measurement) != _restriction_id(*args, m2)
    assert _restriction_id(*args, ex.measurement) == _restriction_id(
        *args, ex.measurement.model_copy()
    )
    assert _restriction_id(*args, ex.measurement) == _restriction_id(
        *args,
        ex.measurement.model_copy(update={"indicator": "переименованный показатель"}),
    )


@pytest.mark.parametrize(
    "value", [None, RestrictionValue(number=25, unit="м"), RestrictionValue(unit="%")]
)
async def test_area_measurement_requires_a_percentage_and_numeric_threshold(value):
    ex = area().model_copy(update={"value": value})
    plan = await CheckPlanPlanner().plan("r", ex)
    assert plan.planner_status == "unsupported"
    assert "measurement_unit_mismatch" in plan.params["blocked_reasons"]


@pytest.mark.parametrize(
    "subject,object_,text,reason",
    [
        (
            "расчетный радиус",
            "значение радиуса",
            "Расчетный радиус не превышает 400 м.",
            "non_spatial_entity",
        ),
        (
            "населенный пункт",
            "максимально допустимый уровень территориальной доступности",
            "Не менее 500 м.",
            "non_spatial_entity",
        ),
        (
            "разворотные площадки автобусов",
            "радиус разворота",
            "Радиус разворота не менее 15 м.",
            "linear_size_not_distance",
        ),
        (
            "площадка",
            "автобусы",
            "Радиус разворота не менее 15 м.",
            "linear_size_not_distance",
        ),
        (
            "однополосные проезды",
            "разъездные площадки",
            "Площадки не более 75 м одна от другой.",
            "same_entity_spacing_not_supported",
        ),
        (
            "территория парка",
            "автостоянки",
            "Стоянки не далее 400 м от входа в парк.",
            "specific_geometry_required",
        ),
    ],
)
@pytest.mark.parametrize("measurement_kind", [None, "distance"])
async def test_live_invalid_spatial_plans_are_blocked_before_llm(
    subject, object_, text, reason, measurement_kind
):
    ex = ExtractedRestriction(
        subject=subject,
        object=object_,
        kind="расстояние",
        value=RestrictionValue(operator="<=", number=400, unit="м"),
        extraction_text=text,
        measurement=(
            RestrictionMeasurement(kind=measurement_kind) if measurement_kind else None
        ),
    )
    llm = AsyncMock()
    plan = await CheckPlanPlanner(llm).plan("r", ex)
    assert plan.planner_status == "unsupported"
    assert reason in plan.params["blocked_reasons"]
    assert plan.params["candidate_plan"] is None
    assert plan.source.extraction_text == text
    llm.complete.assert_not_awaited()


@pytest.mark.parametrize(
    "entity", ["расчетный радиус", "уровень доступности", "Другая территория"]
)
async def test_llm_cannot_invent_layers_or_use_indicators_as_geometry(entity):
    import json

    base = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="Парк",
            object="Жилой дом",
            kind="расстояние",
            value=RestrictionValue(operator=">=", number=50, unit="м"),
        ),
    )
    payload = base.model_dump(mode="json")
    payload["declared_requirements"]["layers"][0]["entity"] = entity
    llm = AsyncMock()
    llm.complete.return_value = json.dumps(payload)
    plan = await CheckPlanPlanner(llm).plan(
        "r",
        ExtractedRestriction(subject="Парк", object="Жилой дом", kind="неизвестное"),
    )
    assert plan.planner_status == "unsupported"
    llm.complete.assert_awaited_once()


async def test_actual_building_label_is_not_confused_with_height_indicator():
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="Промышленное предприятие",
            object="Высотные жилые здания",
            kind="минимальное_расстояние",
            value=RestrictionValue(operator=">=", number=50, unit="м"),
        ),
    )
    assert plan.template == "distance_from_source"
    assert plan.planner_status == "auto"
