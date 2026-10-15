"""Zone regulations are read from a ПЗЗ's structure: headings, use tables, parameter tables."""

from types import SimpleNamespace

import pytest

from src.dvd_client.models import DocumentDetail, DocumentFragment
from src.regulations.parser import classify, parse_parameter, parse_regulations
from src.regulations.service import RegulationService, is_pzz


def _cell(text):
    return f'<td colspan="1" rowspan="1"><p>{text}</p></td>'


def _table(*rows):
    body = ""
    for row in rows:
        if isinstance(row, str):  # a section row spanning the table
            body += f'<tr><td colspan="3" rowspan="1"><p>{row}</p></td></tr>'
        else:
            body += "<tr>" + "".join(_cell(c) for c in row) + "</tr>"
    return f"<table>{body}</table>"


USES = _table(
    [
        "Наименование вида разрешенного использования земельного участка",
        "Описание вида разрешенного использования земельного участка",
        "Код",
    ],
    "ОСНОВНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ",
    [
        "Малоэтажная многоквартирная жилая застройка",
        "Размещение малоэтажного дома",
        "2.1.1",
    ],
    ["Магазины", "Размещение объектов торговли", "4.4"],
    "УСЛОВНО РАЗРЕШЕННЫЕ ВИДЫ ИСПОЛЬЗОВАНИЯ",
    ["Размещение гаражей для собственных нужд<*>", "Размещение гаражей", "2.7.2"],
    "ВСПОМОГАТЕЛЬНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ: не подлежат установлению",
)
PARAMS = _table(
    [
        "1.",
        "Минимальное расстояние от красной линии улиц до стены жилого дома",
        "м",
        "5",
    ],
    [
        "7.",
        "Максимальная высота застройки* для всех видов разрешенного использования, "
        "кроме вида с кодом 2.7.2",
        "м",
        "15",
    ],
    [
        "7.1",
        "предельное количество этажей для вида разрешенного использования с кодом 2.7.2",
        "этаж",
        "2",
    ],
    [
        "8.",
        "Минимальные и (или) максимальные размеры земельных участков",
        "",
        "Не подлежат установлению",
    ],
    ["9.", "Максимальный процент застройки земельного участка", "%", "40"],
)
CAPTION = (
    "Предельные (минимальные и (или) максимальные) размеры земельных участков, предельные "
    "параметры разрешенного строительства, реконструкции объектов капитального строительства"
)


def _frag(fid, text="", html=None, numbering="", amended_by=()):
    return SimpleNamespace(
        id=fid,
        text=text,
        kind="table" if html else "text",
        table_html=html,
        numbering=numbering,
        amended_by=list(amended_by),
    )


def test_a_zone_is_read_from_separate_blocks():
    zones = parse_regulations(
        [
            _frag("a", "Статья 17.1. ЖИЛЫЕ ЗОНЫ"),
            _frag(
                "h", "Ж-2.15    ЗОНА ЗАСТРОЙКИ МАЛОЭТАЖНЫМИ ЖИЛЫМИ ДОМАМИ ЗРЗ 1, ЗРЗ 3,"
            ),
            _frag("h2", "ЗРЗ 4"),
            _frag("n", "(устанавливается в зонах ограничений по условиям охраны ОКН)"),
            _frag("u", "", USES, amended_by=["Приказ № 170"]),
            _frag("c", CAPTION),
            _frag("p", "", PARAMS),
            _frag(
                "s", "Иные требования выполнять в соответствии со статьей 16 Правил."
            ),
        ]
    )

    (zone,) = zones
    assert zone.code == "Ж-2.15"
    assert zone.name.endswith("ЗРЗ 1, ЗРЗ 3, ЗРЗ 4")
    assert (zone.article, zone.group) == ("17.1", "ЖИЛЫЕ ЗОНЫ")
    assert [(u.section, u.codes, u.only_existing) for u in zone.uses] == [
        ("main", ["2.1.1"], False),
        ("main", ["4.4"], False),
        ("conditional", ["2.7.2"], True),
    ]
    assert zone.section_notes["auxiliary"].endswith("не подлежат установлению")
    assert zone.amended_by == ["Приказ № 170"]
    assert zone.see_articles == ["16"]
    by_number = {p.number: p for p in zone.parameters}
    assert (by_number["1"].kind, by_number["1"].operator, by_number["1"].value) == (
        "distance",
        ">=",
        5.0,
    )
    height = by_number["7"]
    assert (height.kind, height.value, height.except_vri_codes, height.footnote) == (
        "max_height",
        15.0,
        ["2.7.2"],
        True,
    )
    floors = by_number["7.1"]
    assert (floors.kind, floors.value, floors.unit, floors.vri_codes) == (
        "max_floors",
        2.0,
        "этаж",
        ["2.7.2"],
    )
    plot = by_number["8"]
    assert (plot.kind, plot.not_set, plot.operator, plot.value) == (
        "plot_size",
        True,
        None,
        None,
    )
    assert by_number["9"].kind == "max_coverage"


def test_a_zone_is_read_from_merged_dvd_fragments():
    zones = parse_regulations(
        [
            _frag("a", "Статья 17.5. ПРОИЗВОДСТВЕННЫЕ ЗОНЫ"),
            # IDU_DVD took the code for numbering and kept the note with the heading
            _frag(
                "h",
                "ЗОНА ОБЪЕКТОВ ЖЕЛЕЗНОДОРОЖНОГО ТРАНСПОРТА ОЗ-1\n(устанавливается в зонах ОЗ 1)",
                numbering="Т.10",
            ),
            _frag("u", "", USES),
            _frag("c", f"{CAPTION}\n"),
            _frag("p", "", PARAMS),
            # one fragment ends a zone and starts the next
            _frag(
                "e",
                "Иные требования по статье 16 Правил.\nИ ЗОНА ОБЪЕКТОВ ИНЖЕНЕРНОЙ ИНФРАСТРУКТУРЫ",
            ),
            _frag("u2", "", USES),
        ]
    )

    assert [(z.code, len(z.uses), len(z.parameters)) for z in zones] == [
        ("Т.10", 3, 5),
        ("И", 3, 0),
    ]
    assert zones[0].notes[0].startswith("(устанавливается")


def test_a_mention_of_a_zone_is_not_a_regulation():
    zones = parse_regulations(
        [
            _frag("m", "Ж-1 Зона застройки индивидуальными жилыми домами"),
            _frag("x", "Текст статьи о порядке применения."),
            _frag("h", "Ж-1 ЗОНА ЗАСТРОЙКИ ИНДИВИДУАЛЬНЫМИ ЖИЛЫМИ ДОМАМИ"),
            _frag("u", "", USES),
        ]
    )
    assert [(z.code, z.fragment_ids[0]) for z in zones] == [("Ж-1", "h")]


def _span_table(*rows):
    """Rows of ``(text, colspan)`` cells; a bare string is a one-column cell."""
    body = ""
    for row in rows:
        body += "<tr>"
        for cell in row:
            text, span = cell if isinstance(cell, tuple) else (cell, 1)
            body += f'<td colspan="{span}" rowspan="1"><p>{text}</p></td>'
        body += "</tr>"
    return f"<table>{body}</table>"


# Южно-Сахалинск: one table per zone, the limits in the row of each use
COMBINED_HEAD = (
    [
        ("Виды разрешенного использования", 2),
        ("Предельные (минимальные и (или) максимальные) размеры земельных участков", 5),
        "Иные предельные параметры разрешенного строительства",
    ],
    [
        "Код",
        "Наименование вида разрешенного использования",
        ("Площадь земельного участка (кв. м)", 2),
        "Процент застройки (%)",
        "Этажность (эт.)",
        "Высота (м)",
    ],
    ["мин.", "макс.", "макс.", "макс.", "макс."],  # code and name span down
)
COMBINED = _span_table(
    *COMBINED_HEAD,
    [("ОСНОВНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ", 8)],
    [
        "2.5",
        "Среднеэтажная жилая застройка",
        "3000",
        "н.у.",
        "45",
        "8",
        "35",
        "Минимальный процент озеленения - 25%. Площадь стилобата не подлежит учету",
    ],
    ["3.1.1", "Предоставление коммунальных услуг"] + ["н.у."] * 5 + [""],
    [("(в ред. Постановления Администрации от 01.02.2023 № 100-па)", 8)],
)
COMBINED_NEXT_PAGE = _span_table(
    [("УСЛОВНО РАЗРЕШЕННЫЕ ВИДЫ ИСПОЛЬЗОВАНИЯ", 8)],
    [
        "4.4*",
        "Магазины &lt;1&gt;",
        "200</p><p>1000 &lt;1&gt;",
        "н.у.",
        "60",
        "3",
        "15",
        "-",
    ],
)
STAMP = _span_table(["", "", "Постановлением Администрации города от 01.02.2023", ""])


def test_a_zone_is_read_from_a_combined_table():
    heading = 'Градостроительный регламент территориальных зон вида "{}" {}'
    zones = parse_regulations(
        [
            _frag(
                "h", "8.2.2. " + heading.format("Ж1С", "Зона среднеэтажной застройки.")
            ),
            _frag("t", "", COMBINED),
            _frag("s", "", STAMP),
            _frag("t2", "", COMBINED_NEXT_PAGE),  # the table goes on over a page break
            _frag("h2", "8.2.17. " + heading.format("П", "Производственная зона.")),
            _frag("t3", "", COMBINED),
        ]
    )

    assert [(z.code, z.name) for z in zones] == [
        ("Ж1С", "Зона среднеэтажной застройки"),
        ("П", "Производственная зона"),
    ]
    zone = zones[0]
    assert [(u.section, u.name, u.codes, u.only_existing) for u in zone.uses] == [
        ("main", "Среднеэтажная жилая застройка", ["2.5"], False),
        ("main", "Предоставление коммунальных услуг", ["3.1.1"], False),
        ("conditional", "Магазины", ["4.4"], True),
    ]
    housing = {p.name: p for p in zone.parameters if p.vri_codes == ["2.5"]}
    plot_min = housing["Площадь земельного участка (кв. м) мин."]
    plot_max = housing["Площадь земельного участка (кв. м) макс."]
    assert (plot_min.kind, plot_min.operator, plot_min.minimum, plot_min.unit) == (
        "plot_size",
        ">=",
        3000.0,
        "кв.м",
    )
    assert (plot_max.operator, plot_max.not_set) == ("<=", True)
    by_kind = {p.kind: p for p in housing.values()}
    assert (by_kind["max_coverage"].value, by_kind["max_coverage"].unit) == (45.0, "%")
    assert (by_kind["max_floors"].value, by_kind["max_height"].value) == (8.0, 35.0)
    assert not by_kind["other"].not_set  # «не подлежит учету» inside a note
    shop = [p for p in zone.parameters if p.vri_codes == ["4.4"]]
    assert (shop[0].values, shop[0].footnote) == ([200.0, 1000.0], True)
    assert "other" not in {p.kind for p in shop}  # «-»
    assert len(zones[1].uses) == 2


@pytest.mark.parametrize(
    "name, kind, operator",
    [
        ("Максимальная высота зданий, строений, сооружений", "max_height", "<="),
        ("Максимальное количество этажей от планировочной отметки", "max_floors", "<="),
        (
            "Предельная этажность (максимальное количество надземных этажей)",
            "max_floors",
            "<=",
        ),
        (
            "Максимальный процент застройки в границах земельного участка",
            "max_coverage",
            "<=",
        ),
        ("Минимальные отступы от границ земельных участков", "setback", ">="),
        ("Минимальное расстояние между длинными сторонами зданий", "distance", ">="),
        ("Класс опасности размещаемых объектов", "hazard_class", None),
        ("Минимальная площадь озелененных территорий", "min_green_share", ">="),
        ("Минимальное количество машино-мест для хранения", "parking", ">="),
    ],
)
def test_parameter_rows_are_classified(name, kind, operator):
    assert classify(name) == (kind, operator)


def test_a_three_column_row_takes_its_unit_from_the_name():
    param = parse_parameter(
        [
            "4",
            "Предельная этажность (максимальное количество надземных этажей), эт.",
            "12",
        ],
        "f",
    )
    assert (param.kind, param.value, param.unit, param.number) == (
        "max_floors",
        12.0,
        "этаж",
        "4",
    )
    header = parse_parameter(["№ п/п", "Параметры", "Предельные значения"], "f")
    assert header is None


def test_a_size_row_with_a_minimum_and_a_maximum():
    param = parse_parameter(
        [
            "2.",
            "Размеры земельных участков вида «ведение садоводства 13.2»\nминимальный\nмаксимальный",
            "кв.м",
            "600\n1200",
        ],
        "f",
    )
    assert (param.kind, param.minimum, param.maximum, param.vri_codes) == (
        "plot_size",
        600.0,
        1200.0,
        ["13.2"],
    )


def _detail(**overrides):
    data = {
        "doc_id": "d1",
        "name": "Правила землепользования и застройки МО «Город»",
        "fragments": [
            DocumentFragment(id="h", text="Ж-1 ЗОНА ЗАСТРОЙКИ"),
            DocumentFragment(id="u", kind="table", table_html=USES),
            DocumentFragment(id="h2", text="Ж-2 ЗОНА ЗАСТРОЙКИ"),
            DocumentFragment(id="u2", kind="table", table_html=USES),
        ],
    }
    data.update(overrides)
    return DocumentDetail(**data)


def test_only_land_use_rules_are_read():
    assert is_pzz(_detail())
    assert not is_pzz(_detail(name="СП 42.13330.2016"))
    assert not is_pzz(_detail(name="Приказ № 170", amends="Правила землепользования"))


class FakeStore:
    def __init__(self):
        self.saved = {}

    async def replace(self, doc_id, zones):
        self.saved[doc_id] = zones
        return len(zones)


@pytest.mark.asyncio
async def test_rebuild_stores_the_zones_or_clears_them():
    store = FakeStore()
    service = RegulationService(store)

    assert await service.rebuild(_detail()) == 2
    assert [z.code for z in store.saved["d1"]] == ["Ж-1", "Ж-2"]

    assert await service.rebuild(_detail(name="СП 42.13330.2016")) == 0
    assert store.saved["d1"] == []
