"""What a clause points to: the clauses it needs and the references it cannot resolve.

A clause often gives its value elsewhere — «по таблице 7.2», «в соответствии с п. 4.2.1»,
the list items after a lead-in, a refining clause. IDU_DVD's fragment relations (mirrored as
``DEPENDS_ON``) and its resolved references (``REFERENCES`` to a clause) name those clauses;
NormGraph also resolves a reference to a stored document by its name and clause number.

Extraction and the planner's LLM passes see these clauses as reference material: norms are
still extracted from the clause itself, but a value, condition or object may be read from a
linked clause, which is then recorded as the value's source. A reference to a document that is
not in the corpus is kept as unresolved, so a norm without its value says why. A clarifying
document's clause that addresses the clause (``EXPLAINS``) is shown the same way, as
``[разъяснение]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A DVD relation of this weight or more is needed to apply the source clause; ``same_topic``
# only says two clauses are about the same thing.
MIN_RELATION_WEIGHT = 0.7
_IGNORED_RELATIONS = {"same_topic"}
# Each linked clause is shown up to this many characters (tables are long).
_RELATED_CLAUSE_LIMIT = 1500
# Before the extracted clause a linked clause is cut shorter: it shares the chunk.
_PREFIX_CLAUSE_LIMIT = 800

_RELATION_LABELS = {
    "reference": "ссылка",
    "table_ref": "таблица",
    "explanation": "разъяснение",
    "refines": "уточнение",
    "condition": "условие",
    "exception": "исключение",
    "definition": "определение",
    "completes": "продолжение",
}
# Explicit references first, then what DVD found needed to apply the clause.
_RELATION_ORDER = list(_RELATION_LABELS)

# Amendment notes («в ред. постановления …», «введен постановлением …») cite documents
# but carry no content of the norm.
_AMENDMENT = re.compile(
    r"\bв\s+ред\.|\bредакци|\bвведен|\bутратил|\bизменени[ея]м?\b.*\bвнесен", re.I
)


# The tail of a note split off by the document structure: «от 23.07.2024 N 1678-ПП)».
_NOTE_TAIL = re.compile(
    r"\s*от\s+\d{1,2}\.\d{1,2}\.\d{4}\s*(?:г\.)?\s*[N№]\s*\S+\)\s*", re.I
)


def _amendment_note(text: str) -> bool:
    """«(в ред. постановления … от 24.12.2019 N 1809-ПП)»: history, not a norm's content."""
    if len(text) >= 300:
        return False
    return bool(
        (text.startswith("(") and _AMENDMENT.search(text)) or _NOTE_TAIL.fullmatch(text)
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


@dataclass(frozen=True)
class RelatedClause:
    node_id: str
    text: str
    relation: str
    numbering: str = ""
    document: str | None = None  # set for a clause of another document

    def title(self) -> str | None:
        """A table or unnumbered fragment is named by its first line («Таблица 6.1 …»)."""
        if self.numbering:
            return None
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return _clip(first, 80) or None

    def label(self) -> str:
        where = f"п. {self.numbering}" if self.numbering else self.title() or "пункт"
        if self.document:
            where = f"{self.document}, {where}"
        return f"[{_RELATION_LABELS.get(self.relation, self.relation)}] {where}"

    def source(self) -> dict:
        """The clause a value was read from, as stored with the restriction."""
        return {
            "node_id": self.node_id,
            "numbering": self.numbering or None,
            "title": self.title(),
            "document": self.document,
            "relation": self.relation,
        }


@dataclass(frozen=True)
class UnresolvedReference:
    raw: str
    target_name: str = ""
    target_numbering: str = ""
    in_corpus: bool = False  # the document is stored, its clause is not identified

    def label(self) -> str:
        name = self.target_name or self.raw
        if self.target_numbering and self.target_numbering not in name:
            name = f"{name}, п. {self.target_numbering}"
        return name


@dataclass(frozen=True)
class ClauseContext:
    related: tuple[RelatedClause, ...] = ()
    unresolved: tuple[UnresolvedReference, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.related or self.unresolved)

    @classmethod
    def from_row(
        cls,
        depends: list[dict] | None,
        references: list[dict] | None,
        explanations: list[dict] | None = None,
        *,
        own_node_id: str | None = None,
    ) -> "ClauseContext":
        """Build from the ``depends`` / ``references`` / ``explanations`` of ``CLAUSE_CONTEXT``."""
        related: dict[str, RelatedClause] = {}
        unresolved: dict[str, UnresolvedReference] = {}
        for ref in references or []:
            raw = (ref.get("raw") or "").strip()
            if _AMENDMENT.search(raw):
                continue
            node_id, text = ref.get("node_id"), (ref.get("text") or "").strip()
            if _amendment_note(text):
                continue
            if node_id and text and node_id != own_node_id:
                related.setdefault(
                    node_id,
                    RelatedClause(
                        node_id=node_id,
                        text=text,
                        relation="reference",
                        numbering=ref.get("numbering") or "",
                        document=ref.get("document") if ref.get("external") else None,
                    ),
                )
            elif not node_id or not text:
                item = UnresolvedReference(
                    raw=raw,
                    target_name=(ref.get("target_name") or "").strip(),
                    target_numbering=(ref.get("target_numbering") or "").strip(),
                    in_corpus=bool(ref.get("in_corpus")),
                )
                if item.raw or item.target_name:
                    unresolved.setdefault(item.label().casefold(), item)
        for item in explanations or []:
            node_id, text = item.get("node_id"), (item.get("text") or "").strip()
            if node_id and text and node_id != own_node_id:
                related.setdefault(
                    node_id,
                    RelatedClause(
                        node_id=node_id,
                        text=text,
                        relation="explanation",
                        numbering=item.get("numbering") or "",
                        document=item.get("document"),
                    ),
                )
        for dep in sorted(depends or [], key=lambda row: -(row.get("weight") or 0.0)):
            node_id, text = dep.get("node_id"), (dep.get("text") or "").strip()
            if (
                not node_id
                or not text
                or node_id == own_node_id
                or _amendment_note(text)
                or dep.get("relation") in _IGNORED_RELATIONS
                or (dep.get("weight") or 0.0) < MIN_RELATION_WEIGHT
            ):
                continue
            related.setdefault(
                node_id,
                RelatedClause(
                    node_id=node_id,
                    text=text,
                    relation=dep.get("relation") or "refines",
                    numbering=dep.get("numbering") or "",
                ),
            )
        order = {name: index for index, name in enumerate(_RELATION_ORDER)}
        return cls(
            related=tuple(
                sorted(
                    related.values(),
                    key=lambda item: order.get(item.relation, len(order)),
                )
            ),
            unresolved=tuple(unresolved.values()),
        )

    def shown(self, limit: int) -> tuple[RelatedClause, ...]:
        """The linked clauses that fit into ``limit`` characters, in priority order."""
        if limit <= 0:
            return ()
        shown, used = [], 0
        for item in self.related:
            size = min(len(item.text), _RELATED_CLAUSE_LIMIT)
            if shown and used + size > limit:
                break
            shown.append(item)
            used += size
        return tuple(shown)

    def render(self, limit: int) -> str:
        """The linked clauses and unresolved references as plain text (empty if none)."""
        lines = [
            f"{item.label()}:\n{_clip(item.text, _RELATED_CLAUSE_LIMIT)}"
            for item in self.shown(limit)
        ]
        if self.unresolved and limit > 0:
            lines.append(
                "Ссылки на документы или пункты, текста которых нет: "
                + "; ".join(item.label() for item in self.unresolved[:10])
            )
        return "\n\n".join(lines)

    def extraction_prefix(self, limit: int) -> str:
        """Linked clauses put before the clause text; empty without linked clauses.

        Read inline, the model takes the lead-in's object and indicator into a list item
        («— не более 250 м» is the walking distance to a stop). A separate instruction
        block made it return nothing. Restrictions quoted from the prefix alone are
        dropped by grounding: their own clause yields them.
        """
        shown = self.prefix_clauses(limit)
        if not shown:
            return ""
        blocks = [f"{item.label()}: {text}" for item, text in shown]
        return "\n\n".join(blocks) + "\n\nРазмечаемый пункт:\n"

    def prefix_clauses(self, limit: int) -> list[tuple[RelatedClause, str]]:
        """The linked clauses before the clause text, as the model sees them."""
        return [
            (item, _clip(item.text, _PREFIX_CLAUSE_LIMIT)) for item in self.shown(limit)
        ]

    def unresolved_labels(self) -> list[str]:
        return [item.label() for item in self.unresolved]
