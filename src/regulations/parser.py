"""Zone regulations of land-use and development rules (ПЗЗ), read from the document structure.

A ПЗЗ states its regulations zone by zone, the same way every time: a heading «Ж-2.15 ЗОНА
ЗАСТРОЙКИ …», a table of permitted uses (ВРИ) split into main / conditional / auxiliary rows,
and a table of limit parameters («Предельные … параметры»: height, floors, coverage, setbacks,
plot sizes). Both are tables in IDU_DVD (``table_html``), so they are read here directly — no
LLM — and every value keeps the fragment it came from.

The parser takes the fragments of a document in reading order and returns one
``ZoneRegulation`` per zone that has a use table or a parameter table. Zones with special use
conditions (Н-1 «санитарно-защитная зона», ЗОУИТ) are not territorial zones and are not
headed «… ЗОНА …» in capitals, so they are skipped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from src.regulations.tables import clean, table_rows

# «Ж-2.15 ЗОНА ЗАСТРОЙКИ …», «ПП ЗОНА ПРОМЫШЛЕННЫХ ПАРКОВ», «ОИ-1 Зона …», «Т.10 ЗОНА …»
_ZONE = re.compile(
    r"^(?P<code>[А-ЯЁ]{1,4}(?:[-.]\d+(?:\.\d+)*)?)\s+(?P<name>(?i:зона)\b.*)$", re.S
)
_CODE = re.compile(r"^[А-ЯЁ]{1,4}(?:[-.]\d+(?:\.\d+)*)?$")
_ARTICLE = re.compile(r"^Статья\s+(?P<number>\d+(?:\.\d+)*)\.?\s*(?P<title>.*)$", re.S)
_VRI_CODE = re.compile(r"\b\d{1,2}(?:\.\d{1,2}){0,3}\b")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_ROW_NUMBER = re.compile(r"^\d+(?:\.\d+)*\.?$")
_ARTICLE_REF = re.compile(r"стать[а-яё]*\s+(\d+(?:\.\d+)*)", re.I)
_PARAMS_CAPTION = re.compile(r"предельн[а-яё]*\s*\(?\s*минимальн", re.I)

SECTIONS = ("main", "conditional", "auxiliary")
_SECTION_WORDS = (
    ("основн", "main"),
    ("условно", "conditional"),
    ("вспомогат", "auxiliary"),
)


@dataclass
class PermittedUse:
    section: str  # main | conditional | auxiliary
    name: str
    description: str = ""
    codes: list[str] = field(default_factory=list)  # ВРИ classifier codes («2.1.1»)
    # «<*>»: the use applies only to plots under existing buildings
    only_existing: bool = False
    fragment_id: str | None = None


@dataclass
class ZoneParameter:
    name: str
    kind: str  # see ``classify``
    operator: str | None = None  # "<=" | ">=" | None
    value: float | None = None
    values: list[float] = field(default_factory=list)
    unit: str | None = None
    raw_value: str = ""
    not_set: bool = False  # «не подлежит установлению»
    minimum: float | None = None  # a size row with a minimum and a maximum
    maximum: float | None = None
    vri_codes: list[str] = field(
        default_factory=list
    )  # the row applies to these uses only
    except_vri_codes: list[str] = field(default_factory=list)
    building: str | None = (
        None  # "residential" | "non_residential" when the row says so
    )
    footnote: bool = False  # «*»: see the zone's notes (e.g. heritage requirements)
    number: str | None = None
    fragment_id: str | None = None


@dataclass
class ZoneRegulation:
    code: str
    name: str
    article: str | None = None  # «17.1»
    group: str | None = None  # «ЖИЛЫЕ ЗОНЫ»
    uses: list[PermittedUse] = field(default_factory=list)
    parameters: list[ZoneParameter] = field(default_factory=list)
    section_notes: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    see_articles: list[str] = field(default_factory=list)
    fragment_ids: list[str] = field(default_factory=list)
    amended_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- parameter rows ---------------------------------------------------------


def classify(name: str) -> tuple[str, str | None]:
    """The canonical kind of a parameter row and the operator its value is a limit with."""
    n = name.casefold()
    if "машино-мест" in n or "машиномест" in n:
        return "parking", ">="
    if "класс" in n and "опасност" in n:
        return "hazard_class", None
    if "процент застройки" in n:
        return "max_coverage", "<="
    if "озелен" in n:
        return "min_green_share", ">="
    if "расстояни" in n or "разрыв" in n:
        return "distance", ">="
    if "отступ" in n:
        return "setback", ">="
    if "этаж" in n:
        return "max_floors", "<="
    if "высот" in n:
        return "max_height", "<="
    if "участк" in n and ("размер" in n or "площад" in n):
        return "plot_size", None
    return "other", None


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", ".")) for x in _NUMBER.findall(text)]


_UNITS = {
    "эт": "этаж",
    "этаж": "этаж",
    "этажей": "этаж",
    "кв.м": "кв.м",
    "кв. м": "кв.м",
    "м2": "кв.м",
}


def _unit(unit: str | None) -> str | None:
    if not unit:
        return None
    key = unit.casefold().strip().rstrip(".")
    return _UNITS.get(key, key)


def _unit_from_name(name: str) -> tuple[str, str | None]:
    """«Предельная высота зданий, строений, сооружений, м.» -> (name, "м")."""
    m = re.search(r",\s*(м|%|эт|кв\.\s?м|га)\.?\s*$", name)
    if not m:
        return name, None
    return name[: m.start()].rstrip(), m.group(1)


def parse_parameter(cells: list[str], fragment_id: str | None) -> ZoneParameter | None:
    cells = [c for c in cells]
    if len(cells) < 2:
        return None
    number = cells[0] if _ROW_NUMBER.match(cells[0].strip()) else None
    rest = cells[1:] if (number or not cells[0]) else cells
    if len(rest) < 2:
        return None
    name, raw_value = rest[0], rest[-1]
    unit = clean(" ".join(rest[1:-1])) or None
    if not name or not raw_value or name.casefold() in ("параметры", "наименование"):
        return None
    if raw_value.casefold().startswith("предельн"):
        return None  # header row «№ п/п | Параметры | Предельные значения»
    if unit is None:
        name, unit = _unit_from_name(name)
    kind, operator = classify(name)
    if unit and "опасност" in unit.casefold():
        unit = None
    unit = _unit(unit)
    lower = name.casefold()
    param = ZoneParameter(
        name=clean(name),
        kind=kind,
        operator=operator,
        unit=unit,
        raw_value=raw_value,
        not_set="не подлеж" in raw_value.casefold(),
        footnote="*" in name,
        number=(number or "").rstrip(".") or None,
        fragment_id=fragment_id,
    )
    if kind in ("plot_size", "other"):
        # «Минимальные и (или) максимальные размеры» names both: no single operator
        has_max, has_min = "максимальн" in lower, "минимальн" in lower
        if has_max != has_min:
            param.operator = "<=" if has_max else ">="
    if "нежил" in lower:
        param.building = "non_residential"
    elif "жил" in lower and kind in ("max_height", "max_floors", "distance", "setback"):
        param.building = (
            "residential"
            if "жилой застройки" in lower or "жилого дома" in lower
            else None
        )
    scope = re.search(
        r"(кроме\s+)?(?:вид[а-яё]*\s+)?с\s+кодом\s+([\d.,\s]+)", name, re.I
    )
    if scope:
        codes = _VRI_CODE.findall(scope.group(2))
        if scope.group(1):
            param.except_vri_codes = codes
        else:
            param.vri_codes = codes
    elif "«" in name:
        param.vri_codes = _VRI_CODE.findall(" ".join(re.findall(r"«([^»]*)»", name)))
    if not param.not_set and kind not in ("hazard_class", "parking"):
        values = _numbers(raw_value)
        param.values = values
        if len(values) == 1:
            param.value = values[0]
        labels = [line.strip().casefold() for line in name.split("\n")]
        if (
            len(values) == 2
            and any(line.startswith("минимальн") for line in labels)
            and any(line.startswith("максимальн") for line in labels)
        ):
            param.minimum, param.maximum = values
    return param


# --- use rows ---------------------------------------------------------------


def _section_of(text: str) -> str | None:
    lower = text.casefold()
    if "вид" not in lower or "использ" not in lower:
        return None
    for word, section in _SECTION_WORDS:
        if word in lower:
            return section
    return None


def _uses_table(rows: list[list[str]]) -> bool:
    return any(
        "наименование вида разрешенного" in (row[0] or "").casefold()
        or any(_section_of(cell) for cell in row if cell)
        for row in rows[:3]
    )


def _parameter_table(rows: list[list[str]]) -> bool:
    head = [cell.casefold() for cell in rows[0]] if rows else []
    return bool(rows) and (
        any("предельные значения" in cell or cell == "параметры" for cell in head)
        or _ROW_NUMBER.match(rows[0][0].strip() or "x") is not None
        or any("машино-мест" in cell for row in rows for cell in row)
    )


class _Zone:
    def __init__(self, regulation: ZoneRegulation) -> None:
        self.reg = regulation
        self.section: str | None = None
        self.in_params = False
        self.last_table: str | None = None  # "uses" | "params"


def _add_uses(zone: _Zone, rows: list[list[str]], fragment_id: str | None) -> None:
    reg = zone.reg
    for row in rows:
        filled = [cell for cell in row if cell]
        if not filled:
            continue
        first = filled[0]
        if "наименование вида разрешенного" in first.casefold():
            continue
        if len(filled) == 1:
            section = _section_of(first)
            if section:
                zone.section = section
                if ":" in first:
                    reg.section_notes[section] = first
                continue
            if reg.uses:  # a description carried over from the previous page
                reg.uses[-1].description = f"{reg.uses[-1].description} {first}".strip()
            continue
        name = row[0] if row else ""
        code_cell = row[-1] if len(row) >= 3 else ""
        description = (
            " ".join(cell for cell in row[1:-1] if cell) if len(row) >= 3 else row[-1]
        )
        codes = _VRI_CODE.findall(code_cell)
        if not name and not codes and reg.uses:
            reg.uses[-1].description = (
                f"{reg.uses[-1].description} {description}".strip()
            )
            continue
        if not name:
            continue
        reg.uses.append(
            PermittedUse(
                section=zone.section or "main",
                name=clean(name.replace("<*>", "").replace("*", "")),
                description=description,
                codes=codes,
                only_existing="*" in name or "*" in code_cell,
                fragment_id=fragment_id,
            )
        )


def _add_parameters(
    zone: _Zone, rows: list[list[str]], fragment_id: str | None
) -> None:
    for row in rows:
        param = parse_parameter(row, fragment_id)
        if param is not None:
            zone.reg.parameters.append(param)


def _amended(reg: ZoneRegulation, frag) -> None:
    for act in getattr(frag, "amended_by", None) or []:
        if act not in reg.amended_by:
            reg.amended_by.append(act)


def parse_regulations(fragments: list) -> list[ZoneRegulation]:
    """Zone regulations of a ПЗЗ from its fragments (IDU_DVD ``DocumentFragment``-like).

    A fragment needs ``id``, ``text``, ``kind`` and ``table_html`` (and may carry
    ``amended_by``).
    """
    zones: list[ZoneRegulation] = []
    zone: _Zone | None = None
    article: tuple[str, str] | None = None
    for frag in fragments:
        text = (getattr(frag, "text", "") or "").strip()
        html = getattr(frag, "table_html", None)
        fid = getattr(frag, "id", None)
        is_table = bool(html) or getattr(frag, "kind", "") == "table"
        if not is_table:
            # IDU_DVD may keep a heading together with the note after it, or end a
            # fragment with the next zone's heading: every line is read on its own.
            # A code it took for numbering («Т.10») is put back before «ЗОНА …».
            numbering = (getattr(frag, "numbering", "") or "").strip()
            for index, line in enumerate(clean(line) for line in text.split("\n")):
                if not line:
                    continue
                if (
                    index == 0
                    and _CODE.match(numbering)
                    and line.casefold().startswith("зона")
                ):
                    line = f"{numbering} {line}"
                heading = _ARTICLE.match(line)
                if heading and len(line) < 200:
                    article = (heading.group("number"), clean(heading.group("title")))
                    zone = None
                    continue
                match = _ZONE.match(line) if len(line) < 300 else None
                if match:
                    zone = _Zone(
                        ZoneRegulation(
                            code=match.group("code"),
                            name=clean(match.group("name")),
                            article=article[0] if article else None,
                            group=article[1] if article else None,
                        )
                    )
                    zone.reg.fragment_ids.append(fid)
                    zones.append(zone.reg)
                    continue
                if zone is None:
                    continue
                zone.reg.fragment_ids.append(fid)
                _amended(zone.reg, frag)
                if _PARAMS_CAPTION.search(line):
                    zone.in_params = True
                    continue
                # The tail of a heading split over lines: «ЗРЗ 4» after «… ЗРЗ 1, ЗРЗ 3,».
                if (
                    not zone.reg.uses
                    and not zone.reg.notes
                    and len(line) < 80
                    and line == line.upper()
                ):
                    zone.reg.name = clean(f"{zone.reg.name} {line}")
                    continue
                zone.reg.notes.append(line)
                for ref in _ARTICLE_REF.findall(line):
                    if ref not in zone.reg.see_articles:
                        zone.reg.see_articles.append(ref)
            continue
        if zone is None:
            continue
        rows = table_rows(html) if html else [[line] for line in text.split("\n")]
        if not rows:
            continue
        zone.reg.fragment_ids.append(fid)
        if _uses_table(rows) and not zone.in_params:
            zone.last_table = "uses"
            _add_uses(zone, rows, fid)
        elif zone.in_params or _parameter_table(rows):
            zone.last_table = "params"
            _add_parameters(zone, rows, fid)
        elif zone.last_table == "uses":
            _add_uses(zone, rows, fid)  # the use table goes on over a page break
        _amended(zone.reg, frag)

    kept: dict[str, ZoneRegulation] = {}
    for reg in zones:
        if not (reg.uses or reg.parameters):
            continue  # a mention, not the zone's regulation
        if reg.code in kept:
            continue
        reg.fragment_ids = [f for f in dict.fromkeys(reg.fragment_ids) if f]
        kept[reg.code] = reg
    return list(kept.values())
