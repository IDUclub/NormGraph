"""NormSpec → CheckPlan compilation: every template and every refusal."""

import pytest

from src.dto.check_plan import validate_check_plan
from src.pipeline.norm_spec import NormSpec, SpecCompiler, numbers_in, render_plan
from tests.unit._catalog import CATALOG

HOUSE = {"entity": "Жилой дом", "entity_type": "physical_object"}
SCHOOL = {"entity": "Школа", "entity_type": "service"}
RESIDENTIAL = {"entity": "residential", "entity_type": "functional_zone"}


def compile_(spec: dict, text: str):
    return SpecCompiler(CATALOG).compile(
        NormSpec.model_validate({"territorial": True, "unconditional": True, **spec}),
        restriction_id="r",
        source_text=text,
    )


def test_min_distance():
    plan, reasons = compile_(
        dict(
            template="min_distance",
            checked=HOUSE,
            other={
                "entity": "Автозаправочная станция",
                "entity_type": "physical_object",
            },
            operator=">=",
            value=50,
            unit="м",
        ),
        "Расстояние от АЗС до жилых домов — не менее 50 м.",
    )
    assert reasons == []
    assert plan.template == "distance_from_source"
    assert plan.params["distance_m"] == 50
    layers = {item.role: item.entity for item in plan.declared_requirements.layers}
    assert layers == {"source": "Автозаправочная станция", "targets": "Жилой дом"}
    assert "не менее 50 м" in render_plan(plan)


def test_prohibited_within_needs_no_value():
    plan, reasons = compile_(
        dict(
            template="prohibited_within",
            checked=HOUSE,
            other={"entity": "industrial", "entity_type": "functional_zone"},
        ),
        "Размещение жилых домов в промышленной зоне не допускается.",
    )
    assert reasons == []
    assert plan.params["geometry_mode"] == "source_geometry"


def test_max_distance_in_km():
    plan, reasons = compile_(
        dict(
            template="max_distance",
            checked=HOUSE,
            other=SCHOOL,
            operator="<=",
            value=1,
            unit="км",
        ),
        "Расстояние от жилых домов до школ не более 1 км.",
    )
    assert reasons == []
    assert plan.template == "presence_within"
    assert plan.params["distance_m"] == 1000


def test_accessibility_in_minutes():
    plan, reasons = compile_(
        dict(
            template="accessibility",
            checked=HOUSE,
            other=SCHOOL,
            operator="<=",
            value=15,
            unit="мин",
        ),
        "Пешеходная доступность школ — не более 15 мин.",
    )
    assert reasons == []
    assert plan.template == "accessibility_within"
    assert plan.params["limit"] == {"kind": "time", "minutes": 15}
    assert plan.params["measurement"] == "buffer_v1"


def test_attribute_limit_floors_and_height():
    floors, reasons = compile_(
        dict(
            template="attribute_limit",
            checked=HOUSE,
            attribute="floors",
            operator="<=",
            value=9,
            unit="эт",
        ),
        "Этажность жилых домов — не более 9 этажей.",
    )
    assert reasons == []
    assert floors.template == "object_attribute_threshold"
    assert floors.params["threshold"] == 9 and floors.params["unit"] == "floors"
    height, reasons = compile_(
        dict(
            template="attribute_limit",
            checked=HOUSE,
            attribute="height",
            operator="<=",
            value=28,
            unit="м",
        ),
        "Высота жилых зданий — не более 28 м.",
    )
    assert reasons == []
    accepts = height.declared_requirements.attributes[0].accepts
    assert accepts[-1].derive == "floors_to_height_v1"


def test_zone_attribute_limit_and_share():
    plan, reasons = compile_(
        dict(
            template="zone_attribute_limit",
            checked=HOUSE,
            other=RESIDENTIAL,
            attribute="floors",
            operator="<=",
            value=4,
            unit="этажей",
        ),
        "В жилой зоне этажность — не более 4 этажей.",
    )
    assert reasons == [] and plan.template == "zonal_attribute_threshold"
    share, reasons = compile_(
        dict(
            template="zone_share",
            checked=HOUSE,
            other=RESIDENTIAL,
            operator="<=",
            value=40,
            unit="%",
        ),
        "Процент застройки в жилой зоне — не более 40 %.",
    )
    assert reasons == [] and share.template == "zonal_ratio"
    assert share.declared_requirements.layers[1].geometry_types == [
        "Polygon",
        "MultiPolygon",
    ]


def test_provision_per_thousand():
    plan, reasons = compile_(
        dict(
            template="provision",
            other=SCHOOL,
            operator=">=",
            value=124,
            unit="мест на 1000 жителей",
            accessibility_value=500,
            accessibility_unit="м",
        ),
        "Обеспеченность школами — 124 места на 1000 жителей, радиус доступности 500 м.",
    )
    assert reasons == []
    assert plan.template == "service_provision"
    assert plan.params["capacity_per_1000"] == 124
    assert plan.params["accessibility"] == {"kind": "distance", "meters": 500}


@pytest.mark.parametrize(
    "spec,text,residents",
    [
        (
            dict(provision_basis="residents_per_object", value=10, unit="тыс. жителей"),
            "Поликлиники — 1 объект на 10 тыс. жителей, доступность 30 мин.",
            10_000,
        ),
        (
            dict(value=5000, unit="жителей"),
            "Аптеки — 1 объект на 5000 жителей, доступность 30 мин.",
            5000,
        ),
        (
            dict(value=20, unit="тыс. человек", objects_count=2),
            "Не менее 2 объектов на 20 тыс. человек, доступность 30 мин.",
            10_000,
        ),
    ],
)
def test_provision_objects_per_residents(spec, text, residents):
    plan, reasons = compile_(
        dict(
            template="provision",
            other=SCHOOL,
            operator=">=",
            accessibility_value=30,
            accessibility_unit="мин",
            **spec,
        ),
        text,
    )
    assert reasons == []
    assert plan.params["residents_per_service"] == residents
    assert plan.params.get("capacity_per_1000") is None
    assert f"1 объект на {residents:g} жителей" in render_plan(plan)


def test_provision_objects_count_must_be_in_the_source():
    _, reasons = compile_(
        dict(
            template="provision",
            other=SCHOOL,
            value=10,
            unit="тыс. жителей",
            objects_count=3,
        ),
        "1 объект на 10 тыс. жителей",
    )
    assert reasons == ["value_not_in_source"]


def test_provision_has_one_capacity_basis():
    with pytest.raises(ValueError):
        validate_check_plan(
            dict(
                schema_version="1.0",
                template="service_provision",
                template_version=1,
                params=dict(
                    services_layer="services",
                    capacity_per_1000=10,
                    residents_per_service=1000,
                ),
                declared_requirements=dict(
                    layers=[dict(role="services", **SCHOOL)], attributes=[]
                ),
                source=dict(restriction_id="r"),
                planner_status="auto",
            )
        )


@pytest.mark.parametrize(
    "update,text,reason",
    [
        (
            {"territorial": False},
            "Опалубка должна быть инвентарной.",
            "rewrite_not_territorial",
        ),
        (
            {"unconditional": False},
            "При этажности более 9 — не менее 50 м.",
            "applicability_not_verified",
        ),
        ({"value": 70}, "не менее 50 м", "value_not_in_source"),
        (
            {
                "other": {
                    "entity": "Котельная установка",
                    "entity_type": "physical_object",
                }
            },
            "не менее 50 м",
            "entity_not_in_catalog",
        ),
        (
            {"other": {"entity": "Школа", "entity_type": "functional_zone"}},
            "не менее 50 м",
            "entity_not_in_catalog",
        ),
        ({"operator": "<="}, "не менее 50 м", "operator_direction_conflict"),
        ({"unit": "мм"}, "не менее 50 мм", "measurement_unit_mismatch"),
        ({"value": 2, "unit": "м"}, "не менее 2 м", "distance_below_territorial_scale"),
        ({"other": HOUSE}, "не менее 50 м", "same_entity_spacing_not_supported"),
        ({}, "Гидранты размещать через каждые 50 м", "periodic_spacing_not_supported"),
    ],
)
def test_refusals(update, text, reason):
    spec = dict(
        template="min_distance",
        checked=HOUSE,
        other=SCHOOL,
        operator=">=",
        value=50,
        unit="м",
        territorial=True,
        unconditional=True,
    )
    spec.update(update)
    plan, reasons = SpecCompiler(CATALOG).compile(
        NormSpec.model_validate(spec), restriction_id="r", source_text=text
    )
    assert plan is None
    assert reason in reasons


def test_compiled_plans_are_valid_contract_plans():
    plan, _ = compile_(
        dict(
            template="max_distance",
            checked=HOUSE,
            other=SCHOOL,
            operator="<=",
            value=500,
            unit="м",
        ),
        "не более 500 м",
    )
    validate_check_plan(plan.model_dump(mode="json"))


def test_numbers_in_text_handle_decimal_comma_and_thousands():
    assert {1.5, 1000.0} <= numbers_in("не менее 1,5 м и не более 1 000 м")


def test_service_accessibility_checks_the_residents():
    plan, reasons = compile_(
        dict(
            template="accessibility",
            checked={"entity": "школа", "entity_type": "service"},
            operator="<=",
            value=15,
            unit="мин",
        ),
        "Максимально допустимый уровень территориальной доступности школ — 15 мин.",
    )
    assert reasons == []
    layers = {item.role: item.entity for item in plan.declared_requirements.layers}
    assert layers == {"objects": "Жилой дом", "neighbors": "Школа"}


def test_transport_accessibility_is_not_a_walking_buffer():
    plan, reasons = compile_(
        dict(
            template="accessibility",
            checked=HOUSE,
            other=SCHOOL,
            operator="<=",
            value=30,
            unit="мин",
            quote="не более 30 минут транспортной доступности",
        ),
        "не более 30 минут транспортной доступности",
    )
    assert plan is None and reasons == ["transport_accessibility_not_supported"]


def test_unique_name_under_the_other_object_type_is_grounded():
    plan, reasons = compile_(
        dict(
            template="max_distance",
            checked=HOUSE,
            other={"entity": "Поликлиника", "entity_type": "physical_object"},
            operator="<=",
            value=1,
            unit="км",
        ),
        "не более 1 км",
    )
    assert reasons == []
    assert plan.declared_requirements.layers[1].entity_type == "service"
    # «Парк» is both a service and a physical object: the stated type decides.
    plan, _ = compile_(
        dict(
            template="max_distance",
            checked=HOUSE,
            other={"entity": "Парк", "entity_type": "physical_object"},
            operator="<=",
            value=1,
            unit="км",
        ),
        "не более 1 км",
    )
    assert plan.declared_requirements.layers[1].entity_type == "physical_object"
