"""Domain models for extracted restrictions.

The restriction semantics agreed for the project: a triple ``{subject, object, kind}`` plus an
optional structured ``value``:

* ``subject`` — the concise real-world entity or territory providing the restriction's context
  (e.g. "санитарно-защитная зона");
* ``object``  — what the restriction applies to (free text for now, e.g. "объекты пищевой
  промышленности");
* ``kind``    — the kind of restriction, from a controlled, dynamically-extensible vocabulary
  (e.g. "запрет_размещения", "минимальная_ширина");
* ``value``   — an optional quantitative constraint ``{operator, number, unit, condition}``; a
  clause with conditional norms yields several restrictions, one per value.
* ``measurement`` — the indicator, calculation basis and (for area shares) numerator/denominator
  entities. Provision against demand is distinct from a geometric area ratio.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RestrictionValue(BaseModel):
    operator: str | None = None  # >=, <=, >, <, =, range, ...
    number: float | None = None
    unit: str | None = None  # м, %, эт., ...
    condition: str | None = None  # free-text applicability condition

    def is_empty(self) -> bool:
        return (
            self.operator is None
            and self.number is None
            and self.unit is None
            and self.condition is None
        )

    def to_props(self) -> dict:
        """Flat, Neo4j-storable representation (drops empties)."""
        data = {
            "value_operator": self.operator,
            "value_number": self.number,
            "value_unit": self.unit,
            "value_condition": self.condition,
        }
        return {k: v for k, v in data.items() if v is not None}


class RestrictionMeasurement(BaseModel):
    """Meaning of the quantity, separate from entities and applicability conditions."""

    kind: Literal[
        "area_share", "count_share", "provision", "distance", "linear_size", "other"
    ] = "other"
    indicator: str | None = None
    basis: str | None = None
    numerator_entity: str | None = None
    denominator_entity: str | None = None

    @classmethod
    def from_storage(cls, raw: str | None) -> "RestrictionMeasurement | None":
        if raw is None:
            return None
        try:
            return cls.model_validate_json(raw)
        except ValueError:
            # Corrupt metadata must not silently become an executable legacy rule.
            return cls(kind="other", indicator="invalid_measurement_metadata")


class ExtractedRestriction(BaseModel):
    """One restriction as extracted from a single clause (before graph resolution)."""

    subject: str
    object: str
    kind: str
    value: RestrictionValue | None = None
    measurement: RestrictionMeasurement | None = None
    # Grounding of the extraction inside the clause text (langextract char interval).
    extraction_text: str = ""
    char_start: int | None = None
    char_end: int | None = None
    # Any extra attributes langextract returned but we do not model explicitly.
    extra: dict = Field(default_factory=dict)
