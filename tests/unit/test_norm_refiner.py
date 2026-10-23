"""Multi-pass planning with a scripted LLM: rewrite votes, verification, grounding."""

import json

from src.pipeline.check_plan_planner import CHECK_PLANNER_VERSION, CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.norm_refiner import (
    REWRITE_SYSTEM,
    VERIFY_SYSTEM,
    PlanContext,
    clause_excerpt,
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
        self.options: list[dict] = []
        self.prompts: list[str] = []

    async def complete(
        self,
        prompt,
        *,
        system=None,
        temperature=None,
        max_tokens=None,
        reasoning_effort=None,
        seed=None,
    ):
        self.options.append({"reasoning_effort": reasoning_effort, "seed": seed})
        self.prompts.append(prompt)
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
    assert llm.calls == [("rewrite", 0.0), ("rewrite", 0.3), ("verify", 0.0)]
    assert trace["planner_version"] == CHECK_PLANNER_VERSION
    assert [item["pass"] for item in trace["passes"]] == [
        "deterministic",
        "rewrite",
        "verify",
    ]


async def test_disagreeing_votes_keep_the_candidate_for_review():
    below = {**FLOORS_SPEC, "operator": "<"}
    at_least = {**FLOORS_SPEC, "operator": ">="}
    llm = ScriptedLLM(rewrites=[FLOORS_SPEC, below, at_least])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "unsupported"
    assert "rewrite_votes_disagree" in plan.params["blocked_reasons"]
    assert plan.params["candidate_plan"]["template"] == "object_attribute_threshold"
    assert [call[0] for call in llm.calls] == ["rewrite"] * 3


async def test_two_of_three_votes_are_enough():
    below = {**FLOORS_SPEC, "operator": "<"}
    llm = ScriptedLLM(rewrites=[FLOORS_SPEC, below, FLOORS_SPEC], verdicts=[ACCEPT])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "auto"
    assert plan.params["operator"] == "<="
    assert llm.calls == [
        ("rewrite", 0.0),
        ("rewrite", 0.3),
        ("rewrite", 0.5),
        ("verify", 0.0),
    ]


async def test_invalid_vote_is_outvoted():
    llm = ScriptedLLM(rewrites=["no json", FLOORS_SPEC, FLOORS_SPEC], verdicts=[ACCEPT])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "auto"


async def test_compile_refusal_and_one_plan_do_not_agree():
    missing = {**FLOORS_SPEC, "value": 12}  # 12 is not in the clause
    llm = ScriptedLLM(rewrites=[missing, FLOORS_SPEC, missing])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=CLAUSE))
    assert plan.planner_status == "unsupported"
    assert {"rewrite_votes_disagree", "value_not_in_source"} <= set(
        plan.params["blocked_reasons"]
    )


async def test_calls_are_seeded_per_restriction_and_use_the_reasoning_budget():
    def run():
        return ScriptedLLM(rewrites=[FLOORS_SPEC, FLOORS_SPEC], verdicts=[ACCEPT])

    first, second, other = run(), run(), run()
    for llm, restriction_id in ((first, "r"), (second, "r"), (other, "q")):
        await _planner(llm, reasoning_effort="medium").plan(
            restriction_id, _height(), PlanContext(clause_text=CLAUSE)
        )
    seeds = [item["seed"] for item in first.options]
    assert seeds == [item["seed"] for item in second.options]
    assert seeds != [item["seed"] for item in other.options]
    assert len(set(seeds)) == 3
    assert {item["reasoning_effort"] for item in first.options} == {"medium"}


def test_long_clause_keeps_its_head_and_the_restriction_row():
    head = "Наименование объекта | Уровень обеспеченности | Доступность. "
    rows = "".join(f"Объект {n} — 1 на 10 тыс. человек, 30 минут. " for n in range(300))
    row = "Концертный зал — 1 объект, транспортная доступность 30-40 минут. "
    text = head + rows + row + rows
    ex = ExtractedRestriction(
        subject="концертный зал",
        object="доступность",
        kind="максимальное_время",
        extraction_text="",
    )
    excerpt = clause_excerpt(text, ex, limit=4000)
    assert len(excerpt) <= 4010
    assert excerpt.startswith(head)
    assert row.strip() in excerpt
    assert clause_excerpt(CLAUSE, ex) == CLAUSE


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


STRICTEST_ACCEPT = {**ACCEPT, "strictest_ok": True}


async def test_conditional_clause_is_planned_with_its_strictest_value():
    clause = (
        "7.3 Высота жилых домов не должна превышать 9 этажей, "
        "в сельских поселениях — 4 этажей."
    )
    conditional = {
        **FLOORS_SPEC,
        "unconditional": False,
        "conditions": ["в сельских поселениях"],
        "variants": [{"value": 4, "unit": "эт", "condition": "в сельских поселениях"}],
    }
    # 4 floors in rural settlements is stricter than 9 and applies to every house.
    llm = ScriptedLLM(rewrites=[conditional, conditional], verdicts=[STRICTEST_ACCEPT])
    plan, trace = await _planner(llm).plan_with_trace(
        "r", _height(), PlanContext(clause_text=clause)
    )
    assert plan.planner_status == "auto"
    assert plan.params["threshold"] == 4
    assert plan.applicability.conditions == ["в сельских поселениях"]
    rendering = trace["passes"][-1]["verdict"]["rendering"]
    assert "самое строгое значение: 4 эт" in rendering


async def test_agreeing_votes_keep_the_strictest_marker():
    clause = "7.4 Высота жилых домов не должна превышать 9 этажей, кроме доминант."
    conditional = {
        **FLOORS_SPEC,
        "unconditional": False,
        "conditions": ["кроме доминант"],
    }
    llm = ScriptedLLM(rewrites=[FLOORS_SPEC, conditional], verdicts=[STRICTEST_ACCEPT])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=clause))
    assert plan.planner_status == "auto"
    assert plan.applicability is not None


async def test_strictest_plan_needs_the_verifier_to_confirm_no_stricter_value():
    clause = "7.4 Высота жилых домов не должна превышать 9 этажей, кроме доминант."
    conditional = {**FLOORS_SPEC, "unconditional": False, "conditions": ["кроме"]}
    llm = ScriptedLLM(rewrites=[conditional, conditional], verdicts=[ACCEPT])
    plan = await _planner(llm).plan("r", _height(), PlanContext(clause_text=clause))
    assert plan.planner_status == "unsupported"
    assert "verifier_strictest_ok_failed" in plan.params["blocked_reasons"]
