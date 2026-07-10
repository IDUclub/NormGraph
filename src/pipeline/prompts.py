"""Prompt and few-shot examples for restriction extraction (Russian normative texts).

The examples were derived from СП 42.13330.2016 and reviewed for the project. ``value`` is encoded
as flat string attributes (``value_operator`` / ``value_number`` / ``value_unit`` /
``value_condition``) because langextract attributes are string-valued; the extractor parses them
back into a ``RestrictionValue``. A clause with conditional norms yields several extractions — one
per value — as in the second example.
"""

from __future__ import annotations

import langextract as lx

# The extraction class label carried on every restriction extraction.
RESTRICTION_CLASS = "ограничение"

# Seed of the controlled restriction-kind vocabulary. Extended dynamically at ingest time:
# a kind that matches none of the known ones (by alias or embedding similarity) is added with
# status "pending" for later review.
SEED_KINDS: list[str] = [
    "запрет_размещения",
    "запрет_использования",
    "минимальное_расстояние",
    "минимальная_ширина",
    "минимальная_доля_площади",
    "предельная_высота",
    "плотность_застройки",
    "требование_размещения",
]

PROMPT_DESCRIPTION = (
    "Извлеки из текста нормативные ограничения. Каждое ограничение — это тройка:\n"
    "- subject: сущность, которая накладывает ограничение, дословно из текста "
    "(например «санитарно-защитная зона», «водоохранная зона»);\n"
    "- object: на что накладывается ограничение, кратко (например «объекты пищевой "
    "промышленности», «озелененная территория»);\n"
    "- kind: вид ограничения из списка: " + ", ".join(SEED_KINDS) + ". "
    "Если ни один не подходит — предложи короткий новый снейк-кейс код вида ограничения.\n"
    "Если в ограничении есть количественный параметр, добавь атрибуты: value_operator "
    "(>=, <=, >, <, =), value_number (число), value_unit (единица измерения: м, %, эт.), "
    "value_condition (условие применимости, если оно есть).\n"
    "Используй точные фрагменты исходного текста для extraction_text. "
    "Не извлекай ограничений, которых нет в тексте."
)


def _ex(text: str, attrs: dict[str, str]) -> lx.data.Extraction:
    return lx.data.Extraction(
        extraction_class=RESTRICTION_CLASS,
        extraction_text=text,
        attributes=attrs,
    )


EXAMPLES: list[lx.data.ExampleData] = [
    lx.data.ExampleData(
        text=(
            "В границах санитарно-защитной зоны не допускается использование "
            "земельных участков по [22]."
        ),
        extractions=[
            _ex(
                "не допускается использование земельных участков",
                {
                    "subject": "санитарно-защитная зона",
                    "object": "использование земельных участков",
                    "kind": "запрет_использования",
                },
            )
        ],
    ),
    lx.data.ExampleData(
        text=(
            "В санитарно-защитных зонах со стороны жилых и общественно-деловых зон "
            "необходимо предусматривать полосу древесно-кустарниковых насаждений "
            "шириной не менее 50 м, а при ширине зоны до 100 м – не менее 20 м."
        ),
        extractions=[
            _ex(
                "полосу древесно-кустарниковых насаждений шириной не менее 50 м",
                {
                    "subject": "санитарно-защитная зона",
                    "object": "полоса древесно-кустарниковых насаждений",
                    "kind": "минимальная_ширина",
                    "value_operator": ">=",
                    "value_number": "50",
                    "value_unit": "м",
                },
            ),
            _ex(
                "при ширине зоны до 100 м – не менее 20 м",
                {
                    "subject": "санитарно-защитная зона",
                    "object": "полоса древесно-кустарниковых насаждений",
                    "kind": "минимальная_ширина",
                    "value_operator": ">=",
                    "value_number": "20",
                    "value_unit": "м",
                    "value_condition": "ширина зоны до 100 м",
                },
            ),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "На территории животноводческих комплексов и ферм и в их "
            "санитарно-защитных зонах не допускается размещать объекты пищевой "
            "промышленности."
        ),
        extractions=[
            _ex(
                "не допускается размещать объекты пищевой промышленности",
                {
                    "subject": "санитарно-защитная зона животноводческого комплекса",
                    "object": "объекты пищевой промышленности",
                    "kind": "запрет_размещения",
                },
            )
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Объекты с размерами санитарно-защитной зоны свыше 300 м следует "
            "размещать на обособленных земельных участках за пределами границ "
            "сельских населенных пунктов."
        ),
        extractions=[
            _ex(
                "следует размещать на обособленных земельных участках за пределами "
                "границ сельских населенных пунктов",
                {
                    "subject": "объект с санитарно-защитной зоной свыше 300 м",
                    "object": "размещение объекта",
                    "kind": "требование_размещения",
                    "value_operator": ">",
                    "value_number": "300",
                    "value_unit": "м",
                    "value_condition": "за пределами границ населенного пункта",
                },
            )
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Площадь озелененной территории микрорайона должна составлять не менее "
            "25 % площади территории микрорайона."
        ),
        extractions=[
            _ex(
                "должна составлять не менее 25 %",
                {
                    "subject": "микрорайон жилой зоны",
                    "object": "озелененная территория",
                    "kind": "минимальная_доля_площади",
                    "value_operator": ">=",
                    "value_number": "25",
                    "value_unit": "%",
                },
            )
        ],
    ),
]
