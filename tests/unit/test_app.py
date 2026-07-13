"""Smoke test: the app boots (lifespan builds dependencies offline) and serves /ping."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.main import app


def test_ping_and_settings_masking():
    with TestClient(app) as client:
        assert client.get("/ping").json() == {"ping": "pong"}

        body = client.get("/system/settings").json()
        assert body["env_prefix"] == "NG_"
        # Password default is non-empty, so it must come back masked.
        assert body["settings"]["neo4j_password"] == "***"
