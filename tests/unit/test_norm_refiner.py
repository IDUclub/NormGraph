"""Multi-pass planning with a scripted LLM: rewrite votes, verification, grounding."""

import json

from src.pipeline.check_plan_planner import CHECK_PLANNER_VERSION, CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.norm_refiner import (
    REWRITE_SYSTEM,
    VERIFY_SYSTEM,
    PlanContext,
    parse_json_object,
)
from tests.unit._catalog import catalog_provider

CLAUSE = (
    "7.2 Высота жилых домов в границах исторического поселения не должна превышать "
    "9 этажей."
)
FLOORS_SPEC = {
    "territorial": True,
    "template": "attribute_limit",
    "checked": {"entity": "Жилой дом", "entity_type": "physical_object"},
    "operator": "<=",
    "value": 9,
    "unit": "эт",
    "attribute": "floors",
    "unconditional": True,
    "quote": "не должна превышать 9 этажей",
}
ACCEPT = {
    "faithful": True,
    "checked_side_ok": True,
    "direction_ok": True,
    "value_ok": True,
    "unconditional": True,
    "territorial": True,
    "issues": [],
}


class ScriptedLLM:
    """Answers rewrite and verify prompts from separate queues and records the calls."""

    def __init__(self, rewrites=(), verdicts=()):
        self.rewrites = list(rewrites)
        self.verdicts = list(verdicts)
        self.calls: list[tuple[str, float | None]] = []

    async def complete(self, prompt, *, system=None, temperature=None, max_tokens=None):
        if system == REWRITE_SYSTEM:
            self.calls.append(("rewrite", temperature))
            answer = self.rewrites.pop(0)
        elif system == VERIFY_SYSTEM:
            self.calls.append(("verify", temperature))
            answer = self.verdicts.pop(0)
        else:  # pragma: no cover - unexpected prompt
            raise AssertionError(system)
        if isinstance(answer, Exception):
            raise answer
        return (
            answer
            if isinstance(answer, str)
            else json.dumps(answer, ensure_ascii=False)
        )


def _height():
    return ExtractedRestriction(
        subject="жилые дома",
        object="высота",
        kind="предельная_высота",
        value=RestrictionValue(operator="<=", number=9, unit="этажей"),
        extraction_text="не должна превышать 9 этажей",
    )


def _planner(llm, **kwargs):
    return CheckPlanPlanner(llm, catalog=catalog_provider(), **kwargs)


async def test_blocked_norm_is_rewritten_by_agreeing_votes_and_verified():
    llm = ScriptedLLM(rewrites=[FLOORS_SPEC, FLOORS_SPEC], verdicts=[ACCEPT])
    plan, trace = await _planner(llm).plan_with_trace(
        "r", _height(), PlanContext(clause_text=CLAUSE, document_name="ПЗЗ")
    )
    assert plan.planner_status == "auto"
    assert plan.template == "object_attribute_threshold"
    assert plan.params["threshold"] == 9
    # Two votes at different temperatures, then one verification.
    assert llm.calls == [("rewrite", 0.0), ("rewrite", 0.7), ("verify", 0.0)]
    assert trace["planner_version"] == CHECK_PLANNER_VERSION
    assert [item["pass"] for item in trace["passes"]] == [
        "deterministic",
        "rewrite",
        "verify",
    ]


async def test_disagreeing_votes_keep_the_candidate_for_review():
    other = {**FLOORS_SPEC, "value": 9, "operator": "<"}
    llm = ScriptedLLM(rewrites=[FLOORS_SPEC, other])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "unsupported"
    assert "rewrite_votes_disagree" in plan.params["blocked_reasons"]
    assert plan.params["candidate_plan"]["template"] == "object_attribute_threshold"


async def test_verifier_rejection_blocks_the_rewritten_plan():
    llm = ScriptedLLM(
        rewrites=[FLOORS_SPEC, FLOORS_SPEC],
        verdicts=[
            {
                **ACCEPT,
                "unconditional": False,
                "issues": ["только в исторической части"],
            }
        ],
    )
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "unsupported"
    assert plan.params["blocked_reasons"] == ["verifier_unconditional_failed"]
    assert plan.params["candidate_plan"]["template"] == "object_attribute_threshold"


async def test_first_refusal_stops_further_votes():
    llm = ScriptedLLM(rewrites=[{"territorial": False, "template": "none"}])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "unsupported"
    assert "rewrite_not_territorial" in plan.params["blocked_reasons"]
    assert llm.calls == [("rewrite", 0.0)]


async def test_deterministic_plan_is_verified_and_falls_back_to_rewrite():
    ex = ExtractedRestriction(
        subject="Школа",
        object="Жилой дом",
        kind="минимальное_расстояние",
        value=RestrictionValue(operator=">=", number=50, unit="м"),
        extraction_text="Расстояние от школ до жилых домов не менее 50 м",
    )
    llm = ScriptedLLM(
        rewrites=[{"territorial": False, "template": "none"}],
        verdicts=[{**ACCEPT, "faithful": False}],
    )
    plan, trace = await _planner(llm).plan_with_trace("r", ex)
    assert plan.planner_status == "unsupported"
    assert "verifier_faithful_failed" in plan.params["blocked_reasons"]
    assert plan.params["candidate_plan"]["template"] == "distance_from_source"
    assert [item["pass"] for item in trace["passes"]] == [
        "deterministic",
        "verify",
        "rewrite",
    ]


async def test_deterministic_plan_with_non_catalog_entity_is_rewritten():
    ex = ExtractedRestriction(
        subject="Котельная",
        object="Жилой дом",
        kind="минимальное_расстояние",
        value=RestrictionValue(operator=">=", number=50, unit="м"),
        extraction_text="не менее 50 м",
    )
    llm = ScriptedLLM(rewrites=[{"territorial": True, "template": "none"}])
    plan = await _planner(llm).plan("r", ex)
    assert "entity_not_in_catalog" in plan.params["blocked_reasons"]
    assert llm.calls == [("rewrite", 0.0)]


async def test_obviously_non_territorial_norm_is_not_sent_to_the_llm():
    ex = ExtractedRestriction(
        subject="плёнка",
        object="перехлёст",
        kind="минимальный_размер",
        value=RestrictionValue(operator=">=", number=300, unit="мм"),
        extraction_text="перехлест пленки не менее 300 мм",
    )
    llm = ScriptedLLM()
    plan = await _planner(llm).plan("r", ex)
    assert plan.planner_status == "unsupported"
    assert llm.calls == []


async def test_llm_outage_only_blocks_the_plan():
    llm = ScriptedLLM(rewrites=[RuntimeError("down")])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "unsupported"
    assert "rewrite_llm_failed" in plan.params["blocked_reasons"]


async def test_without_catalog_the_rewrite_is_skipped():
    llm = ScriptedLLM()
    plan, trace = await CheckPlanPlanner(llm).plan_with_trace(
        "r", _height(), PlanContext(clause_text=CLAUSE)
    )
    assert plan.planner_status == "unsupported"
    assert llm.calls == []
    assert trace["passes"][-1] == {
        "pass": "rewrite",
        "skipped": "urban_catalog_unavailable",
    }


async def test_single_vote_and_disabled_verifier():
    llm = ScriptedLLM(rewrites=[FLOORS_SPEC])
    plan = await _planner(llm, votes=1, verify=False).plan(
        "r", _height(), PlanContext(clause_text=CLAUSE)
    )
    assert plan.planner_status == "auto"
    assert llm.calls == [("rewrite", 0.0)]


def test_json_is_found_inside_wrappers_and_fences():
    assert parse_json_object('```json\n{"spec": {"template": "none"}}\n```') == {
        "template": "none"
    }
    assert parse_json_object("no json") is None


async def test_provision_places_are_sent_to_the_rewrite():
    ex = ExtractedRestriction(
        subject="дошкольные образовательные организации",
        object="места",
        kind="требование_числа_посадочных_мест",
        value=RestrictionValue(operator=">=", number=61, unit="мест"),
        extraction_text="61 мест на 1000 человек",
    )
    llm = ScriptedLLM(rewrites=[{"territorial": True, "template": "none"}])
    await _planner(llm).plan("r", ex)
    assert llm.calls == [("rewrite", 0.0)]
