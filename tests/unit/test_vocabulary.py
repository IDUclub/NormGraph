"""Kind vocabulary matching and cross-document entity resolution."""

from __future__ import annotations

import pytest
from _fakes import FakeEmbedder, FakeWriter

from src.pipeline.kind_taxonomy import KINDS
from src.pipeline.models import RestrictionValue
from src.pipeline.vocabulary import (
    EntityResolver,
    KindVocabulary,
    layer_entity_keys,
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
async def test_listed_kind_is_kept_without_embedding():
    w = FakeWriter()
    name, status = await _kinds(w).resolve("Запрет размещения")
    assert (name, status) == ("запрет_размещения", "approved")
    assert not w.named("nearest")


@pytest.mark.asyncio
async def test_coined_label_is_mapped_to_a_listed_kind():
    w = FakeWriter()
    name, status = await _kinds(w).resolve(
        "максимальная_дистанция_пешеходной_доступности",
        RestrictionValue(operator="<=", number=500, unit="м"),
    )
    assert (name, status) == ("максимальное_расстояние", "approved")
    assert not w.named("ensure_kind")


@pytest.mark.asyncio
async def test_unlisted_label_matches_a_listed_kind_by_embedding():
    w = FakeWriter()
    w.nearest_result = [
        {"name": "старый_вид", "score": 0.99, "status": "pending"},
        {"name": "минимальный_размер", "score": 0.95, "status": "approved"},
    ]
    assert await _kinds(w).resolve("габаритность") == (
        "минимальный_размер",
        "approved",
    )


@pytest.mark.asyncio
async def test_label_matching_no_listed_kind_is_other_and_pending():
    w = FakeWriter()
    w.nearest_result = [{"name": "минимальный_размер", "score": 0.1}]
    assert await _kinds(w).resolve("невиданный вид") == ("прочее", "pending")
    assert not w.named("ensure_kind")


@pytest.mark.asyncio
async def test_seed_provisions_the_closed_list():
    w = FakeWriter()
    await _kinds(w).ensure_seed()
    names = [call["name"] for call in w.named("ensure_kind")]
    assert names == list(KINDS)
    assert {call["status"] for call in w.named("ensure_kind")} == {"approved"}


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


def test_layer_entity_keys_normalize_and_deduplicate_declared_layers():
    requirements = {
        "layers": [
            {"role": "schools", "entity": "Школа"},
            {"role": "others", "entity": " школа "},
            {"role": "homes", "entity": "Жилой  дом"},
            "not-a-layer",
        ]
    }

    assert layer_entity_keys(requirements) == ["жилой дом", "школа"]
    assert layer_entity_keys(None) == []
