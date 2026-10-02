"""Re-planning plans of older planner versions: dry run, guarded writes, summary."""

from src.dto.check_plan import CheckPlanReplanRequest
from src.pipeline.check_plan_backfill import CheckPlanBackfillService
from src.pipeline.check_plan_planner import CHECK_PLANNER_VERSION, CheckPlanPlanner


def _row(rid, *, number, text, template="distance_from_source", status="auto"):
    return {
        "id": rid,
        "subject": "Пожарный гидрант",
        "object": "Жилой дом",
        "kind": "минимальное_расстояние",
        "value_operator": ">=",
        "value_number": number,
        "value_unit": "м",
        "value_condition": None,
        "extraction_text": text,
        "clause_text": f"5.1 {text}",
        "breadcrumb": "5 Противопожарные требования",
        "numbering": "5.1",
        "name": "СП 8.13130",
        "check_template": template,
        "check_planner_status": status,
        "check_revision": 3,
    }


class Reader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def restrictions_with_stale_check_plan(self, *, version, after_id, limit):
        self.calls.append((version, after_id, limit))
        return [r for r in self.rows if after_id is None or r["id"] > after_id][:limit]

    async def count_stale_check_plans(self, *, version):
        self.calls.append((version, "count"))
        return len(self.rows)


class Writer:
    def __init__(self, *, conflict=()):
        self.calls = []
        self.conflict = set(conflict)

    async def append_check_plan_revision(self, restriction_id, plan, **kwargs):
        self.calls.append({"restriction_id": restriction_id, "plan": plan, **kwargs})
        return (
            None if restriction_id in self.conflict else kwargs["expected_revision"] + 1
        )


ROWS = [
    # A defective plan from the audit: a rhythm read as a minimum distance.
    _row("r1", number=100, text="Гидранты размещать не реже чем через 100 м"),
    _row("r2", number=50, text="Расстояние до жилых домов не менее 50 м"),
    _row(
        "r3",
        number=0.5,
        text="не менее 0,5 м от стены",
        template="unsupported",
        status="unsupported",
    ),
]


async def test_dry_run_summarizes_transitions_without_writing():
    reader, writer = Reader(ROWS), Writer()
    service = CheckPlanBackfillService(reader, writer, CheckPlanPlanner())
    result = await service.replan(CheckPlanReplanRequest(limit=10))

    assert reader.calls == [(CHECK_PLANNER_VERSION, None, 11)]
    assert writer.calls == []
    assert result.dry_run is True and result.written == 0
    assert result.transitions == {
        "auto->unsupported": 1,
        "auto->auto": 1,
        "unsupported->unsupported": 1,
    }
    assert result.templates == {"distance_from_source": 1}
    assert result.blocked_reasons["periodic_spacing_not_supported"] == 1
    assert result.blocked_reasons["distance_below_territorial_scale"] == 1
    by_id = {item.restriction_id: item for item in result.items}
    assert by_id["r1"].after_status == "unsupported"


async def test_apply_writes_guarded_versioned_revisions():
    reader, writer = Reader(ROWS), Writer(conflict={"r2"})
    service = CheckPlanBackfillService(reader, writer, CheckPlanPlanner())
    result = await service.replan(
        CheckPlanReplanRequest(limit=2, dry_run=False, include_items=False)
    )

    assert result.has_more is True and result.next_after_id == "r2"
    assert result.written == 1 and result.failed == 1
    assert result.items == []
    assert {call["restriction_id"] for call in writer.calls} == {"r1", "r2"}
    for call in writer.calls:
        assert call["expected_revision"] == 3
        assert call["protect_reviewed"] is True
        assert call["planner_version"] == CHECK_PLANNER_VERSION
        assert call["trace"]["planner_version"] == CHECK_PLANNER_VERSION
        assert call["reason"] == f"replanned_by_planner_v{CHECK_PLANNER_VERSION}"
    rejected = next(call for call in writer.calls if call["restriction_id"] == "r1")
    assert rejected["review_status"] == "rejected"


async def test_include_current_also_selects_plans_of_this_planner_version():
    reader = Reader(ROWS)
    service = CheckPlanBackfillService(reader, Writer(), CheckPlanPlanner())

    assert await service.count_replannable() == 3
    assert await service.count_replannable(include_current=True) == 3
    await service.replan(CheckPlanReplanRequest(limit=10, include_current=True))

    assert reader.calls == [
        (CHECK_PLANNER_VERSION, "count"),
        (CHECK_PLANNER_VERSION + 1, "count"),
        (CHECK_PLANNER_VERSION + 1, None, 11),
    ]
