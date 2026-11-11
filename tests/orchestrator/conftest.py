import pytest
from fastapi.testclient import TestClient
from orchestrator.app import create_app
from orchestrator.config import Config

TOKEN = "owner-token-test"


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db_path = str(tmp_path / "t.db"); c.admin_token = TOKEN; c.runner_secret = "runner-secret-test"
    c.session_secret = "sess-secret-test"; c.local_storage_dir = str(tmp_path / "obj"); c.dispatch_mode = "none"
    c.max_upload_bytes = 10_000_000
    return c


@pytest.fixture
def client(cfg):
    return TestClient(create_app(cfg), headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def anon(cfg):
    return TestClient(create_app(cfg))
