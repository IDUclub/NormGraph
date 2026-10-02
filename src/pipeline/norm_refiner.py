"""LLM passes of the planner: rewrite a norm into a ``NormSpec``, then verify the plan.

Pass 2 (rewrite) reads the whole clause and fills a closed ``NormSpec``; it runs
up to ``votes`` times at different temperatures and is accepted only when
``agreement`` votes compile to the same plan. Pass 3 (verify) shows an independent
prompt the clause and a plain-language rendering of the plan and asks targeted
yes/no questions. A plan runs automatically only after both.

Every call carries a seed derived from the restriction, so planning the same norm
again samples the same answers.

Nothing here can loosen a refusal: the LLM proposes, ``SpecCompiler`` and the
verifier decide.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from src.dto.check_plan import CheckPlan
from src.pipeline.models import ExtractedRestriction
from src.pipeline.norm_spec import (
    TRANSPORT_SPEED_M_PER_MIN,
    NormSpec,
    SpecCompiler,
    plan_fingerprint,
    render_plan,
)
from src.pipeline.urban_catalog import UrbanCatalog
from src.providers.base import LLMProvider

log = structlog.get_logger(__name__)

_CLAUSE_LIMIT = 4000
# Of a longer clause the prompt keeps the head (a table's column titles) and a window
# around the restriction's own row.
_CLAUSE_HEAD = 600
# The verifier judges one requirement: a shorter excerpt keeps it on the right row.
_VERIFY_CLAUSE_LIMIT = 2500
# A vote judging the norm uncheckable ends the rewrite pass: no later vote is needed.
_FINAL_REFUSALS = {"rewrite_not_territorial", "rewrite_no_template"}

REWRITE_SYSTEM = """Ты — эксперт по градостроительным нормам. Тебе дают пункт нормативного документа и одно извлечённое из него ограничение. Определи, можно ли проверить это ограничение по карте города, и запиши его в виде JSON NormSpec. Верни только JSON без пояснений.

Шаблоны (поле template):
- min_distance: объекты checked должны быть не ближе value от объектов other (operator ">=").
- prohibited_within: размещение объектов checked в границах объектов или зон other запрещено (value не нужен).
- max_distance: для каждого объекта checked в радиусе value по прямой должен быть хотя бы один объект other (operator "<=").
- accessibility: то же, но норма задаёт доступность: время (unit "мин") или длину пути (unit "м"/"км"), operator "<=". accessibility_mode: "walk" — пешеходная, шаговая; "transport" — транспортная (на транспорте, автомобиле, комбинированная). Для «территориальной доступности» сервиса checked — «Жилой дом» (жители), other — сервис. «Максимально допустимый уровень территориальной доступности … м», «… от места проживания» — это accessibility, а не min_distance.
- attribute_limit: атрибут attribute каждого объекта checked сравнивается с value (operator, unit). attribute: "floors" (этажность, unit "эт"), "height" (высота, unit "м"), "building_area" (площадь застройки здания, unit "м2" или "га"), "area" (площадь объекта, unit "м2" или "га").
- zone_attribute_limit: как attribute_limit, но только для объектов checked внутри функциональной зоны other.
- zone_share: доля площади, занятой объектами checked, в каждой функциональной зоне other (unit "%", operator).
- provision: обеспеченность жителей сервисом other (checked=null, operator ">="); accessibility_value/accessibility_unit — нормативная доступность, если указана в пункте. Два вида норматива (поле provision_basis):
  - "places_per_1000": value мест на 1000 жителей (unit "мест на 1000 жителей");
  - "residents_per_object": objects_count объектов на value жителей («1 объект на 10 тыс. жителей» → value 10, unit "тыс. жителей", objects_count 1; «1 аптека на 5000 человек» → value 5000, unit "жителей").
- none: ограничение нельзя проверить по карте (конструкции, материалы, помещения внутри здания, оборудование, документы, процессы, санитарные требования, значения по ссылке на другой пункт или таблицу).

Правила:
- territorial=true только если ограничение касается размещения объектов на территории, расстояний между ними, их этажности/высоты/площади или обеспеченности населения.
- checked и other — только точные названия из каталога ниже, с указанным entity_type. Если объекта нет в каталоге дословно, выбери тип каталога, разновидностью которого он является («краеведческий музей» → «Музей», «розничный рынок» → «Рынок», «общеобразовательная организация» → «Школа», «дом культуры» → «Дворец культуры», «кинозал» → «Кинотеатр», «остановка НГПТ» → «Остановка наземного общественного транспорта»). Если такого типа нет (бульвар, сквер, гидропарк) — template="none".
- checked — объекты, соответствие которых проверяется (обычно жилые дома или размещаемый объект). Для расстояния «от A до B не менее X» checked — объект, который размещают, other — объект, от которого отсчитывают.
- value — число ровно так, как оно написано в пункте; unit — его единица. Не пересчитывай единицы. Если пункт даёт несколько значений, основное value — значение извлечённого ограничения. Диапазон («30-40 минут», «1200-1500 м») — это два значения: value — одна граница, вторая — в variants; value никогда не оставляй пустым, если в строке пункта есть число.
- Таблица «Максимально допустимый уровень территориальной доступности»: значение в строке объекта — верхняя граница доступности объекта для жителей (template="accessibility", operator "<=", checked — «Жилой дом»), даже если извлечённое ограничение записано как «не менее».
- «не более», «не выше», «не далее», «не превышает», «до» — operator "<="; «не менее», «не ближе», «не ниже» — operator ">="; «не реже чем через» и «через каждые» — это шаг вдоль линии, template="none".
- housing — если значение задано для типа жилой застройки, к которому относятся проверяемые жилые дома: "individual" (индивидуальная), "lowrise" (малоэтажная, блокированная), "midrise" (среднеэтажная), "multistorey" (многоэтажная); иначе []. Программа проверит только дома этого типа (по этажности).
- unconditional=true только если пункт не содержит условий, исключений и вариантов («при», «если», «кроме», «за исключением», «для ... допускается», разные значения для разных случаев: по типу поселения, численности населения и т. п.), которые меняют значение для проверяемых объектов. Деление по типу жилой застройки, записанное в housing, условием не считается.
- Если unconditional=false: conditions — короткие цитаты всех условий и исключений пункта; variants — все значения пункта для этого же требования, каждое со своим условием: [{"value": число, "unit": "...", "objects_count": число | null, "accessibility_value": число | null, "accessibility_unit": "..." | null, "accessibility_mode": "walk|transport" | null, "housing": [...], "condition": "при каком условии"}]. Программа применит ко всем объектам самое строгое из значений, поэтому перечисли все варианты, включая исключения, смягчающие норму. Числа вариантов, как и value, пиши ровно так, как они написаны в пункте, без пересчёта единиц («1 км» → value 1, unit "км"). Основные value/unit — любое из значений пункта.
- quote — точная цитата из пункта, где стоит число.

Формат ответа:
{"territorial": bool, "template": "...", "checked": {"entity": "...", "entity_type": "service|physical_object|functional_zone"} | null, "other": {...} | null, "operator": "<=|>=|<|>|==" | null, "value": number | null, "unit": "..." | null, "attribute": "floors|height|building_area|area" | null, "accessibility_value": number | null, "accessibility_unit": "..." | null, "accessibility_mode": "walk|transport" | null, "housing": ["individual|lowrise|midrise|multistorey"], "provision_basis": "places_per_1000|residents_per_object" | null, "objects_count": number | null, "unconditional": bool, "conditions": ["..."], "variants": [{"value": number, "unit": "...", "housing": [], "condition": "..."}], "quote": "...", "reason": "кратко, почему так"}"""

VERIFY_SYSTEM = """Ты проверяешь автоматическую формализацию градостроительной нормы. Тебе дают пункт документа и описание проверки, которую выполнит программа на карте. Ответь, верно ли проверка передаёт смысл пункта. Будь строгим: при любом сомнении отвечай false. Верни только JSON:
{"faithful": bool, "checked_side_ok": bool, "direction_ok": bool, "value_ok": bool, "unconditional": bool, "territorial": bool, "strictest_ok": bool, "issues": ["кратко, что не так"]}
- faithful: проверка соответствует проверяемому требованию пункта, а не другому требованию из него (другие строки таблицы и другие требования пункта проверяются отдельно);
- checked_side_ok: проверяются те объекты, к которым норма предъявляет требование;
- direction_ok: верно понято, минимум это или максимум (не ближе / не дальше, не менее / не более);
- value_ok: число и единица взяты из пункта без искажений;
- unconditional: в пункте нет условий или исключений, которые проверка не учитывает. Если в описании проверки сказано, что ко всем объектам применяется самое строгое значение пункта, ответь true, когда это значение действительно самое строгое из значений пункта для этого требования (наибольшее для минимума, наименьшее для максимума), и false, если в пункте есть более строгое значение;
- strictest_ok: если проверка применяет самое строгое значение пункта — выпиши все значения этого требования для этих же объектов из пункта (во всех строках и колонках) и ответь true, только если ни одно из них не строже применённого; если проверка не говорит о самом строгом значении — true;
- territorial: это требование к размещению или параметрам объектов на территории, а не к конструкциям, помещениям, оборудованию или документам.
Если в описании проверки сказано, что пункт содержит условия и ко всем объектам применяется самое строгое значение, это намеренное консервативное упрощение, о котором пользователь будет предупреждён: не считай неучтённые условия применения (тип или численность поселения, этажность, размер, исключения) ошибкой в faithful и checked_side_ok. Оцени, верно ли выбраны требование, проверяемые объекты, направление и самое строгое значение; checked_side_ok=false ставь, только если требование относится к объектам другого вида.
Норма о доступности или обеспеченности сервисом («уровень территориальной доступности школ — 500 м») — требование к удобству жителей: её проверяют для жилых домов, у которых сервис должен быть в пределах доступности. Такая проверка жилых домов верна (checked_side_ok=true).
Транспортную доступность программа оценивает приближённо — радиусом по прямой при средней скорости транспорта; это не ошибка. Если норма обеспеченности не задаёт доступность, программа берёт нормативную доступность сервиса из справочника — это тоже не ошибка.
Если пункт задаёт диапазон («30-40 минут»), а проверка применяет его строгую границу как самое строгое значение, это верное значение (value_ok=true, direction_ok=true).
Если проверка охватывает только жилые дома определённой этажности, это учёт деления пункта по типу застройки (малоэтажная — до 4 этажей, среднеэтажная — 5–8, многоэтажная — от 9): оцени, верно ли выбран тип застройки для этого значения."""

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
    strictest_ok: bool = False
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


def clause_excerpt(
    text: str, ex: ExtractedRestriction, limit: int = _CLAUSE_LIMIT
) -> str:
    """The clause as the prompts show it: whole, or its head plus the restriction's row.

    Long clauses are mostly tables; their first characters hold the column titles and
    the restriction's own row may be anywhere below.
    """
    if len(text) <= limit:
        return text
    folded = text.casefold()
    anchors = [ex.extraction_text, ex.subject, ex.object]
    position = next(
        (
            index
            for anchor in anchors
            if anchor and len(anchor.strip()) >= 4
            for index in [folded.find(anchor.strip().casefold())]
            if index >= 0
        ),
        0,
    )
    if position < limit - _CLAUSE_HEAD // 2:
        return text[:limit]
    window = limit - _CLAUSE_HEAD
    start = max(_CLAUSE_HEAD, position - window // 3)
    end = min(len(text), start + window)
    return f"{text[:_CLAUSE_HEAD]} … {text[start:end]}"


def _seed(restriction_id: str, salt: str) -> int:
    digest = hashlib.sha256(f"{restriction_id}:{salt}".encode()).hexdigest()
    return int(digest[:8], 16) & 0x7FFFFFFF


class NormRefiner:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        votes: int = 3,
        agreement: int = 2,
        verify: bool = True,
        min_distance_m: float = 3.0,
        concurrency: int = 16,
        vote_temperatures: tuple[float, ...] = (0.0, 0.3, 0.5),
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        transport_speed_m_per_min: float = TRANSPORT_SPEED_M_PER_MIN,
    ) -> None:
        self.llm = llm
        self.votes = max(1, votes)
        self.agreement = max(1, min(agreement, self.votes))
        self.verify_enabled = verify
        self.min_distance_m = min_distance_m
        self.vote_temperatures = vote_temperatures
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.transport_speed_m_per_min = transport_speed_m_per_min
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _complete(
        self, prompt: str, system: str, temperature: float, seed: int
    ) -> str:
        async with self._semaphore:
            for attempt in range(2):
                try:
                    return await self.llm.complete(
                        prompt,
                        system=system,
                        temperature=temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                        seed=seed,
                    )
                except Exception:  # noqa: BLE001 - one retry of a transient failure
                    if attempt:
                        raise
                    await asyncio.sleep(1.0)
        raise AssertionError("unreachable")  # pragma: no cover

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
                f"Пункт {ctx.clause_number or ''}:\n{clause_excerpt(_source_text(ex, ctx), ex)}",
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
        compiler = SpecCompiler(
            catalog,
            min_distance_m=self.min_distance_m,
            transport_speed_m_per_min=self.transport_speed_m_per_min,
        )
        source_text = _source_text(ex, ctx)
        prompt = self._rewrite_prompt(ex, ctx, catalog, blocked_reasons)
        votes: list[dict[str, Any]] = []
        groups: dict[str, list[CheckPlan]] = {}
        refusals: list[list[str]] = []
        for index in range(self.votes):
            temperature = self.vote_temperatures[index % len(self.vote_temperatures)]
            try:
                raw = await self._complete(
                    prompt,
                    REWRITE_SYSTEM,
                    temperature,
                    _seed(restriction_id, f"rewrite:{index}"),
                )
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
                if payload is None:
                    raise ValueError("no JSON object in the answer")
                spec = NormSpec.model_validate(payload)
            except ValueError:  # ValidationError included
                votes.append({"error": "invalid_spec"})
                refusals.append(["rewrite_invalid_output"])
                if not self._can_agree(groups, index):
                    break
                continue
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
                refusals.append(reasons)
                if not groups and set(reasons) & _FINAL_REFUSALS:
                    # The model judged the norm uncheckable before any plan was seen.
                    break
            else:
                groups.setdefault(plan_fingerprint(plan), []).append(plan)
            if self._agreed(groups) or not self._can_agree(groups, index):
                break
        agreed = self._agreed(groups)
        if agreed is not None:
            # Agreeing votes check the same thing; keep the strictest-variant marker
            # if any of them saw the clause's conditions.
            plan = next((item for item in agreed if item.applicability), agreed[0])
            return RefineOutcome(plan, [], candidate=plan, trace={"votes": votes})
        first_refusal = refusals[0] if refusals else []
        if groups:
            candidate = next(iter(groups.values()))[0]
            return RefineOutcome(
                None,
                list(dict.fromkeys(["rewrite_votes_disagree", *first_refusal])),
                candidate=candidate,
                trace={"votes": votes},
            )
        return RefineOutcome(None, first_refusal, trace={"votes": votes})

    def _agreed(self, groups: dict[str, list[CheckPlan]]) -> list[CheckPlan] | None:
        return next(
            (plans for plans in groups.values() if len(plans) >= self.agreement), None
        )

    def _can_agree(self, groups: dict[str, list[CheckPlan]], index: int) -> bool:
        """Whether the votes left after ``index`` can still reach the agreement."""
        best = max((len(plans) for plans in groups.values()), default=0)
        return best + (self.votes - index - 1) >= self.agreement

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
        restriction = " — ".join(
            item for item in (ex.subject, ex.object, ex.extraction_text) if item
        )
        prompt = "\n".join(
            [
                f"Документ: {ctx.document_name or '—'}",
                f"Раздел: {ctx.breadcrumb or '—'}",
                f"Пункт {ctx.clause_number or ''}:\n"
                f"{clause_excerpt(_source_text(ex, ctx), ex, limit=_VERIFY_CLAUSE_LIMIT)}",
                "",
                f"Проверяемое требование пункта (строка или фраза): {restriction}",
                f"Проверка: {render_plan(plan)}",
            ]
        )
        try:
            raw = await self._complete(
                prompt,
                VERIFY_SYSTEM,
                0.0,
                _seed(plan.source.restriction_id or "", "verify"),
            )
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
        # For a strictest-variant plan «strictest_ok» replaces «unconditional»: the
        # conditions are known and reported, the question is the value chosen.
        keys = (
            tuple(key for key in _VERIFY_KEYS if key != "unconditional")
            + ("strictest_ok",)
            if plan.applicability
            else _VERIFY_KEYS
        )
        failed = [key for key in keys if not getattr(verdict, key)]
        reasons = [f"verifier_{key}_failed" for key in failed]
        return (
            not failed,
            reasons,
            {**verdict.model_dump(), "rendering": render_plan(plan)},
        )
