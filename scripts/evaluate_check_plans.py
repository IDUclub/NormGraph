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
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    service_auth = build_service_auth(settings)
    auth = ServiceTokenAuth(service_auth)
    rows = await fetch_restrictions(args.normgraph_url, auth, args.limit)
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
        min_distance_m=settings.check_plan_min_distance_m,
        llm_concurrency=settings.check_plan_llm_concurrency,
    )

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


if __name__ == "__main__":
    asyncio.run(main())
