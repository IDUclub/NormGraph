import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse

from src.__version__ import VERSION
from src.admin_service.router import router as admin_router
from src.common.auth import require_service_token
from src.common.middlewares import RequestLoggingMiddleware
from src.dependencies import init_dependencies
from src.graph.schema import VectorIndexDimensionMismatch, ensure_schema
from src.ingestion.router import ingestion_router
from src.mcp_server.app import mcp_app
from src.pipeline.router import extraction_router
from src.query.router import query_router
from src.sync.router import sync_router
from src.system_service import system_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps = init_dependencies()
    log = structlog.get_logger()
    log.info(f"Started server version {VERSION}")
    async with deps.service_auth:
        await deps.service_auth.get_access_token()
        # Provision constraints + vector indexes. Tolerate an unreachable DB at boot so the
        # app still starts (health/settings stay available); schema is re-ensured on demand.
        try:
            await ensure_schema(deps.graph, deps.settings)
            await deps.kinds.ensure_seed()
        except VectorIndexDimensionMismatch:
            # Serving traffic with an incompatible persisted vector space only defers the
            # failure until the first search. Fail startup with the actionable schema error.
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("graph_bootstrap_failed", error=str(exc))

        # Startup catch-up runs in the background so readiness is not blocked by a slow reconcile
        # (it hits IDU_DVD + the LLM); the Kafka consumer then keeps the graph current.
        startup_tasks = []
        if deps.settings.reconcile_on_startup:
            app.state.reconcile_task = asyncio.create_task(deps.sync.reconcile())
            startup_tasks.append(app.state.reconcile_task)
        if deps.settings.check_plan_backfill_on_startup:
            app.state.check_plan_backfill_task = asyncio.create_task(
                deps.check_plan_backfill.run_on_startup()
            )
            startup_tasks.append(app.state.check_plan_backfill_task)
        try:
            try:
                await deps.consumer.start()
            except (
                Exception
            ) as exc:  # noqa: BLE001 — a broker hiccup must not block startup
                log.warning("kafka_consumer_start_failed", error=str(exc))

            async with mcp_app.lifespan(app):
                yield
        finally:
            for task in startup_tasks:
                task.cancel()
            await asyncio.gather(*startup_tasks, return_exceptions=True)
            try:
                await deps.consumer.stop()
            finally:
                await deps.aclose()


app = FastAPI(
    title="NormGraph — граф-RAG нормативных ограничений",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestLoggingMiddleware)
app.include_router(admin_router)
app.include_router(system_router)
app.include_router(ingestion_router, dependencies=[Depends(require_service_token)])
app.include_router(extraction_router, dependencies=[Depends(require_service_token)])
app.include_router(query_router, dependencies=[Depends(require_service_token)])
app.include_router(sync_router, dependencies=[Depends(require_service_token)])
app.mount("/mcp", mcp_app)


@app.get("/")
async def read_root():
    return RedirectResponse("/docs")


@app.get("/ping")
async def ping_server():
    return {"ping": "pong"}
