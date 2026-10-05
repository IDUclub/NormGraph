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
    ["Малоэтажная многоквартирная жилая застройка", "Размещение малоэтажного дома", "2.1.1"],
    ["Магазины", "Размещение объектов торговли", "4.4"],
    "УСЛОВНО РАЗРЕШЕННЫЕ ВИДЫ ИСПОЛЬЗОВАНИЯ",
    ["Размещение гаражей для собственных нужд<*>", "Размещение гаражей", "2.7.2"],
    "ВСПОМОГАТЕЛЬНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ: не подлежат установлению",
)
PARAMS = _table(
    ["1.", "Минимальное расстояние от красной линии улиц до стены жилого дома", "м", "5"],
    [
        "7.",
        "Максимальная высота застройки* для всех видов разрешенного использования, "
        "кроме вида с кодом 2.7.2",
        "м",
        "15",
    ],
    ["7.1", "предельное количество этажей для вида разрешенного использования с кодом 2.7.2", "этаж", "2"],
    ["8.", "Минимальные и (или) максимальные размеры земельных участков", "", "Не подлежат установлению"],
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
            _frag("h", "Ж-2.15    ЗОНА ЗАСТРОЙКИ МАЛОЭТАЖНЫМИ ЖИЛЫМИ ДОМАМИ ЗРЗ 1, ЗРЗ 3,"),
            _frag("h2", "ЗРЗ 4"),
            _frag("n", "(устанавливается в зонах ограничений по условиям охраны ОКН)"),
            _frag("u", "", USES, amended_by=["Приказ № 170"]),
            _frag("c", CAPTION),
            _frag("p", "", PARAMS),
            _frag("s", "Иные требования выполнять в соответствии со статьей 16 Правил."),
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
            _frag("e", "Иные требования по статье 16 Правил.\nИ ЗОНА ОБЪЕКТОВ ИНЖЕНЕРНОЙ ИНФРАСТРУКТУРЫ"),
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


@pytest.mark.parametrize(
    "name, kind, operator",
    [
        ("Максимальная высота зданий, строений, сооружений", "max_height", "<="),
        ("Максимальное количество этажей от планировочной отметки", "max_floors", "<="),
        ("Предельная этажность (максимальное количество надземных этажей)", "max_floors", "<="),
        ("Максимальный процент застройки в границах земельного участка", "max_coverage", "<="),
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
        ["4", "Предельная этажность (максимальное количество надземных этажей), эт.", "12"],
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
        ["2.", "Размеры земельных участков вида «ведение садоводства 13.2»\nминимальный\nмаксимальный", "кв.м", "600\n1200"],
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
