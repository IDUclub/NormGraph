"""Linked clauses reach extraction: a value given by reference is read, not invented."""

from types import SimpleNamespace

import langextract as lx
import pytest

from src.dvd_client.models import DocumentDetail, DocumentFragment, DocumentRef
from src.ingestion.service import _InternalTargets
from src.pipeline.clause_context import ClauseContext
from src.pipeline.extractor import RestrictionExtractor
from src.pipeline.norm_refiner import PlanContext, _related_block

TABLE = "Таблица 7.2\nОбъект | Расстояние\nАЗС | 50 м\nСклады | 100 м"


def _context(**overrides):
    depends = overrides.pop("depends", [])
    references = overrides.pop(
        "references",
        [{"raw": "таблице 7.2", "node_id": "t72", "numbering": "", "text": TABLE}],
    )
    return ClauseContext.from_row(depends, references, own_node_id="own")


def test_context_orders_references_first_and_drops_weak_or_topical_links():
    context = _context(
        depends=[
            {
                "node_id": "lead",
                "text": "Расстояния следует принимать:",
                "relation": "completes",
                "weight": 1.0,
            },
            {
                "node_id": "topic",
                "text": "Общие положения",
                "relation": "same_topic",
                "weight": 0.3,
            },
            {
                "node_id": "weak",
                "text": "Уточнение",
                "relation": "refines",
                "weight": 0.3,
            },
            {
                "node_id": "own",
                "text": "Сам пункт",
                "relation": "refines",
                "weight": 1.0,
            },
            {"node_id": "t72", "text": TABLE, "relation": "table_ref", "weight": 1.0},
        ]
    )
    assert [(item.node_id, item.relation) for item in context.related] == [
        ("t72", "reference"),
        ("lead", "completes"),
    ]


def test_amendment_notes_are_no_linked_clauses():
    context = _context(
        references=[],
        depends=[
            {
                "node_id": "n1",
                "text": "(в ред. постановления Правительства Москвы",
                "relation": "completes",
                "weight": 1.0,
            },
            {
                "node_id": "n2",
                "text": "от 23.07.2024 N 1678-ПП)",
                "relation": "completes",
                "weight": 1.0,
            },
            {
                "node_id": "n3",
                "text": "Введение ограничений допускается не ранее 2026 года.",
                "relation": "refines",
                "weight": 0.7,
            },
        ],
    )
    assert [item.node_id for item in context.related] == ["n3"]


def test_amendment_notes_and_missing_documents_are_kept_apart():
    context = _context(
        references=[
            {"raw": "(в ред. постановления Правительства Москвы от 01.01.2020 N 1)"},
            {"raw": "СП 2.13130", "target_name": "СП 2.13130", "in_corpus": False},
            {
                "raw": "по п. 4.1 СП 42",
                "target_name": "СП 42.13330",
                "target_numbering": "4.1",
                "in_corpus": True,
            },
            {"raw": "СП 2.13130", "target_name": "СП 2.13130"},
        ]
    )
    assert context.related == ()
    assert context.unresolved_labels() == ["СП 2.13130", "СП 42.13330, п. 4.1"]
    # Nothing to read before the clause: it is extracted alone.
    assert context.extraction_prefix(3000) == ""
    assert "СП 2.13130" in context.render(3000)


def test_clause_of_another_document_is_labelled_with_it():
    context = _context(
        references=[
            {
                "raw": "п. 7.1 СП 42",
                "node_id": "x",
                "numbering": "7.1",
                "text": "Не менее 10 м.",
                "document": "СП 42.13330.2016",
                "external": True,
            }
        ]
    )
    assert context.related[0].label() == "[ссылка] СП 42.13330.2016, п. 7.1"
    assert context.related[0].source() == {
        "node_id": "x",
        "numbering": "7.1",
        "title": None,
        "document": "СП 42.13330.2016",
        "relation": "reference",
    }


def test_the_budget_keeps_at_least_one_clause_and_stops_at_the_limit():
    context = _context(
        references=[],
        depends=[
            {
                "node_id": f"c{i}",
                "text": "x" * 800,
                "relation": "refines",
                "weight": 0.7,
            }
            for i in range(5)
        ],
    )
    assert (
        len(context.shown(500)) == 1
    )  # one clause even if it alone exceeds the budget
    assert len(context.shown(1000)) == 1
    assert len(context.shown(2500)) == 3
    assert context.shown(0) == ()
    assert ClauseContext().extraction_prefix(3000) == ""


def _extraction(quote, number=None, unit=None):
    attrs = dict(subject="АЗС", object="жилые дома", kind="минимальное_расстояние")
    if number is not None:
        attrs.update(value_operator=">=", value_number=number, value_unit=unit)
    return lx.data.Extraction(
        extraction_class="ограничение", extraction_text=quote, attributes=attrs
    )


def _extract(monkeypatch, clause, context, *extractions, context_chars=3000):
    seen = {}

    def fake_extract(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(extractions=list(extractions))

    monkeypatch.setattr(lx, "extract", fake_extract)
    extractor = RestrictionExtractor(None, context_chars=context_chars)
    return extractor.extract_clause_sync(clause, context), seen


CLAUSE = "Расстояние от АЗС до жилых домов следует принимать по таблице 7.2."


def test_value_given_by_a_table_is_read_from_it_and_recorded(monkeypatch):
    found, seen = _extract(
        monkeypatch,
        CLAUSE,
        _context(),
        _extraction("Расстояние от АЗС до жилых домов", 50, "м"),
    )
    # The table is read before the clause, in the same chunk.
    assert seen["text_or_documents"].startswith("[ссылка] Таблица 7.2: Таблица 7.2")
    assert seen["text_or_documents"].endswith("Размечаемый пункт:\n" + CLAUSE)
    assert seen["max_char_buffer"] == 1500 + len(seen["text_or_documents"]) - len(
        CLAUSE
    )
    assert found[0].value_source == {
        "node_id": "t72",
        "numbering": None,
        "title": "Таблица 7.2",
        "document": None,
        "relation": "reference",
    }


def test_without_the_linked_clause_a_value_by_reference_is_rejected(monkeypatch):
    extraction = _extraction("Расстояние от АЗС до жилых домов", 50, "м")
    with pytest.raises(Exception):
        _extract(monkeypatch, CLAUSE, None, extraction)
    with pytest.raises(Exception):  # a disabled context behaves as before
        _extract(monkeypatch, CLAUSE, _context(), extraction, context_chars=0)


def test_a_number_absent_everywhere_is_still_rejected(monkeypatch):
    with pytest.raises(Exception):
        _extract(
            monkeypatch,
            CLAUSE,
            _context(),
            _extraction("Расстояние от АЗС до жилых домов", 70, "м"),
        )


def test_restriction_quoted_from_a_linked_clause_is_left_to_that_clause(monkeypatch):
    found, _ = _extract(
        monkeypatch,
        CLAUSE,
        _context(),
        _extraction("Склады | 100 м", 100, "м"),
        _extraction("Расстояние от АЗС до жилых домов", 50, "м"),
    )
    assert len(found) == 1 and found[0].value.number == 50


def test_only_restrictions_of_linked_clauses_yield_nothing_but_do_not_fail(monkeypatch):
    found, _ = _extract(
        monkeypatch, CLAUSE, _context(), _extraction("Склады | 100 м", 100, "м")
    )
    assert found == []


def test_list_item_quoted_with_its_linked_lead_in_is_grounded(monkeypatch):
    context = _context(
        references=[],
        depends=[
            {
                "node_id": "lead",
                "text": "Расстояние от АЗС до жилых домов должно быть не менее:",
                "relation": "completes",
                "weight": 1.0,
            }
        ],
    )
    found, _ = _extract(
        monkeypatch,
        "- 50 м при вместимости резервуаров до 40 м3;",
        context,
        _extraction("не менее: 50 м при вместимости резервуаров до 40 м3", 50, "м"),
    )
    assert len(found) == 1 and found[0].value_source is None


def test_norm_without_value_lists_the_references_without_text(monkeypatch):
    context = _context(
        references=[
            {
                "raw": "СанПиН 2.2.1/2.1.1.1200-03",
                "target_name": "СанПиН 2.2.1/2.1.1.1200-03",
            }
        ]
    )
    clause = "Размер санитарно-защитной зоны принимается по СанПиН 2.2.1/2.1.1.1200-03."
    found, _ = _extract(
        monkeypatch, clause, context, _extraction("Размер санитарно-защитной зоны")
    )
    assert found[0].unresolved_references == ["СанПиН 2.2.1/2.1.1.1200-03"]


def test_planner_prompts_show_the_linked_clauses():
    block = _related_block(PlanContext(clause_text=CLAUSE, related=_context()))
    assert "Таблица 7.2" in "\n".join(block)
    assert _related_block(PlanContext(clause_text=CLAUSE)) == []


def _targets():
    return _InternalTargets(
        DocumentDetail(
            doc_id="d",
            name="РНГП",
            fragments=[
                DocumentFragment(
                    id="p421", numbering="4.2.1", text="Станции учитываются …"
                ),
                DocumentFragment(id="t61", text="Таблица 6.1\nНормы обеспеченности"),
                DocumentFragment(id="i1", numbering="1", text="первая строка"),
                DocumentFragment(
                    id="i1b", numbering="1", text="первая строка другой таблицы"
                ),
                DocumentFragment(id="src", numbering="5.3", text="по таблице 6.1"),
            ],
        )
    )


@pytest.mark.parametrize(
    "ref, target",
    [
        (
            DocumentRef(
                raw="таблицей 6.1 настоящих Нормативов",
                scope="internal",
                target_doc_id="d",
                resolved=True,
            ),
            "t61",
        ),
        (
            DocumentRef(
                raw="пункт 4.2.1 настоящих Нормативов", scope="internal", resolved=False
            ),
            "p421",
        ),
        (
            DocumentRef(
                raw="согласно 4.2.1", target_numbering="4.2.1", scope="internal"
            ),
            "p421",
        ),
        # Numbers used by every table name no clause; other documents are not ours.
        (DocumentRef(raw="п. 1", scope="internal"), None),
        (DocumentRef(raw="таблица 6.1", scope="internal", target_doc_id="other"), None),
        (DocumentRef(raw="таблица 6.1 СП 42", scope="external"), None),
    ],
)
def test_internal_references_resolve_to_the_numbered_clause_or_titled_table(
    ref, target
):
    resolved = _targets().resolve(ref, own_id="src")
    assert resolved.target_node_id == target
    if target:
        assert resolved.resolved and resolved.target_doc_id == "d"


async def test_replanning_reads_the_linked_clauses_of_the_restriction_clause():
    from src.pipeline.check_plan_backfill import CheckPlanBackfillService

    class Reader:
        def __init__(self):
            self.asked = []

        async def clause_contexts(self, node_ids):
            self.asked.append(node_ids)
            return {"c": _context()}

    class Planner:
        async def plan_with_trace(self, rid, ex, context):
            return context, {}

    reader = Reader()
    service = CheckPlanBackfillService(reader, None, Planner())
    row = {
        "id": "r",
        "subject": "АЗС",
        "object": "дом",
        "kind": "k",
        "clause_text": CLAUSE,
    }
    context, _ = await service._plan({**row, "clause_node_id": "c"})
    assert reader.asked == [["c"]] and context.related_text().startswith("[ссылка]")
    context, _ = await service._plan(row)  # a restriction without a clause
    assert context.related is None and reader.asked == [["c"]]
