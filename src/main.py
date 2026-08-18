import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse

from src.__version__ import VERSION
from src.common.auth import require_service_token
from src.common.middlewares import RequestLoggingMiddleware
from src.dependencies import get_dependencies, init_dependencies
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
        if deps.settings.reconcile_on_startup:
            app.state.reconcile_task = asyncio.create_task(deps.sync.reconcile())
        try:
            await deps.consumer.start()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a broker hiccup must not block startup
            log.warning("kafka_consumer_start_failed", error=str(exc))

        async with mcp_app.lifespan(app):
            try:
                yield
            finally:
                await deps.consumer.stop()
                await get_dependencies().aclose()


app = FastAPI(
    title="NormGraph — граф-RAG нормативных ограничений",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestLoggingMiddleware)
app.include_router(system_router, dependencies=[Depends(require_service_token)])
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
