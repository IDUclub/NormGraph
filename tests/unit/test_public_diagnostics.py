from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.common.config import Settings
from src.main import app
from src.system_service import router


def test_diagnostics_are_public_and_health_stays_protected(tmp_path, monkeypatch):
    settings = Settings(log_dir=str(tmp_path), log_file="app.log")
    (tmp_path / "app.log").write_text("diagnostic log\n", encoding="utf-8")
    monkeypatch.setattr(
        router, "get_dependencies", lambda: SimpleNamespace(settings=settings)
    )
    client = TestClient(app)
    response = client.get("/system/settings")
    assert response.status_code == 200
    assert response.json()["settings"]["neo4j_password"] == "***"
    response = client.get("/system/logs")
    assert response.status_code == 200
    assert response.text == "diagnostic log\n"
    assert client.get("/system/health").status_code in {401, 403}
