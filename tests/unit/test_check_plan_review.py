"""Expert review of a blocked plan: approve promotes the planner's candidate."""

import json

import pytest

from src.common.config import Settings
from src.dto.check_plan import CheckPlanReviewRequest
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.query.service import QueryService

CANDIDATE_SOURCE = ExtractedRestriction(
    subject="Пожарный гидрант",
    object="Жилой дом",
    kind="минимальное_расстояние",
    value=RestrictionValue(operator=">=", number=100, unit="м"),
    extraction_text="Гидранты размещать не реже чем через 100 м",
)


class Store:
    """Reader and writer over one restriction's revision history."""

    def __init__(self, plan, trace=None):
        self.rows = [self._row(plan, 1, trace=trace)]

    @staticmethod
    def _row(plan, revision, *, author=None, review_status="rejected", trace=None):
        return {
            "restriction_id": "r",
            "schema_version": plan["schema_version"],
            "template": plan["template"],
            "template_version": plan["template_version"],
            "params_json": json.dumps(plan["params"], ensure_ascii=False),
            "requirements_json": json.dumps(plan.get("declared_requirements")),
            "source_json": json.dumps(plan["source"], ensure_ascii=False),
            "planner_status": plan["planner_status"],
            "review_status": review_status,
            "revision": revision,
            "author": author,
            "reason": None,
            "created_at": None,
            "current": True,
            "planner_version": 2 if author is None else None,
            "trace_json": json.dumps(trace) if trace else None,
        }

    async def check_plan_revisions(self, restriction_id):
        return sorted(self.rows, key=lambda row: -row["revision"])

    async def append_check_plan_revision(self, restriction_id, plan, **kwargs):
        for row in self.rows:
            row["current"] = False
        self.rows.append(
            self._row(
                plan,
                len(self.rows) + 1,
                author=kwargs.get("author"),
                review_status=kwargs["review_status"],
            )
        )
        return len(self.rows)


async def _blocked_plan():
    plan = await CheckPlanPlanner().plan("r", CANDIDATE_SOURCE)
    assert plan.planner_status == "unsupported"
    return plan.model_dump(mode="json")


async def test_approving_a_blocked_plan_promotes_its_candidate():
    store = Store(await _blocked_plan(), trace={"passes": [{"pass": "deterministic"}]})
    service = QueryService(store, None, None, Settings(), writer=store)

    [before] = await service.check_plan_revisions("r")
    assert before.planner_version == 2 and before.trace["passes"]

    item = await service.review_check_plan(
        "r",
        CheckPlanReviewRequest(action="approve", reason="шаг — тоже расстояние"),
        "expert",
    )
    assert item.plan.template == "distance_from_source"
    assert item.plan.planner_status == "reviewed"
    assert item.plan.source.restriction_id == "r"
    assert item.review_status == "approved" and item.author == "expert"


async def test_approving_a_plan_without_candidate_is_refused():
    plan = (
        await CheckPlanPlanner().plan(
            "r",
            ExtractedRestriction(subject="Объект", object="Территория", kind="иное"),
        )
    ).model_dump(mode="json")
    service = QueryService(Store(plan), None, None, Settings(), writer=Store(plan))
    with pytest.raises(ValueError):
        await service.review_check_plan(
            "r", CheckPlanReviewRequest(action="approve"), "expert"
        )
