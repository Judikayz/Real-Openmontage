"""The Worker deployed WITHOUT any file storage (no R2/KV): everything except files must work, and failures must be honest."""
import json, os, subprocess, sys, time
from pathlib import Path
import httpx, pytest
from tests.orchestrator import test_e2e as e2e

ROOT = Path(__file__).resolve().parents[2]; WDIR = ROOT / "worker"
pytestmark = pytest.mark.skipif(not (WDIR / "node_modules" / ".bin" / "wrangler").exists(), reason="worker dependencies not installed (cd worker && npm ci)")


@pytest.fixture(scope="module")
def nostore(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("nostore"); port = e2e.free_port()
    cfg = json.loads((WDIR / "wrangler.jsonc").read_text()); cfg.pop("r2_buckets", None)  # exactly what the deploy workflow does when use_r2 is off
    (WDIR / ".wrangler-nostorage.jsonc").write_text(json.dumps(cfg))
    p, db_exec = e2e._start_worker(tmp, port, config=".wrangler-nostorage.jsonc")
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(base + "/api/v1/me", headers={"Authorization": f"Bearer {e2e.TOKEN}"}).status_code == 200: break
        except httpx.HTTPError: pass
        time.sleep(0.5)
    else: p.kill(); pytest.fail("worker did not start")
    c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {e2e.TOKEN}"}, timeout=60)
    yield c, tmp, base
    import signal
    try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception: p.terminate()
    p.wait(15)
    (WDIR / ".wrangler-nostorage.jsonc").unlink(missing_ok=True)


def test_status_honestly_reports_missing_storage(nostore):
    c, tmp, base = nostore; s = c.get("/api/v1/settings/status").json()
    assert s["storage"]["backend"] == "none" and s["storage"]["ok"] is False and "not configured" in s["storage"]["error"]
    assert s["database"]["ok"] is True and s["database"]["engine"] == "cloudflare-d1"


def test_uploads_are_refused_with_a_clear_message_not_a_crash(nostore):
    c, tmp, base = nostore; pid = c.post("/api/v1/projects", json={"name": "x"}).json()["id"]
    r = c.post(f"/api/v1/projects/{pid}/uploads", json={"name": "a.mp4", "size": 10})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_chat_job_dispatch_and_completion_work_without_storage(nostore):
    c, tmp, base = nostore
    p = c.post("/api/v1/projects", json={"name": "chat only"}).json(); conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    j = e2e.wait_job(c, e2e.say(c, conv["id"], "hello there")["id"])
    assert j["status"] == "completed", j
    msgs = c.get(f"/api/v1/conversations/{conv['id']}/messages").json(); assert msgs[-1]["role"] == "assistant" and "Plan:" in msgs[-1]["content"]
    warnings = [e["data"]["msg"] for e in c.get(f"/api/v1/jobs/{j['id']}/events.json").json() if e["type"] == "warning"]
    assert any("checkpoint failed" in w for w in warnings)  # snapshot could not be stored: reported, not hidden


def test_a_render_that_cannot_be_saved_is_reported_loudly(nostore):
    c, tmp, base = nostore
    p = c.post("/api/v1/projects", json={"name": "render"}).json(); conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    j = e2e.wait_job(c, e2e.say(c, conv["id"], "render a test video")["id"])
    assert j["status"] == "completed", j
    last = c.get(f"/api/v1/conversations/{conv['id']}/messages").json()[-1]["content"]
    assert "WARNING" in last and "final.mp4" in last and "storage is not configured" in last


def test_smoke_script_passes_against_a_storageless_worker(nostore):
    c, tmp, base = nostore
    r = subprocess.run([sys.executable, str(ROOT / "scripts/ci/smoke_deployed.py"), "--timeout", "120"], env={**os.environ, "BASE_URL": base, "ORCH_ADMIN_TOKEN": e2e.TOKEN}, capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL CHECKS PASSED" in r.stdout and "job completed on a runner" in r.stdout and e2e.TOKEN not in r.stdout
