"""Recompute restriction vectors stored with an older embedding text.

Runs in the background on startup. It needs only the embeddings service — no LLM and no
re-extraction — and each page is committed on its own, so an interrupted run resumes on the next
startup from whatever is still stale.
"""

from __future__ import annotations

import asyncio

import structlog

from src.graph.reader import GraphReader
from src.graph.writer import GraphWriter
from src.pipeline.embedding_text import (
    RESTRICTION_EMBEDDING_VERSION,
    restriction_embedding_text,
)
from src.providers.base import Embedder

log = structlog.get_logger(__name__)


class RestrictionReembedService:
    def __init__(
        self,
        reader: GraphReader,
        writer: GraphWriter,
        embedder: Embedder,
        *,
        batch: int = 32,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.embedder = embedder
        self.batch = max(1, int(batch))

    async def run_page(self, after_id: str | None = None) -> tuple[int, str | None]:
        """Re-embed one page; return ``(updated, next_after_id)`` (``None`` when done)."""
        rows = await self.reader.restrictions_with_stale_embedding(
            version=RESTRICTION_EMBEDDING_VERSION, after_id=after_id, limit=self.batch
        )
        if not rows:
            return 0, None
        texts = [
            restriction_embedding_text(
                row.get("subject") or "",
                row.get("object") or "",
                row.get("kind") or "",
                value_operator=row.get("value_operator"),
                value_number=row.get("value_number"),
                value_unit=row.get("value_unit"),
                extraction_text=row.get("extraction_text"),
            )
            for row in rows
        ]
        vectors = await self.embedder.embed_documents(texts)
        await self.writer.set_restriction_embeddings(
            [
                {"id": row["id"], "embedding": vector}
                for row, vector in zip(rows, vectors)
            ],
            version=RESTRICTION_EMBEDDING_VERSION,
        )
        next_after_id = rows[-1]["id"] if len(rows) == self.batch else None
        return len(rows), next_after_id

    async def run_on_startup(self) -> None:
        updated, after_id = 0, None
        log.info("restriction_reembed_started", version=RESTRICTION_EMBEDDING_VERSION)
        try:
            while True:
                count, after_id = await self.run_page(after_id)
                updated += count
                if after_id is None:
                    break
        except asyncio.CancelledError:
            log.info("restriction_reembed_cancelled", updated=updated)
            raise
        except Exception as exc:  # noqa: BLE001 - background work must not break startup
            log.warning("restriction_reembed_failed", error=str(exc), updated=updated)
            return
        log.info("restriction_reembed_completed", updated=updated)
