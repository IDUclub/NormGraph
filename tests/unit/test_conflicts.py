"""Rule-based conflict detection: disjoint numeric bounds on a shared kind/unit are flagged."""

from __future__ import annotations

from src.pipeline.conflicts import find_conflicts
from src.pipeline.models import RestrictionValue


def _neighbor(id_, kind, operator, number, unit="м", condition=None):
    return {
        "id": id_,
        "kind": kind,
        "value_operator": operator,
        "value_number": number,
        "value_unit": unit,
        "value_condition": condition,
    }


def test_disjoint_bounds_conflict():
    candidate = RestrictionValue(operator="<=", number=20, unit="м")
    neighbors = [_neighbor("n1", "минимальная_ширина", ">=", 25)]

    found = find_conflicts("r1", "минимальная_ширина", candidate, neighbors)

    assert len(found) == 1
    assert found[0].other_id == "n1"
    assert found[0].severity == "certain"


def test_equal_points_conflict():
    candidate = RestrictionValue(operator="=", number=10, unit="м")
    neighbors = [_neighbor("n1", "минимальная_ширина", "=", 15)]

    found = find_conflicts("r1", "минимальная_ширина", candidate, neighbors)

    assert len(found) == 1


def test_tighter_bound_same_side_is_not_a_conflict():
    candidate = RestrictionValue(operator="<=", number=20, unit="м")
    neighbors = [_neighbor("n1", "минимальная_ширина", "<=", 15)]

    assert find_conflicts("r1", "минимальная_ширина", candidate, neighbors) == []


def test_overlapping_bounds_not_a_conflict():
    candidate = RestrictionValue(operator=">=", number=10, unit="м")
    neighbors = [_neighbor("n1", "минимальная_ширина", "<=", 20)]

    assert find_conflicts("r1", "минимальная_ширина", candidate, neighbors) == []


def test_different_kind_skipped():
    candidate = RestrictionValue(operator="<=", number=20, unit="м")
    neighbors = [_neighbor("n1", "запрет_размещения", ">=", 25)]

    assert find_conflicts("r1", "минимальная_ширина", candidate, neighbors) == []


def test_mismatched_unit_skipped():
    candidate = RestrictionValue(operator="<=", number=20, unit="м")
    neighbors = [_neighbor("n1", "минимальная_ширина", ">=", 25, unit="%")]

    assert find_conflicts("r1", "минимальная_ширина", candidate, neighbors) == []


def test_non_comparable_operator_skipped():
    candidate = RestrictionValue(operator="range", number=20, unit="м")
    neighbors = [_neighbor("n1", "минимальная_ширина", ">=", 25)]

    assert find_conflicts("r1", "минимальная_ширина", candidate, neighbors) == []


def test_missing_candidate_value_skipped():
    neighbors = [_neighbor("n1", "минимальная_ширина", ">=", 25)]
    assert find_conflicts("r1", "минимальная_ширина", None, neighbors) == []


def test_self_reference_skipped():
    candidate = RestrictionValue(operator="<=", number=20, unit="м")
    neighbors = [_neighbor("r1", "минимальная_ширина", ">=", 25)]

    assert find_conflicts("r1", "минимальная_ширина", candidate, neighbors) == []


def test_differing_condition_is_only_possible_severity():
    candidate = RestrictionValue(
        operator="<=", number=20, unit="м", condition="для жилой застройки"
    )
    neighbors = [
        _neighbor(
            "n1",
            "минимальная_ширина",
            ">=",
            25,
            condition="для промышленной застройки",
        )
    ]

    found = find_conflicts("r1", "минимальная_ширина", candidate, neighbors)

    assert len(found) == 1
    assert found[0].severity == "possible"


def test_equal_condition_text_is_certain_severity():
    candidate = RestrictionValue(
        operator="<=", number=20, unit="м", condition="  Для Жилой  застройки"
    )
    neighbors = [
        _neighbor("n1", "минимальная_ширина", ">=", 25, condition="для жилой застройки")
    ]

    found = find_conflicts("r1", "минимальная_ширина", candidate, neighbors)

    assert len(found) == 1
    assert found[0].severity == "certain"
