"""System router: health, logs and effective-configuration endpoints.

Follows the workspace convention of exposing operational surfaces over HTTP: a readiness check
that actually pings Neo4j, the JSON log file for retrieval, and a masked read of the effective
``NG_`` configuration. All endpoints are unauthenticated — keep the service on a trusted network.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from src.common.logger import log_file_path
from src.dependencies import get_dependencies

system_router = APIRouter(prefix="/system", tags=["system"])

# Setting fields that must never be returned in clear text.
_SENSITIVE = {"neo4j_password", "llm_api_key", "embeddings_api_key"}


@system_router.get("/health")
async def health() -> dict:
    """Readiness: reports whether the graph store is reachable."""
    deps = get_dependencies()
    graph_ok = True
    try:
        await deps.graph.verify_connectivity()
    except Exception:
        graph_ok = False
    status = "ok" if graph_ok else "degraded"
    return {"status": status, "graph": "up" if graph_ok else "down"}


@system_router.get("/settings")
async def read_settings() -> dict:
    """Current effective ``NG_`` configuration; secrets are masked."""
    deps = get_dependencies()
    data = deps.settings.model_dump()
    for key in _SENSITIVE:
        if data.get(key):
            data[key] = "***"
    return {"env_prefix": "NG_", "settings": data}


@system_router.get("/logs")
async def get_logs() -> FileResponse:
    """Download the JSON application log file."""
    deps = get_dependencies()
    path = log_file_path(deps.settings)
    if not path.exists():
        raise HTTPException(status_code=404, detail="log file does not exist yet")
    return FileResponse(path, media_type="text/plain", filename=path.name)
