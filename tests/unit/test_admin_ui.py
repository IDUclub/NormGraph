"""Hermetic browser/admin contract tests: auth, scoping, pagination and safe errors."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth import AccessToken
from neo4j.exceptions import ServiceUnavailable
from pydantic import SecretStr

from src.admin_service import auth, router
from src.admin_service.repository import AdminRepository
from src.common.config import Settings
from src.main import app
from src.pipeline.service import ExtractResult
from src.sync.service import SyncResult

HEADERS = {"X-NormGraph-Admin": "1"}


def token(role="ADMIN", expires=None):
    return AccessToken(
        token="admin-token",
        client_id="admin",
        scopes=[],
        expires_at=int(time.time()) + 300 if expires is None else expires,
        claims={"preferred_username": "operator", "realm_access": {"roles": [role]}},
    )


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_role", "ADMIN")
    monkeypatch.setattr(
        auth.token_verifier, "verify_token", AsyncMock(return_value=token())
    )
    graph = SimpleNamespace(run=AsyncMock(return_value=[]))
    deps = SimpleNamespace(
        graph=graph,
        writer=SimpleNamespace(stats=AsyncMock(return_value={"documents": 2})),
        consumer=SimpleNamespace(enabled=False),
        settings=Settings(_env_file=None),
        sync=SimpleNamespace(
            sync_document=AsyncMock(
                return_value=SyncResult(doc_id="d1", restrictions=3)
            ),
            sync_name=AsyncMock(return_value=[]),
        ),
        extraction=SimpleNamespace(
            extract_document=AsyncMock(return_value=ExtractResult(doc_id="d1"))
        ),
    )
    monkeypatch.setattr(router, "get_dependencies", lambda: deps)
    client = TestClient(app)
    client.cookies.set(
        auth.SESSION_COOKIE, "admin-token", domain="testserver.local", path="/admin/ui"
    )
    return client, deps


def test_anonymous_cannot_access_admin_api_or_page():
    client = TestClient(app)
    response = client.get("/admin/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"
    assert client.get("/admin/ui/api/documents").status_code == 401
    assert client.post("/admin/ui/api/sync", json={"target": "d1"}).status_code == 401
    assert client.get("/admin/ui/login").status_code == 200


def test_admin_page_and_assets_have_security_headers(admin):
    client, _ = admin
    response = client.get("/admin/ui")
    assert response.status_code == 200
    assert "NormGraph" in response.text
    assert "{{ version }}" not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get("/admin/ui/assets/admin.js").headers["x-content-type-options"]
        == "nosniff"
    )
    assert client.get("/admin/ui/assets/auth.py").status_code == 404


@pytest.mark.parametrize(
    "access,status", [(token("USER"), 403), (token(expires=1), 401), (None, 401)]
)
def test_invalid_expired_and_non_admin_sessions_are_rejected(
    admin, monkeypatch, access, status
):
    client, deps = admin
    monkeypatch.setattr(
        auth.token_verifier, "verify_token", AsyncMock(return_value=access)
    )
    assert client.get("/admin/ui/api/documents").status_code == status
    deps.graph.run.assert_not_awaited()


def test_login_cookie_logout_and_no_token_in_response(admin, monkeypatch):
    client, _ = admin
    issue = AsyncMock(return_value="issued-token")
    monkeypatch.setattr(router, "issue_token", issue)
    response = client.post(
        "/admin/ui/session",
        headers=HEADERS,
        json={"username": "operator", "password": "password"},
    )
    assert response.status_code == 200
    issue.assert_awaited_once_with("operator", "password")
    assert "issued-token" not in response.text
    cookie = response.headers["set-cookie"]
    assert (
        "HttpOnly" in cookie
        and "SameSite=strict" in cookie
        and "Path=/admin/ui" in cookie
    )
    assert client.post("/admin/ui/logout", headers=HEADERS).status_code == 200
    assert client.get("/admin/ui/api/documents").status_code == 401


def test_https_session_is_secure(admin, monkeypatch):
    monkeypatch.setattr(router, "issue_token", AsyncMock(return_value="issued-token"))
    response = TestClient(app, base_url="https://admin.test").post(
        "/admin/ui/session",
        headers=HEADERS,
        json={"username": "operator", "password": "password"},
    )
    assert "Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {**HEADERS, "Origin": "https://evil.test"},
        {**HEADERS, "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_login_and_mutations_are_rejected(admin, monkeypatch, headers):
    client, deps = admin
    issue = AsyncMock()
    monkeypatch.setattr(router, "issue_token", issue)
    assert (
        client.post(
            "/admin/ui/session",
            headers=headers,
            json={"username": "a", "password": "b"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/ui/api/sync", headers=headers, json={"target": "d1"}
        ).status_code
        == 403
    )
    issue.assert_not_awaited()
    deps.sync.sync_document.assert_not_awaited()


def test_admin_cookie_does_not_unlock_service_routes(admin):
    client, _ = admin
    assert client.post("/restrictions/search", json={"doc_id": "d1"}).status_code in {
        401,
        403,
    }


def test_document_listing_is_bounded_and_preserves_unknown_status(admin):
    client, deps = admin
    deps.graph.run.return_value = [
        {"document": {"doc_id": "d1", "state": "unknown", "restrictions": 5}},
        {"document": {"doc_id": "d2", "state": "complete", "restrictions": 0}},
    ]
    response = client.get("/admin/ui/api/documents?limit=1&query=СП")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "items": [{"doc_id": "d1", "state": "unknown", "restrictions": 5}],
        "has_more": True,
        "next_after": "d1",
    }
    assert deps.graph.run.call_args.kwargs["search"] == "сп"
    assert deps.graph.run.call_args.kwargs["limit"] == 2
    assert client.get("/admin/ui/api/documents?limit=100000").status_code == 422
    assert client.get("/admin/ui/api/documents?state=running").status_code == 422


def test_detail_not_found_and_graph_outage(admin):
    client, deps = admin
    assert client.get("/admin/ui/api/documents/missing").status_code == 404
    deps.graph.run.side_effect = ServiceUnavailable("private-host:7687")
    response = client.get("/admin/ui/api/documents")
    assert response.status_code == 503
    assert "private-host" not in response.text


def test_sync_preserves_document_scope_and_rejects_reassignment(admin):
    client, deps = admin
    deps.graph.run.return_value = [
        {"document": {"doc_id": "d1", "user_id": "u1", "scenario_id": "s1"}}
    ]
    response = client.post("/admin/ui/api/sync", headers=HEADERS, json={"target": "d1"})
    assert response.status_code == 200
    deps.sync.sync_document.assert_awaited_once_with(
        "d1", user_id="u1", scenario_id="s1", replace=False
    )
    response = client.post(
        "/admin/ui/api/sync",
        headers=HEADERS,
        json={"target": "d1", "user_id": "u2", "scenario_id": "s2"},
    )
    assert response.status_code == 409
    assert deps.sync.sync_document.await_count == 1


def test_sync_by_name_and_scope_validation(admin):
    client, deps = admin
    assert (
        client.post(
            "/admin/ui/api/sync",
            headers=HEADERS,
            json={"target": "Doc", "user_id": "u"},
        ).status_code
        == 422
    )
    response = client.post(
        "/admin/ui/api/sync",
        headers=HEADERS,
        json={"target": "Doc", "by": "name", "user_id": "u", "scenario_id": "s"},
    )
    assert response.status_code == 200
    deps.sync.sync_name.assert_awaited_once_with(
        "Doc", user_id="u", scenario_id="s", replace=False
    )


def test_sync_missing_document_and_partial_extraction(admin):
    client, deps = admin
    deps.sync.sync_document.return_value = SyncResult(
        doc_id="missing", skipped=True, reason="not found in DVD"
    )
    assert (
        client.post(
            "/admin/ui/api/sync", headers=HEADERS, json={"target": "missing"}
        ).status_code
        == 404
    )
    deps.graph.run.return_value = [{"document": {"doc_id": "d1", "clauses": 5}}]
    deps.extraction.extract_document.return_value = ExtractResult(
        doc_id="d1", incomplete=True, failed_clause_ids=["c1"]
    )
    result = client.post("/admin/ui/api/documents/d1/extract", headers=HEADERS, json={})
    assert result.json()["incomplete"] is True
    assert result.json()["failed_clause_ids"] == ["c1"]
    deps.extraction.extract_document.assert_awaited_once_with("d1", replace=False)


def test_settings_never_expose_secrets(admin, monkeypatch):
    client, deps = admin
    deps.settings.auth_helper_api_key = SecretStr("auth-helper-secret")
    deps.settings.llm_api_key = "llm-secret"
    response = client.get("/admin/ui/api/settings")
    assert response.status_code == 200
    assert (
        "auth-helper-secret" not in response.text and "llm-secret" not in response.text
    )
    from src.system_service import router as system_router

    monkeypatch.setattr(system_router, "get_dependencies", lambda: deps)
    public = client.get("/system/settings")
    assert public.status_code == 200
    assert "auth-helper-secret" not in public.text
    assert public.json()["settings"]["auth_helper_api_key"] == "**********"


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {}, 401),
        (503, {}, 502),
        (200, [], 502),
        (200, {"access_token": "token"}, None),
    ],
)
async def test_auth_helper_contract(monkeypatch, respx_mock, status, body, expected):
    monkeypatch.setattr(auth.settings, "auth_helper_url", "https://helper.test")
    monkeypatch.setattr(
        auth.settings, "auth_helper_api_key", SecretStr("helper-secret")
    )
    route = respx_mock.post("https://helper.test/api/token").mock(
        return_value=httpx.Response(status, json=body)
    )
    if expected:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await auth.issue_token("user", "password")
        assert exc.value.status_code == expected
    else:
        assert await auth.issue_token("user", "password") == "token"
    assert route.calls[0].request.headers["X-Auth-Helper-Api-Key"] == "helper-secret"


async def test_auth_helper_unconfigured(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(auth.settings, "auth_helper_url", None)
    with pytest.raises(HTTPException) as exc:
        await auth.issue_token("user", "password")
    assert exc.value.status_code == 503


async def test_child_pagination_never_includes_embeddings():
    graph = SimpleNamespace(
        run=AsyncMock(
            return_value=[{"clause": {"node_id": "c1"}}, {"clause": {"node_id": "c2"}}]
        )
    )
    result = await AdminRepository(graph).clauses("doc", after="c0", limit=1)
    assert result == {
        "items": [{"node_id": "c1"}],
        "has_more": True,
        "next_after": "c1",
    }
    assert graph.run.call_args.kwargs == {"doc_id": "doc", "after": "c0", "limit": 2}
    assert "embedding" not in graph.run.call_args.args[0]
