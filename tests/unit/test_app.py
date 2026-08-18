"""Smoke test: the app boots (lifespan builds dependencies offline) and serves /ping."""

from __future__ import annotations

from fastapi.testclient import TestClient
from fastmcp.server.auth import AccessToken
from idu_service_auth import KeycloakTokenClient

from src.common.auth import service_token_verifier
from src.main import app


def test_ping_and_settings_masking(monkeypatch):
    async def fake_access_token(_self):
        return "service-token"

    async def fake_verify_token(_token):
        return AccessToken(
            token="service-token",
            client_id="service",
            scopes=[],
            claims={"preferred_username": "service-account-test"},
        )

    monkeypatch.setattr(KeycloakTokenClient, "get_access_token", fake_access_token)
    monkeypatch.setattr(service_token_verifier, "verify_token", fake_verify_token)
    with TestClient(app) as client:
        assert client.get("/ping").json() == {"ping": "pong"}

        body = client.get(
            "/system/settings",
            headers={"Authorization": "Bearer service-token"},
        ).json()
        assert body["env_prefix"] == "NG_"
        # Password default is non-empty, so it must come back masked.
        assert body["settings"]["neo4j_password"] == "***"
