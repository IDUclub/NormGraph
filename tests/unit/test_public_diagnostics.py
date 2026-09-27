from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.common.config import Settings
from src.main import app
from src.system_service import router


def test_diagnostics_are_public_and_health_stays_protected(tmp_path, monkeypatch):
    settings = Settings(log_dir=str(tmp_path), log_file="app.log")
    (tmp_path / "app.log").write_bytes(b"diagnostic log\n")
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
    assert response.headers["content-length"] == "15"
    assert client.get("/system/health").status_code in {401, 403}


def test_log_download_is_a_snapshot_of_a_growing_file(tmp_path):
    log = tmp_path / "app.log"
    log.write_bytes(b"a" * 10)
    handle = log.open("rb")
    chunks = router._snapshot(handle, 10)
    with log.open("ab") as writer:  # the service keeps logging during the download
        writer.write(b"b" * 5)
    assert b"".join(chunks) == b"a" * 10
    assert handle.closed


def test_missing_log_file_is_404(tmp_path, monkeypatch):
    settings = Settings(log_dir=str(tmp_path), log_file="absent.log")
    monkeypatch.setattr(
        router, "get_dependencies", lambda: SimpleNamespace(settings=settings)
    )
    assert TestClient(app).get("/system/logs").status_code == 404
