"""Extraction orchestration wires clauses to restriction nodes and edges."""

from __future__ import annotations

import pytest
from _fakes import FakeEmbedder, FakeWriter

from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.service import ExtractionService


class FakeExtractor:
    def __init__(self, per_clause):
        self._per_clause = per_clause

    async def extract_clause(self, text):
        return self._per_clause


class FakeKinds:
    def __init__(self, result):
        self._result = result

    async def resolve(self, label):
        return self._result


class FakeEntities:
    async def resolve(self, text):
        return text.strip().lower()


@pytest.mark.asyncio
async def test_extract_document_writes_restrictions_and_shares():
    w = FakeWriter()
    w.clauses = [
        {"node_id": "c1", "text": "clause text", "char_start": 100, "version_id": "v1"}
    ]
    extracted = [
        ExtractedRestriction(
            subject="СЗЗ",
            object="объекты пищевой промышленности",
            kind="запрет размещения",
            value=RestrictionValue(operator=">=", number=50, unit="м"),
            char_start=5,
            char_end=20,
        )
    ]
    svc = ExtractionService(
        w,
        FakeExtractor(extracted),
        FakeKinds(("запрет_размещения", "approved")),
        FakeEntities(),
        FakeEmbedder(),
    )

    result = await svc.extract_document("d1")

    assert result.clauses_processed == 1
    assert result.restrictions == 1
    assert result.pending_kinds == 0

    upsert = w.named("upsert_restriction")[0]
    assert upsert["subject"] == "сзз"
    assert upsert["object"] == "объекты пищевой промышленности"
    assert upsert["kind"] == "запрет_размещения"
    # value + absolute grounding (clause base 100 + relative 5/20)
    assert upsert["props"]["value_number"] == 50
    assert upsert["props"]["char_start"] == 105
    assert upsert["props"]["char_end"] == 120
    # shares-entity linking is attempted for the new restriction
    assert w.named("link_shares_entity")[0]["id"] == upsert["id"]


@pytest.mark.asyncio
async def test_pending_kind_counted():
    w = FakeWriter()
    w.clauses = [{"node_id": "c1", "text": "t", "char_start": None, "version_id": "v1"}]
    svc = ExtractionService(
        w,
        FakeExtractor(
            [ExtractedRestriction(subject="a", object="b", kind="странный вид")]
        ),
        FakeKinds(("странный_вид", "pending")),
        FakeEntities(),
        FakeEmbedder(),
    )
    result = await svc.extract_document("d1")
    assert result.pending_kinds == 1


@pytest.mark.asyncio
async def test_conflicting_neighbor_writes_conflict_edge():
    w = FakeWriter()
    w.clauses = [
        {"node_id": "c1", "text": "clause text", "char_start": 0, "version_id": "v1"}
    ]
    # An existing restriction of the same kind whose bound is incompatible with the new one.
    w.shares_entity_result = [
        {
            "id": "existing-1",
            "kind": "минимальная_ширина",
            "doc_id": "official-doc",
            "value_operator": ">=",
            "value_number": 25,
            "value_unit": "м",
            "value_condition": None,
        }
    ]
    extracted = [
        ExtractedRestriction(
            subject="СЗЗ",
            object="объекты пищевой промышленности",
            kind="минимальная ширина",
            value=RestrictionValue(operator="<=", number=20, unit="м"),
        )
    ]
    svc = ExtractionService(
        w,
        FakeExtractor(extracted),
        FakeKinds(("минимальная_ширина", "approved")),
        FakeEntities(),
        FakeEmbedder(),
    )

    result = await svc.extract_document("d1")

    assert result.conflicts == 1
    conflict_call = w.named("upsert_conflict")[0]
    assert conflict_call["other_id"] == "existing-1"
    assert conflict_call["severity"] == "certain"


@pytest.mark.asyncio
async def test_no_clauses_skips():
    w = FakeWriter()
    w.clauses = []
    svc = ExtractionService(
        w,
        FakeExtractor([]),
        FakeKinds(("k", "approved")),
        FakeEntities(),
        FakeEmbedder(),
    )
    result = await svc.extract_document("empty")
    assert result.skipped is True
