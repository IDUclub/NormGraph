"""Explanations: clarifying documents as context for the clauses they explain.

IDU_DVD links a clarification — a letter or note on how a document is to be applied — to the
document it explains (``explains`` on the act's ``/library`` document). The clarification stays a
document of its own; for the explained document its clauses are reference material, read the way a
referenced clause is: a clause an explanation addresses is extracted with it beside the text
(``[разъяснение] …``, see ``src/pipeline/clause_context.py``).

An explanation clause addresses a clause it cites — a resolved reference, or a reference to the
explained document by clause number — and, when it cites none there, the explained clauses
closest to it by IDU_DVD's vector search (``explanation_min_score``). The links are ``EXPLAINS``
edges, rebuilt whenever either document is synced; the explained clauses whose explanations
changed are returned so that only they are extracted again.
"""

from __future__ import annotations

import structlog

from src.dvd_client import DVDClient
from src.graph.context import load_clause_contexts
from src.graph.writer import GraphWriter

log = structlog.get_logger(__name__)

# Clauses shorter than this (headings, «Уважаемый …») are not searched for.
_MIN_SEARCH_CHARS = 60


class ExplanationLinker:
    def __init__(
        self,
        dvd: DVDClient,
        writer: GraphWriter,
        *,
        min_score: float,
        per_clause: int,
    ) -> None:
        self.dvd = dvd
        self.writer = writer
        self.min_score = min_score
        self.per_clause = per_clause

    async def link(self, doc_id: str) -> dict[str, list[str]]:
        """Rebuild the ``EXPLAINS`` edges of a document, as an explanation or as explained.

        Returns ``{explained doc_id: clause ids}`` — the clauses that gained or lost an
        explanation.
        """
        changed: dict[str, set[str]] = {}
        for target, ids in (await self.writer.drop_stale_explanations(doc_id)).items():
            changed.setdefault(target, set()).update(ids)
        for pair in await self.writer.explanation_pairs(doc_id):
            links = await self._links(pair["explanation"], pair["explained"])
            ids = await self.writer.replace_explanations(
                pair["explanation"], pair["explained"], links
            )
            if ids:
                changed.setdefault(pair["explained"], set()).update(ids)
            log.info(
                "explanation_linked",
                explanation=pair["explanation"],
                explained=pair["explained"],
                links=len(links),
                changed=len(ids),
            )
        return {target: sorted(ids) for target, ids in changed.items() if ids}

    async def _links(self, explanation: str, explained: str) -> list[dict]:
        targets = {c["node_id"] for c in await self.writer.get_clauses(explained)}
        clauses = await self.writer.get_clauses(explanation)
        contexts = await load_clause_contexts(
            self.writer.client,
            "MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})\n",
            doc_id=explanation,
        )
        # A cited clause carried over to a new edition keeps its link, though the
        # reference still names the old clause: both texts are unchanged.
        kept: dict[str, list[str]] = {}
        for edge in await self.writer.explanations_between(explanation, explained):
            if edge["via"] == "reference" and edge["target"] in targets:
                kept.setdefault(edge["source"], []).append(edge["target"])
        links: dict[tuple[str, str], dict] = {}
        for clause in clauses:
            context = contexts.get(clause["node_id"])
            cited = [
                item.node_id
                for item in (context.related if context else ())
                if item.relation == "reference" and item.node_id in targets
            ] + kept.get(clause["node_id"], [])
            for target in cited:
                links[(clause["node_id"], target)] = {
                    "source": clause["node_id"],
                    "target": target,
                    "via": "reference",
                    "score": None,
                }
            text = (clause.get("text") or "").strip()
            if cited or len(text) < _MIN_SEARCH_CHARS or not self.per_clause:
                continue
            found = await self.dvd.search(
                text, doc_id=explained, limit=self.per_clause, related=False
            )
            for hit in found.hits:
                if hit.score >= self.min_score and hit.id in targets:
                    links.setdefault(
                        (clause["node_id"], hit.id),
                        {
                            "source": clause["node_id"],
                            "target": hit.id,
                            "via": "similar",
                            "score": round(hit.score, 4),
                        },
                    )
        return list(links.values())
