"""The closed list of restriction kinds and the rules that put a norm into one of them.

The model used to coin a new snake-case kind whenever none of the eight seed kinds fit: on
dev that grew to hundreds of kinds, most used once («требование_частоты_уборки»,
«запрет_обратной_силы»), and the same norm extracted twice often got two kinds. A kind is now
one of ``KINDS``. The model is asked to choose from them; whatever label it returns is kept
on the restriction as ``kind_label`` and mapped here, the value and its measurement deciding
for a quantitative norm (a distance «не более 500 м» is a maximum distance whatever the label
says).
"""

from __future__ import annotations

import re

from src.pipeline.models import RestrictionMeasurement, RestrictionValue

OTHER = "прочее"

# Code → what the kind covers; the descriptions go into the extraction prompt.
KINDS: dict[str, str] = {
    "запрет_размещения": "нельзя размещать, строить или располагать объект (в зоне, рядом)",
    "запрет_использования": "запрещено действие, деятельность или использование, кроме размещения",
    "требование_размещения": "объект должен быть размещён, предусмотрен или иметься",
    "допустимость": "разрешение, допущение или исключение из правила («допускается», «не требуется»)",
    "минимальное_расстояние": "расстояние, отступ или разрыв между объектами не менее N",
    "максимальное_расстояние": "расстояние или радиус доступности не более N (в метрах)",
    "время_доступности": "время пешей или транспортной доступности объекта (в минутах)",
    "минимальный_размер": "ширина, длина, площадь, глубина или радиус самого объекта не менее N",
    "максимальный_размер": "ширина, длина, площадь, глубина или радиус самого объекта не более N",
    "предельная_высота": "высота или этажность здания",
    "минимальная_доля_площади": "доля площади территории не менее N % (озеленение, площадки)",
    "максимальная_доля_площади": "доля площади территории не более N % (процент застройки)",
    "плотность_застройки": "плотность или коэффициент застройки, площадь на 1 га",
    "обеспеченность": "число мест, объектов или площадь на жителей или на человека",
    "количество": "число объектов, мест, людей; вместимость или наполняемость",
    "срок": "срок, продолжительность, периодичность, время суток",
    "физический_параметр": "температура, освещённость, шум, влажность, воздухообмен и др.",
    "требование_к_объекту": "качественное требование к устройству, оборудованию, отделке",
    "процедурное_требование": "документы, согласования, расчёты, порядок действий, полномочия",
    OTHER: "ничто из перечисленного",
}

# Former names of kinds now in the list.
ALIASES = {
    "минимальная_ширина": "минимальный_размер",
}

_MIN = {">=", ">"}
_MAX = {"<=", "<"}

_DISTANCE_UNIT = re.compile(r"^(?:м|метр\w*|км|километр\w*|m|km)$")
_AREA_UNIT = re.compile(r"^(?:м2|м²|кв\.?\s*м\w*|га|гектар\w*|m2)\b")
_TIME_UNIT = re.compile(
    r"^(?:мин\w*|ч|час\w*|сут\w*|дн\w*|дня|день|недел\w*|мес\w*|год\w*|лет)$"
)
_FLOOR_UNIT = re.compile(r"^(?:эт\.?|этаж\w*)$")
_PERCENT_UNIT = re.compile(r"^(?:%|процент\w*)$")
_PHYSICAL_UNIT = re.compile(
    r"^(?:°\s*c?|градус\w*|лк|люкс\w*|дб\w*|дба|к?па|мпа|квт|вт|в|а|мм|см|"
    r"м3/ч|м/с|л/с|мг\w*|ppm|%\s*влажн\w*)$"
)

_DISTANCE = re.compile(r"расстоян|удал[её]н|отступ|разрыв|до\s+(?:границ|ближайш)")
_ACCESS = re.compile(r"доступност|радиус\w*\s+обслуж")
_SIZE = re.compile(r"ширин|длин|глубин|диаметр|радиус|размер|площад|габарит|толщин")
_HEIGHT = re.compile(r"высот|этажн|этаж")
_SHARE = re.compile(r"озелен|застро|площад")
_DENSITY = re.compile(
    r"плотност\w*\s+(?:застрой|жил|населен|заселен)|коэффициент\w*\s+(?:застрой|использ)|"
    r"на\s+1\s*га"
)
_PROVISION = re.compile(
    r"обеспеченност|уровень\s+обеспечен|на\s+1\s*000|на\s+человек|на\s+жител|"
    r"на\s+(?:одного|1)\s+(?:жител|человек|чел|ребен|учащ|посетит|место)"
)
_PROHIBIT = re.compile(
    r"^(?:запрет|не_допуска|недопуст)|не\s+допуска|запрещ|не\s+разреша"
)
_ALLOW = re.compile(
    r"^(?:разрешени|допуска|допустим|исключени|не_требует|не_применя|не_распростран|"
    r"не_нормир|не_учитыв|не_включа|не_устанавл|условн|право)"
)
_PLACEMENT = re.compile(r"размещ|располож|строит|сооруж|возвед|застрой|участ")
# Label stems; a short one only at a word start («акт», not «контакт»).
_PROCEDURE = re.compile(
    r"документ|согласов|проект|расчет|расчёт|утвержд|порядок|заявк|полномоч|решени|закон|"
    r"выдач|регистрац|отчет|сведени|схем|изыскан|экспертиз|уведомл|опубликов|публичн|"
    r"членств|договор|информац|контрол|надзор|(?:^|_)(?:акт|прав[оа]|учет|учёт)"
)
_TIME = re.compile(
    r"срок|время|времен|период|продолжительн|длительн|частот|периодичн|(?:^|_)час"
)
_PHYSICAL = re.compile(
    r"температур|освещ|шум|влажн|воздухообм|вентиляц|звук|акустич|излучен|концентрац|"
    r"инсоляц|прочност|деформац|нагрузк|давлен|тепло|огнестойк|уклон|отражен"
)
_COUNT = re.compile(r"числ|количеств|вместим|наполняем|мест|групп|коек|count")


def canonical_kind(label: str | None) -> str | None:
    """The listed kind a label names, if it names one."""
    label = (label or "").strip()
    label = ALIASES.get(label, label)
    return label if label in KINDS else None


def _directed(value: RestrictionValue, minimum: str, maximum: str) -> str:
    return maximum if value.operator in _MAX else minimum


def _quantitative(
    value: RestrictionValue,
    measurement: RestrictionMeasurement | None,
    words: str,
) -> str | None:
    """The kind of a norm with a number, decided by its unit and measurement."""
    unit = (value.unit or "").strip().casefold()
    mk = measurement.kind if measurement else None
    if mk == "area_share" or (_PERCENT_UNIT.match(unit) and _SHARE.search(words)):
        if _DENSITY.search(words):
            return "плотность_застройки"
        return _directed(value, "минимальная_доля_площади", "максимальная_доля_площади")
    if mk == "provision" or _PROVISION.search(words):
        return "обеспеченность"
    if _FLOOR_UNIT.match(unit) or (_HEIGHT.search(words) and not _SIZE.search(words)):
        return "предельная_высота"
    if _DENSITY.search(words):
        return "плотность_застройки"
    if _TIME_UNIT.match(unit):
        return "время_доступности" if _ACCESS.search(words) else "срок"
    if _DISTANCE_UNIT.match(unit):
        distance = _directed(value, "минимальное_расстояние", "максимальное_расстояние")
        size = _directed(value, "минимальный_размер", "максимальный_размер")
        if mk in ("distance", "linear_size"):
            return distance if mk == "distance" else size
        if _DISTANCE.search(words) or _ACCESS.search(words):
            return distance
        return size if _SIZE.search(words) else None
    if _AREA_UNIT.match(unit):
        return _directed(value, "минимальный_размер", "максимальный_размер")
    if _PHYSICAL_UNIT.match(unit) or _PHYSICAL.search(words):
        return "физический_параметр"
    if mk == "count_share" or _COUNT.search(words) or _PERCENT_UNIT.match(unit):
        return "количество"
    return None


def _qualitative(label: str, text: str) -> str:
    """The kind of a norm without a number, decided by its label (then its text)."""
    if _ALLOW.search(label):
        return "допустимость"
    if _PROHIBIT.search(label) or (not label and _PROHIBIT.search(text)):
        return (
            "запрет_размещения" if _PLACEMENT.search(label) else "запрет_использования"
        )
    if re.search(r"размещ|наличи|предусм", label):
        return "требование_размещения"
    if _PROVISION.search(label.replace("_", " ")):
        return "обеспеченность"
    if _HEIGHT.search(label):
        return "предельная_высота"
    if _TIME.search(label):
        return "срок"
    if _PHYSICAL.search(label):
        return "физический_параметр"
    if _PROCEDURE.search(label):
        return "процедурное_требование"
    if label.startswith(("требовани", "обязан", "необходим")):
        return "требование_к_объекту"
    return OTHER


def classify_kind(
    label: str | None,
    value: RestrictionValue | None = None,
    measurement: RestrictionMeasurement | None = None,
    text: str = "",
) -> str:
    """The listed kind of a restriction the model labelled ``label``."""
    label = (label or "").strip().casefold()
    listed = canonical_kind(label)
    indicator = measurement.indicator if measurement else None
    words = f"{label.replace('_', ' ')} {indicator or ''}".casefold()
    quantitative = value is not None and value.number is not None
    # The label, indicator and unit name the quantity; the quote only when they do not.
    if quantitative and (found := _quantitative(value, measurement, words)):
        return found
    if listed:
        return listed
    if quantitative and (
        found := _quantitative(value, measurement, f"{words} {text}".casefold())
    ):
        return found
    return _qualitative(label, text.casefold())
