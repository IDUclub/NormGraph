"""Controlled vocabulary (restriction kinds) and cross-document entity resolution.

Both are stored *in the graph* (``:RestrictionKind`` / ``:Entity`` nodes) and matched in two tiers:

1. **exact** — by normalized name or an existing alias (cheap, no embedding);
2. **fuzzy** — by embedding cosine similarity against the vector index; above the configured
   threshold the incoming label is filed as an alias of the matched node, otherwise a new node is
   created (a kind as ``status="pending"`` for review; an entity as a fresh canonical).

This is what makes the graph connect: the same "санитарно-защитная зона" written slightly
differently across documents collapses onto one ``:Entity``. The deeper terminology store is a
deferred TODO — for now the canonical form is the first-seen normalized name.
"""

from __future__ import annotations

import re

import structlog

from src.graph.writer import GraphWriter
from src.pipeline.prompts import SEED_KINDS
from src.providers.base import Embedder

log = structlog.get_logger(__name__)

_WS = re.compile(r"\s+")
_PUNCT_EDGES = re.compile(r"^[\s\.,;:—–\-()\[\]«»\"']+|[\s\.,;:—–\-()\[\]«»\"']+$")


def normalize(text: str) -> str:
    """Normalized entity key: lowercase, ё→е, collapsed whitespace, trimmed punctuation."""
    text = text.lower().replace("ё", "е")
    text = _WS.sub(" ", text).strip()
    text = _PUNCT_EDGES.sub("", text)
    return text


def normalize_kind(label: str) -> str:
    """Kind code: normalized, spaces/dashes → underscores."""
    base = normalize(label)
    return re.sub(r"[\s\-]+", "_", base)


class KindVocabulary:
    def __init__(
        self, writer: GraphWriter, embedder: Embedder, *, threshold: float, index: str
    ) -> None:
        self.writer = writer
        self.embedder = embedder
        self.threshold = threshold
        self.index = index

    async def ensure_seed(self) -> None:
        """Provision the seed kinds (idempotent), embedding each for fuzzy matching."""
        vectors = await self.embedder.embed_documents(SEED_KINDS)
        for name, vec in zip(SEED_KINDS, vectors):
            await self.writer.ensure_kind(name, status="approved", embedding=vec)

    async def resolve(self, label: str) -> tuple[str, str]:
        """Return ``(canonical_kind_name, status)`` for an extracted kind label."""
        norm = normalize_kind(label)
        exact = await self.writer.get_kind(norm)
        if exact:
            return exact["name"], exact.get("status", "approved")

        vec = (await self.embedder.embed_documents([norm]))[0]
        matches = await self.writer.nearest(self.index, vec, k=1)
        if matches and matches[0].get("score", 0.0) >= self.threshold:
            name = matches[0]["name"]
            await self.writer.ensure_kind(name, aliases=[norm])
            return name, matches[0].get("status", "approved")

        await self.writer.ensure_kind(norm, status="pending", embedding=vec)
        log.info("kind_added_pending", kind=norm)
        return norm, "pending"


class EntityResolver:
    def __init__(
        self, writer: GraphWriter, embedder: Embedder, *, threshold: float, index: str
    ) -> None:
        self.writer = writer
        self.embedder = embedder
        self.threshold = threshold
        self.index = index

    async def resolve(self, text: str) -> str:
        """Return the canonical (normalized) key for an entity mention, deduping near-matches."""
        norm = normalize(text)
        if not norm:
            return norm
        exact = await self.writer.get_entity(norm)
        if exact:
            return exact["normalized"]

        vec = (await self.embedder.embed_documents([norm]))[0]
        matches = await self.writer.nearest(self.index, vec, k=1)
        if matches and matches[0].get("score", 0.0) >= self.threshold:
            canonical = matches[0]["normalized"]
            await self.writer.upsert_entity(
                canonical, name=matches[0].get("name") or canonical, aliases=[norm]
            )
            return canonical

        await self.writer.upsert_entity(
            norm, name=text.strip(), aliases=[norm], embedding=vec
        )
        return norm
