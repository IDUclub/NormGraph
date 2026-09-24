from __future__ import annotations

import pytest
from _fakes import FakeEmbedder

from src.pipeline.embedding_text import (
    RESTRICTION_EMBEDDING_VERSION,
    restriction_embedding_text,
)
from src.pipeline.restriction_reembed import RestrictionReembedService


def test_embedding_text_carries_the_clause_sentence():
    text = restriction_embedding_text(
        "детский сад",
        "жилой дом",
        "минимальное_расстояние",
        value_operator="<=",
        value_number=500.0,
        value_unit="м",
        extraction_text="Расстояние  от дома\nдо сада не более 500 м.",
    )
    assert text == (
        "детский сад | жилой дом | минимальное_расстояние | <=500м\n"
        "Расстояние от дома до сада не более 500 м."
    )
    assert restriction_embedding_text("a", "b", "k") == "a | b | k"


class StaleReader:
    def __init__(self, ids):
        self.stale = {rid: {"id": rid, "subject": "s", "object": "o", "kind": "k",
                            "extraction_text": f"текст {rid}"} for rid in ids}

    async def restrictions_with_stale_embedding(self, *, version, after_id, limit):
        assert version == RESTRICTION_EMBEDDING_VERSION
        rows = sorted(
            (row for rid, row in self.stale.items() if after_id is None or rid > after_id),
            key=lambda row: row["id"],
        )
        return rows[:limit]


class RecordingWriter:
    def __init__(self, reader):
        self.reader = reader
        self.written = []

    async def set_restriction_embeddings(self, rows, *, version):
        for row in rows:
            self.written.append((row["id"], version))
            self.reader.stale.pop(row["id"])


@pytest.mark.asyncio
async def test_startup_reembed_visits_every_stale_restriction_once():
    reader = StaleReader([f"r{i}" for i in range(5)])
    writer = RecordingWriter(reader)

    await RestrictionReembedService(
        reader, writer, FakeEmbedder(), batch=2
    ).run_on_startup()

    assert writer.written == [(f"r{i}", 2) for i in range(5)]
    assert reader.stale == {}


@pytest.mark.asyncio
async def test_startup_reembed_failure_does_not_raise():
    class BrokenEmbedder(FakeEmbedder):
        async def embed_documents(self, texts):
            raise RuntimeError("embeddings down")

    reader = StaleReader(["r1"])
    await RestrictionReembedService(
        reader, RecordingWriter(reader), BrokenEmbedder()
    ).run_on_startup()
    assert "r1" in reader.stale
