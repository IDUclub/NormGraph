"""Controlled vocabulary (restriction kinds) and cross-document entity resolution.

Both are stored *in the graph* (``:RestrictionKind`` / ``:Entity`` nodes).

Kinds are a closed list (``src/pipeline/kind_taxonomy.py``): an extracted label is mapped to
one of them, never added. Entities are matched in two tiers:

1. **exact** — by normalized name or an existing alias (cheap, no embedding);
2. **fuzzy** — by embedding cosine similarity against the vector index; above the configured
   threshold the incoming label is filed as an alias of the matched node, otherwise a new node is
   created as a fresh canonical.

This is what makes the graph connect: the same "санитарно-защитная зона" written slightly
differently across documents collapses onto one ``:Entity``. The deeper terminology store is a
deferred TODO — for now the canonical form is the first-seen normalized name.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from src.pipeline.kind_taxonomy import KINDS, OTHER, classify_kind
from src.pipeline.models import RestrictionMeasurement, RestrictionValue
from src.pipeline.prompts import SEED_KINDS
from src.providers.base import Embedder

if TYPE_CHECKING:
    # Type-only: the writer imports ``normalize`` to key CheckPlan layer entities.
    from src.graph.writer import GraphWriter

log = structlog.get_logger(__name__)

_WS = re.compile(r"\s+")
_PUNCT_EDGES = re.compile(r"^[\s\.,;:—–\-()\[\]«»\"']+|[\s\.,;:—–\-()\[\]«»\"']+$")


def normalize(text: str) -> str:
    """Normalized entity key: lowercase, ё→е, collapsed whitespace, trimmed punctuation."""
    text = text.lower().replace("ё", "е")
    text = _WS.sub(" ", text).strip()
    text = _PUNCT_EDGES.sub("", text)
    return text


def layer_entity_keys(declared_requirements: dict | None) -> list[str]:
    """Normalized entity labels of a CheckPlan's declared layers (topic filtering)."""
    layers = (declared_requirements or {}).get("layers") or []
    keys = (
        normalize(str(layer.get("entity") or ""))
        for layer in layers
        if isinstance(layer, dict)
    )
    return sorted({key for key in keys if key})


def normalize_kind(label: str) -> str:
    """Kind code: normalized, spaces/dashes → underscores."""
    base = normalize(label)
    return re.sub(r"[\s\-]+", "_", base)


class KindVocabulary:
    """The closed list of kinds, kept as approved ``:RestrictionKind`` nodes.

    An extracted label is mapped to a listed kind by ``classify_kind``; a label the rules
    leave as «прочее» is matched by embedding against the listed kinds. A restriction left
    as «прочее» has ``kind_status="pending"``: its kind needs a look.
    """

    def __init__(
        self, writer: GraphWriter, embedder: Embedder, *, threshold: float, index: str
    ) -> None:
        self.writer = writer
        self.embedder = embedder
        self.threshold = threshold
        self.index = index

    async def ensure_seed(self) -> None:
        """Provision the listed kinds (idempotent), embedding each for fuzzy matching."""
        vectors = await self.embedder.embed_documents(SEED_KINDS)
        for name, vec in zip(SEED_KINDS, vectors):
            await self.writer.ensure_kind(name, status="approved", embedding=vec)

    async def resolve(
        self,
        label: str,
        value: RestrictionValue | None = None,
        measurement: RestrictionMeasurement | None = None,
        text: str = "",
    ) -> tuple[str, str]:
        """Return ``(listed_kind, status)`` for an extracted kind label."""
        norm = normalize_kind(label)
        kind = classify_kind(norm, value, measurement, text)
        if kind == OTHER and norm and norm != OTHER:
            vec = (await self.embedder.embed_documents([norm]))[0]
            # Kinds coined before the list was closed stay in the index until consolidated.
            for match in await self.writer.nearest(self.index, vec, k=5):
                if match.get("name") in KINDS and match["name"] != OTHER:
                    if match.get("score", 0.0) >= self.threshold:
                        kind = match["name"]
                    break
        if kind == OTHER:
            log.info("kind_not_listed", label=norm)
            return kind, "pending"
        return kind, "approved"


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
