"""End-to-end: real HTTP server + real runner subprocesses (fresh empty workspace per job).
OpenCode is the stand-in in fake_opencode.py (no LLM). Everything else is real: HTTP, SQLite, signed
uploads/downloads, tar state snapshots, ffmpeg/ffprobe QC, session export/import, recovery."""
import json, os, shutil, signal, socket, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
import httpx, pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "e2e-owner"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


DB_EXEC = None  # set by the srv fixture: runs a SQL statement against whichever backend is under test
BACKEND = os.environ.get("E2E_BACKEND", "python")
RUNNER_ENV = {"HEARTBEAT_SECONDS": "1", "OPENCODE_TIMEOUT_SECONDS": "25"}


def _start_python(tmp, port):
    env = {**os.environ, "ORCH_DB_PATH": str(tmp / "db.sqlite"), "ORCH_ADMIN_TOKEN": TOKEN, "ORCH_RUNNER_SECRET": "e2e-runner",
           "ORCH_LOCAL_STORAGE_DIR": str(tmp / "obj"), "ORCH_PUBLIC_URL": f"http://127.0.0.1:{port}", "ORCH_DISPATCH_MODE": "local",
           "OPENCODE_BIN": f"{sys.executable} {ROOT}/tests/orchestrator/fake_opencode.py", "FAKE_OC_HOME": str(tmp / "ochome"),
           "FAKE_OC_LOG": str(tmp / "oc.log"), "PYTHONPATH": str(ROOT), **RUNNER_ENV}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "orchestrator.app:create_app", "--factory", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, start_new_session=True)  # own session: teardown kills only the server, never pytest
    def db_exec(sql):
        d = sqlite3.connect(tmp / "db.sqlite"); d.execute(sql); d.commit(); d.close()
    return p, db_exec


def _start_worker(tmp, port):
    """Real Cloudflare runtime (workerd) with local D1 + R2. GitHub dispatch is replaced by a webhook that launches real runner subprocesses."""
    import http.server, threading
    wdir = ROOT / "worker"; persist = str(tmp / "wstate")
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ws = tempfile.mkdtemp(prefix="om-runner-")
            env = {**os.environ, "JOB_ID": d["job_id"], "BACKEND_URL": d["backend_url"], "ORCH_RUNNER_SECRET": "e2e-runner", "OM_REPO_ROOT": ws, "PYTHONPATH": str(ROOT),
                   "OPENCODE_BIN": f"{sys.executable} {ROOT}/tests/orchestrator/fake_opencode.py", "FAKE_OC_HOME": str(tmp / "ochome"), "FAKE_OC_LOG": str(tmp / "oc.log"), **RUNNER_ENV}
            pr = subprocess.Popen([sys.executable, "-m", "runner.run_job"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            body = json.dumps({"pid": pr.pid, "workspace": ws}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=hs.serve_forever, daemon=True).start()
    (wdir / ".dev.vars").write_text(f"ORCH_ADMIN_TOKEN={TOKEN}\nORCH_RUNNER_SECRET=e2e-runner\nSESSION_SECRET=e2e-session\nDISPATCH_MODE=webhook\nDISPATCH_WEBHOOK_URL=http://127.0.0.1:{hs.server_address[1]}/launch\n")
    subprocess.run(["npx", "wrangler", "d1", "migrations", "apply", "DB", "--local", "--persist-to", persist], cwd=wdir, check=True, capture_output=True)
    p = subprocess.Popen(["npx", "wrangler", "dev", "--local", "--port", str(port), "--persist-to", persist, "--var", "MAX_CONCURRENT_JOBS:2"], cwd=wdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)  # test uses a small limit to exercise queueing
    def db_exec(sql):
        subprocess.run(["npx", "wrangler", "d1", "execute", "DB", "--local", "--persist-to", persist, "--command", sql], cwd=wdir, check=True, capture_output=True)
    return p, db_exec


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    global DB_EXEC
    tmp = tmp_path_factory.mktemp("e2e"); port = free_port()
    p, DB_EXEC = (_start_worker if BACKEND == "worker" else _start_python)(tmp, port)
    base = f"http://127.0.0.1:{port}"
    for _ in range(90):
        try:
            if httpx.get(base + "/api/v1/me", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200: break
        except httpx.HTTPError: pass
        time.sleep(0.5)
    else: p.kill(); pytest.fail("server did not start")
    c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
    yield c, tmp
    try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception: p.terminate()
    p.wait(15)


def test_0_the_backend_under_test_is_the_one_requested(srv):
    """Guard: a mis-wired fixture must never again silently test the wrong backend."""
    c, tmp = srv
    engine = c.get("/api/v1/settings/status").json()["database"]["engine"]
    assert engine == ("cloudflare-d1" if BACKEND == "worker" else "sqlite"), f"E2E_BACKEND={BACKEND} but the server reports {engine}"


def wait_job(c, jid, timeout=90, until=("completed", "failed", "cancelled")):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] in until: return j
        time.sleep(0.5)
    ev = c.get(f"/api/v1/jobs/{jid}/events.json").json()[-8:]
    pytest.fail(f"job {jid} stuck in {j['status']} (cancel_requested={j.get('cancel_requested')}); last events: {[(e['type'], e['data']) for e in ev]}")


def say(c, cid, text, assets=()):
    r = c.post(f"/api/v1/conversations/{cid}/messages", json={"content": text, "asset_ids": list(assets)}); r.raise_for_status(); return r.json()["job"]


def oclog(tmp): return [json.loads(l) for l in (tmp / "oc.log").read_text().splitlines()]


@pytest.fixture(scope="module")
def world(srv, tmp_path_factory):
    c, tmp = srv
    p = c.post("/api/v1/projects", json={"name": "E2E"}).json()
    conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    return c, tmp, p, conv


def test_1_upload_video_then_render_in_fresh_runner(world):
    c, tmp, p, conv = world
    src = tmp / "avatar.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
                    "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    up = c.post(f"/api/v1/projects/{p['id']}/uploads", json={"name": "avatar.mp4", "size": src.stat().st_size, "mime": "video/mp4"}).json()
    assert httpx.put(up["put_url"], content=src.read_bytes()).status_code == 200  # direct-to-storage, not via chat payload
    assert c.post(f"/api/v1/assets/{up['asset_id']}/complete").json()["status"] == "uploaded"
    c.patch("/api/v1/settings/general", json={"opencode_model": "openrouter/test-model", "resolution": "640x360"})
    j = wait_job(c, say(c, conv["id"], "render a short test video with my avatar", [up["asset_id"]])["id"])
    assert j["status"] == "completed", j
    msgs = c.get(f"/api/v1/conversations/{conv['id']}/messages").json()
    assert msgs[-1]["role"] == "assistant" and "Plan:" in msgs[-1]["content"] and "final.mp4" in msgs[-1]["content"]
    first = oclog(tmp)[0]
    assert first["session_arg"] is None and first["uploads"] and first["uploads"][0].endswith("avatar.mp4") and first["model"] == "openrouter/test-model"
    outs = c.get(f"/api/v1/projects/{p['id']}/assets?source=generated").json()
    assert len(outs) == 1 and outs[0]["meta"]["qc_ok"] and outs[0]["meta"]["width"] == 640 and outs[0]["meta"]["has_audio"]
    data = httpx.get(c.get(f"/api/v1/assets/{outs[0]['id']}/url").json()["url"]).content
    f = tmp / "dl.mp4"; f.write_bytes(data)
    assert subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "null", "-"], capture_output=True).returncode == 0  # downloaded output decodes
    types = [e["type"] for e in c.get(f"/api/v1/jobs/{j['id']}/events.json").json()]
    for t in ("queued", "dispatched", "runner_started", "restored", "assistant_text", "qc", "checkpoint", "output_ready"): assert t in types, (t, types)


def test_2_followup_restores_session_state_and_uploads_on_new_runner(world):
    c, tmp, p, conv = world
    before = len(oclog(tmp))
    j = wait_job(c, say(c, conv["id"], "make the intro shorter")["id"])
    assert j["status"] == "completed", j
    run2 = oclog(tmp)[before]
    assert run2["session_arg"] and run2["session_arg"].startswith("ses_fake_")  # same OpenCode session resumed via export/import
    assert run2["had_notes"] is True                                          # project state tarball restored on empty workspace
    assert run2["uploads"] and run2["uploads"][0].endswith("avatar.mp4")      # uploaded media re-fetched from storage
    assert not run2["prompt_has_history"]                                     # session resume, not history fallback
    ev1 = [e for e in c.get(f"/api/v1/jobs/{j['id']}/events.json").json() if e["type"] == "restored"]
    assert {e["data"]["what"] for e in ev1} >= {"project_state", "uploads", "opencode_session"}


def test_3a_session_restored_from_storage_after_local_opencode_data_wiped(world):
    c, tmp, p, conv = world
    shutil.rmtree(tmp / "ochome")  # the runner's local OpenCode data is gone; only persistent storage remains
    before = len(oclog(tmp))
    j = wait_job(c, say(c, conv["id"], "add a call to action")["id"])
    assert j["status"] == "completed", j
    run = oclog(tmp)[before]
    assert run["session_arg"] and not run["prompt_has_history"]  # imported from storage, resumed


def test_3b_missing_session_checkpoint_falls_back_to_message_history(world):
    c, tmp, p, conv = world
    DB_EXEC("DELETE FROM checkpoints WHERE kind='opencode_session'")
    shutil.rmtree(tmp / "ochome", ignore_errors=True)
    before = len(oclog(tmp))
    j = wait_job(c, say(c, conv["id"], "tighten the ending")["id"])
    assert j["status"] == "completed", j
    run = oclog(tmp)[before]
    assert run["session_arg"] is None and run["prompt_has_history"]  # context rebuilt from persisted conversation


def test_4_runner_crash_is_reported_and_retry_works(world):
    c, tmp, p, conv = world
    j = wait_job(c, say(c, conv["id"], "crash please")["id"])
    assert j["status"] == "failed" and "OpenCode exited" in j["error"]
    r = c.post(f"/api/v1/jobs/{j['id']}/retry").json()
    assert r["attempt"] == 2
    j2 = wait_job(c, r["id"])  # message still says "crash", so it fails again -> proves retry really re-executes
    assert j2["status"] == "failed" and j2["parent_job_id"] == j["id"]


def test_5_cancel_running_job(world):
    c, tmp, p, conv = world
    j = say(c, conv["id"], "sleep for a long time")
    wait_job(c, j["id"], until=("running",))
    time.sleep(2)
    c.post(f"/api/v1/jobs/{j['id']}/cancel")
    done = wait_job(c, j["id"], timeout=30)
    assert done["status"] == "cancelled"


def test_6_killed_runner_is_recovered_on_new_runner(srv):
    c, tmp = srv
    p = c.post("/api/v1/projects", json={"name": "Recovery"}).json()
    conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    j = say(c, conv["id"], "sleep then render")
    wait_job(c, j["id"], until=("running",)); time.sleep(2)
    pid = next(e["data"]["pid"] for e in c.get(f"/api/v1/jobs/{j['id']}/events.json").json() if e["type"] == "dispatched")
    os.killpg(os.getpgid(pid), signal.SIGKILL)  # hard kill: no cleanup handlers run, like a lost runner
    time.sleep(4)  # heartbeat silence
    import hashlib, hmac
    sig = hmac.new(b"e2e-runner", b"runner:recover", hashlib.sha256).hexdigest()
    r = httpx.post(str(c.base_url) + "/api/runner/recover?stale_seconds=3", headers={"X-Runner-Signature": sig}).json()
    assert r["recovered"] and r["recovered"][0]["failed"] == j["id"]
    assert c.get(f"/api/v1/jobs/{j['id']}").json()["error"].startswith("runner lost")
    retry = r["recovered"][0]["retry"]; assert retry
    wait_job(c, retry, until=("running", "failed", "completed", "cancelled"))  # a new runner really picked it up
    c.post(f"/api/v1/jobs/{retry}/cancel")


def test_7_failed_qc_triggers_one_fix_pass_then_succeeds(srv):
    c, tmp = srv
    p = c.post("/api/v1/projects", json={"name": "QC"}).json()
    conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    j = wait_job(c, say(c, conv["id"], "render badrender")["id"])
    assert j["status"] == "completed", j
    types = [e["type"] for e in c.get(f"/api/v1/jobs/{j['id']}/events.json").json()]
    assert "qc_fix_attempt" in types  # corrupt first render was detected by real ffprobe/ffmpeg decode
    outs = c.get(f"/api/v1/projects/{p['id']}/assets?source=generated").json()
    assert [o["meta"]["qc_ok"] for o in outs][0] is True  # re-render passed QC
    assert any(o["meta"]["qc_ok"] is False for o in outs)  # and the failed one is flagged, not hidden


def _project_with_video(c, tmp, name):
    p = c.post("/api/v1/projects", json={"name": name}).json()
    conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    src = tmp / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1", "-pix_fmt", "yuv420p", str(src)], check=True)
    up = c.post(f"/api/v1/projects/{p['id']}/uploads", json={"name": "avatar.mp4", "size": src.stat().st_size, "mime": "video/mp4"}).json()
    httpx.put(up["put_url"], content=src.read_bytes()); c.post(f"/api/v1/assets/{up['asset_id']}/complete")
    return p, conv, up["asset_id"]


def test_8_storyboard_is_validated_persisted_and_served(srv):
    c, tmp = srv
    p, conv, aid = _project_with_video(c, tmp, "plan")
    j = wait_job(c, say(c, conv["id"], "plan a storyboard", [aid])["id"])
    assert j["status"] == "completed", j
    sb = c.get(f"/api/v1/projects/{p['id']}/storyboard").json()
    assert sb["validation"]["ok"] and sb["validation"]["scenes"] == 2 and sb["storyboard"]["scenes"][1]["type"] == "screen_recording"
    msgs = c.get(f"/api/v1/conversations/{conv['id']}/messages").json()
    assert "Storyboard: 2 scenes" in msgs[-1]["content"]


def test_9_fabricated_demo_is_caught_and_fixed_by_a_second_pass(srv):
    c, tmp = srv
    p, conv, aid = _project_with_video(c, tmp, "fab")
    j = wait_job(c, say(c, conv["id"], "storyboard badplan", [aid])["id"])
    assert j["status"] == "completed", j
    ev = c.get(f"/api/v1/jobs/{j['id']}/events.json").json()
    sbev = [e for e in ev if e["type"] == "storyboard"]
    assert sbev[0]["data"]["ok"] is False and any("generated" in x or "never generate" in x for x in sbev[0]["data"]["errors"])
    assert any(e["type"] == "plan_fix_attempt" for e in ev) and sbev[-1]["data"]["ok"] is True
    assert c.get(f"/api/v1/projects/{p['id']}/storyboard").json()["validation"]["ok"] is True


def test_10_needs_input_is_surfaced(srv):
    c, tmp = srv
    p = c.post("/api/v1/projects", json={"name": "ask"}).json()
    conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    j = wait_job(c, say(c, conv["id"], "askuser please")["id"])
    assert j["status"] == "completed"
    assert any(e["type"] == "needs_input" and "brand color" in e["data"]["question"] for e in c.get(f"/api/v1/jobs/{j['id']}/events.json").json())
    assert "I need your input: Which brand color?" in c.get(f"/api/v1/conversations/{conv['id']}/messages").json()[-1]["content"]


def test_11_concurrency_limit_defers_and_auto_starts(srv):
    c, tmp = srv
    t = time.time()
    while c.get("/api/v1/jobs?status=running").json() or c.get("/api/v1/jobs?status=starting").json():
        assert time.time() - t < 60, "earlier jobs still running"; time.sleep(0.5)
    p = c.post("/api/v1/projects", json={"name": "conc"}).json()
    convs = [c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json() for _ in range(3)]
    jobs = [say(c, cv["id"], "sleep") for cv in convs]
    time.sleep(1.5)
    st = [c.get(f"/api/v1/jobs/{j['id']}").json()["status"] for j in jobs]
    assert st[2] == "queued" and st[0] in ("starting", "running") and st[1] in ("starting", "running"), st  # limit = 2
    assert any(e["type"] == "deferred" for e in c.get(f"/api/v1/jobs/{jobs[2]['id']}/events.json").json())
    c.post(f"/api/v1/jobs/{jobs[0]['id']}/cancel")
    wait_job(c, jobs[2]["id"], timeout=40, until=("starting", "running"))  # freed slot -> queued job starts on its own
    for j in jobs: c.post(f"/api/v1/jobs/{j['id']}/cancel")
    for j in jobs: wait_job(c, j["id"], timeout=40)


def test_12_hung_opencode_is_stopped_and_reported(srv):
    c, tmp = srv
    p = c.post("/api/v1/projects", json={"name": "hang"}).json()
    conv = c.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    j = wait_job(c, say(c, conv["id"], "sleep forever")["id"], timeout=80)
    assert j["status"] == "failed" and "timed out" in j["error"], j
