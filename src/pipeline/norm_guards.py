"""Precision guards: reasons a candidate plan would check something the norm does not say.

Each guard targets a defect observed in production plans (2026-09-30 audit):

* a quantity label used as a layer («отступ от красной линии» as ``targets``);
* an upper bound read as a lower one («не реже чем через 100 м» → ``>= 100``);
* an in-building scale (cots, shelves, tanks — distances of a few metres);
* a non-territorial share («площадь окон ≥ 20 % площади стены» as ``zonal_ratio``).

The guards only block; they never repair. A blocked candidate is kept for review and,
when an LLM is configured, the norm is rewritten from its source text.
"""

from __future__ import annotations

import re

# Upper/lower bound wording around a number.
_MAX_MARKERS = re.compile(
    r"не\s+(?:более|выше|далее|дальше|реже|чаще|глубже|превыша\w*)|"
    r"не\s+должн\w*\s+превыша\w*|максимальн\w*|предельн\w*\s+(?:расстояни|удаленн)|"
    r"не\s+свыше",
    re.I,
)
_MIN_MARKERS = re.compile(
    r"не\s+(?:менее|ближе|ниже|меньше)|минимальн\w*|не\s+допуска\w*\s+ближе",
    re.I,
)
# A rhythm along a line (hydrants, trees, ladders), not a distance between layers.
_PERIODIC = re.compile(
    r"через\s+кажд\w*|не\s+(?:реже|чаще)\b[^.;]{0,25}\bчерез\b|"
    r"\bчерез\s+\d+(?:[.,]\d+)?\s*(?:м|км|метр\w*)\b|с\s+шагом|с\s+интервал\w*",
    re.I,
)
# Vertical measures are not planar buffers.
_DEPTH = re.compile(r"глубин\w*|заглублени\w*|не\s+глубже|высот\w*\s+над", re.I)
# Quantity/measure words that name a value, never a mapped object.
_MEASURE_LABEL = re.compile(
    r"\b(?:отступ\w*|удаленн\w*|удалени\w*|разрыв\w*|интервал\w*|промежут\w*|"
    r"габарит\w*|глубин\w*|расположени\w*|размер\w*)\b",
    re.I,
)
# Parts and furnishings of a building: the share or distance is not territorial.
_BUILDING_PART = re.compile(
    r"\b(?:окн\w*|оконн\w*|стен\w*|потол\w*|пол|полов|помещени\w*|комнат\w*|"
    r"квартир\w*|фасад\w*|кровл\w*|двер\w*|проем\w*|лестниц\w*|коридор\w*|"
    r"санузл\w*|кухн\w*|шкаф\w*|стеллаж\w*|кроват\w*|кроватк\w*|поддон\w*|бак\w*|"
    r"оборудовани\w*|прокладк\w*|раковин\w*|умывальник\w*)\b",
    re.I,
)


def is_measure_label(label: str) -> bool:
    return bool(_MEASURE_LABEL.search(label))


def is_building_part(label: str) -> bool:
    return bool(_BUILDING_PART.search(label))


def direction_conflict(text: str, operator: str | None) -> bool:
    """The wording bounds the value from the other side than ``operator`` does.

    Only an unambiguous text decides: wording with both kinds of markers (a range,
    a table) is left to the other guards.
    """
    if not operator or not text:
        return False
    has_max = bool(_MAX_MARKERS.search(text))
    has_min = bool(_MIN_MARKERS.search(text))
    if has_max == has_min:
        return False
    if operator in {">", ">="}:
        return has_max
    if operator in {"<", "<="}:
        return has_min
    return False


def precision_reasons(
    text: str,
    *,
    operator: str | None = None,
    distance_m: float | None = None,
    labels: tuple[str, ...] = (),
    min_distance_m: float = 3.0,
) -> list[str]:
    """Reasons a distance/ratio candidate built from ``text`` must not run automatically."""
    reasons = []
    if direction_conflict(text, operator):
        reasons.append("operator_direction_conflict")
    if _PERIODIC.search(text):
        reasons.append("periodic_spacing_not_supported")
    if distance_m is not None:
        if _DEPTH.search(text):
            reasons.append("depth_not_distance")
        if distance_m < min_distance_m:
            reasons.append("distance_below_territorial_scale")
    if any(is_measure_label(label) for label in labels):
        reasons.append("measure_label_as_entity")
    if any(is_building_part(label) for label in labels):
        reasons.append("non_territorial_entity")
    return reasons
