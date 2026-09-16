"""Grounded compilation of explicit spatial rules supported by CheckPlan v1.

Match the *whole* clause, including its calculation basis. Unknown qualifiers,
exceptions, routes and geometry references go through normal extraction/review.
Numbers and layer roles come from the source, never from an LLM repair guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.dto.check_plan import DistanceBand, validate_check_plan
from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)

_NUMBER = r"\d+(?:[.,]\d+)?"
# Exact aliases only: qualifiers such as «многоквартирный» must not disappear.
_ENTITIES = {
    "жилых домов": ("Жилой дом", "physical_object"),
    "жилого дома": ("Жилой дом", "physical_object"),
    "жилые дома": ("Жилой дом", "physical_object"),
    "школ": ("Школа", "service"),
    "школа": ("Школа", "service"),
    "школы": ("Школа", "service"),
    "спортивных площадок": ("Спортивная площадка", "service"),
    "спортивные площадки": ("Спортивная площадка", "service"),
    "детских садов": ("Детский сад", "service"),
    "детский сад": ("Детский сад", "service"),
    "детского сада": ("Детский сад", "service"),
    "парков": ("Парк", "physical_object"),
    "парк": ("Парк", "physical_object"),
}


def _number(value: str) -> float:
    return float(value.replace(",", "."))


def _layer(role: str, entity: str, *, contours: bool = True) -> dict:
    name, kind = (
        ("functional_zones", "functional_zone")
        if entity == "functional_zones"
        else _ENTITIES[entity]
    )
    # These rules explicitly measure contours/footprints, not point centroids.
    return dict(
        role=role,
        entity=name,
        entity_type=kind,
        geometry_types=(
            ["Polygon", "MultiPolygon"]
            if contours
            else ["Point", "MultiPoint", "Polygon", "MultiPolygon"]
        ),
        required=True,
    )


def _floors(on: str) -> dict:
    return dict(
        role="floors",
        on=on,
        required=True,
        min_fill_rate=0,
        accepts=[dict(field="building.floors", unit="floors", quality="direct")],
    )


@dataclass
class SpatialRule:
    restriction: ExtractedRestriction
    template: str
    params: dict
    layers: list[dict]
    attributes: list[dict]

    def plan(self, restriction_id: str):
        return validate_check_plan(
            dict(
                schema_version="1.0",
                template=self.template,
                template_version=1,
                params={**self.params, "result_mode": "both"},
                declared_requirements=dict(
                    layers=self.layers, attributes=self.attributes
                ),
                source=dict(
                    restriction_id=restriction_id,
                    extraction_text=self.restriction.extraction_text,
                ),
                planner_status="auto",
            )
        )


def compile_spatial_rule(text: str) -> SpatialRule | None:
    """Return a source-grounded rule, or None if any semantics are unrecognised."""
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    normalized = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", normalized)
    try:
        return _compile(normalized, text)
    except (KeyError, ValueError):
        # Unknown catalogue aliases, invalid numbers/bands or geometry semantics
        # are not grounds for guessing an executable plan.
        return None


def _compile(text: str, original: str) -> SpatialRule | None:
    def rule(
        template,
        subject,
        object_,
        kind,
        params,
        layers,
        *,
        value=None,
        measurement=None,
        attributes=(),
    ):
        result = SpatialRule(
            ExtractedRestriction(
                subject=subject,
                object=object_,
                kind=kind,
                value=value,
                measurement=measurement,
                extraction_text=original,
                char_start=0,
                char_end=len(original),
            ),
            template,
            params,
            layers,
            list(attributes),
        )
        result.plan("validation")
        return result

    m = re.fullmatch(
        rf"расстояние от (.+?) до (.+?) следует принимать не менее ({_NUMBER}) (м|км)\."
        r"(?: расстояние измеряется между контурами объектов\.)?",
        text,
    )
    if m:
        source, target, amount, unit = m.groups()
        if _ENTITIES[source] == _ENTITIES[target]:
            return None
        distance = _number(amount) * (1000 if unit == "км" else 1)
        return rule(
            "distance_from_source",
            _ENTITIES[source][0],
            _ENTITIES[target][0],
            "минимальное_расстояние",
            dict(
                source_layer="source",
                targets=["targets"],
                geometry_mode="buffered",
                distance_m=distance,
                predicate="intersects",
                violation_when="matched",
            ),
            [
                _layer("source", source, contours="контур" in text),
                _layer("targets", target, contours="контур" in text),
            ],
            value=RestrictionValue(operator=">=", number=distance, unit="м"),
            measurement=RestrictionMeasurement(kind="distance"),
        )

    m = re.fullmatch(
        rf"этажность (.+?), расположенных (полностью или частично|полностью) в пределах "
        rf"функциональных зон, не должна превышать ({_NUMBER}) этаж(?:ей|а)\."
        r"(?: проверка выполняется по числу этажей здания\.)?",
        text,
    )
    if m:
        objects, join, threshold = m.groups()
        return rule(
            "zonal_attribute_threshold",
            "functional_zones",
            _ENTITIES[objects][0],
            "предельная_этажность",
            dict(
                objects_layer="objects",
                zones_layer="zones",
                attribute_role="floors",
                operator="<=",
                threshold_source=dict(
                    kind="constant", value=_number(threshold), unit="floors"
                ),
                join_predicate=(
                    "intersects" if join == "полностью или частично" else "within"
                ),
            ),
            [_layer("objects", objects), _layer("zones", "functional_zones")],
            value=RestrictionValue(
                operator="<=", number=_number(threshold), unit="эт."
            ),
            measurement=RestrictionMeasurement(
                kind="attribute",
                indicator="этажность",
                attribute="building.floors",
                basis=join,
            ),
            attributes=[_floors("objects")],
        )

    # The quantified object is checked; the nearby service is its context.
    m = re.fullmatch(
        r"для каждого жилого дома (?:должна быть обеспечена|должен быть обеспечен|"
        r"должно быть обеспечено|должны быть обеспечены) (?:хотя бы|не менее) "
        r"(одна|один|одно|\d+) (.+?) "
        rf"на расстоянии не более ({_NUMBER}) (м|км) от контура дома до контура (.+?)\.",
        text,
    )
    if m:
        count, neighbors, amount, unit, contour = m.groups()
        if _ENTITIES[neighbors] != _ENTITIES[contour]:
            return None
        minimum_neighbors = 1 if count in {"одна", "один", "одно"} else int(count)
        distance = _number(amount) * (1000 if unit == "км" else 1)
        return rule(
            "presence_within",
            _ENTITIES[neighbors][0],
            "Жилой дом",
            "наличие_в_радиусе",
            dict(
                objects_layer="objects",
                required_neighbor_layers=["neighbors"],
                distance_m=distance,
                minimum_neighbors=minimum_neighbors,
            ),
            [_layer("objects", "жилого дома"), _layer("neighbors", neighbors)],
            value=RestrictionValue(operator="<=", number=distance, unit="м"),
            measurement=RestrictionMeasurement(
                kind="distance",
                basis="между контурами",
                minimum_neighbors=minimum_neighbors,
            ),
        )

    m = re.fullmatch(
        r"расстояние от (.+?) до жилых домов следует принимать (.+?)\. "
        r"расстояние измеряется между контурами объектов\. "
        r"проверяемыми объектами являются (.+?)\.",
        text,
    )
    if m:
        targets, table, checked = m.groups()
        if (
            _ENTITIES[targets] != _ENTITIES[checked]
            or _ENTITIES[targets][0] == "Жилой дом"
        ):
            return None
        rows = re.split(r",\s*(?=не менее)| и (?=не менее)", table)
        bands = []
        for row in rows:
            band = re.fullmatch(
                rf"не менее ({_NUMBER}) м при этажности (?:дома )?"
                r"(?:от (\d+) до (\d+) этажей включительно|(\d+) этажей и более)",
                row,
            )
            if not band:
                return None
            distance, low, high, lower = band.groups()
            bands.append(
                DistanceBand(
                    min=int(low or lower),
                    max=int(high) if high else None,
                    distance_m=_number(distance),
                )
            )
        return rule(
            "distance_table",
            "Жилой дом",
            _ENTITIES[targets][0],
            "расстояние_по_этажности",
            dict(
                source_layer="source",
                targets=["targets"],
                attribute_role="floors",
                bands=[b.model_dump() for b in bands],
                predicate="intersects",
                violation_when="matched",
            ),
            [_layer("source", "жилых домов"), _layer("targets", targets)],
            measurement=RestrictionMeasurement(
                kind="distance_table",
                attribute="building.floors",
                bands=[b.model_dump() for b in bands],
            ),
            attributes=[_floors("source")],
        )

    m = re.fullmatch(
        rf"в каждой функциональной зоне доля площади, занятой контурами (.+?), "
        rf"не должна превышать ({_NUMBER})\s*% площади зоны\. "
        r"площадь застройки определяется по объединению частей контуров (.+?) "
        r"внутри зоны без двойного учета их пересечений\.",
        text,
    )
    if m:
        objects, threshold, repeated = m.groups()
        if _ENTITIES[objects] != _ENTITIES[repeated]:
            return None
        return rule(
            "zonal_ratio",
            "functional_zones",
            _ENTITIES[objects][0],
            "максимальная_доля_площади",
            dict(
                zones_layer="zones",
                numerator=dict(layer="numerator", measure="area"),
                denominator=dict(measure="zone_area"),
                operator="<=",
                threshold=_number(threshold),
                unit="%",
            ),
            [_layer("zones", "functional_zones"), _layer("numerator", objects)],
            value=RestrictionValue(operator="<=", number=_number(threshold), unit="%"),
            measurement=RestrictionMeasurement(
                kind="area_share",
                basis="площади функциональной зоны",
                numerator_entity=_ENTITIES[objects][0],
                denominator_entity="functional_zones",
            ),
        )
    return None
