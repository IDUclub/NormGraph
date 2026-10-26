"""Norms about red lines are blocked before any pass: Urban API has no red lines."""

import pytest

from src.pipeline.check_plan_planner import RED_LINE_REASON, CheckPlanPlanner
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.norm_guards import mentions_red_line
from tests.unit._catalog import catalog_provider
from tests.unit.test_norm_refiner import ScriptedLLM


@pytest.mark.parametrize(
    "text",
    [
        "отступ от красной линии",
        "Ширина улиц и дорог в красных линиях",
        "территория квартала, ограниченного красными линиями",
        "линии отступа от красных линий",
        "Красная линия",
        "по линии регулирования застройки",
        "по линии застройки",
    ],
)
def test_red_line_wording_is_found(text):
    assert mentions_red_line(text)


@pytest.mark.parametrize(
    "text",
    [
        "линии электропередачи",
        "Красногвардейский район",
        "красные кирпичи на линии фасада",
        "",
    ],
)
def test_other_wording_is_not_a_red_line(text):
    assert not mentions_red_line(text)


def _setback(subject="жилые дома", object_="красная линия", text=None):
    return ExtractedRestriction(
        subject=subject,
        object=object_,
        kind="минимальное_расстояние",
        value=RestrictionValue(operator=">=", number=5, unit="м"),
        extraction_text=text
        or "Жилые дома располагаются не ближе 5 м от красной линии.",
    )


async def test_red_line_norm_is_blocked_without_llm_and_keeps_the_candidate():
    llm = ScriptedLLM()
    planner = CheckPlanPlanner(llm, catalog=catalog_provider())
    plan, trace = await planner.plan_with_trace("r", _setback())
    assert plan.planner_status == "unsupported"
    assert plan.params["blocked_reasons"][0] == RED_LINE_REASON
    assert plan.params["candidate_plan"]["template"] == "distance_from_source"
    assert trace["passes"] == [{"pass": "data", "reasons": [RED_LINE_REASON]}]
    assert llm.calls == []


async def test_red_line_in_the_quote_alone_blocks_the_norm():
    plan = await CheckPlanPlanner().plan(
        "r",
        ExtractedRestriction(
            subject="территория квартала",
            object="границы",
            kind="требование_размещения",
            extraction_text="Границы территории квартала следует устанавливать по "
            "красным линиям улиц и дорог",
        ),
    )
    assert plan.params["blocked_reasons"] == [RED_LINE_REASON]


async def test_other_guards_stay_next_to_the_red_line_reason():
    plan = await CheckPlanPlanner().plan(
        "r", _setback(object_="отступ от красной линии")
    )
    reasons = plan.params["blocked_reasons"]
    assert reasons[0] == RED_LINE_REASON
    assert "non_spatial_entity" in reasons


async def test_norm_without_red_lines_is_planned_as_before():
    plan = await CheckPlanPlanner().plan(
        "r",
        _setback(
            object_="автозаправочные станции",
            text="Жилые дома располагаются не ближе 5 м от автозаправочных станций.",
        ),
    )
    assert RED_LINE_REASON not in (plan.params.get("blocked_reasons") or [])
