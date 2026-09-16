from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.pipeline.check_plan_backfill import CheckPlanRevisionConflict
from src.query import router


@pytest.mark.parametrize(
    "outcome,status", [(None, 404), (CheckPlanRevisionConflict("stale"), 409)]
)
async def test_regenerate_http_errors(monkeypatch, outcome, status):
    regenerate = (
        AsyncMock(side_effect=outcome)
        if isinstance(outcome, Exception)
        else AsyncMock(return_value=outcome)
    )
    monkeypatch.setattr(
        router,
        "get_dependencies",
        lambda: SimpleNamespace(
            check_plan_backfill=SimpleNamespace(regenerate=regenerate)
        ),
    )
    app = FastAPI()
    app.include_router(router.query_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/check-plans/r1/regenerate", json={"expected_revision": 1}
        )
    assert response.status_code == status
    args = regenerate.call_args.args
    assert args[0] == "r1"
    assert args[1].dry_run is True


@pytest.mark.parametrize(
    "body", [{}, {"expected_revision": -1}, {"expected_revision": 1, "force": True}]
)
async def test_regenerate_rejects_unsafe_request_shape(body):
    app = FastAPI()
    app.include_router(router.query_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/check-plans/r1/regenerate", json=body)
    assert response.status_code == 422
