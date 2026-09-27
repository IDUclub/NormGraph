"""System router: health, logs and effective-configuration endpoints.

Follows the workspace convention of exposing operational surfaces over HTTP: a readiness check
that actually pings Neo4j, the JSON log file for retrieval, and a masked read of the effective
``NG_`` configuration. Logs and settings are public; health requires a service token.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import BinaryIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.common.auth import require_service_token
from src.common.logger import log_file_path
from src.dependencies import get_dependencies

system_router = APIRouter(prefix="/system", tags=["system"])

# Setting fields that must never be returned in clear text.
_SENSITIVE = {"neo4j_password", "llm_api_key", "embeddings_api_key"}

_LOG_CHUNK = 1 << 20


@system_router.get("/health", dependencies=[Depends(require_service_token)])
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
    data = deps.settings.model_dump(mode="json")
    for key in _SENSITIVE:
        if data.get(key):
            data[key] = "***"
    return {"env_prefix": "NG_", "settings": data}


def _snapshot(handle: BinaryIO, size: int) -> Iterator[bytes]:
    """The first ``size`` bytes of the open log, however much is appended meanwhile."""
    with handle:
        remaining = size
        while remaining > 0:
            chunk = handle.read(min(_LOG_CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


@system_router.get("/logs")
async def get_logs() -> StreamingResponse:
    """Download the JSON application log file as it was when the request arrived.

    The service keeps writing while the file is sent, so a plain file response announced the old
    length and then streamed more, which the server aborted mid-download. The size is fixed up
    front and the handle is opened before streaming, so a rotation cannot swap the file either.
    """
    deps = get_dependencies()
    path = log_file_path(deps.settings)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="log file does not exist yet")
    size = os.fstat(handle.fileno()).st_size
    return StreamingResponse(
        _snapshot(handle, size),
        media_type="text/plain",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
    )
