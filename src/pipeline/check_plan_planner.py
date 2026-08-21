"""Allowlisted deterministic planner with a strictly validated LLM fallback."""

from __future__ import annotations

import json
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


class CheckPlanPlanner:
    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm

    async def plan(self, restriction_id: str, ex: ExtractedRestriction) -> CheckPlan:
        deterministic = self._deterministic(restriction_id, ex)
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
        return CheckPlan(
            schema_version="1.0",
            template="unsupported",
            template_version=1,
            params={},
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
        if (
            value is not None
            and value.number is not None
            and (value.unit or "").casefold() in {"м", "m", "метр", "метров", "метра"}
        ):
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
                            "distance_m": float(value.number),
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
                return validate_check_plan(
                    {
                        "schema_version": "1.0",
                        "template": "presence_within",
                        "template_version": 1,
                        "params": {
                            "objects_layer": "objects",
                            "required_neighbor_layers": ["neighbors"],
                            "distance_m": float(value.number),
                            "minimum_neighbors": 1,
                            "result_mode": "both",
                        },
                        "declared_requirements": {
                            "layers": [
                                _layer("objects", ex.object),
                                _layer("neighbors", ex.subject),
                            ],
                            "attributes": [],
                        },
                        "source": source,
                        "planner_status": "auto",
                    }
                )
        if (
            value is not None
            and value.number is not None
            and (value.unit or "") in {"%", "процент", "процентов"}
            and any(
                word in ex.kind.casefold()
                for word in ("доля", "коэффициент", "плотност")
            )
        ):
            return validate_check_plan(
                {
                    "schema_version": "1.0",
                    "template": "zonal_ratio",
                    "template_version": 1,
                    "params": {
                        "zones_layer": "zones",
                        "numerator": {"layer": "numerator", "measure": "area"},
                        "denominator": {"measure": "zone_area"},
                        "operator": value.operator or "<=",
                        "threshold": float(value.number),
                        "unit": "%",
                    },
                    "declared_requirements": {
                        "layers": [
                            _layer("zones", ex.object),
                            _layer("numerator", ex.subject),
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
                "and planner_status=unsupported."
            ),
            temperature=0,
            max_tokens=1800,
        )
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            candidate = json.loads(match.group(0))
            candidate.setdefault("source", {})
            candidate["source"]["restriction_id"] = restriction_id
            candidate["source"].setdefault("extraction_text", ex.extraction_text)
            candidate["planner_status"] = "auto"
            return validate_check_plan(candidate)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "check_plan_llm_invalid",
                restriction_id=restriction_id,
                error=str(exc),
            )
            return None
