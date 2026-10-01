"""Multi-pass CheckPlan planner.

1. **Deterministic** — whole-clause grammar (``spatial_rules``), then the allowlisted
   triple planner below, both behind the precision guards of ``norm_guards``.
2. **Grounding** — every layer entity must be a canonical Urban API type.
3. **Rewrite** (LLM, optional) — a norm without a grounded plan is re-read from its
   clause into a ``NormSpec`` by several independent votes (``norm_refiner``).
4. **Verify** (LLM, optional) — an independent prompt confirms that the plan states
   what the clause requires.

Only a plan that passes every enabled pass is ``auto``; anything else is
``unsupported`` with its reasons and, when one was built, the blocked candidate.
"""

from __future__ import annotations

import math
import re
from typing import Any

import structlog

from src.dto.check_plan import CheckPlan, validate_check_plan
from src.pipeline.models import ExtractedRestriction
from src.pipeline.norm_guards import (
    is_building_part,
    is_measure_label,
    precision_reasons,
)
from src.pipeline.norm_refiner import NormRefiner, PlanContext
from src.pipeline.spatial_rules import compile_spatial_rule
from src.pipeline.urban_catalog import UrbanCatalog
from src.providers.base import LLMProvider

log = structlog.get_logger(__name__)

# Bump whenever planning semantics change: plans of older versions are re-planned by
# ``POST /check-plans/replan`` (expert-reviewed plans are never touched).
CHECK_PLANNER_VERSION = 2

EXECUTABLE_TEMPLATE_MANIFEST = {
    "schema_version": "1.0",
    "templates": [
        {"template": "distance_from_source", "version": 1},
        {"template": "distance_table", "version": 1},
        {"template": "presence_within", "version": 1},
        {"template": "zonal_attribute_threshold", "version": 1},
        {"template": "zonal_ratio", "version": 1},
        {"template": "object_attribute_threshold", "version": 1},
        {"template": "accessibility_within", "version": 1},
        {"template": "service_provision", "version": 1},
    ],
}

# Units of construction, materials and indoor climate: the norm is never territorial.
# Places, people and minutes stay: provision and accessibility norms use them.
_NON_TERRITORIAL_UNITS = re.compile(
    r"^(?:мм|см|°|°c|градус\w*|к?па|мпа|дб\w*|лк|квт|вт|в|а|ккал|л|л/с|м3/ч|"
    r"м/с|раз\w*|кг\w*|т|мг\w*)$",
    re.I,
)
_NON_TERRITORIAL_KINDS = re.compile(
    r"документ|материал|конструкц|температур|влажност|освещ|отделк|персонал|"
    r"оборудовани|маркиров|испытани|прочност|огнестойк|теплоизоляц|вентиляц",
    re.I,
)

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


# Places where people live or stay. A maximum-distance norm is an accessibility requirement for
# the people, so it is checked on this side whichever way the sentence runs.
_RESIDENCE = re.compile(
    r"жил\w*\s+(?:дом|здани|застройк|квартал|район|зон)|жиль|проживани|общежити|"
    r"интернат|сирот|пансионат|престарел",
    re.I,
)


def _checked_side(ex: ExtractedRestriction) -> tuple[str, str] | None:
    """``(checked objects, required neighbours)`` of a maximum-distance norm, or ``None``.

    The extractor follows the sentence ("от A до B" gives subject=A, object=B), and norms run
    both ways: «от школ до жилых домов не более 500 м» checks the houses, «от детских домов
    до школ не более 1 км» checks the children's homes. Only a residence on exactly one side
    tells which objects must have the other within reach.
    """
    subject, object_ = (bool(_RESIDENCE.search(x)) for x in (ex.subject, ex.object))
    if subject == object_:
        return None
    return (ex.subject, ex.object) if subject else (ex.object, ex.subject)


def _entity_type(name: str) -> str:
    folded = name.casefold()
    if "зон" in folded or "территори" in folded:
        return "functional_zone"
    if any(word in folded for word in _SERVICE_WORDS):
        return "service"
    return "physical_object"


def _layer(role: str, entity: str) -> dict[str, Any]:
    if _non_spatial_entity(entity):
        raise ValueError("non_spatial_entity")
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
    # Windows/walls/rooms: an in-building share is not a zonal ratio.
    if any(
        is_building_part(label)
        for label in (m.numerator_entity, m.denominator_entity, m.basis)
    ):
        return None
    text = ex.extraction_text.casefold()
    if re.search(r"обеспеченно|автомобилизац|численност|количеств", text):
        return None
    # Require the area basis in the source too; a model label alone is insufficient.
    if text.count("площад") < 2 or "площад" not in m.basis.casefold():
        return None
    return m.numerator_entity, m.denominator_entity


def _non_spatial_entity(label: str) -> bool:
    """Reject quantity/requirement labels even if the model calls them objects.

    This is a conservative semantic guard, not an Urban API catalogue lookup.
    """
    return is_measure_label(label) or bool(
        re.search(
            r"\b(?:радиус\w*|значени\w*|показател\w*|уровень|уровня|уровнем|"
            r"доступност\w*|обеспеченност\w*|расстояни\w*|дистанци\w*|ширин\w*|высот(?:а|ы|у|е|ой|ам|ами|ах)?|"
            r"длин[аыуеой]\w*|количеств\w*|численност\w*|плотност\w*|"
            r"этажност\w*|требовани\w*|размещени\w*)\b",
            label,
            re.I,
        )
    )


def _spatial_semantic_reasons(ex: ExtractedRestriction) -> list[str]:
    reasons = []
    # A legacy triple cannot prove that these source qualifiers were represented.
    # Grounded whole-clause compilation runs before this conservative fallback.
    if re.search(
        r"за исключением|\b(?:кроме|при|если)\b|для сельск|в сельск",
        ex.extraction_text,
        re.I,
    ):
        reasons.append("applicability_not_verified")
    if re.search(r"контур", ex.extraction_text, re.I):
        reasons.append("contour_geometry_not_verified")
    if re.search(r"для кажд|проверяемыми объектами", ex.extraction_text, re.I):
        reasons.append("checked_entity_not_verified")
    # The legacy educational case has an explicit, fixed residential mapping.
    # Route requirements are checked separately from the source quotation.
    labels = [ex.subject] if _education_layers(ex) else [ex.subject, ex.object]
    if any(_non_spatial_entity(label) for label in labels):
        reasons.append("non_spatial_entity")
    text = " ".join(
        (
            ex.kind.replace("_", " "),
            ex.extraction_text,
            ex.measurement.indicator or "" if ex.measurement else "",
        )
    )
    if re.search(r"радиус\w*\s+(?:разворот|поворот|закруглен)|диаметр", text, re.I):
        reasons.append("linear_size_not_distance")
    if re.search(
        r"\b(?:от|до)\s+(?:главн\w*\s+|основн\w*\s+)?(?:вход|выход)|вход\w*\s+в\b",
        text,
        re.I,
    ):
        reasons.append("specific_geometry_required")
    if re.search(
        r"(?:друг\s+от\s+друга|одн\w*\s+от\s+друг\w*|между\s+собой)", text, re.I
    ):
        reasons.append("same_entity_spacing_not_supported")
    elif (
        ex.subject.casefold() == ex.object.casefold()
        and ex.value
        and (ex.value.unit or "").strip().casefold() in _DISTANCE_UNITS
    ):
        reasons.append("same_entity_spacing_not_supported")
    return reasons


def _grounding_reasons(plan: CheckPlan, catalog: UrbanCatalog) -> list[str]:
    """``entity_not_in_catalog`` unless every layer names a canonical Urban API type."""
    layers = plan.declared_requirements.layers if plan.declared_requirements else []
    for layer in layers:
        if catalog.resolve(layer.entity, layer.entity_type) is None:
            return ["entity_not_in_catalog"]
    return []


def _worth_rewriting(ex: ExtractedRestriction, ctx: PlanContext) -> bool:
    """Cheap filter: skip norms that are obviously not about territory."""
    unit = (ex.value.unit or "").strip() if ex.value else ""
    if unit and _NON_TERRITORIAL_UNITS.match(unit):
        return False
    if _NON_TERRITORIAL_KINDS.search(ex.kind or ""):
        return False
    text = ctx.clause_text or ex.extraction_text
    # Only a prohibition can be checked without a number (``prohibited_within``).
    return bool(re.search(r"\d", text)) or bool(
        re.search(r"запрет|не\s+допуска", f"{ex.kind} {text}", re.I)
    )


class CheckPlanPlanner:
    def __init__(
        self,
        llm: LLMProvider | None = None,
        *,
        catalog=None,
        refine: bool = True,
        verify: bool = True,
        votes: int = 2,
        min_distance_m: float = 3.0,
        llm_concurrency: int = 16,
    ) -> None:
        """``catalog`` is an ``UrbanCatalogProvider``-like object with ``async get()``.

        Without ``llm`` the planner is purely deterministic (passes 1–2).
        """
        self.llm = llm
        self.catalog = catalog
        self.refine = refine
        self.verify = verify
        self.min_distance_m = min_distance_m
        self.refiner = (
            NormRefiner(
                llm,
                votes=votes,
                verify=verify,
                min_distance_m=min_distance_m,
                concurrency=llm_concurrency,
            )
            if llm is not None
            else None
        )

    async def plan(
        self,
        restriction_id: str,
        ex: ExtractedRestriction,
        context: PlanContext | None = None,
    ) -> CheckPlan:
        plan, _ = await self.plan_with_trace(restriction_id, ex, context)
        return plan

    async def plan_with_trace(
        self,
        restriction_id: str,
        ex: ExtractedRestriction,
        context: PlanContext | None = None,
    ) -> tuple[CheckPlan, dict[str, Any]]:
        ctx = context or PlanContext(clause_text=ex.extraction_text)
        trace: dict[str, Any] = {
            "planner_version": CHECK_PLANNER_VERSION,
            "passes": [],
        }
        catalog = await self.catalog.get() if self.catalog is not None else None

        # Pass 1: a whole-clause grammar match is source-grounded and needs no review.
        for text in dict.fromkeys(filter(None, (ex.extraction_text, ctx.clause_text))):
            if rule := compile_spatial_rule(text):
                plan = rule.plan(restriction_id)
                trace["passes"].append({"pass": "grammar", "template": plan.template})
                return plan, trace

        candidate, reasons = self._first_pass(restriction_id, ex)
        trace["passes"].append(
            {
                "pass": "deterministic",
                "template": candidate.template if candidate else None,
                "reasons": list(reasons),
            }
        )
        # Pass 2: the executor resolves entities against these very dictionaries.
        if candidate is not None and not reasons and catalog is not None:
            reasons += _grounding_reasons(candidate, catalog)
            if reasons:
                trace["passes"].append({"pass": "grounding", "reasons": list(reasons)})
        if candidate is not None and not reasons:
            if self.refiner is None or not self.verify:
                return candidate, trace
            accepted, verify_reasons, verdict = await self.refiner.verify(
                candidate, ex, ctx
            )
            trace["passes"].append({"pass": "verify", "verdict": verdict})
            if accepted:
                return candidate, trace
            reasons += verify_reasons

        # Pass 3: re-read the norm from its clause.
        if self.refiner is not None and self.refine and _worth_rewriting(ex, ctx):
            if catalog is None:
                # Without the dictionaries no rewritten entity could be grounded.
                trace["passes"].append(
                    {"pass": "rewrite", "skipped": "urban_catalog_unavailable"}
                )
            else:
                outcome = await self.refiner.rewrite(
                    restriction_id, ex, ctx, catalog, reasons
                )
                trace["passes"].append(
                    {"pass": "rewrite", "reasons": outcome.reasons, **outcome.trace}
                )
                if outcome.plan is not None:
                    # Pass 4: an independent check of the rewritten plan.
                    accepted, verify_reasons, verdict = await self.refiner.verify(
                        outcome.plan, ex, ctx
                    )
                    trace["passes"].append({"pass": "verify", "verdict": verdict})
                    if accepted:
                        return outcome.plan, trace
                    return (
                        self.unsupported_plan(
                            restriction_id,
                            ex,
                            reasons=verify_reasons,
                            candidate=outcome.plan,
                        ),
                        trace,
                    )
                reasons += outcome.reasons
                candidate = candidate or outcome.candidate
        return (
            self.unsupported_plan(
                restriction_id,
                ex,
                reasons=list(dict.fromkeys(reasons)) or ["no_executable_template"],
                candidate=candidate,
            ),
            trace,
        )

    def _first_pass(
        self, restriction_id: str, ex: ExtractedRestriction
    ) -> tuple[CheckPlan | None, list[str]]:
        """The allowlisted triple planner: ``(auto candidate | None, reasons)``."""
        # v1 has neither applicability predicates nor walking-route execution. A
        # candidate may be useful for review, but must never run as a compliance
        # verdict while these requirements are unresolved.
        reasons = _spatial_semantic_reasons(ex)
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
            "attribute",
            "distance_table",
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
        # Educational entities alone do not imply walking accessibility. Use
        # geometric distance unless the quotation explicitly requires a route.
        if re.search(
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
        if deterministic is not None and deterministic.planner_status == "unsupported":
            # The triple planner refused on its own (range, strictness, roles).
            reasons += deterministic.params.get("blocked_reasons") or []
            candidate = deterministic.params.get("candidate_plan")
            deterministic = validate_check_plan(candidate) if candidate else None
        if deterministic is not None:
            reasons += self._precision_reasons(ex, deterministic)
        return deterministic, list(dict.fromkeys(reasons))

    def _precision_reasons(
        self, ex: ExtractedRestriction, plan: CheckPlan
    ) -> list[str]:
        labels = tuple(
            layer.entity
            for layer in (
                plan.declared_requirements.layers if plan.declared_requirements else []
            )
        )
        distance = plan.params.get("distance_m")
        return precision_reasons(
            ex.extraction_text,
            operator=ex.value.operator if ex.value else None,
            distance_m=distance,
            labels=labels,
            min_distance_m=self.min_distance_m,
        )

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
                sides = _checked_side(ex)
                if education:
                    object_layer = _layer("objects", "Жилой дом")
                    neighbors = education
                else:
                    # Unresolved roles keep the sentence order for the reviewer only.
                    checked, required = sides or (ex.object, ex.subject)
                    object_layer = _layer("objects", checked)
                    neighbors = [_layer("neighbors", required)]
                plan = validate_check_plan(
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
                if not education and sides is None:
                    # A reversed plan flags every correct object as a violation.
                    return self.unsupported_plan(
                        restriction_id,
                        ex,
                        reasons=["checked_entity_not_verified"],
                        candidate=plan,
                    )
                return plan
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
