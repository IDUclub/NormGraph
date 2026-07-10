"""Kind vocabulary matching and cross-document entity resolution."""

from __future__ import annotations

import pytest
from _fakes import FakeEmbedder, FakeWriter

from src.pipeline.vocabulary import (
    EntityResolver,
    KindVocabulary,
    normalize,
    normalize_kind,
)


def test_normalize():
    assert normalize("  Санитарно-Защитная  ЗОНА. ") == "санитарно-защитная зона"
    assert normalize("ЁЛКА") == "елка"


def test_normalize_kind():
    assert normalize_kind("Минимальная ширина") == "минимальная_ширина"


def _kinds(writer, threshold=0.88):
    return KindVocabulary(writer, FakeEmbedder(), threshold=threshold, index="kind")


@pytest.mark.asyncio
async def test_kind_exact_match():
    w = FakeWriter()
    w.kind_exact = {"name": "запрет_размещения", "status": "approved"}
    name, status = await _kinds(w).resolve("Запрет размещения")
    assert (name, status) == ("запрет_размещения", "approved")
    assert not w.named("nearest")  # exact short-circuits


@pytest.mark.asyncio
async def test_kind_fuzzy_match_adds_alias():
    w = FakeWriter()
    w.nearest_result = [
        {"name": "минимальная_ширина", "score": 0.95, "status": "approved"}
    ]
    name, status = await _kinds(w).resolve("мин ширина")
    assert name == "минимальная_ширина"
    assert w.named("ensure_kind")[0]["aliases"] == ["мин_ширина"]


@pytest.mark.asyncio
async def test_kind_new_is_pending():
    w = FakeWriter()
    w.nearest_result = [{"name": "x", "score": 0.10, "status": "approved"}]
    name, status = await _kinds(w).resolve("невиданный вид")
    assert status == "pending"
    assert w.named("ensure_kind")[0]["status"] == "pending"


def _entities(writer, threshold=0.90):
    return EntityResolver(writer, FakeEmbedder(), threshold=threshold, index="entity")


@pytest.mark.asyncio
async def test_entity_exact_match():
    w = FakeWriter()
    w.entity_exact = {"normalized": "сзз", "name": "СЗЗ"}
    assert await _entities(w).resolve("СЗЗ") == "сзз"


@pytest.mark.asyncio
async def test_entity_fuzzy_merges_into_canonical():
    w = FakeWriter()
    w.nearest_result = [
        {"normalized": "санитарно-защитная зона", "name": "СЗЗ", "score": 0.93}
    ]
    got = await _entities(w).resolve("санитарнозащитная зона")
    assert got == "санитарно-защитная зона"
    assert w.named("upsert_entity")[0]["normalized"] == "санитарно-защитная зона"


@pytest.mark.asyncio
async def test_entity_new_canonical():
    w = FakeWriter()
    w.nearest_result = [{"normalized": "y", "name": "Y", "score": 0.1}]
    got = await _entities(w).resolve("Новая сущность")
    assert got == "новая сущность"
    created = w.named("upsert_entity")[0]
    assert created["normalized"] == "новая сущность" and created["has_emb"] is True
