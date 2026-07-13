"""Domain models for extracted restrictions.

The restriction semantics agreed for the project: a triple ``{subject, object, kind}`` plus an
optional structured ``value``:

* ``subject`` — the real-world entity that imposes the restriction (extracted verbatim from the
  clause text, e.g. "санитарно-защитная зона");
* ``object``  — what the restriction applies to (free text for now, e.g. "объекты пищевой
  промышленности");
* ``kind``    — the kind of restriction, from a controlled, dynamically-extensible vocabulary
  (e.g. "запрет_размещения", "минимальная_ширина");
* ``value``   — an optional quantitative constraint ``{operator, number, unit, condition}``; a
  clause with conditional norms yields several restrictions, one per value.
"""

from __future__ import annotations

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


class ExtractedRestriction(BaseModel):
    """One restriction as extracted from a single clause (before graph resolution)."""

    subject: str
    object: str
    kind: str
    value: RestrictionValue | None = None
    # Grounding of the extraction inside the clause text (langextract char interval).
    extraction_text: str = ""
    char_start: int | None = None
    char_end: int | None = None
    # Any extra attributes langextract returned but we do not model explicitly.
    extra: dict = Field(default_factory=dict)
