"""NormSpec: the normalized reading of a norm, and its deterministic compilation.

The LLM never writes a CheckPlan. It reads the clause and fills a small, closed
``NormSpec``; this module turns the spec into a plan and refuses whenever the spec
cannot be grounded:

* every entity must be a canonical Urban API type (``UrbanCatalog``);
* the number must occur in the source clause;
* the operator must match the template (a minimum distance is ``>=``);
* the precision guards of ``norm_guards`` must pass.

A clause with conditions or case-dependent values compiles every stated variant and
keeps the strictest one for all objects; the plan records this in ``applicability``.

A refused spec yields ``(None, reasons)`` — never a guessed plan.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.dto.check_plan import (
    CheckPlan,
    CheckPlanApplicability,
    validate_check_plan,
)
from src.pipeline.norm_guards import precision_reasons
from src.pipeline.urban_catalog import ALL_ZONES, UrbanCatalog

SpecTemplate = Literal[
    "min_distance",
    "prohibited_within",
    "max_distance",
    "accessibility",
    "attribute_limit",
    "zone_attribute_limit",
    "zone_share",
    "provision",
    "none",
]
Operator = Literal["<", "<=", ">", ">=", "=="]


class SpecEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity: str = Field(min_length=1, max_length=200)
    entity_type: Literal["service", "physical_object", "functional_zone"]


class SpecVariant(BaseModel):
    """One case-dependent value of a clause; unset fields keep the main spec's."""

    model_config = ConfigDict(extra="ignore")

    value: float | None = None
    unit: str | None = None
    objects_count: float | None = Field(default=None, gt=0)
    accessibility_value: float | None = None
    accessibility_unit: str | None = None
    condition: str | None = Field(default=None, max_length=500)

    @field_validator("value", "accessibility_value", "objects_count", mode="before")
    @classmethod
    def _decimal_comma(cls, value):
        if isinstance(value, str):
            value = value.replace(",", ".").strip() or None
        return value

    def overrides(self) -> dict[str, Any]:
        return self.model_dump(exclude={"condition"}, exclude_none=True)


class NormSpec(BaseModel):
    """What the norm checks, in the vocabulary of the executable templates."""

    model_config = ConfigDict(extra="ignore")

    territorial: bool = False
    template: SpecTemplate = "none"
    # The objects whose compliance is decided (buildings, plots, ...).
    checked: SpecEntity | None = None
    # The other side: distance source, neighbour, zone, area numerator or service.
    other: SpecEntity | None = None
    operator: Operator | None = None
    value: float | None = None
    unit: str | None = None
    attribute: Literal["floors", "height", "building_area", "area"] | None = None
    accessibility_value: float | None = None
    accessibility_unit: str | None = None
    # provision: "places_per_1000" — value places per 1000 residents;
    # "residents_per_object" — objects_count objects per value residents.
    provision_basis: Literal["places_per_1000", "residents_per_object"] | None = None
    objects_count: float | None = Field(default=None, gt=0)
    # True only when the clause states no condition, exception or case split
    # that the plan cannot represent.
    unconditional: bool = False
    # Conditions and exceptions of a conditional clause, and its case-dependent values.
    conditions: list[str] = Field(default_factory=list, max_length=20)
    variants: list[SpecVariant] = Field(default_factory=list, max_length=20)
    quote: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("operator", mode="before")
    @classmethod
    def _single_equals(cls, value):
        return "==" if value == "=" else value

    @field_validator("value", "accessibility_value", "objects_count", mode="before")
    @classmethod
    def _decimal_comma(cls, value):
        if isinstance(value, str):
            value = value.replace(",", ".").strip() or None
        return value


_LENGTH_UNITS = {
    **dict.fromkeys(("м", "m", "метр", "метра", "метров", "м."), 1.0),
    **dict.fromkeys(("км", "km", "километр", "километра", "километров"), 1000.0),
}
_TIME_UNITS = {
    **dict.fromkeys(("мин", "min", "минут", "минуты", "минута", "мин."), 1.0),
    **dict.fromkeys(("ч", "час", "часа", "часов", "h"), 60.0),
}
_AREA_UNITS = {
    **dict.fromkeys(("м2", "м²", "кв. м", "кв.м", "m2", "кв м"), 1.0),
    **dict.fromkeys(("га", "ha", "гектар", "гектаров"), 10_000.0),
}
_FLOOR_UNITS = {"эт", "эт.", "этаж", "этажа", "этажей", "floors"}
_PERCENT_UNITS = {"%", "процент", "процента", "процентов"}

# Attribute candidates in Urban API physical objects, most direct first.
_ATTRIBUTES: dict[str, tuple[str, list[dict[str, str]]]] = {
    "floors": (
        "floors",
        [dict(field="building.floors", unit="floors", quality="direct")],
    ),
    "height": (
        "m",
        [
            dict(field="building.height", unit="m", quality="direct"),
            dict(
                field="building.floors",
                unit="m",
                derive="floors_to_height_v1",
                quality="derived",
            ),
        ],
    ),
    "building_area": (
        "m2",
        [
            dict(field="building.building_area_official", unit="m2", quality="direct"),
            dict(field="building.building_area_modeled", unit="m2", quality="direct"),
            dict(field="properties.building_area", unit="m2", quality="direct"),
            dict(
                field="geometry",
                unit="m2",
                derive="geometry_area_m2_v1",
                quality="derived",
            ),
        ],
    ),
    "area": (
        "m2",
        [
            dict(
                field="geometry",
                unit="m2",
                derive="geometry_area_m2_v1",
                quality="derived",
            )
        ],
    ),
}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

# Walking accessibility measured by a straight-line buffer (``buffer_v1``): 4.8 km/h,
# with routes assumed 1.3 times longer than the straight line.
WALKING_SPEED_M_PER_MIN = 80.0
DETOUR_FACTOR = 1.3

_TRANSPORT = re.compile(r"транспортн|автомобил|общественн\w*\s+транспорт", re.I)

_PROVISION_TABLE = re.compile(
    r"на\s*1\s*000\s*(?:чел|жител|насел)|уровн\w*\s+обеспеченност", re.I
)


_PER_CAPITA = re.compile(
    r"(?:в\s+расч[её]те\s+)?на\s+(?:одн\w+|1)\s+"
    r"(?:мест|человек|чел\b|ребен|ребён|жител|учащ|воспитан|осужд|койк)",
    re.I,
)


def _is_residence(entity: SpecEntity | None) -> bool:
    if entity is None:
        return False
    name = entity.entity.casefold()
    return name.startswith("жил") or name == "residential"


# Where residents live: the checked side of service accessibility norms.
RESIDENTIAL_BUILDING = "Жилой дом"


def _unit(value: str | None) -> str:
    return " ".join((value or "").casefold().replace("ё", "е").split())


def numbers_in(text: str) -> set[float]:
    """Every number written in ``text`` (decimal comma or point, thin spaces joined)."""
    joined = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", text or "")
    return {float(item.replace(",", ".")) for item in _NUMBER.findall(joined)}


def _grounded(value: float, text: str) -> bool:
    return any(math.isclose(value, item, rel_tol=1e-9) for item in numbers_in(text))


def _geometry(entity_type: str) -> list[str]:
    if entity_type == "functional_zone":
        return ["Polygon", "MultiPolygon"]
    return ["Point", "MultiPoint", "Polygon", "MultiPolygon"]


def _layer(role: str, name: str, entity_type: str, *, polygons=False) -> dict:
    return dict(
        role=role,
        entity=name,
        entity_type=entity_type,
        geometry_types=(
            ["Polygon", "MultiPolygon"] if polygons else _geometry(entity_type)
        ),
        required=True,
    )


def plan_fingerprint(plan: CheckPlan) -> str:
    """Identity of what a plan checks, for agreement between independent rewrites."""
    params = {k: v for k, v in plan.params.items() if k != "result_mode"}
    layers = sorted(
        (item.role, item.entity, item.entity_type)
        for item in (
            plan.declared_requirements.layers if plan.declared_requirements else []
        )
    )
    return json.dumps(
        [plan.template, params, layers], ensure_ascii=False, sort_keys=True
    )


class SpecCompiler:
    """Compile a ``NormSpec`` into a validated ``auto`` CheckPlan, or refuse."""

    def __init__(self, catalog: UrbanCatalog, *, min_distance_m: float = 3.0) -> None:
        self.catalog = catalog
        self.min_distance_m = min_distance_m

    def compile(
        self,
        spec: NormSpec,
        *,
        restriction_id: str,
        source_text: str,
        source: dict[str, Any] | None = None,
    ) -> tuple[CheckPlan | None, list[str]]:
        if not spec.territorial:
            return None, ["rewrite_not_territorial"]
        if spec.template == "none":
            return None, ["rewrite_no_template"]
        kwargs = dict(
            restriction_id=restriction_id, source_text=source_text, source=source
        )
        if spec.unconditional:
            return self._compile_one(spec, **kwargs)
        # Conditions or case-dependent values: the strictest value applies to every
        # object. Each variant must compile on its own, so none is guessed.
        candidates = [spec] + [
            spec.model_copy(update=variant.overrides()) for variant in spec.variants
        ]
        plans = []
        for candidate in candidates:
            plan, reasons = self._compile_one(candidate, **kwargs)
            if plan is None:
                return None, reasons
            plans.append(plan)
        keys = [_strictness(plan) for plan in plans]
        if any(key is None for key in keys) or len({key[0] for key in keys}) > 1:
            return None, ["variants_not_comparable"]
        best = min(range(len(plans)), key=lambda index: keys[index][1])
        plan = _tightest_accessibility(plans[best], plans)
        applied = _variant_text(candidates[best], None)
        if plan is not plans[best]:
            applied += "; доступность — самая строгая из вариантов"
        conditions = [item.strip() for item in spec.conditions if item.strip()]
        conditions = conditions or [
            variant.condition.strip()
            for variant in spec.variants
            if variant.condition and variant.condition.strip()
        ]
        conditions = conditions or [spec.reason or "условия пункта не перечислены"]
        variants = list(
            dict.fromkeys(
                _variant_text(candidate, variant.condition)
                for candidate, variant in zip(candidates[1:], spec.variants)
            )
        )
        applicability = CheckPlanApplicability(
            mode="strictest_variant",
            conditions=[item[:500] for item in conditions[:20]],
            variants=[item[:500] for item in variants[:20]],
            applied=applied[:500],
        )
        return plan.model_copy(update={"applicability": applicability}), []

    def _compile_one(
        self,
        spec: NormSpec,
        *,
        restriction_id: str,
        source_text: str,
        source: dict[str, Any] | None,
    ) -> tuple[CheckPlan | None, list[str]]:
        reasons: list[str] = []
        if _TRANSPORT.search(
            " ".join([spec.quote or "", *(source or {}).get("labels", ())])
        ) and (
            spec.template == "accessibility"
            or spec.template == "provision"
            and spec.accessibility_value is not None
        ):
            # buffer_v1 models walking only; the extracted triple may name the mode
            # even when the quoted table row does not.
            reasons.append("transport_accessibility_not_supported")
        if spec.template == "provision" and spec.other is None:
            # The service is the only entity of a provision norm, whichever slot holds it.
            spec = spec.model_copy(update={"other": spec.checked, "checked": None})
        if (
            spec.template in {"accessibility", "max_distance"}
            and spec.other is None
            and spec.checked is not None
            and spec.checked.entity_type == "service"
        ):
            # «Территориальная доступность» of a service is the residents' access to
            # it: the residential buildings are checked, the service is the neighbour.
            spec = spec.model_copy(
                update={
                    "other": spec.checked,
                    "checked": SpecEntity(
                        entity=RESIDENTIAL_BUILDING, entity_type="physical_object"
                    ),
                }
            )
        if (
            spec.template in {"accessibility", "max_distance"}
            and _is_residence(spec.other)
            and not _is_residence(spec.checked)
        ):
            # «От школ до жилых зданий не более 500 м» is the residents' access: the
            # dwellings are checked, whichever way the sentence runs.
            spec = spec.model_copy(
                update={"checked": spec.other, "other": spec.checked}
            )
        if spec.template in {"attribute_limit", "zone_attribute_limit"} and (
            _PROVISION_TABLE.search(source_text)
        ):
            # «50 кв. м общей площади на 1000 человек» is a provision norm, not the
            # size of each object.
            reasons.append("provision_norm_not_attribute")
        quote_text = spec.quote if spec.quote and spec.quote in source_text else ""
        if spec.template in {"attribute_limit", "zone_attribute_limit"} and (
            _PER_CAPITA.search(quote_text)
        ):
            # «7 м2 в расчёте на одно место» scales with places, not a fixed size.
            reasons.append("per_capita_norm_not_attribute")
        if (
            spec.value is not None
            and re.search(r"превыша|больше|выше|меньше|ниже", quote_text, re.I)
            and re.search(
                rf"\bна\s+{re.escape(f'{spec.value:g}')}(?:[.,]0)?\s*(?:этаж|м\b|%|процент)",
                quote_text,
                re.I,
            )
        ):
            # «не может превышать предельную этажность … на 3 этажа» is an increment
            # over another norm, not the limit itself.
            reasons.append("relative_value_not_supported")
        if (
            spec.template == "min_distance"
            and (_is_residence(spec.checked) or _is_residence(spec.other))
            and re.search(r"доступност", source_text, re.I)
        ):
            # A distance to dwellings in an accessibility norm is an upper bound.
            reasons.append("accessibility_not_min_distance")

        def ground(item: SpecEntity | None, *, role: str) -> tuple[str, str] | None:
            if item is None:
                reasons.append(f"rewrite_missing_{role}")
                return None
            entry = self.catalog.resolve(item.entity, item.entity_type)
            if entry is None and item.entity_type != "functional_zone":
                # A unique name under the other object type («Парк» is a service).
                fallback = self.catalog.resolve(item.entity)
                if fallback is not None and fallback.entity_type != "functional_zone":
                    entry = fallback
            if entry is None:
                reasons.append("entity_not_in_catalog")
                return None
            return entry.name, entry.entity_type

        checked = (
            None
            if spec.template == "provision"
            else ground(spec.checked, role="checked")
        )
        other = (
            None
            if spec.template in {"attribute_limit"}
            else ground(spec.other, role="other")
        )
        if (
            checked
            and other
            and checked == other
            and spec.template not in {"zone_attribute_limit", "zone_share"}
        ):
            reasons.append("same_entity_spacing_not_supported")

        quote = spec.quote if spec.quote and spec.quote in source_text else source_text
        unit = _unit(spec.unit)
        value = spec.value
        if spec.template != "prohibited_within":
            if value is None or not math.isfinite(value):
                reasons.append("rewrite_missing_value")
            elif not _grounded(value, source_text):
                reasons.append("value_not_in_source")
        if spec.objects_count not in {None, 1} and not _grounded(
            spec.objects_count, source_text
        ):
            reasons.append("value_not_in_source")
        if reasons:
            return None, list(dict.fromkeys(reasons))

        build = getattr(self, f"_{spec.template}")
        try:
            params, layers, attributes, guard_args = build(
                spec, checked, other, unit, value
            )
        except _Refused as refused:
            return None, list(dict.fromkeys(refused.reasons))
        reasons += precision_reasons(
            quote,
            operator=spec.operator,
            labels=tuple(
                name for name in (checked and checked[0], other and other[0]) if name
            ),
            min_distance_m=self.min_distance_m,
            **guard_args,
        )
        if reasons:
            return None, list(dict.fromkeys(reasons))
        try:
            plan = validate_check_plan(
                dict(
                    schema_version="1.0",
                    template=params.pop("template"),
                    template_version=1,
                    params={**params, "result_mode": "both"},
                    declared_requirements=dict(layers=layers, attributes=attributes),
                    source=dict(
                        restriction_id=restriction_id,
                        extraction_text=(source or {}).get("extraction_text"),
                    ),
                    planner_status="auto",
                )
            )
        except ValueError:
            return None, ["invalid_plan_parameters"]
        return plan, []

    # --- one builder per spec template ------------------------------------------------

    @staticmethod
    def _distance(unit: str, value: float) -> float:
        scale = _LENGTH_UNITS.get(unit)
        if scale is None:
            raise _Refused("measurement_unit_mismatch")
        distance = value * scale
        if not 0 < distance <= 100_000:
            raise _Refused("distance_out_of_range")
        return distance

    def _min_distance(self, spec, checked, other, unit, value):
        if spec.operator not in {">=", ">"}:
            raise _Refused("operator_direction_conflict")
        distance = self._distance(unit, value)
        return (
            dict(
                template="distance_from_source",
                source_layer="source",
                targets=["targets"],
                geometry_mode="buffered",
                distance_m=distance,
                predicate="intersects",
                violation_when="matched",
            ),
            [_layer("source", *other), _layer("targets", *checked)],
            [],
            dict(distance_m=distance),
        )

    def _prohibited_within(self, spec, checked, other, unit, value):
        return (
            dict(
                template="distance_from_source",
                source_layer="source",
                targets=["targets"],
                geometry_mode="source_geometry",
                predicate="intersects",
                violation_when="matched",
            ),
            [_layer("source", *other), _layer("targets", *checked)],
            [],
            {},
        )

    def _max_distance(self, spec, checked, other, unit, value):
        if spec.operator == "<":
            raise _Refused("strict_distance_not_supported")
        if spec.operator != "<=":
            raise _Refused("operator_direction_conflict")
        distance = self._distance(unit, value)
        return (
            dict(
                template="presence_within",
                objects_layer="objects",
                required_neighbor_layers=["neighbors"],
                distance_m=distance,
                minimum_neighbors=1,
            ),
            [_layer("objects", *checked), _layer("neighbors", *other)],
            [],
            dict(distance_m=distance),
        )

    def _accessibility(self, spec, checked, other, unit, value):
        if spec.operator not in {"<=", "<"}:
            raise _Refused("operator_direction_conflict")
        if unit in _TIME_UNITS:
            minutes = value * _TIME_UNITS[unit]
            if not 0 < minutes <= 240:
                raise _Refused("accessibility_out_of_range")
            limit, guard = dict(kind="time", minutes=minutes), {}
        else:
            meters = self._distance(unit, value)
            limit, guard = dict(kind="distance", meters=meters), dict(distance_m=meters)
        return (
            dict(
                template="accessibility_within",
                objects_layer="objects",
                required_neighbor_layers=["neighbors"],
                limit=limit,
                # Explicit, so the executor never substitutes its own defaults.
                speed_m_per_min=WALKING_SPEED_M_PER_MIN,
                detour_factor=DETOUR_FACTOR,
                measurement="buffer_v1",
                minimum_neighbors=1,
            ),
            [_layer("objects", *checked), _layer("neighbors", *other)],
            [],
            guard,
        )

    def _attribute(self, spec, unit, value) -> tuple[float, list[dict], str]:
        if spec.operator is None:
            raise _Refused("rewrite_missing_operator")
        attribute = spec.attribute
        if attribute is None:
            raise _Refused("rewrite_missing_attribute")
        threshold_unit, candidates = _ATTRIBUTES[attribute]
        if attribute == "floors":
            if unit not in _FLOOR_UNITS:
                raise _Refused("measurement_unit_mismatch")
            threshold = value
        elif attribute == "height":
            if unit not in _LENGTH_UNITS:
                raise _Refused("measurement_unit_mismatch")
            threshold = value * _LENGTH_UNITS[unit]
        else:
            if unit not in _AREA_UNITS:
                raise _Refused("measurement_unit_mismatch")
            threshold = value * _AREA_UNITS[unit]
        attribute_requirement = dict(
            role="attribute",
            on="objects",
            required=True,
            min_fill_rate=0,
            accepts=candidates,
        )
        return threshold, [attribute_requirement], threshold_unit

    def _attribute_limit(self, spec, checked, other, unit, value):
        if checked[1] == "functional_zone":
            raise _Refused("attribute_on_zone_not_supported")
        threshold, attributes, threshold_unit = self._attribute(spec, unit, value)
        return (
            dict(
                template="object_attribute_threshold",
                objects_layer="objects",
                attribute_role="attribute",
                operator=spec.operator,
                threshold=threshold,
                unit=threshold_unit,
            ),
            [_layer("objects", *checked)],
            attributes,
            {},
        )

    def _zone_attribute_limit(self, spec, checked, other, unit, value):
        if other[1] != "functional_zone" or checked[1] == "functional_zone":
            raise _Refused("zone_roles_not_verified")
        threshold, attributes, threshold_unit = self._attribute(spec, unit, value)
        return (
            dict(
                template="zonal_attribute_threshold",
                objects_layer="objects",
                zones_layer="zones",
                attribute_role="attribute",
                operator=spec.operator,
                threshold_source=dict(
                    kind="constant", value=threshold, unit=threshold_unit
                ),
                join_predicate="intersects",
            ),
            [_layer("objects", *checked), _layer("zones", *other)],
            attributes,
            {},
        )

    def _zone_share(self, spec, checked, other, unit, value):
        if unit not in _PERCENT_UNITS:
            raise _Refused("measurement_unit_mismatch")
        if spec.operator is None:
            raise _Refused("rewrite_missing_operator")
        if not 0 <= value <= 100:
            raise _Refused("ratio_out_of_range")
        if other[1] != "functional_zone" or checked[1] == "functional_zone":
            raise _Refused("ratio_basis_not_supported")
        return (
            dict(
                template="zonal_ratio",
                zones_layer="zones",
                numerator=dict(layer="numerator", measure="area"),
                denominator=dict(measure="zone_area"),
                operator=spec.operator,
                threshold=value,
                unit="%",
            ),
            [
                _layer("zones", *other),
                _layer("numerator", *checked, polygons=True),
            ],
            [],
            {},
        )

    def _provision(self, spec, checked, other, unit, value):
        if other[1] != "service":
            raise _Refused("provision_requires_service")
        if spec.operator not in {">=", ">", None}:
            raise _Refused("operator_direction_conflict")
        capacity = self._provision_capacity(spec, unit, value)
        accessibility = None
        if spec.accessibility_value is not None:
            access_unit = _unit(spec.accessibility_unit)
            if access_unit in _TIME_UNITS:
                accessibility = dict(
                    kind="time",
                    minutes=spec.accessibility_value * _TIME_UNITS[access_unit],
                )
            elif access_unit in _LENGTH_UNITS:
                accessibility = dict(
                    kind="distance",
                    meters=spec.accessibility_value * _LENGTH_UNITS[access_unit],
                )
            else:
                raise _Refused("measurement_unit_mismatch")
        return (
            dict(
                template="service_provision",
                services_layer="services",
                **capacity,
                accessibility=accessibility,
                min_provision=1.0,
            ),
            [_layer("services", *other)],
            [],
            {},
        )

    def _provision_capacity(self, spec, unit, value) -> dict:
        basis = spec.provision_basis or (
            "residents_per_object"
            if "мест" not in unit and re.match(r"(тыс\.?\s*)?(жител|чел|населен)", unit)
            else "places_per_1000"
        )
        if basis == "places_per_1000":
            if not re.search(r"1\s*000|тыс", unit):
                raise _Refused("measurement_unit_mismatch")
            return dict(capacity_per_1000=value)
        # «1 объект на N тыс. жителей»: value residents (thousands) per objects_count.
        if "мест" in unit or not re.search(r"жител|чел|населен", unit):
            raise _Refused("measurement_unit_mismatch")
        objects = spec.objects_count or 1.0
        scale = 1000.0 if "тыс" in unit else 1.0
        return dict(residents_per_service=value * scale / objects)


def _accessibility_meters(limit: dict | None) -> float:
    """A time or path limit as walking metres, to compare variants."""
    if limit.get("kind") == "time":
        return limit["minutes"] * WALKING_SPEED_M_PER_MIN
    return limit["meters"]


def _strictness(plan: CheckPlan) -> tuple[str, float] | None:
    """``(kind, key)``: the smaller key is the stricter plan; ``None`` if undefined."""
    p = plan.params

    def signed(operator: str, value: float | None) -> float | None:
        if value is None or operator == "==":
            return None
        return value if operator in {"<", "<="} else -value

    key: float | None
    if plan.template == "distance_from_source":
        if p.get("geometry_mode") == "source_geometry":
            kind, key = "prohibited", 0.0
        else:
            kind, key = "min_distance", -p["distance_m"]
    elif plan.template == "presence_within":
        kind, key = "max_distance", p["distance_m"]
    elif plan.template == "accessibility_within":
        kind, key = "accessibility", _accessibility_meters(p["limit"])
    elif plan.template == "object_attribute_threshold":
        kind = f"attribute{p['operator']}"
        key = signed(p["operator"], p["threshold"])
    elif plan.template == "zonal_attribute_threshold":
        kind = f"zone_attribute{p['operator']}"
        key = signed(p["operator"], (p.get("threshold_source") or {}).get("value"))
    elif plan.template == "zonal_ratio":
        kind, key = f"share{p['operator']}", signed(p["operator"], p["threshold"])
    elif plan.template == "service_provision":
        if p.get("residents_per_service"):
            kind, key = "residents_per_service", p["residents_per_service"]
        elif p.get("capacity_per_1000"):
            kind, key = "capacity_per_1000", -p["capacity_per_1000"]
        else:
            kind, key = "urban_api_capacity", 0.0
    else:
        return None
    return None if key is None else (kind, key)


def _tightest_accessibility(plan: CheckPlan, plans: list[CheckPlan]) -> CheckPlan:
    """Provision is strictest with the largest demand *and* the shortest accessibility."""
    if plan.template != "service_provision":
        return plan
    limits = [item.params["accessibility"] for item in plans]
    limits = [limit for limit in limits if limit]
    if not limits:
        return plan
    tightest = min(limits, key=_accessibility_meters)
    if tightest == plan.params.get("accessibility"):
        return plan
    return plan.model_copy(
        update={"params": {**plan.params, "accessibility": tightest}}
    )


def _variant_text(spec: NormSpec, condition: str | None) -> str:
    parts = []
    if spec.value is not None:
        prefix = (
            f"{spec.objects_count:g} на " if spec.objects_count not in {None, 1} else ""
        )
        parts.append(f"{prefix}{spec.value:g} {spec.unit or ''}".strip())
    if spec.accessibility_value is not None:
        access = f"{spec.accessibility_value:g} {spec.accessibility_unit or ''}"
        parts.append(f"доступность {access.strip()}")
    text = ", ".join(parts) or "без числового значения"
    if condition and condition.strip():
        text = f"{text} — {condition.strip()}"
    return text


class _Refused(Exception):
    def __init__(self, *reasons: str) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = list(reasons)


def render_plan(plan: CheckPlan) -> str:
    """Russian sentences stating exactly what the plan checks (for the verifier)."""
    text = _render_requirement(plan)
    applicability = plan.applicability
    if applicability is not None:
        text += (
            " Пункт содержит условия или разные значения для разных случаев ("
            + "; ".join(applicability.conditions)
            + "). Проверка применяет ко всем объектам самое строгое значение: "
            + applicability.applied
            + "."
        )
        if applicability.variants:
            text += " Варианты пункта: " + "; ".join(applicability.variants) + "."
    return text


def _render_requirement(plan: CheckPlan) -> str:
    layers = {
        item.role: f"«{item.entity}»"
        for item in (
            plan.declared_requirements.layers if plan.declared_requirements else []
        )
    }
    p = plan.params
    zone = lambda role: (  # noqa: E731 - local formatter
        "любой функциональной зоны"
        if layers.get(role) == f"«{ALL_ZONES}»"
        else f"функциональной зоны {layers.get(role)}"
    )
    if plan.template == "distance_from_source":
        if p.get("geometry_mode") == "source_geometry":
            return (
                f"Нарушение: объект {layers.get('targets')} пересекается с объектом "
                f"{layers.get('source')} (размещение запрещено)."
            )
        return (
            f"Нарушение: объект {layers.get('targets')} расположен ближе "
            f"{p['distance_m']:g} м от объекта {layers.get('source')}; "
            f"требуется расстояние не менее {p['distance_m']:g} м."
        )
    if plan.template == "presence_within":
        return (
            f"Для каждого объекта {layers.get('objects')} в радиусе "
            f"{p['distance_m']:g} м должен находиться хотя бы один объект "
            f"{layers.get('neighbors')}."
        )
    if plan.template == "accessibility_within":
        limit = p["limit"]
        bound = (
            f"{limit['minutes']:g} мин"
            if limit["kind"] == "time"
            else f"{limit['meters']:g} м пути"
        )
        return (
            f"Для каждого объекта {layers.get('objects')} в пределах доступности "
            f"{bound} должен находиться хотя бы один объект {layers.get('neighbors')}."
        )
    if plan.template == "object_attribute_threshold":
        return (
            f"У каждого объекта {layers.get('objects')} значение атрибута "
            f"({_attribute_label(plan)}) должно быть {p['operator']} "
            f"{p['threshold']:g} {p['unit']}."
        )
    if plan.template == "zonal_attribute_threshold":
        threshold = p["threshold_source"]
        return (
            f"У каждого объекта {layers.get('objects')} в границах {zone('zones')} "
            f"значение атрибута ({_attribute_label(plan)}) должно быть "
            f"{p['operator']} {threshold.get('value', 0):g} {threshold.get('unit', '')}."
        )
    if plan.template == "zonal_ratio":
        return (
            f"В каждой {zone('zones')} доля площади, занятой объектами "
            f"{layers.get('numerator')}, должна быть {p['operator']} "
            f"{p['threshold']:g} % площади зоны."
        )
    if plan.template == "service_provision":
        access = p.get("accessibility")
        access_text = (
            "в пределах нормативной доступности сервиса"
            if not access
            else (
                f"в пределах {access['minutes']:g} мин"
                if access["kind"] == "time"
                else f"в пределах {access['meters']:g} м"
            )
        )
        capacity = p.get("capacity_per_1000")
        residents = p.get("residents_per_service")
        if residents:
            capacity_text = f"из расчёта 1 объект на {residents:g} жителей"
        elif capacity:
            capacity_text = f"из расчёта {capacity:g} мест на 1000 жителей"
        else:
            capacity_text = "по нормативу сервиса"
        return (
            f"Жители каждого жилого дома должны быть обеспечены объектами "
            f"{layers.get('services')} {capacity_text} {access_text}."
        )
    if plan.template == "distance_table":
        return (
            f"Расстояние от объектов {layers.get('source')} до объектов "
            f"{layers.get('targets')} зависит от этажности по таблице диапазонов."
        )
    return f"Шаблон {plan.template}."


def _attribute_label(plan: CheckPlan) -> str:
    attributes = (
        plan.declared_requirements.attributes if plan.declared_requirements else []
    )
    fields = [candidate.field for item in attributes for candidate in item.accepts]
    if "building.floors" in fields and all(f == "building.floors" for f in fields):
        return "этажность"
    if any(f.startswith("building.building_area") for f in fields):
        return "площадь застройки"
    if fields == ["geometry"]:
        return "площадь"
    if "building.height" in fields:
        return "высота, м"
    return ", ".join(fields)
