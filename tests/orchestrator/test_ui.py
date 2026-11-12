import shutil, subprocess
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def backlot(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE", "1"); monkeypatch.setenv("ORCH_DB_PATH", str(tmp_path / "u.db"))
    monkeypatch.setenv("ORCH_LOCAL_STORAGE_DIR", str(tmp_path / "o")); monkeypatch.setenv("ORCH_ADMIN_TOKEN", "t")
    monkeypatch.setenv("ORCH_RUNNER_SECRET", "r"); monkeypatch.setenv("ORCH_DISPATCH_MODE", "none")
    from backlot.server import create_app
    return TestClient(create_app())


def test_app_page_and_assets_are_served_inside_backlot(backlot):
    r = backlot.get("/app"); assert r.status_code == 200 and "Backlot" in r.text and 'id="composer"' in r.text
    for path in ("/ui/app.js", "/ui/app.css", "/ui/board.css"): assert backlot.get(path).status_code == 200, path


def test_original_backlot_board_still_works_with_orchestrator_mounted(backlot):
    assert backlot.get("/").status_code == 200


def test_api_is_protected_in_backlot(backlot):
    assert backlot.get("/api/v1/projects").status_code == 401
    assert backlot.post("/api/v1/auth/login", json={"token": "t"}).status_code == 200
    assert backlot.get("/api/v1/projects").status_code == 200


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_app_js_parses():
    assert subprocess.run(["node", "--check", "backlot/ui/app.js"], capture_output=True).returncode == 0
