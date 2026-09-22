"""Admin pages and role-protected API, following IDU_DVD's /admin/ui convention."""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel, Field, SecretStr, model_validator

from src.__version__ import VERSION
from src.admin_service.auth import (
    SESSION_COOKIE,
    issue_token,
    require_admin,
    require_same_origin,
    verify_admin_token,
)
from src.admin_service.repository import AdminRepository
from src.common.logger import log_file_path
from src.dependencies import get_dependencies

router = APIRouter(prefix="/admin/ui", tags=["admin"], include_in_schema=False)
_ROOT = Path(__file__).resolve().parent
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)
log = structlog.get_logger(__name__)


def html_page(name: str) -> HTMLResponse:
    return HTMLResponse(
        (_ROOT / "templates" / name)
        .read_text(encoding="utf-8")
        .replace("{{ version }}", html.escape(VERSION)),
        headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store"},
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr


class SyncRequest(BaseModel):
    target: str = Field(min_length=1, max_length=512)
    by: Literal["id", "name"] = "id"
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    scenario_id: str | None = Field(default=None, min_length=1, max_length=256)
    replace: bool = False

    @model_validator(mode="after")
    def validate_scope(self):
        if bool(self.user_id) != bool(self.scenario_id):
            raise ValueError("user_id and scenario_id must be supplied together")
        if not self.target.strip():
            raise ValueError("target must not be blank")
        return self


class ExtractRequest(BaseModel):
    replace: bool = False


@router.get("/login")
async def login_page():
    return html_page("login.html")


@router.post("/session", dependencies=[Depends(require_same_origin)])
async def login(body: LoginRequest, request: Request):
    token = await issue_token(body.username, body.password.get_secret_value())
    access = await verify_admin_token(token)
    seconds = max(1, int(access.expires_at - time.time()))
    response = JSONResponse(
        {"expires_in": seconds}, headers={"Cache-Control": "no-store"}
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=seconds,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/admin/ui",
    )
    return response


@router.post("/logout", dependencies=[Depends(require_same_origin)])
async def logout():
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(SESSION_COOKIE, path="/admin/ui")
    return response


@router.get("/assets/{filename}")
async def asset(filename: str):
    allowed = {"admin.css": "text/css", "admin.js": "application/javascript"}
    if filename not in allowed:
        raise HTTPException(404)
    return FileResponse(
        _ROOT / "static" / filename,
        media_type=allowed[filename],
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get("")
@router.get("/")
async def admin_ui(request: Request):
    try:
        await require_admin(request)
    except HTTPException:
        return RedirectResponse("/admin/ui/login", status_code=303)
    return html_page("admin.html")


async def dependencies():
    """Map infrastructure failures to safe, useful admin errors."""
    try:
        yield get_dependencies()
    except (Neo4jError, ServiceUnavailable) as exc:
        log.warning("admin_graph_unavailable", error_type=type(exc).__name__)
        raise HTTPException(
            503, "Граф недоступен. Проверьте подключение к Neo4j"
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("admin_upstream_unavailable", error_type=type(exc).__name__)
        raise HTTPException(
            502, "Ошибка внешнего сервиса. Подробности в логах"
        ) from exc


async def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


api = APIRouter(prefix="/api", dependencies=[Depends(require_admin), Depends(no_store)])


@api.get("/overview")
async def overview(deps=Depends(dependencies)):
    return {
        "stats": await deps.writer.stats(),
        "kafka_enabled": deps.consumer.enabled,
        "reconcile_on_startup": deps.settings.reconcile_on_startup,
        "version": VERSION,
    }


@api.get("/documents")
async def documents(
    query: str = Query(default="", max_length=512),
    state: Literal["", "complete", "incomplete", "unknown", "no_clauses"] = "",
    after: str = Query(default="", max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    deps=Depends(dependencies),
):
    return await AdminRepository(deps.graph).documents(query, state, after, limit)


async def stored_document(doc_id: str, deps) -> dict:
    document = await AdminRepository(deps.graph).document(doc_id)
    if document is None:
        raise HTTPException(404, "Документ не найден в NormGraph")
    return document


@api.get("/documents/{doc_id}")
async def document(doc_id: str, deps=Depends(dependencies)):
    return await stored_document(doc_id, deps)


@api.get("/documents/{doc_id}/{collection}")
async def document_items(
    doc_id: str,
    collection: Literal["clauses", "restrictions"],
    after: str = Query(default="", max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    deps=Depends(dependencies),
):
    await stored_document(doc_id, deps)
    repo = AdminRepository(deps.graph)
    method = repo.clauses if collection == "clauses" else repo.restrictions
    return await method(doc_id, after, limit)


@api.post("/sync")
async def sync(body: SyncRequest, deps=Depends(dependencies)):
    kwargs = {
        "user_id": body.user_id,
        "scenario_id": body.scenario_id,
        "replace": body.replace,
    }
    if body.by == "name":
        return await deps.sync.sync_name(body.target.strip(), **kwargs)
    # Preserve ownership when re-syncing a document already in a user's index.
    previous = await AdminRepository(deps.graph).document(body.target.strip())
    if previous:
        for key in ("user_id", "scenario_id"):
            if kwargs[key] is not None and kwargs[key] != previous.get(key):
                raise HTTPException(409, "Документ уже загружен в другой контекст")
            kwargs[key] = previous.get(key)
    result = await deps.sync.sync_document(body.target.strip(), **kwargs)
    if result.skipped and result.reason == "not found in DVD":
        raise HTTPException(404, "Документ не найден в IDU_DVD")
    return result


@api.post("/documents/{doc_id}/extract")
async def extract(doc_id: str, body: ExtractRequest, deps=Depends(dependencies)):
    await stored_document(doc_id, deps)
    return await deps.extraction.extract_document(doc_id, replace=body.replace)


@api.get("/settings")
async def configuration(deps=Depends(dependencies)):
    # An allowlist keeps service credentials and future secrets out of this surface.
    fields = (
        "dvd_base_url",
        "llm_provider",
        "llm_model",
        "embeddings_model",
        "vector_size",
        "extract_concurrency",
        "extraction_passes",
        "entity_merge_threshold",
        "kind_match_threshold",
        "kafka_topic",
        "kafka_group_id",
        "reconcile_on_startup",
        "check_plan_backfill_on_startup",
    )
    return {key: getattr(deps.settings, key) for key in fields}


@api.get("/logs")
async def logs(deps=Depends(dependencies)):
    path = log_file_path(deps.settings)
    if not path.is_file():
        raise HTTPException(404, "Файл логов пока не создан")
    return FileResponse(
        path,
        media_type="text/plain",
        filename="normgraph.log",
        headers={"Cache-Control": "no-store"},
    )


router.include_router(api)
