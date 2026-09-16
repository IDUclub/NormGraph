"""Extraction orchestration wires clauses to restriction nodes and edges."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from _fakes import FakeEmbedder, FakeWriter
from test_measurement_planning import area, parking

from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.service import ExtractionService


class FakeExtractor:
    def __init__(self, per_clause):
        self._per_clause = per_clause

    async def extract_clause(self, text):
        return self._per_clause


class ConcurrentExtractor:
    def __init__(self, expected):
        self.expected = expected
        self.active = 0
        self.peak = 0
        self.all_started = asyncio.Event()

    async def extract_clause(self, text):
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == self.expected:
            self.all_started.set()
        await asyncio.wait_for(self.all_started.wait(), timeout=1)
        self.active -= 1
        return []


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
async def test_extract_document_processes_clauses_concurrently():
    w = FakeWriter()
    w.clauses = [
        {"node_id": f"c{i}", "text": f"clause {i}", "char_start": 0, "version_id": "v1"}
        for i in range(4)
    ]
    extractor = ConcurrentExtractor(expected=4)
    svc = ExtractionService(
        w,
        extractor,
        FakeKinds(("kind", "approved")),
        FakeEntities(),
        FakeEmbedder(),
        extract_concurrency=4,
    )

    result = await svc.extract_document("d1")

    assert result.clauses_processed == 4
    assert extractor.peak == 4


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


@pytest.mark.parametrize("planner_crashes", [False, True])
async def test_bad_plan_does_not_stop_later_clauses_and_measurement_is_persisted(
    planner_crashes,
):
    writer = FakeWriter()
    writer.clauses = [
        {"node_id": "parking", "text": "parking"},
        {"node_id": "area", "text": "area"},
    ]
    writer.append_check_plan_revision = AsyncMock(return_value=1)

    class Extractor:
        async def extract_clause(self, text):
            return [parking() if text == "parking" else area()]

    class Planner(CheckPlanPlanner):
        async def plan(self, rid, ex):
            if planner_crashes and ex.subject == parking().subject:
                raise RuntimeError("unexpected planner error")
            return await super().plan(rid, ex)

    service = ExtractionService(
        writer,
        Extractor(),
        FakeKinds(("kind", "approved")),
        FakeEntities(),
        FakeEmbedder(),
        check_plan_planner=Planner(),
    )
    result = await service.extract_document("doc")
    assert result.clauses_processed == result.restrictions == 2
    saved = writer.named("upsert_restriction")
    assert saved[0]["props"]["subject"] == parking().subject
    assert saved[1]["props"]["measurement_json"] == area().measurement.model_dump_json()
    plans = writer.append_check_plan_revision.await_args_list
    assert plans[0].args[1]["planner_status"] == "unsupported"
    assert plans[1].args[1]["template"] == "zonal_ratio"
    assert bool(result.warnings) == planner_crashes


async def test_plan_storage_failure_is_not_reported_as_successful_extraction():
    writer = FakeWriter()
    writer.clauses = [{"node_id": "c", "text": "area"}]
    writer.append_check_plan_revision = AsyncMock(
        side_effect=RuntimeError("database unavailable")
    )
    service = ExtractionService(
        writer,
        FakeExtractor([area()]),
        FakeKinds(("kind", "approved")),
        FakeEntities(),
        FakeEmbedder(),
        check_plan_planner=CheckPlanPlanner(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.extract_document("doc")


@pytest.mark.parametrize("replace", [False, True])
async def test_exhausted_llm_output_is_reported_without_losing_other_clauses(replace):
    from src.providers.langextract_backend import InvalidExtractionOutput

    writer = FakeWriter()
    writer.clauses = [{"node_id": name, "text": name} for name in ("bad", "good")]

    class Extractor:
        async def extract_clause(self, text):
            if text == "bad":
                raise InvalidExtractionOutput("invalid_llm_output after 3 attempts")
            return [area()]

    service = ExtractionService(
        writer,
        Extractor(),
        FakeKinds(("kind", "approved")),
        FakeEntities(),
        FakeEmbedder(),
    )
    result = await service.extract_document("doc", replace=replace)
    assert result.incomplete and not result.replaced
    assert result.failed_clause_ids == ["bad"]
    assert result.clauses_processed == result.restrictions == 1
    assert any("bad: invalid_llm_output" in warning for warning in result.warnings)
    assert writer.named("upsert_restriction")[0]["clause"] == "good"
    assert not writer.named("delete_restrictions_of_doc")
    assert writer.named("upsert_document")[-1]["extraction_incomplete"] is True


async def test_successful_retry_clears_incomplete_marker_and_replaces_only_after_extraction():
    writer = FakeWriter()
    writer.clauses = [{"node_id": "good", "text": "good"}]

    class Extractor:
        async def extract_clause(self, text):
            assert not writer.named("delete_restrictions_of_doc")
            return [area()]

    service = ExtractionService(
        writer,
        Extractor(),
        FakeKinds(("kind", "approved")),
        FakeEntities(),
        FakeEmbedder(),
    )
    result = await service.extract_document("doc", replace=True)
    assert result.replaced and not result.incomplete
    assert writer.named("delete_restrictions_of_doc")
    assert writer.named("upsert_document")[-1] == {
        "doc_id": "doc",
        "extraction_incomplete": False,
        "extraction_failed_clause_ids": [],
    }
