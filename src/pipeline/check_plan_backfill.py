"""Generate missing CheckPlans and re-plan outdated ones from stored restrictions."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import structlog

from src.dto.check_plan import (
    CheckPlanBackfillFailure,
    CheckPlanBackfillRequest,
    CheckPlanBackfillResponse,
    CheckPlanRegenerateRequest,
    CheckPlanRegenerateResponse,
    CheckPlanReplanItem,
    CheckPlanReplanRequest,
    CheckPlanReplanResponse,
)
from src.graph.reader import GraphReader
from src.graph.writer import GraphWriter
from src.pipeline.check_plan_planner import CHECK_PLANNER_VERSION, CheckPlanPlanner
from src.pipeline.clause_context import ClauseContext
from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)
from src.pipeline.norm_refiner import PlanContext

log = structlog.get_logger(__name__)


class CheckPlanRevisionConflict(ValueError):
    """The caller's revision is stale or an expert decision is protected."""


@dataclass(frozen=True)
class _Outcome:
    status: Literal["auto", "unsupported", "skipped", "failed"]
    failure: CheckPlanBackfillFailure | None = None


def _replan_version(include_current: bool) -> int:
    """Plans built by a planner older than this are re-planned."""
    return CHECK_PLANNER_VERSION + 1 if include_current else CHECK_PLANNER_VERSION


class CheckPlanBackfillService:
    """Run bounded, resumable and non-destructive CheckPlan backfill pages."""

    def __init__(
        self,
        reader: GraphReader,
        writer: GraphWriter,
        planner: CheckPlanPlanner,
        *,
        concurrency: int = 64,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.planner = planner
        self.concurrency = max(1, int(concurrency))

    async def run_on_startup(self) -> None:
        """Visit every missing plan once; leave failed rows for the next startup."""
        after_id = None
        totals = dict.fromkeys(
            ("selected", "generated", "auto", "unsupported", "skipped", "failed"), 0
        )
        log.info("check_plan_startup_started")
        try:
            while True:
                result = await self.run(CheckPlanBackfillRequest(after_id=after_id))
                for key in totals:
                    totals[key] += getattr(result, key)
                if not result.has_more:
                    break
                after_id = result.next_after_id
        except asyncio.CancelledError:
            log.info("check_plan_startup_cancelled", **totals)
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 - background work must not break startup
            log.warning("check_plan_startup_failed", error=str(exc), **totals)
            return
        log.info("check_plan_startup_completed", **totals)

    @staticmethod
    def _as_extracted(row: dict) -> ExtractedRestriction:
        value = RestrictionValue(
            operator=row.get("value_operator"),
            number=row.get("value_number"),
            unit=row.get("value_unit"),
            condition=row.get("value_condition"),
        )
        return ExtractedRestriction(
            subject=row.get("subject") or "",
            object=row.get("object") or "",
            # The model's own label says more than the listed kind («освещенность»).
            kind=row.get("kind_label") or row.get("kind") or "",
            value=None if value.is_empty() else value,
            measurement=RestrictionMeasurement.from_storage(
                row.get("measurement_json")
            ),
            extraction_text=row.get("extraction_text") or "",
        )

    @staticmethod
    def _context(row: dict, related: ClauseContext | None = None) -> PlanContext:
        return PlanContext(
            clause_text=row.get("clause_text") or "",
            breadcrumb=row.get("breadcrumb"),
            document_name=row.get("name"),
            clause_number=row.get("numbering"),
            related=related,
        )

    async def _plan(self, row: dict):
        clause = row.get("clause_node_id")
        contexts = await self.reader.clause_contexts([clause]) if clause else {}
        return await self.planner.plan_with_trace(
            str(row["id"]),
            self._as_extracted(row),
            self._context(row, contexts.get(clause)),
        )

    async def run(self, request: CheckPlanBackfillRequest) -> CheckPlanBackfillResponse:
        rows = await self.reader.restrictions_without_current_check_plan(
            after_id=request.after_id,
            limit=request.limit + 1,
        )
        has_more = len(rows) > request.limit
        page = rows[: request.limit]
        next_after_id = str(page[-1]["id"]) if page and has_more else None

        if request.dry_run:
            return CheckPlanBackfillResponse(
                selected=len(page),
                generated=0,
                auto=0,
                unsupported=0,
                skipped=0,
                failed=0,
                has_more=has_more,
                next_after_id=next_after_id,
                dry_run=True,
            )

        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(row: dict) -> _Outcome:
            restriction_id = str(row["id"])
            async with semaphore:
                try:
                    plan, trace = await self._plan(row)
                    revision = await self.writer.append_check_plan_revision(
                        restriction_id,
                        plan.model_dump(mode="json"),
                        review_status=(
                            "pending" if plan.planner_status == "auto" else "rejected"
                        ),
                        protect_reviewed=True,
                        skip_if_current=True,
                        planner_version=CHECK_PLANNER_VERSION,
                        trace=trace,
                    )
                    if revision is None:
                        return _Outcome("skipped")
                    return _Outcome(
                        "auto" if plan.planner_status == "auto" else "unsupported"
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one bad restriction
                    log.warning(
                        "check_plan_backfill_failed",
                        restriction_id=restriction_id,
                        error=str(exc),
                    )
                    return _Outcome(
                        "failed",
                        CheckPlanBackfillFailure(
                            restriction_id=restriction_id,
                            error=str(exc),
                        ),
                    )

        outcomes = await asyncio.gather(*(process(row) for row in page))
        failures = [outcome.failure for outcome in outcomes if outcome.failure]
        auto = sum(outcome.status == "auto" for outcome in outcomes)
        unsupported = sum(outcome.status == "unsupported" for outcome in outcomes)
        skipped = sum(outcome.status == "skipped" for outcome in outcomes)
        failed = len(failures)
        result = CheckPlanBackfillResponse(
            selected=len(page),
            generated=auto + unsupported,
            auto=auto,
            unsupported=unsupported,
            skipped=skipped,
            failed=failed,
            failures=failures,
            has_more=has_more,
            next_after_id=next_after_id,
        )
        log.info("check_plan_backfill_completed", **result.model_dump())
        return result

    async def regenerate(
        self, restriction_id: str, request: CheckPlanRegenerateRequest
    ) -> CheckPlanRegenerateResponse | None:
        """Re-plan one stored restriction, retaining history and expert decisions."""
        rows = await self.reader.get_by_ids([restriction_id])
        if not rows:
            return None
        row = rows[0]
        revision = row.get("check_revision") or 0
        if revision != request.expected_revision:
            raise CheckPlanRevisionConflict(
                "check plan revision changed; fetch it again"
            )
        if row.get("check_planner_status") == "reviewed" or row.get("check_author"):
            raise CheckPlanRevisionConflict("expert-reviewed plan is protected")
        plan, trace = await self._plan(row)
        plan.source.document_name = row.get("name")
        plan.source.clause_number = row.get("numbering")
        if plan.template == "unsupported" and plan.params.get("candidate_plan"):
            plan.params["candidate_plan"]["source"] = plan.source.model_dump(
                mode="json"
            )
        if not request.dry_run:
            saved = await self.writer.append_check_plan_revision(
                restriction_id,
                plan.model_dump(mode="json"),
                review_status=(
                    "pending" if plan.planner_status == "auto" else "rejected"
                ),
                protect_reviewed=True,
                expected_revision=revision,
                reason="regenerated_from_stored_restriction",
                planner_version=CHECK_PLANNER_VERSION,
                trace=trace,
            )
            if saved is None:
                raise CheckPlanRevisionConflict(
                    "check plan changed or an expert decision is protected"
                )
            revision = saved
        return CheckPlanRegenerateResponse(
            restriction_id=restriction_id,
            revision=revision,
            dry_run=request.dry_run,
            plan=plan,
            trace=trace,
        )

    async def count_replannable(self, *, include_current: bool = False) -> int:
        """Plans a full ``replan`` pass would select."""
        return await self.reader.count_stale_check_plans(
            version=_replan_version(include_current)
        )

    async def replan(self, request: CheckPlanReplanRequest) -> CheckPlanReplanResponse:
        """Re-plan one page of plans built by an older planner version.

        Every write is guarded by the revision read with the page, so a concurrent
        expert review wins. In ``dry_run`` nothing is written and the response is the
        summary of what applying the page would change.
        """
        rows = await self.reader.restrictions_with_stale_check_plan(
            version=_replan_version(request.include_current),
            after_id=request.after_id,
            limit=request.limit + 1,
        )
        has_more = len(rows) > request.limit
        page = rows[: request.limit]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(row: dict) -> CheckPlanReplanItem:
            restriction_id = str(row["id"])
            item = CheckPlanReplanItem(
                restriction_id=restriction_id,
                before_template=row.get("check_template"),
                before_status=row.get("check_planner_status"),
            )
            async with semaphore:
                try:
                    plan, trace = await self._plan(row)
                    item.after_template = plan.template
                    item.after_status = plan.planner_status
                    item.blocked_reasons = list(
                        plan.params.get("blocked_reasons") or []
                    )
                    if request.dry_run:
                        return item
                    saved = await self.writer.append_check_plan_revision(
                        restriction_id,
                        plan.model_dump(mode="json"),
                        review_status=(
                            "pending" if plan.planner_status == "auto" else "rejected"
                        ),
                        protect_reviewed=True,
                        expected_revision=row.get("check_revision") or 0,
                        reason=f"replanned_by_planner_v{CHECK_PLANNER_VERSION}",
                        planner_version=CHECK_PLANNER_VERSION,
                        trace=trace,
                    )
                    item.written = saved is not None
                    if saved is None:
                        item.error = "check plan changed or is protected"
                except Exception as exc:  # noqa: BLE001 - isolate one bad restriction
                    log.warning(
                        "check_plan_replan_failed",
                        restriction_id=restriction_id,
                        error=str(exc),
                    )
                    item.error = str(exc)
                return item

        items = await asyncio.gather(*(process(row) for row in page))
        transitions: Counter[str] = Counter()
        templates: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for item in items:
            if item.after_status is None:
                continue
            transitions[f"{item.before_status}->{item.after_status}"] += 1
            if item.after_status == "auto":
                templates[item.after_template or "unknown"] += 1
            reasons.update(item.blocked_reasons)
        result = CheckPlanReplanResponse(
            planner_version=CHECK_PLANNER_VERSION,
            selected=len(page),
            written=sum(item.written for item in items),
            failed=sum(item.error is not None for item in items),
            transitions=dict(transitions),
            templates=dict(templates),
            blocked_reasons=dict(reasons.most_common()),
            items=list(items) if request.include_items else [],
            has_more=has_more,
            next_after_id=str(page[-1]["id"]) if page and has_more else None,
            dry_run=request.dry_run,
        )
        log.info(
            "check_plan_replan_completed",
            **result.model_dump(exclude={"items", "blocked_reasons"}),
        )
        return result
