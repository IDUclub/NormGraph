"""Restrictions stating the same norm are grouped, kept and shown once."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from _fakes import FakeEmbedder, FakeWriter

from src.dto.query import RestrictionSearchRequest
from src.pipeline.duplicates import group_all, group_of, norm_key, same_quote
from src.pipeline.kind_consolidation import consolidate
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.service import ExtractionService
from src.pipeline.vocabulary import KindVocabulary
from src.query.service import QueryService

LAMPS = "Неисправные и перегоревшие люминесцентные лампы хранятся в отдельном помещении"


def test_norm_key_ignores_spelling_but_not_meaning():
    value = RestrictionValue(operator="<=", number=500.0, unit="м")
    key = norm_key("школа", "жилой дом", "максимальное_расстояние", value)
    assert key == norm_key(
        "Школа ",
        "жилой  дом",
        "максимальное_расстояние",
        RestrictionValue(operator="<=", number=500, unit="М"),
    )
    assert key != norm_key(
        "школа",
        "жилой дом",
        "максимальное_расстояние",
        RestrictionValue(operator="<=", number=300, unit="м"),
    )
    assert key != norm_key("школа", "жилой дом", "минимальное_расстояние", value)


def test_same_quote_accepts_rewording_and_a_part_but_not_another_norm():
    assert same_quote(LAMPS + ".", "неисправные и перегоревшие люминесцентные лампы")
    assert same_quote(
        "ЭСО должны иметь документы об оценке (подтверждении) соответствия.",
        "Использование ЭСО должно осуществляться при наличии документов об оценке "
        "(подтверждении) соответствия.",
    )
    assert not same_quote(
        "не более 3 детей с расстройствами аутистического спектра",
        "не более 3 детей с умственной отсталостью умеренной, тяжелой степени",
    )


def test_group_of_joins_an_existing_group_or_starts_one():
    candidates = [
        {"id": "b", "extraction_text": LAMPS, "duplicate_group": None},
        {
            "id": "a",
            "extraction_text": "другая норма о мебели",
            "duplicate_group": None,
        },
    ]
    assert group_of(LAMPS, candidates) == ("b", ["b"])
    candidates[0]["duplicate_group"] = "g"
    assert group_of(LAMPS, candidates) == ("g", [])
    assert group_of("совсем другое требование", candidates) == (None, [])


def test_group_all_names_groups_by_their_smallest_id_and_leaves_singletons():
    rows = [
        {"id": "r3", "norm_key": "k", "extraction_text": LAMPS},
        {"id": "r1", "norm_key": "k", "extraction_text": LAMPS.lower()},
        {"id": "r2", "norm_key": "k", "extraction_text": "мебель с документами"},
        {"id": "r4", "norm_key": "other", "extraction_text": LAMPS},
    ]
    assert group_all(rows) == {"r1": "r1", "r3": "r1", "r2": None, "r4": None}
    assert group_all(list(reversed(rows))) == group_all(rows)


class _Extractor:
    def __init__(self, items):
        self.items = items

    async def extract_clause(self, text, context=None):
        return self.items


class _Entities:
    async def resolve(self, text):
        return text.strip().lower()


@pytest.mark.asyncio
async def test_extraction_stores_the_label_key_and_joins_a_duplicate_group():
    w = FakeWriter()
    w.clauses = [{"node_id": "c1", "text": LAMPS}]
    w.duplicates = [{"id": "old", "extraction_text": LAMPS, "duplicate_group": None}]
    svc = ExtractionService(
        w,
        _Extractor(
            [
                ExtractedRestriction(
                    subject="Лампы",
                    object="отдельное помещение",
                    kind="требование_хранения_ламп",
                    extraction_text=LAMPS,
                )
            ]
        ),
        KindVocabulary(w, FakeEmbedder(), threshold=0.88, index="kind"),
        _Entities(),
        FakeEmbedder(),
    )
    await svc.extract_document("d1")

    props = w.named("upsert_restriction")[0]["props"]
    assert props["kind_label"] == "требование_хранения_ламп"
    assert props["kind"] == "требование_к_объекту"
    assert props["norm_key"] == norm_key(
        "лампы", "отдельное помещение", "требование_к_объекту", None
    )
    assert props["duplicate_group"] == "old"
    candidates = w.named("duplicate_candidates")[0]
    assert (candidates["key"], candidates["doc_id"]) == (props["norm_key"], "d1")
    assert w.named("set_duplicate_group") == [{"ids": ["old"], "group": "old"}]


class _ConsolidationWriter(FakeWriter):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def restrictions_for_consolidation(self):
        return self.rows

    async def update_restriction_kinds(self, rows):
        self._rec("update_restriction_kinds", rows=rows)

    async def remove_unlisted_kinds(self, listed):
        self._rec("remove_unlisted_kinds", listed=listed)
        return 5


def _stored(id_, kind, text, **extra):
    row = {
        "id": id_,
        "kind": kind,
        "kind_label": None,
        "extraction_text": text,
        "subject": "организация",
        "object": "лампы",
        "norm_key": None,
        "duplicate_group": None,
        "shared": True,
    }
    row.update(extra)
    return row


@pytest.mark.asyncio
async def test_consolidation_maps_kinds_groups_duplicates_and_is_idempotent():
    rows = [
        _stored("r1", "требование_хранения", LAMPS),
        _stored("r2", "требование_хранения_ламп", LAMPS.lower()),
        _stored(
            "r3",
            "минимальное_расстояние",
            "не более 500 м",
            value_operator="<=",
            value_number=500.0,
            value_unit="м",
        ),
        # A user's document is never grouped with the shared corpus.
        _stored("r4", "требование_хранения", LAMPS, shared=False),
    ]
    w = _ConsolidationWriter(rows)
    kinds = KindVocabulary(w, FakeEmbedder(), threshold=0.88, index="kind")

    preview = await consolidate(w, kinds, dry_run=True)
    assert not w.named("update_restriction_kinds") and not w.named("ensure_kind")
    assert (preview.restrictions, preview.updated) == (4, 4)
    assert (preview.duplicate_groups, preview.grouped) == (1, 2)
    assert preview.transitions["минимальное_расстояние → максимальное_расстояние"] == 1

    result = await consolidate(w, kinds)
    written = {row["id"]: row for row in w.named("update_restriction_kinds")[0]["rows"]}
    assert written["r1"]["kind"] == written["r2"]["kind"] == "требование_к_объекту"
    assert written["r2"]["kind_label"] == "требование_хранения_ламп"
    assert written["r1"]["duplicate_group"] == written["r2"]["duplicate_group"] == "r1"
    assert written["r4"]["duplicate_group"] is None
    assert written["r3"]["kind"] == "максимальное_расстояние"
    assert result.kinds_removed == 5

    # Stored as written, the same pass has nothing left to change.
    w.rows = [{**row, **written[row["id"]]} for row in rows]
    again = await consolidate(w, kinds, dry_run=True)
    assert again.updated == 0 and again.kinds_changed == 0


def _row(id_, group=None):
    return {
        "id": id_,
        "subject": "s",
        "object": "o",
        "kind": "k",
        "duplicate_group": group,
    }


@pytest.mark.asyncio
async def test_search_shows_a_group_once_and_lists_the_others():
    reader = SimpleNamespace(
        search_filter=AsyncMock(
            return_value=[_row("a", "g"), _row("b"), _row("c", "g"), _row("d")]
        ),
        duplicate_members=AsyncMock(
            return_value=[
                {
                    "duplicate_group": "g",
                    "id": "a",
                    "document": "СП 1",
                    "numbering": "1",
                },
                {
                    "duplicate_group": "g",
                    "id": "c",
                    "document": "СП 2",
                    "numbering": "7",
                },
            ]
        ),
        entity_keys=AsyncMock(),
    )
    settings = SimpleNamespace(dvd_search_fallback=False)
    service = QueryService(reader, FakeEmbedder(), None, settings)

    found = await service.search(RestrictionSearchRequest(limit=2))
    assert [hit.id for hit in found.hits] == ["a", "b"]
    assert [(d.id, d.document) for d in found.hits[0].duplicates] == [("c", "СП 2")]
    assert reader.search_filter.call_args.kwargs["limit"] == 4

    every = await service.search(
        RestrictionSearchRequest(limit=10, collapse_duplicates=False)
    )
    assert [hit.id for hit in every.hits] == ["a", "b", "c", "d"]
    assert [d.id for d in every.hits[2].duplicates] == ["a"]
