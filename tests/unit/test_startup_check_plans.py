"""Startup backfill must not hold readiness or outlive its dependencies."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

import src.main as main


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_startup_backfill_is_optional_and_cancelled_before_dependencies_close(
    monkeypatch, enabled
):
    started = asyncio.Event()
    stopped = asyncio.Event()
    reembed_started = asyncio.Event()
    reembed_stopped = asyncio.Event()

    def blocking(started_event, stopped_event):
        async def run():
            started_event.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped_event.set()

        return run

    backfill = blocking(started, stopped)

    async def close_dependencies():
        assert stopped.is_set() == enabled
        assert reembed_stopped.is_set() == enabled

    @asynccontextmanager
    async def mcp_lifespan(_app):
        yield

    auth = AsyncMock()
    deps = SimpleNamespace(
        settings=SimpleNamespace(
            reconcile_on_startup=False,
            check_plan_backfill_on_startup=enabled,
            restriction_reembed_on_startup=enabled,
        ),
        service_auth=auth,
        graph=object(),
        kinds=SimpleNamespace(ensure_seed=AsyncMock()),
        writer=SimpleNamespace(
            backfill_check_plan_layer_entities=AsyncMock(return_value=0)
        ),
        consumer=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        check_plan_backfill=SimpleNamespace(
            run_on_startup=AsyncMock(side_effect=backfill)
        ),
        restriction_reembed=SimpleNamespace(
            run_on_startup=AsyncMock(
                side_effect=blocking(reembed_started, reembed_stopped)
            )
        ),
        aclose=AsyncMock(side_effect=close_dependencies),
    )
    monkeypatch.setattr(main, "init_dependencies", lambda: deps)
    monkeypatch.setattr(main, "ensure_schema", AsyncMock())
    monkeypatch.setattr(main, "mcp_app", SimpleNamespace(lifespan=mcp_lifespan))
    app = FastAPI()

    async with main.lifespan(app):
        if enabled:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(reembed_started.wait(), timeout=1)
            assert not app.state.check_plan_backfill_task.done()
            assert not app.state.restriction_reembed_task.done()
        else:
            deps.check_plan_backfill.run_on_startup.assert_not_called()
            deps.restriction_reembed.run_on_startup.assert_not_called()

    if enabled:
        assert app.state.check_plan_backfill_task.cancelled()
        assert app.state.restriction_reembed_task.cancelled()
    deps.aclose.assert_awaited_once()
    deps.consumer.stop.assert_awaited_once()
    deps.writer.backfill_check_plan_layer_entities.assert_awaited_once()
