from __future__ import annotations

import pytest

from src.dto.check_plan import CheckPlan, CheckPlanBackfillRequest
from src.pipeline.check_plan_backfill import CheckPlanBackfillService


def _row(restriction_id: str, *, number: float | None = None) -> dict:
    return {
        "id": restriction_id,
        "subject": "Школа",
        "object": "Жилой дом",
        "kind": "минимальное_расстояние",
        "value_operator": ">=" if number is not None else None,
        "value_number": number,
        "value_unit": "м" if number is not None else None,
        "value_condition": None,
        "extraction_text": "Не менее 50 м",
    }


class FakeReader:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str | None, int]] = []

    async def restrictions_without_current_check_plan(self, *, after_id, limit):
        self.calls.append((after_id, limit))
        return [row for row in self.rows if after_id is None or row["id"] > after_id][
            :limit
        ]


class FakePlanner:
    def __init__(
        self, *, unsupported: set[str] | None = None, fail: set[str] | None = None
    ):
        self.unsupported = unsupported or set()
        self.fail = fail or set()
        self.calls: list[tuple[str, object]] = []

    async def plan(self, restriction_id, restriction):
        self.calls.append((restriction_id, restriction))
        if restriction_id in self.fail:
            raise RuntimeError("planner failed")
        unsupported = restriction_id in self.unsupported
        return CheckPlan(
            schema_version="1.0",
            template="unsupported" if unsupported else "distance_from_source",
            template_version=1,
            params={} if unsupported else {"distance_m": 50},
            source={
                "restriction_id": restriction_id,
                "extraction_text": restriction.extraction_text,
            },
            planner_status="unsupported" if unsupported else "auto",
        )


class FakeWriter:
    def __init__(self, *, skip: set[str] | None = None) -> None:
        self.skip = skip or set()
        self.calls: list[dict] = []

    async def append_check_plan_revision(self, restriction_id, plan, **kwargs):
        self.calls.append({"restriction_id": restriction_id, "plan": plan, **kwargs})
        return None if restriction_id in self.skip else 1


@pytest.mark.asyncio
async def test_backfill_generates_bounded_page_and_returns_keyset_cursor():
    reader = FakeReader([_row("r1", number=50), _row("r2"), _row("r3")])
    planner = FakePlanner(unsupported={"r2"})
    writer = FakeWriter()
    service = CheckPlanBackfillService(reader, writer, planner, concurrency=2)

    result = await service.run(CheckPlanBackfillRequest(limit=2, after_id="r0"))

    assert reader.calls == [("r0", 3)]
    assert result.selected == 2
    assert result.generated == 2
    assert result.auto == 1
    assert result.unsupported == 1
    assert result.has_more is True
    assert result.next_after_id == "r2"
    assert planner.calls[0][1].value.number == 50
    assert writer.calls[0]["review_status"] == "pending"
    assert writer.calls[1]["review_status"] == "rejected"
    assert all(call["protect_reviewed"] for call in writer.calls)
    assert all(call["skip_if_current"] for call in writer.calls)


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_plan_or_write():
    reader = FakeReader([_row("r1")])
    planner = FakePlanner()
    writer = FakeWriter()
    service = CheckPlanBackfillService(reader, writer, planner)

    result = await service.run(CheckPlanBackfillRequest(dry_run=True))

    assert result.selected == 1
    assert result.generated == 0
    assert result.dry_run is True
    assert planner.calls == []
    assert writer.calls == []


@pytest.mark.asyncio
async def test_backfill_isolates_failures_and_counts_concurrent_skip():
    reader = FakeReader([_row("r1"), _row("r2"), _row("r3")])
    planner = FakePlanner(fail={"r1"})
    writer = FakeWriter(skip={"r2"})
    service = CheckPlanBackfillService(reader, writer, planner, concurrency=3)

    result = await service.run(CheckPlanBackfillRequest())

    assert result.generated == 1
    assert result.skipped == 1
    assert result.failed == 1
    assert result.failures[0].restriction_id == "r1"
    assert result.has_more is False
    assert result.next_after_id is None


@pytest.mark.asyncio
async def test_startup_visits_all_pages_without_retrying_failed_rows_in_a_loop():
    reader = FakeReader([_row(f"r{i:03}") for i in range(205)])
    planner = FakePlanner(fail={"r000"}, unsupported={"r204"})
    writer = FakeWriter(skip={"r001"})
    service = CheckPlanBackfillService(reader, writer, planner)

    await service.run_on_startup()

    assert reader.calls == [(None, 101), ("r099", 101), ("r199", 101)]
    assert [rid for rid, _ in planner.calls] == [f"r{i:03}" for i in range(205)]
    assert len(writer.calls) == 204
    assert writer.calls[-1]["plan"]["planner_status"] == "unsupported"


@pytest.mark.asyncio
async def test_startup_with_no_missing_plans_does_not_call_planner():
    planner = FakePlanner()
    service = CheckPlanBackfillService(FakeReader([]), FakeWriter(), planner)

    await service.run_on_startup()

    assert planner.calls == []


@pytest.mark.asyncio
async def test_startup_database_failure_is_logged_without_escaping(capsys):
    class UnavailableReader(FakeReader):
        async def restrictions_without_current_check_plan(self, **kwargs):
            raise RuntimeError("database unavailable")

    service = CheckPlanBackfillService(
        UnavailableReader([]), FakeWriter(), FakePlanner()
    )

    await service.run_on_startup()

    assert "check_plan_startup_failed" in capsys.readouterr().out
