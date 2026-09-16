"""Allowlisted deterministic planner with a strictly validated LLM fallback."""

from __future__ import annotations

import json
import math
import re
from typing import Any

import structlog

from src.dto.check_plan import CheckPlan, validate_check_plan
from src.pipeline.models import ExtractedRestriction
from src.providers.base import LLMProvider

log = structlog.get_logger(__name__)

EXECUTABLE_TEMPLATE_MANIFEST = {
    "schema_version": "1.0",
    "templates": [
        {"template": "distance_from_source", "version": 1},
        {"template": "distance_table", "version": 1},
        {"template": "presence_within", "version": 1},
        {"template": "zonal_attribute_threshold", "version": 1},
        {"template": "zonal_ratio", "version": 1},
    ],
}

_SERVICE_WORDS = (
    "школ",
    "детск",
    "сад",
    "поликлиник",
    "больниц",
    "аптек",
    "магазин",
    "спорт",
)

_DISTANCE_UNITS = {
    **dict.fromkeys(("м", "m", "метр", "метров", "метра"), 1),
    **dict.fromkeys(("км", "km", "километр", "километра", "километров"), 1000),
}


def _education_layers(ex: ExtractedRestriction) -> list[dict[str, Any]]:
    """Only resolve the unambiguous residential educational-accessibility case."""
    subject, object_ = ex.subject.casefold(), ex.object.casefold()
    if not re.search(r"жил\w*\s+(?:дом|здани)", object_):
        return []
    layers = []
    if "дошколь" in subject or "детск" in subject and "сад" in subject:
        layers.append(_layer("kindergartens", "Детский сад"))
    if any(
        word in subject
        for word in (
            "школа",
            "школы",
            "школу",
            "общеобразователь",
            "начального общего",
            "основного общего",
            "среднего общего",
        )
    ):
        layers.append(_layer("schools", "Школа"))
    return layers


def _entity_type(name: str) -> str:
    folded = name.casefold()
    if "зон" in folded or "территори" in folded:
        return "functional_zone"
    if any(word in folded for word in _SERVICE_WORDS):
        return "service"
    return "physical_object"


def _layer(role: str, entity: str) -> dict[str, Any]:
    entity_type = _entity_type(entity)
    geometry = (
        ["Polygon", "MultiPolygon"]
        if entity_type == "functional_zone"
        else ["Point", "MultiPoint", "Polygon", "MultiPolygon"]
    )
    return {
        "role": role,
        "entity": entity,
        "entity_type": entity_type,
        "geometry_types": geometry,
        "required": True,
    }


def _area_ratio_entities(ex: ExtractedRestriction) -> tuple[str, str] | None:
    """Only an explicit, grounded area/area measurement can use zonal_ratio v1."""
    m = ex.measurement
    if not m or m.kind != "area_share" or not m.basis:
        return None
    if not m.numerator_entity or not m.denominator_entity:
        return None
    if m.numerator_entity.casefold() == m.denominator_entity.casefold():
        return None
    text = ex.extraction_text.casefold()
    if re.search(r"обеспеченно|автомобилизац|численност|количеств", text):
        return None
    # Require the area basis in the source too; a model label alone is insufficient.
    if text.count("площад") < 2 or "площад" not in m.basis.casefold():
        return None
    return m.numerator_entity, m.denominator_entity


class CheckPlanPlanner:
    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm

    async def plan(self, restriction_id: str, ex: ExtractedRestriction) -> CheckPlan:
        # v1 has neither applicability predicates nor walking-route execution. A
        # candidate may be useful for review, but must never run as a compliance
        # verdict while these requirements are unresolved (including LLM fallback).
        reasons = []
        value = ex.value
        unit = (value.unit or "").strip().casefold() if value else ""
        measurement = ex.measurement
        if (
            measurement
            and measurement.kind == "area_share"
            and (unit not in {"%", "процент", "процентов"} or value.number is None)
        ):
            reasons.append("measurement_unit_mismatch")
        if any(len(label) > 200 for label in (ex.subject, ex.object)):
            reasons.append("entity_label_too_long")
        if measurement and measurement.kind in {
            "provision",
            "count_share",
            "linear_size",
            "other",
        }:
            reasons.append("unsupported_measurement")
        if unit in {"%", "процент", "процентов"} and not _area_ratio_entities(ex):
            reasons.append("ratio_basis_not_supported")
        if unit in _DISTANCE_UNITS and re.search(
            r"ширин|высот|длин|этаж|площад", ex.kind, re.I
        ):
            reasons.append("linear_size_not_distance")
        condition = ex.value.condition if ex.value else None
        if condition and condition.strip():
            reasons.append("applicability_not_verified")
        if (
            ex.value and ex.value.operator in {"<", "<="} and _education_layers(ex)
        ) or re.search(
            r"пешеход|маршрут|транспортн\w*\s+доступ", ex.extraction_text, re.I
        ):
            reasons.append("walking_route_required")
        # Never attempt an incompatible candidate (or let the LLM bypass the guard).
        incompatible = set(reasons) - {
            "applicability_not_verified",
            "walking_route_required",
        }
        deterministic = None
        if not incompatible:
            try:
                deterministic = self._deterministic(restriction_id, ex)
            except ValueError as exc:
                log.warning(
                    "check_plan_deterministic_invalid",
                    restriction_id=restriction_id,
                    error=str(exc),
                )
                reasons.append("invalid_plan_parameters")
        if reasons:
            return self.unsupported_plan(
                restriction_id, ex, reasons=reasons, candidate=deterministic
            )
        if deterministic is not None:
            return deterministic
        if self.llm is not None:
            try:
                fallback = await self._llm_fallback(restriction_id, ex)
            except Exception as exc:
                log.warning(
                    "check_plan_llm_failed",
                    restriction_id=restriction_id,
                    error=str(exc),
                )
                fallback = None
            if fallback is not None:
                return fallback
        return self.unsupported_plan(restriction_id, ex)

    @staticmethod
    def unsupported_plan(
        restriction_id: str,
        ex: ExtractedRestriction,
        *,
        reasons: list[str] | None = None,
        candidate: CheckPlan | None = None,
    ) -> CheckPlan:
        params: dict[str, Any] = {}
        if reasons:
            params = {
                "blocked_reasons": reasons,
                "condition": ex.value.condition if ex.value else None,
                "measurement": (
                    ex.measurement.model_dump(mode="json") if ex.measurement else None
                ),
                "candidate_plan": (
                    candidate.model_dump(mode="json")
                    if candidate and candidate.planner_status == "auto"
                    else None
                ),
            }
        return CheckPlan(
            schema_version="1.0",
            template="unsupported",
            template_version=1,
            params=params,
            source={
                "restriction_id": restriction_id,
                "extraction_text": ex.extraction_text,
            },
            planner_status="unsupported",
        )

    def _deterministic(
        self, restriction_id: str, ex: ExtractedRestriction
    ) -> CheckPlan | None:
        value = ex.value
        distance_scale = (
            _DISTANCE_UNITS.get((value.unit or "").strip().casefold())
            if value
            else None
        )
        if (
            value is not None
            and value.number is not None
            and distance_scale is not None
        ):
            distance_m = float(value.number) * distance_scale
            if not math.isfinite(distance_m) or not 0 < distance_m <= 100_000:
                return self.unsupported_plan(
                    restriction_id, ex, reasons=["distance_out_of_range"]
                )
            source = {
                "restriction_id": restriction_id,
                "extraction_text": ex.extraction_text,
            }
            if value.operator in {">", ">="}:
                return validate_check_plan(
                    {
                        "schema_version": "1.0",
                        "template": "distance_from_source",
                        "template_version": 1,
                        "params": {
                            "source_layer": "source",
                            "targets": ["targets"],
                            "geometry_mode": "buffered",
                            "distance_m": distance_m,
                            "predicate": "intersects",
                            "violation_when": "matched",
                            "result_mode": "both",
                        },
                        "declared_requirements": {
                            "layers": [
                                _layer("source", ex.subject),
                                _layer("targets", ex.object),
                            ],
                            "attributes": [],
                        },
                        "source": source,
                        "planner_status": "auto",
                    }
                )
            if value.operator in {"<", "<="}:
                # A radius includes its boundary; it cannot represent strict <.
                if value.operator == "<":
                    return self.unsupported_plan(
                        restriction_id, ex, reasons=["strict_distance_not_supported"]
                    )
                education = _education_layers(ex)
                object_layer = (
                    _layer("objects", "Жилой дом")
                    if education
                    else _layer("objects", ex.object)
                )
                neighbors = education or [_layer("neighbors", ex.subject)]
                return validate_check_plan(
                    {
                        "schema_version": "1.0",
                        "template": "presence_within",
                        "template_version": 1,
                        "params": {
                            "objects_layer": "objects",
                            "required_neighbor_layers": [
                                item["role"] for item in neighbors
                            ],
                            "distance_m": distance_m,
                            "minimum_neighbors": 1,
                            "result_mode": "both",
                        },
                        "declared_requirements": {
                            "layers": [
                                object_layer,
                                *neighbors,
                            ],
                            "attributes": [],
                        },
                        "source": source,
                        "planner_status": "auto",
                    }
                )
        ratio_entities = _area_ratio_entities(ex)
        if (
            value is not None
            and value.number is not None
            and (value.unit or "").strip().casefold() in {"%", "процент", "процентов"}
            and ratio_entities
        ):
            numerator_entity, zone_entity = ratio_entities
            zones = _layer("zones", zone_entity)
            zones.update(
                entity_type="functional_zone",
                geometry_types=["Polygon", "MultiPolygon"],
            )
            numerator = _layer("numerator", numerator_entity)
            numerator["geometry_types"] = ["Polygon", "MultiPolygon"]
            return validate_check_plan(
                {
                    "schema_version": "1.0",
                    "template": "zonal_ratio",
                    "template_version": 1,
                    "params": {
                        "zones_layer": "zones",
                        "numerator": {"layer": "numerator", "measure": "area"},
                        "denominator": {"measure": "zone_area"},
                        "operator": ("==" if value.operator == "=" else value.operator),
                        "threshold": float(value.number),
                        "unit": "%",
                    },
                    "declared_requirements": {
                        "layers": [
                            zones,
                            numerator,
                        ],
                        "attributes": [],
                    },
                    "source": {
                        "restriction_id": restriction_id,
                        "extraction_text": ex.extraction_text,
                    },
                    "planner_status": "auto",
                }
            )
        return None

    async def _llm_fallback(
        self, restriction_id: str, ex: ExtractedRestriction
    ) -> CheckPlan | None:
        prompt = json.dumps(
            {
                "manifest": EXECUTABLE_TEMPLATE_MANIFEST,
                "restriction": {
                    "id": restriction_id,
                    "subject": ex.subject,
                    "object": ex.object,
                    "kind": ex.kind,
                    "value": ex.value.model_dump() if ex.value else None,
                    "measurement": (
                        ex.measurement.model_dump() if ex.measurement else None
                    ),
                    "extraction_text": ex.extraction_text,
                },
            },
            ensure_ascii=False,
        )
        raw = await self.llm.complete(
            prompt,
            system=(
                "Return only one JSON CheckPlan. Use only the manifest templates and version 1. "
                "Never emit code, URLs, paths or expressions. If uncertain, set template=unsupported "
                "and planner_status=unsupported. Never treat a percentage as an area ratio without "
                "explicit area numerator and area denominator. Never treat width, height, provision "
                "or room floor placement as distance between objects."
            ),
            temperature=0,
            max_tokens=1800,
        )
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            candidate = json.loads(match.group(0))
            if candidate.get("template") == "unsupported":
                return None
            if candidate.get("template") == "zonal_ratio" and not _area_ratio_entities(
                ex
            ):
                return None
            candidate.setdefault("source", {})
            candidate["source"]["restriction_id"] = restriction_id
            candidate["source"]["extraction_text"] = ex.extraction_text
            candidate["planner_status"] = "auto"
            return validate_check_plan(candidate)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "check_plan_llm_invalid",
                restriction_id=restriction_id,
                error=str(exc),
            )
            return None
