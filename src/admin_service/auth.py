"""IDU login and role-checked, short-lived browser sessions.

Admin cookies are accepted only by the admin API. Existing service APIs keep their
service-account authentication contract.
"""

from __future__ import annotations

import time

import httpx
from fastapi import HTTPException, Request
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier

from src.common.config import settings

SESSION_COOKIE = "normgraph_admin_session"
_issuer = (
    f"{settings.service_auth_server_url.rstrip('/')}/realms/"
    f"{settings.service_auth_realm}"
)
token_verifier = JWTVerifier(
    jwks_uri=f"{_issuer}/protocol/openid-connect/certs",
    issuer=_issuer,
    algorithm="RS256",
)


def require_same_origin(request: Request) -> None:
    """A custom header blocks cross-origin form posts, including login CSRF."""
    origin = request.headers.get("origin")
    if (
        request.headers.get("X-NormGraph-Admin") != "1"
        or request.headers.get("sec-fetch-site") == "cross-site"
        or (origin and origin != str(request.base_url).rstrip("/"))
    ):
        raise HTTPException(403, "Запрос должен исходить из панели управления")


async def verify_admin_token(token: str) -> AccessToken:
    try:
        access = await token_verifier.verify_token(token)
    except Exception as exc:
        raise HTTPException(401, "Сессия недействительна. Войдите снова") from exc
    if access is None or access.expires_at is None or access.expires_at <= time.time():
        raise HTTPException(401, "Сессия истекла. Войдите снова")
    realm = access.claims.get("realm_access")
    roles = realm.get("roles") if isinstance(realm, dict) else None
    if not isinstance(roles, list) or settings.admin_role not in roles:
        raise HTTPException(403, "У этой учётной записи нет прав администратора")
    return access


async def require_admin(request: Request) -> AccessToken:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Войдите в панель управления")
    if request.method not in {"GET", "HEAD"}:
        require_same_origin(request)
    return await verify_admin_token(token)


async def issue_token(username: str, password: str) -> str:
    if not settings.auth_helper_url or not settings.auth_helper_api_key:
        raise HTTPException(
            503, "Вход не настроен: задайте NG_AUTH_HELPER_URL и NG_AUTH_HELPER_API_KEY"
        )
    try:
        async with httpx.AsyncClient(timeout=settings.auth_helper_timeout) as client:
            response = await client.post(
                f"{settings.auth_helper_url.rstrip('/')}/api/token",
                headers={
                    "X-Auth-Helper-Api-Key": settings.auth_helper_api_key.get_secret_value()
                },
                json={
                    "username": username,
                    "password": password,
                    "scope": "openid profile email",
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Сервис авторизации недоступен") from exc
    if response.status_code in {400, 401, 403}:
        raise HTTPException(401, "Неверный логин или пароль")
    if response.status_code != 200:
        raise HTTPException(502, "Сервис авторизации недоступен")
    try:
        body = response.json()
        token = body.get("access_token") if isinstance(body, dict) else None
    except ValueError as exc:
        raise HTTPException(502, "Некорректный ответ сервиса авторизации") from exc
    if not isinstance(token, str) or not token:
        raise HTTPException(502, "Сервис авторизации не вернул токен")
    return token
