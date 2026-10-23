"""Bring stored restrictions to the closed list of kinds and group their duplicates.

Extraction maps every new restriction (see ``kind_taxonomy`` and ``duplicates``); this
pass does the same for restrictions extracted before, without the LLM: each keeps its id,
plan and review, its label moves to ``kind_label`` and its kind to a listed one. Kinds no
restriction uses any more are removed. Repeating the pass changes nothing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import structlog

from src.graph.writer import GraphWriter
from src.pipeline.duplicates import group_all, norm_key
from src.pipeline.kind_taxonomy import KINDS, OTHER, classify_kind
from src.pipeline.models import RestrictionMeasurement, RestrictionValue
from src.pipeline.vocabulary import KindVocabulary, normalize_kind

log = structlog.get_logger(__name__)

_BATCH = 500
_FIELDS = ("kind", "kind_label", "kind_status", "norm_key", "duplicate_group")


@dataclass
class ConsolidationResult:
    dry_run: bool = False
    restrictions: int = 0
    updated: int = 0
    kinds_changed: int = 0
    unlisted: int = 0  # left as «прочее»
    kinds_removed: int = 0
    duplicate_groups: int = 0
    grouped: int = 0
    # The most frequent «former kind → listed kind» changes.
    transitions: dict[str, int] = field(default_factory=dict)


async def consolidate(
    writer: GraphWriter, kinds: KindVocabulary, *, dry_run: bool = False
) -> ConsolidationResult:
    result = ConsolidationResult(dry_run=dry_run)
    if not dry_run:
        await kinds.ensure_seed()
    rows = await writer.restrictions_for_consolidation()
    result.restrictions = len(rows)
    unlisted_labels: dict[str, str] = {}
    targets, transitions = [], Counter()
    for row in rows:
        label = row.get("kind_label") or row.get("kind") or ""
        value = RestrictionValue(
            operator=row.get("value_operator"),
            number=row.get("value_number"),
            unit=row.get("value_unit"),
            condition=row.get("value_condition"),
        )
        value = None if value.is_empty() else value
        kind = classify_kind(
            normalize_kind(label),
            value,
            RestrictionMeasurement.from_storage(row.get("measurement_json")),
            row.get("extraction_text") or "",
        )
        if kind == OTHER and label:
            # The embedding fallback, once per label.
            if label not in unlisted_labels:
                unlisted_labels[label] = (await kinds.resolve(label))[0]
            kind = unlisted_labels[label]
        if kind != row.get("kind"):
            transitions[f"{row.get('kind')} → {kind}"] += 1
        targets.append(
            {
                "id": row["id"],
                "kind": kind,
                "kind_label": label,
                "kind_status": "pending" if kind == OTHER else "approved",
                "norm_key": norm_key(row["subject"], row["object"], kind, value),
                "extraction_text": row.get("extraction_text") or "",
                "shared": row.get("shared"),
            }
        )
    groups = group_all([t for t in targets if t["shared"]])
    for target in targets:
        target["duplicate_group"] = groups.get(target["id"])

    before = {row["id"]: row for row in rows}
    changed = [
        {key: t[key] for key in ("id", *_FIELDS)}
        for t in targets
        if any(before[t["id"]].get(key) != t[key] for key in _FIELDS)
    ]
    result.updated = len(changed)
    result.kinds_changed = sum(transitions.values())
    result.unlisted = sum(1 for t in targets if t["kind"] == OTHER)
    names = {g for g in groups.values() if g}
    result.duplicate_groups = len(names)
    result.grouped = sum(1 for g in groups.values() if g)
    result.transitions = dict(transitions.most_common(30))
    if dry_run:
        return result

    for start in range(0, len(changed), _BATCH):
        await writer.update_restriction_kinds(changed[start : start + _BATCH])
    result.kinds_removed = await writer.remove_unlisted_kinds(list(KINDS))
    log.info(
        "kinds_consolidated",
        restrictions=result.restrictions,
        updated=result.updated,
        kinds_changed=result.kinds_changed,
        unlisted=result.unlisted,
        kinds_removed=result.kinds_removed,
        duplicate_groups=result.duplicate_groups,
    )
    return result
