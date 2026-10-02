"""Offline evaluation of the CheckPlan planner on a live NormGraph corpus.

Reads every restriction of a NormGraph instance (``POST /restrictions/list``, read-only),
recovers each clause's full text from IDU_DVD, re-plans the restriction with the current
planner (LLM passes included) and writes, without touching either service:

* ``results.jsonl`` — one line per restriction: stored plan vs. new plan, reasons, trace;
* ``summary.json`` — transitions, executable templates, block reasons, per document;
* ``review.md`` — a random sample of new automatic plans with their clause, for a human.

Configuration comes from the usual ``NG_*`` settings (service auth, IDU_DVD, LLM,
``NG_URBAN_API_URL``). Example::

    uv run python scripts/evaluate_check_plans.py \\
        --normgraph-url http://10.32.11.90:31003 --out eval/dev --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.auth import ServiceTokenAuth, build_service_auth  # noqa: E402
from src.common.config import settings  # noqa: E402
from src.dvd_client import DVDClient  # noqa: E402
from src.pipeline.check_plan_planner import CheckPlanPlanner  # noqa: E402
from src.pipeline.models import (  # noqa: E402
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)
from src.pipeline.norm_refiner import PlanContext  # noqa: E402
from src.pipeline.norm_spec import render_plan  # noqa: E402
from src.pipeline.urban_catalog import UrbanCatalogProvider  # noqa: E402
from src.providers import build_llm  # noqa: E402


async def fetch_restrictions(base_url: str, auth, limit: int | None) -> list[dict]:
    rows: list[dict] = []
    after_id = None
    async with httpx.AsyncClient(base_url=base_url, auth=auth, timeout=120) as client:
        while True:
            body = {"limit": 500, **({"after_id": after_id} if after_id else {})}
            response = await client.post("/restrictions/list", json=body)
            response.raise_for_status()
            page = response.json()
            rows += page.get("hits") or []
            after_id = page.get("next_after_id")
            if not after_id or (limit and len(rows) >= limit):
                return rows[:limit] if limit else rows


async def clause_texts(dvd: DVDClient, doc_ids: set[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for doc_id in sorted(doc_ids):
        try:
            document = await dvd.get_document(doc_id)
        except httpx.HTTPError as exc:
            print(f"! {doc_id}: {exc}", file=sys.stderr)
            continue
        for fragment in document.fragments if document else []:
            texts[fragment.id] = fragment.text
    return texts


def as_restriction(row: dict) -> ExtractedRestriction:
    value = RestrictionValue.model_validate(row.get("value") or {})
    stored = (row.get("check_plan") or {}).get("params") or {}
    measurement = stored.get("measurement")
    return ExtractedRestriction(
        subject=row.get("subject") or "",
        object=row.get("object") or "",
        kind=row.get("kind") or "",
        value=None if value.is_empty() else value,
        measurement=(
            RestrictionMeasurement.model_validate(measurement) if measurement else None
        ),
        extraction_text=row.get("extraction_text") or "",
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--normgraph-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--sample", type=int, default=None, help="random subset of restrictions"
    )
    parser.add_argument("--document", action="append", help="only these document names")
    parser.add_argument(
        "--no-llm", action="store_true", help="deterministic passes only"
    )
    parser.add_argument("--review", type=int, default=60, help="plans in review.md")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--gold",
        type=Path,
        default=None,
        help="hand-labelled cases (JSONL): score precision, recall and stability",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="planner runs per gold case"
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    service_auth = build_service_auth(settings)
    auth = ServiceTokenAuth(service_auth)
    rows = await fetch_restrictions(args.normgraph_url, auth, args.limit)
    if args.gold:
        wanted = {
            json.loads(line)["id"]
            for line in args.gold.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        rows = [r for r in rows if r["id"] in wanted]
    if args.document:
        wanted = [" ".join(name.casefold().split()) for name in args.document]
        rows = [
            r
            for r in rows
            if any(
                name
                in " ".join(
                    ((r.get("provenance") or {}).get("name") or "").casefold().split()
                )
                for name in wanted
            )
        ]
    rng = random.Random(args.seed)
    if args.sample and args.sample < len(rows):
        rows = rng.sample(rows, args.sample)
    print(f"restrictions: {len(rows)}", file=sys.stderr)

    dvd = DVDClient(settings.dvd_base_url, service_auth, timeout=settings.dvd_timeout)
    texts = await clause_texts(
        dvd, {(r.get("provenance") or {}).get("doc_id") for r in rows} - {None}
    )
    llm = None if args.no_llm else build_llm(settings)
    planner = CheckPlanPlanner(
        llm,
        catalog=UrbanCatalogProvider(settings.urban_api_url),
        refine=settings.check_plan_rewrite,
        verify=settings.check_plan_verify,
        votes=settings.check_plan_rewrite_votes,
        agreement=settings.check_plan_rewrite_agreement,
        min_distance_m=settings.check_plan_min_distance_m,
        llm_concurrency=settings.check_plan_llm_concurrency,
        reasoning_effort=settings.check_plan_reasoning_effort,
        transport_speed_kmh=settings.check_plan_transport_speed_kmh,
    )
    if args.gold:
        await gold_main(args, rows, planner, texts)
        await dvd.aclose()
        if llm is not None:
            await llm.aclose()
        return

    done = 0

    async def evaluate(row: dict) -> dict:
        nonlocal done
        provenance = row.get("provenance") or {}
        context = PlanContext(
            clause_text=texts.get(provenance.get("clause_node_id") or "", ""),
            breadcrumb=provenance.get("breadcrumb"),
            document_name=provenance.get("name"),
            clause_number=provenance.get("numbering"),
        )
        before = row.get("check_plan") or {}
        try:
            plan, trace = await planner.plan_with_trace(
                row["id"], as_restriction(row), context
            )
            result = {
                "after_template": plan.template,
                "after_status": plan.planner_status,
                "blocked_reasons": plan.params.get("blocked_reasons") or [],
                "plan": plan.model_dump(mode="json"),
                "rendering": (
                    render_plan(plan) if plan.planner_status == "auto" else None
                ),
                "trace": trace,
            }
        except Exception as exc:  # noqa: BLE001 - keep evaluating the rest
            result = {"error": f"{type(exc).__name__}: {exc}"}
        done += 1
        if done % 100 == 0:
            print(f"  {done}/{len(rows)}", file=sys.stderr)
        return {
            "id": row["id"],
            "document": provenance.get("name"),
            "clause": provenance.get("numbering"),
            "kind": row.get("kind"),
            "subject": row.get("subject"),
            "object": row.get("object"),
            "value": row.get("value"),
            "clause_text": context.clause_text[:1500],
            "before_template": before.get("template"),
            "before_status": before.get("planner_status"),
            **result,
        }

    results = await asyncio.gather(*(evaluate(row) for row in rows))
    await dvd.aclose()
    if llm is not None:
        await llm.aclose()

    with open(args.out / "results.jsonl", "w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    transitions = Counter(
        f"{r.get('before_status')}->{r.get('after_status')}" for r in results
    )
    templates = Counter(
        r["after_template"] for r in results if r.get("after_status") == "auto"
    )
    reasons = Counter(x for r in results for x in r.get("blocked_reasons") or [])
    by_document: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_document[r["document"] or "?"][r.get("after_status") or "error"] += 1
    passes = Counter(
        p["pass"] for r in results for p in (r.get("trace") or {}).get("passes", [])
    )
    summary = {
        "restrictions": len(results),
        "errors": sum("error" in r for r in results),
        "auto_before": sum(r.get("before_status") == "auto" for r in results),
        "auto_after": sum(r.get("after_status") == "auto" for r in results),
        "transitions": dict(transitions.most_common()),
        "templates": dict(templates.most_common()),
        "blocked_reasons": dict(reasons.most_common()),
        "passes": dict(passes),
        "documents": {
            name: dict(counts) for name, counts in sorted(by_document.items())
        },
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    automatic = [r for r in results if r.get("after_status") == "auto"]
    sample = rng.sample(automatic, min(args.review, len(automatic)))
    lines = ["# Automatic plans for review", ""]
    for r in sample:
        lines += [
            f"## {r['document']} п. {r['clause']} — `{r['after_template']}`",
            "",
            f"**Проверка:** {r['rendering']}",
            "",
            f"> {r['clause_text'][:800].strip()}".replace("\n", "\n> "),
            "",
            f"`{r['id']}` · было: {r['before_template']} ({r['before_status']})",
            "",
        ]
    (args.out / "review.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {k: summary[k] for k in list(summary)[:6]}, ensure_ascii=False, indent=1
        )
    )


def plan_signature(plan: dict) -> dict:
    """What an ``auto`` plan checks: template, entity per layer role, main value."""
    params = plan.get("params") or {}
    template = plan.get("template")
    signature: dict = {
        "template": template,
        "layers": {
            item["role"]: item["entity"]
            for item in (plan.get("declared_requirements") or {}).get("layers") or []
        },
        "strictest": plan.get("applicability") is not None,
    }
    scope = plan.get("scope")
    if scope:
        signature["scope"] = [scope.get("min"), scope.get("max")]
    if template == "distance_from_source":
        signature["value"] = (
            0
            if params.get("geometry_mode") == "source_geometry"
            else params.get("distance_m")
        )
    elif template == "presence_within":
        signature["value"] = params.get("distance_m")
    elif template == "accessibility_within":
        limit = params.get("limit") or {}
        timed = limit.get("kind") == "time"
        signature["value"] = limit.get("minutes" if timed else "meters")
        signature["unit"] = "min" if timed else "m"
        signature["mode"] = params.get("mode") or "walk"
    elif template in {"object_attribute_threshold", "zonal_ratio"}:
        signature["value"] = params.get("threshold")
    elif template == "zonal_attribute_threshold":
        signature["value"] = (params.get("threshold_source") or {}).get("value")
    elif template == "distance_table":
        signature["value"] = [band["distance_m"] for band in params.get("bands") or []]
    elif template == "service_provision":
        for key, unit in (
            ("residents_per_service", "residents"),
            ("area_per_1000", "m2_per_1000"),
            ("capacity_per_1000", "per_1000"),
        ):
            if params.get(key):
                signature["value"], signature["unit"] = params[key], unit
                break
        access = params.get("accessibility") or {}
        if access:
            signature["access"] = access.get("minutes") or access.get("meters")
    return signature


def _same(expected, actual) -> bool:
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(_same(e, a) for e, a in zip(expected, actual))
        )
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return isinstance(actual, (int, float)) and abs(
            expected - actual
        ) <= 1e-6 * max(1.0, abs(expected))
    if isinstance(expected, str) and isinstance(actual, str):
        return " ".join(expected.casefold().split()) == " ".join(
            actual.casefold().split()
        )
    return expected == actual


def matches(expected: dict, signature: dict) -> bool:
    """Every key the gold label states agrees with the plan (unstated keys are free)."""
    for key, value in expected.items():
        if key == "layers":
            actual = signature.get("layers") or {}
            if not all(
                _same(entity, actual.get(role)) for role, entity in value.items()
            ):
                return False
        elif not _same(value, signature.get(key)):
            return False
    return True


def score_gold(cases: list[dict], runs: dict[str, list[dict]]) -> dict:
    """Precision, recall and stability of the planner against hand-labelled cases."""
    totals = Counter()
    by_need: dict[str, Counter] = defaultdict(Counter)
    unstable = []
    for case in cases:
        outcomes = runs.get(case["id"]) or []
        expected_auto = case["expected"] == "auto"
        keys = set()
        for outcome in outcomes:
            if outcome.get("error"):
                totals["errors"] += 1
                keys.add("error")
                continue
            signature = outcome.get("signature")
            keys.add(json.dumps(signature, ensure_ascii=False, sort_keys=True))
            correct = signature is not None and any(
                matches(item, signature) for item in case.get("accept") or []
            )
            if signature is None:
                # An optional plan may as well be refused.
                missed = expected_auto and not case.get("optional")
                verdict = "missed" if missed else "correct_reject"
            elif expected_auto and correct:
                verdict = "correct_plan"
            else:
                verdict = "wrong_plan" if expected_auto else "false_plan"
            outcome["verdict"] = verdict
            totals[verdict] += 1
            if expected_auto:
                for need in case.get("needs") or ["none"]:
                    by_need[need][verdict] += 1
        if len(keys) > 1:
            unstable.append(case["id"])
    planned = totals["correct_plan"] + totals["wrong_plan"] + totals["false_plan"]
    expected = totals["correct_plan"] + totals["wrong_plan"] + totals["missed"]
    return {
        "cases": len(cases),
        "runs": max((len(v) for v in runs.values()), default=0),
        "precision": round(totals["correct_plan"] / planned, 3) if planned else None,
        "recall": round(totals["correct_plan"] / expected, 3) if expected else None,
        "stability": round(1 - len(unstable) / len(cases), 3) if cases else None,
        "outcomes": dict(totals),
        "recall_by_need": {
            need: round(c["correct_plan"] / max(1, sum(c.values())), 3)
            for need, c in sorted(by_need.items())
        },
        "unstable": unstable,
    }


async def gold_main(args, rows: list[dict], planner, texts: dict[str, str]) -> None:
    cases = [
        json.loads(line)
        for line in args.gold.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {row["id"]: row for row in rows}
    missing = [case["id"] for case in cases if case["id"] not in by_id]
    cases = [case for case in cases if case["id"] in by_id]
    if missing:
        print(f"! not in NormGraph: {len(missing)}", file=sys.stderr)

    async def run_once(case: dict) -> dict:
        row = by_id[case["id"]]
        provenance = row.get("provenance") or {}
        context = PlanContext(
            clause_text=texts.get(provenance.get("clause_node_id") or "", ""),
            breadcrumb=provenance.get("breadcrumb"),
            document_name=provenance.get("name"),
            clause_number=provenance.get("numbering"),
        )
        try:
            plan, trace = await planner.plan_with_trace(
                row["id"], as_restriction(row), context
            )
        except Exception as exc:  # noqa: BLE001 - keep evaluating the rest
            return {"error": f"{type(exc).__name__}: {exc}"}
        dumped = plan.model_dump(mode="json")
        auto = plan.planner_status == "auto"
        return {
            "signature": plan_signature(dumped) if auto else None,
            "reasons": plan.params.get("blocked_reasons") or [],
            "rendering": render_plan(plan) if auto else None,
            "trace": trace,
        }

    runs: dict[str, list[dict]] = defaultdict(list)
    for attempt in range(args.repeats):
        outcomes = await asyncio.gather(*(run_once(case) for case in cases))
        for case, outcome in zip(cases, outcomes):
            runs[case["id"]].append(outcome)
        print(f"  run {attempt + 1}/{args.repeats} done", file=sys.stderr)

    summary = score_gold(cases, runs)
    with open(args.out / "gold_results.jsonl", "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(
                json.dumps({**case, "runs": runs[case["id"]]}, ensure_ascii=False)
                + "\n"
            )
    (args.out / "gold_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# CheckPlan gold evaluation", ""]
    for case in cases:
        verdicts = [o.get("verdict") or "error" for o in runs[case["id"]]]
        if all(v in {"correct_plan", "correct_reject"} for v in verdicts):
            continue
        lines += [
            f"## {case['document']} п. {case['clause']} — ожидается {case['expected']}",
            "",
            f"{case['note']}",
            "",
        ]
        for outcome, verdict in zip(runs[case["id"]], verdicts):
            detail = outcome.get("rendering") or ", ".join(outcome.get("reasons") or [])
            lines.append(f"- **{verdict}**: {detail or outcome.get('error')}")
        lines += ["", f"`{case['id']}`", ""]
    (args.out / "gold_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "unstable"}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
