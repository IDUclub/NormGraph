"""Group restrictions that state the same norm.

The same norm is extracted more than once: a revised document repeats its predecessor
(СП 2.4.3648-20 and СП 2.4.2.4283-26 share hundreds of norms), a list item is repeated in
several sections, two quotes of one clause yield one norm. Such restrictions are kept — each
has its own provenance and plan — and share a ``duplicate_group``; reads show one of them and
list the others.

Two restrictions are duplicates when they have the same ``norm_key`` (canonical subject and
object, kind and value) and their quotes say the same: a norm without a number keyed only by
its entities would otherwise group unrelated requirements to one object.
"""

from __future__ import annotations

import hashlib
import re

from src.pipeline.models import RestrictionValue
from src.pipeline.vocabulary import normalize

_WORD = re.compile(r"[а-яa-z0-9]+")
# A quote shares this part of its words with another quote of the same norm.
_SIMILAR_WORDS = 0.5
# ... or most of the shorter quote is in the longer one.
_CONTAINED_WORDS = 0.7


def norm_key(
    subject: str, object_: str, kind: str, value: RestrictionValue | None
) -> str:
    """What a norm says, without where it says it: canonical entities, kind, value."""
    parts = [normalize(subject), normalize(object_), kind]
    if value is not None and not value.is_empty():
        number = "" if value.number is None else f"{value.number:g}"
        parts += [
            value.operator or "",
            number,
            normalize(value.unit or ""),
            normalize(value.condition or ""),
        ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _words(text: str) -> set[str]:
    # Six letters stand for the word: «застройки» and «застройка» are one word.
    return {w[:6] for w in _WORD.findall(normalize(text)) if len(w) > 2}


def same_quote(a: str, b: str) -> bool:
    """Whether two quotes state the same norm (one may be a part of the other)."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return normalize(a) == normalize(b)
    common = len(wa & wb)
    return (
        common / len(wa | wb) >= _SIMILAR_WORDS
        or common / min(len(wa), len(wb)) >= _CONTAINED_WORDS
    )


def group_of(text: str, candidates: list[dict]) -> tuple[str | None, list[str]]:
    """The duplicate group a restriction joins among restrictions of its ``norm_key``.

    ``candidates`` are ``{id, extraction_text, duplicate_group}`` rows. Returns the group
    (``None`` when no quote matches) and the ids of matching restrictions that are not in it
    yet. A new group is named after its first restriction.
    """
    matching = [c for c in candidates if same_quote(text, c["extraction_text"] or "")]
    if not matching:
        return None, []
    groups = sorted(c["duplicate_group"] for c in matching if c.get("duplicate_group"))
    group = groups[0] if groups else min(c["id"] for c in matching)
    return group, [c["id"] for c in matching if c.get("duplicate_group") != group]


def group_all(rows: list[dict]) -> dict[str, str | None]:
    """Duplicate groups of ``{id, norm_key, extraction_text}`` rows, keyed by id.

    A restriction without a duplicate maps to ``None``. Groups are named after their
    smallest id, so the result does not depend on the order of the rows.
    """
    by_key: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        by_key.setdefault(row["norm_key"], []).append(row)
    groups: dict[str, str | None] = {}
    for members in by_key.values():
        clusters: list[list[dict]] = []
        for row in members:
            for cluster in clusters:
                if any(
                    same_quote(row["extraction_text"] or "", c["extraction_text"] or "")
                    for c in cluster
                ):
                    cluster.append(row)
                    break
            else:
                clusters.append([row])
        for cluster in clusters:
            name = cluster[0]["id"] if len(cluster) > 1 else None
            for row in cluster:
                groups[row["id"]] = name
    return groups
