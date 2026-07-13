"""Rule-based conflict detection between restrictions sharing an entity.

Two restrictions of the same ``kind`` that constrain the same numeric quantity in mutually
unsatisfiable ways are a *possible conflict*: without a resolved reference/hierarchy between their
source clauses (never guaranteed for ad hoc user uploads — see ``src/ingestion/service.py``),
NormGraph cannot tell which one takes precedence, so both are flagged rather than one silently
overriding the other. This runs against ``SHARES_ENTITY`` neighbours, which already span both the
official corpus and a user's own upload set (same shared ``:Entity``/``:RestrictionKind``
vocabulary, see ``src/pipeline/vocabulary.py``), so one pass covers both conflict scopes.

Deliberately conservative: only ``operator`` in ``{">=","<=",">","<","="}`` with a matching
``unit`` (or both empty) are compared; anything else (missing values, mismatched units,
non-comparable operators such as ``"range"``) is left alone rather than guessed at — a false
"possible conflict" is worse than silence here.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.pipeline.models import RestrictionValue
from src.pipeline.vocabulary import normalize

_COMPARABLE_OPERATORS = {">=", "<=", ">", "<", "="}
_EPSILON = 1e-9


@dataclass
class ConflictCandidate:
    other_id: str
    reason: str
    severity: str  # "certain" | "possible"


def _comparable(value: RestrictionValue) -> bool:
    return value.operator in _COMPARABLE_OPERATORS and value.number is not None


def _bounds(value: RestrictionValue) -> tuple[float, float]:
    """The ``[min, max]`` range a single-sided constraint allows (+/-inf for the open side)."""
    number = value.number
    if value.operator == ">=":
        return number, float("inf")
    if value.operator == ">":
        return number + _EPSILON, float("inf")
    if value.operator == "<=":
        return float("-inf"), number
    if value.operator == "<":
        return float("-inf"), number - _EPSILON
    return number, number  # "="


def _incompatible(a: RestrictionValue, b: RestrictionValue) -> bool:
    lo_a, hi_a = _bounds(a)
    lo_b, hi_b = _bounds(b)
    return hi_a < lo_b or hi_b < lo_a


def find_conflicts(
    candidate_id: str,
    candidate_kind: str,
    candidate_value: RestrictionValue | None,
    neighbors: list[dict],
) -> list[ConflictCandidate]:
    """Possible conflicts between one just-written restriction and its ``SHARES_ENTITY`` neighbours.

    ``neighbors`` are the rows returned by ``GraphWriter.link_shares_entity`` (id, kind, value
    fields). Skips a neighbour when its kind differs, its value isn't structurally comparable to
    ``candidate_value``, or its unit doesn't match.
    """
    if candidate_value is None or not _comparable(candidate_value):
        return []

    found: list[ConflictCandidate] = []
    candidate_unit = normalize(candidate_value.unit or "")
    for row in neighbors:
        if row.get("id") == candidate_id or row.get("kind") != candidate_kind:
            continue
        other = RestrictionValue(
            operator=row.get("value_operator"),
            number=row.get("value_number"),
            unit=row.get("value_unit"),
            condition=row.get("value_condition"),
        )
        if not _comparable(other):
            continue
        if normalize(other.unit or "") != candidate_unit:
            continue
        if not _incompatible(candidate_value, other):
            continue

        cond_a = normalize(candidate_value.condition or "")
        cond_b = normalize(other.condition or "")
        severity = "certain" if cond_a == cond_b else "possible"
        reason = (
            f"{candidate_kind}: {candidate_value.operator}{candidate_value.number}"
            f"{candidate_value.unit or ''} vs {other.operator}{other.number}"
            f"{other.unit or ''}"
        )
        found.append(
            ConflictCandidate(other_id=row["id"], reason=reason, severity=severity)
        )
    return found
