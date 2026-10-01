"""LLM passes of the planner: rewrite a norm into a ``NormSpec``, then verify the plan.

Pass 2 (rewrite) reads the whole clause and fills a closed ``NormSpec``; it runs
``votes`` times at different temperatures and is accepted only when every vote
compiles to the same plan. Pass 3 (verify) shows an independent prompt the clause
and a plain-language rendering of the plan and asks targeted yes/no questions. A
plan runs automatically only after both.

Nothing here can loosen a refusal: the LLM proposes, ``SpecCompiler`` and the
verifier decide.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from src.dto.check_plan import CheckPlan
from src.pipeline.models import ExtractedRestriction
from src.pipeline.norm_spec import (
    NormSpec,
    SpecCompiler,
    plan_fingerprint,
    render_plan,
)
from src.pipeline.urban_catalog import UrbanCatalog
from src.providers.base import LLMProvider

log = structlog.get_logger(__name__)

_CLAUSE_LIMIT = 4000

REWRITE_SYSTEM = """Ты — эксперт по градостроительным нормам. Тебе дают пункт нормативного документа и одно извлечённое из него ограничение. Определи, можно ли проверить это ограничение по карте города, и запиши его в виде JSON NormSpec. Верни только JSON без пояснений.

Шаблоны (поле template):
- min_distance: объекты checked должны быть не ближе value от объектов other (operator ">=").
- prohibited_within: размещение объектов checked в границах объектов или зон other запрещено (value не нужен).
- max_distance: для каждого объекта checked в радиусе value по прямой должен быть хотя бы один объект other (operator "<=").
- accessibility: то же, но норма задаёт пешеходную доступность: время (unit "мин") или длину пути (unit "м"/"км"), operator "<=". Для «территориальной доступности» сервиса checked — «Жилой дом» (жители), other — сервис. Транспортная доступность (на автомобиле, общественном транспорте) — template="none".
- attribute_limit: атрибут attribute каждого объекта checked сравнивается с value (operator, unit). attribute: "floors" (этажность, unit "эт"), "height" (высота, unit "м"), "building_area" (площадь застройки здания, unit "м2" или "га"), "area" (площадь объекта, unit "м2" или "га").
- zone_attribute_limit: как attribute_limit, но только для объектов checked внутри функциональной зоны other.
- zone_share: доля площади, занятой объектами checked, в каждой функциональной зоне other (unit "%", operator).
- provision: обеспеченность жителей сервисом other (checked=null, operator ">="); accessibility_value/accessibility_unit — нормативная доступность, если указана в пункте. Два вида норматива (поле provision_basis):
  - "places_per_1000": value мест на 1000 жителей (unit "мест на 1000 жителей");
  - "residents_per_object": objects_count объектов на value жителей («1 объект на 10 тыс. жителей» → value 10, unit "тыс. жителей", objects_count 1; «1 аптека на 5000 человек» → value 5000, unit "жителей").
- none: ограничение нельзя проверить по карте (конструкции, материалы, помещения внутри здания, оборудование, документы, процессы, санитарные требования, значения по ссылке на другой пункт или таблицу).

Правила:
- territorial=true только если ограничение касается размещения объектов на территории, расстояний между ними, их этажности/высоты/площади или обеспеченности населения.
- checked и other — только точные названия из каталога ниже, с указанным entity_type. Если подходящего названия в каталоге нет — template="none".
- checked — объекты, соответствие которых проверяется (обычно жилые дома или размещаемый объект). Для расстояния «от A до B не менее X» checked — объект, который размещают, other — объект, от которого отсчитывают.
- value — число ровно так, как оно написано в пункте; unit — его единица. Не пересчитывай единицы.
- «не более», «не выше», «не далее», «не превышает», «до» — operator "<="; «не менее», «не ближе», «не ниже» — operator ">="; «не реже чем через» и «через каждые» — это шаг вдоль линии, template="none".
- unconditional=true только если пункт не содержит условий, исключений и вариантов («при», «если», «кроме», «за исключением», «для ... допускается», разные значения для разных случаев), которые меняют значение для проверяемых объектов.
- quote — точная цитата из пункта, где стоит число.

Формат ответа:
{"territorial": bool, "template": "...", "checked": {"entity": "...", "entity_type": "service|physical_object|functional_zone"} | null, "other": {...} | null, "operator": "<=|>=|<|>|==" | null, "value": number | null, "unit": "..." | null, "attribute": "floors|height|building_area|area" | null, "accessibility_value": number | null, "accessibility_unit": "..." | null, "provision_basis": "places_per_1000|residents_per_object" | null, "objects_count": number | null, "unconditional": bool, "quote": "...", "reason": "кратко, почему так"}"""

VERIFY_SYSTEM = """Ты проверяешь автоматическую формализацию градостроительной нормы. Тебе дают пункт документа и описание проверки, которую выполнит программа на карте. Ответь, верно ли проверка передаёт смысл пункта. Будь строгим: при любом сомнении отвечай false. Верни только JSON:
{"faithful": bool, "checked_side_ok": bool, "direction_ok": bool, "value_ok": bool, "unconditional": bool, "territorial": bool, "issues": ["кратко, что не так"]}
- faithful: проверка соответствует требованию пункта, а не другому требованию из него;
- checked_side_ok: проверяются те объекты, к которым норма предъявляет требование;
- direction_ok: верно понято, минимум это или максимум (не ближе / не дальше, не менее / не более);
- value_ok: число и единица взяты из пункта без искажений;
- unconditional: в пункте нет условий или исключений, которые проверка не учитывает;
- territorial: это требование к размещению или параметрам объектов на территории, а не к конструкциям, помещениям, оборудованию или документам.
Норма о доступности или обеспеченности сервисом («уровень территориальной доступности школ — 500 м») — требование к удобству жителей: её проверяют для жилых домов, у которых сервис должен быть в пределах доступности. Такая проверка жилых домов верна (checked_side_ok=true)."""

_VERIFY_KEYS = (
    "faithful",
    "checked_side_ok",
    "direction_ok",
    "value_ok",
    "unconditional",
    "territorial",
)


@dataclass
class PlanContext:
    """Where a restriction comes from; the clause text is what the LLM passes read."""

    clause_text: str = ""
    breadcrumb: str | None = None
    document_name: str | None = None
    clause_number: str | None = None


@dataclass
class RefineOutcome:
    plan: CheckPlan | None
    reasons: list[str]
    candidate: CheckPlan | None = None
    trace: dict[str, Any] = field(default_factory=dict)


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    faithful: bool = False
    checked_side_ok: bool = False
    direction_ok: bool = False
    value_ok: bool = False
    unconditional: bool = False
    territorial: bool = False
    issues: list[str] = []


def parse_json_object(raw: str) -> dict | None:
    """The single JSON object in a model answer (wrappers and fences tolerated)."""
    match = re.search(r"\{.*\}", raw or "", flags=re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and len(value) == 1:
        inner = next(iter(value.values()))
        if isinstance(inner, dict) and ("template" in inner or "faithful" in inner):
            value = inner
    return value if isinstance(value, dict) else None


def _source_text(ex: ExtractedRestriction, ctx: PlanContext) -> str:
    text = ctx.clause_text or ""
    if ex.extraction_text and ex.extraction_text not in text:
        text = f"{text}\n{ex.extraction_text}".strip()
    return text


class NormRefiner:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        votes: int = 2,
        verify: bool = True,
        min_distance_m: float = 3.0,
        concurrency: int = 16,
        vote_temperatures: tuple[float, ...] = (0.0, 0.7, 0.4),
        max_tokens: int = 2048,
    ) -> None:
        self.llm = llm
        self.votes = max(1, votes)
        self.verify_enabled = verify
        self.min_distance_m = min_distance_m
        self.vote_temperatures = vote_temperatures
        self.max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _complete(self, prompt: str, system: str, temperature: float) -> str:
        async with self._semaphore:
            return await self.llm.complete(
                prompt,
                system=system,
                temperature=temperature,
                max_tokens=self.max_tokens,
            )

    # --- pass 2 -----------------------------------------------------------------------

    def _rewrite_prompt(
        self,
        ex: ExtractedRestriction,
        ctx: PlanContext,
        catalog: UrbanCatalog,
        blocked_reasons: list[str],
    ) -> str:
        restriction = {
            "subject": ex.subject,
            "object": ex.object,
            "kind": ex.kind,
            "value": ex.value.model_dump(exclude_none=True) if ex.value else None,
            "fragment": ex.extraction_text,
        }
        return "\n".join(
            [
                f"Каталог Urban API:\n{catalog.prompt_listing()}",
                "",
                f"Документ: {ctx.document_name or '—'}",
                f"Раздел: {ctx.breadcrumb or '—'}",
                f"Пункт {ctx.clause_number or ''}:\n{_source_text(ex, ctx)[:_CLAUSE_LIMIT]}",
                "",
                "Извлечённое ограничение: "
                + json.dumps(restriction, ensure_ascii=False),
                (
                    "Автоматический разбор не смог построить проверку: "
                    + ", ".join(blocked_reasons)
                    if blocked_reasons
                    else ""
                ),
            ]
        )

    async def rewrite(
        self,
        restriction_id: str,
        ex: ExtractedRestriction,
        ctx: PlanContext,
        catalog: UrbanCatalog,
        blocked_reasons: list[str],
    ) -> RefineOutcome:
        compiler = SpecCompiler(catalog, min_distance_m=self.min_distance_m)
        source_text = _source_text(ex, ctx)
        prompt = self._rewrite_prompt(ex, ctx, catalog, blocked_reasons)
        votes: list[dict[str, Any]] = []
        plans: list[CheckPlan] = []
        for index in range(self.votes):
            temperature = self.vote_temperatures[index % len(self.vote_temperatures)]
            try:
                raw = await self._complete(prompt, REWRITE_SYSTEM, temperature)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - an LLM outage only blocks the plan
                log.warning(
                    "check_plan_rewrite_failed",
                    restriction_id=restriction_id,
                    error=str(exc),
                )
                votes.append({"error": "llm_failed"})
                return RefineOutcome(
                    None, ["rewrite_llm_failed"], trace={"votes": votes}
                )
            payload = parse_json_object(raw)
            try:
                spec = NormSpec.model_validate(payload or {})
            except ValidationError:
                votes.append({"error": "invalid_spec"})
                return RefineOutcome(
                    None, ["rewrite_invalid_output"], trace={"votes": votes}
                )
            plan, reasons = compiler.compile(
                spec,
                restriction_id=restriction_id,
                source_text=source_text,
                source={
                    "extraction_text": ex.extraction_text,
                    "labels": [
                        item
                        for item in (
                            ex.subject,
                            ex.object,
                            ex.value.condition if ex.value else None,
                        )
                        if item
                    ],
                },
            )
            votes.append(
                {
                    "spec": spec.model_dump(mode="json", exclude_none=True),
                    "reasons": reasons,
                }
            )
            if plan is None:
                # A refusal is final: further votes cannot make this spec executable.
                # After an executable vote it means the votes disagree.
                if plans:
                    return RefineOutcome(
                        None,
                        ["rewrite_votes_disagree", *reasons],
                        candidate=plans[0],
                        trace={"votes": votes},
                    )
                return RefineOutcome(None, reasons, trace={"votes": votes})
            plans.append(plan)
        fingerprints = {plan_fingerprint(plan) for plan in plans}
        if len(fingerprints) > 1:
            return RefineOutcome(
                None,
                ["rewrite_votes_disagree"],
                candidate=plans[0],
                trace={"votes": votes},
            )
        return RefineOutcome(plans[0], [], candidate=plans[0], trace={"votes": votes})

    # --- pass 3 -----------------------------------------------------------------------

    async def verify(
        self,
        plan: CheckPlan,
        ex: ExtractedRestriction,
        ctx: PlanContext,
    ) -> tuple[bool, list[str], dict[str, Any]]:
        """``(accepted, reasons, verdict)``; disabled verification accepts."""
        if not self.verify_enabled:
            return True, [], {"skipped": True}
        prompt = "\n".join(
            [
                f"Документ: {ctx.document_name or '—'}",
                f"Раздел: {ctx.breadcrumb or '—'}",
                f"Пункт {ctx.clause_number or ''}:\n{_source_text(ex, ctx)[:_CLAUSE_LIMIT]}",
                "",
                f"Проверка: {render_plan(plan)}",
            ]
        )
        try:
            raw = await self._complete(prompt, VERIFY_SYSTEM, 0.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("check_plan_verify_failed", error=str(exc))
            return False, ["verifier_llm_failed"], {"error": "llm_failed"}
        payload = parse_json_object(raw)
        if payload is None:
            return False, ["verifier_invalid_output"], {"error": "invalid_output"}
        try:
            verdict = _Verdict.model_validate(payload)
        except ValidationError:
            return False, ["verifier_invalid_output"], {"error": "invalid_output"}
        failed = [key for key in _VERIFY_KEYS if not getattr(verdict, key)]
        reasons = [f"verifier_{key}_failed" for key in failed]
        return (
            not failed,
            reasons,
            {**verdict.model_dump(), "rendering": render_plan(plan)},
        )
