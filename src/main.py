from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from src.__version__ import VERSION
from src.common.middlewares import RequestLoggingMiddleware
from src.dependencies import get_dependencies, init_dependencies
from src.graph.schema import ensure_schema
from src.ingestion.router import ingestion_router
from src.mcp_server.app import mcp_app
from src.pipeline.router import extraction_router
from src.query.router import query_router
from src.system_service import system_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps = init_dependencies()
    log = structlog.get_logger()
    log.info(f"Started server version {VERSION}")
    # Provision constraints + vector indexes. Tolerate an unreachable DB at boot so the
    # app still starts (health/settings stay available); schema is re-ensured on demand.
    try:
        await ensure_schema(deps.graph, deps.settings)
        await deps.kinds.ensure_seed()
    except Exception as exc:  # noqa: BLE001
        log.warning("graph_bootstrap_failed", error=str(exc))
    async with mcp_app.lifespan(app):
        try:
            yield
        finally:
            await get_dependencies().aclose()


app = FastAPI(
    title="NormGraph — граф-RAG нормативных ограничений",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestLoggingMiddleware)
app.include_router(system_router)
app.include_router(ingestion_router)
app.include_router(extraction_router)
app.include_router(query_router)
app.mount("/mcp", mcp_app)


@app.get("/")
async def read_root():
    return RedirectResponse("/docs")


@app.get("/ping")
async def ping_server():
    return {"ping": "pong"}
