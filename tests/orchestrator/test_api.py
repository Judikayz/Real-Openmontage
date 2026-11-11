import json
import httpx
from orchestrator import dispatch as dsp, secrets_gh, notify
from orchestrator.auth import runner_signature
from orchestrator.routes import recover_stale


def mk(client):
    p = client.post("/api/v1/projects", json={"name": "Demo"}).json()
    c = client.post(f"/api/v1/projects/{p['id']}/conversations", json={}).json()
    return p, c


def test_auth_required_and_login(anon, client):
    assert anon.get("/api/v1/projects").status_code == 401
    assert anon.post("/api/v1/auth/login", json={"token": "wrong"}).status_code == 401
    r = anon.post("/api/v1/auth/login", json={"token": "owner-token-test"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert anon.get("/api/v1/projects").status_code == 200  # cookie session works


def test_session_cookie_tamper_rejected(anon):
    anon.cookies.set("om_session", "9999999999.deadbeef")
    assert anon.get("/api/v1/projects").status_code == 401


def test_project_lifecycle(client):
    p, _ = mk(client)
    assert client.patch(f"/api/v1/projects/{p['id']}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    client.patch(f"/api/v1/projects/{p['id']}", json={"archived": True})
    assert client.get("/api/v1/projects").json() == []
    assert len(client.get("/api/v1/projects?archived=true").json()) == 1


def test_upload_validation(client):
    p, _ = mk(client)
    u = f"/api/v1/projects/{p['id']}/uploads"
    assert client.post(u, json={"name": "x.exe", "size": 10}).status_code == 415
    assert client.post(u, json={"name": "big.mp4", "size": 10**10}).status_code == 413
    assert client.post(u, json={"name": "a.mp4", "size": 0}).status_code == 422
    ok = client.post(u, json={"name": "../../etc/a b.mp4", "size": 5, "mime": "video/mp4"}).json()
    assert ok["kind"] == "video" and ".." not in ok["storage_key"]


def test_upload_roundtrip_and_size_check(client):
    p, _ = mk(client)
    up = client.post(f"/api/v1/projects/{p['id']}/uploads", json={"name": "a.txt", "size": 5, "mime": "text/plain"}).json()
    assert client.post(f"/api/v1/assets/{up['asset_id']}/complete").status_code == 409  # not uploaded yet
    assert client.put(up["put_url"], content=b"hello!").status_code == 200
    assert client.post(f"/api/v1/assets/{up['asset_id']}/complete").status_code == 409  # size mismatch
    up2 = client.post(f"/api/v1/projects/{p['id']}/uploads", json={"name": "b.txt", "size": 5}).json()
    assert client.put(up2["put_url"], content=b"hello").status_code == 200
    assert client.post(f"/api/v1/assets/{up2['asset_id']}/complete").json()["status"] == "uploaded"
    url = client.get(f"/api/v1/assets/{up2['asset_id']}/url").json()["url"]
    assert client.get(url).content == b"hello"


def test_signed_url_forgery_rejected(client):
    p, _ = mk(client)
    up = client.post(f"/api/v1/projects/{p['id']}/uploads", json={"name": "a.txt", "size": 1}).json()
    bad = up["put_url"].replace("sig=", "sig=00")
    assert client.put(bad, content=b"x").status_code == 403


def test_message_creates_job_and_blocks_second(client):
    p, c = mk(client)
    r = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "make a video"})
    assert r.status_code == 200 and r.json()["job"]["status"] == "queued"  # mode none: honest, stays queued
    assert client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "again"}).status_code == 409
    msgs = client.get(f"/api/v1/conversations/{c['id']}/messages").json()
    assert [m["role"] for m in msgs] == ["user"]


def test_attachment_must_be_uploaded_and_same_project(client):
    p, c = mk(client)
    up = client.post(f"/api/v1/projects/{p['id']}/uploads", json={"name": "a.txt", "size": 1}).json()
    r = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x", "asset_ids": [up["asset_id"]]})
    assert r.status_code == 400


def test_cancel_and_retry(client):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    assert client.post(f"/api/v1/jobs/{j['id']}/retry").status_code == 409
    assert client.post(f"/api/v1/jobs/{j['id']}/cancel").json()["status"] == "cancelled"
    r = client.post(f"/api/v1/jobs/{j['id']}/retry").json()
    assert r["attempt"] == 2 and r["parent_job_id"] == j["id"] and r["status"] == "queued"


def test_events_polling_resume(client):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    ev = client.get(f"/api/v1/jobs/{j['id']}/events.json").json()
    assert ev and client.get(f"/api/v1/jobs/{j['id']}/events.json?after={ev[-1]['seq']}").json() == []


def test_sse_stream_replays_and_ends(client):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    client.post(f"/api/v1/jobs/{j['id']}/cancel")
    with client.stream("GET", f"/api/v1/jobs/{j['id']}/events") as r:
        body = "".join(r.iter_text())
    assert "event: queued" in body and "event: status" in body and "event: end" in body
    with client.stream("GET", f"/api/v1/jobs/{j['id']}/events", headers={"Last-Event-ID": "999"}) as r:
        assert "event: queued" not in "".join(r.iter_text())


def test_runner_requires_valid_signature(client, anon):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    assert anon.get(f"/api/runner/jobs/{j['id']}/context").status_code == 401
    other = runner_signature("runner-secret-test", "some-other-job")
    assert anon.get(f"/api/runner/jobs/{j['id']}/context", headers={"X-Runner-Signature": other}).status_code == 401
    good = runner_signature("runner-secret-test", j["id"])
    assert anon.get(f"/api/runner/jobs/{j['id']}/context", headers={"X-Runner-Signature": good}).status_code == 200


def test_terminal_status_is_final(client, anon):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    h = {"X-Runner-Signature": runner_signature("runner-secret-test", j["id"])}
    assert anon.post(f"/api/runner/jobs/{j['id']}/status", json={"status": "completed"}, headers=h).json()["changed"]
    assert not anon.post(f"/api/runner/jobs/{j['id']}/status", json={"status": "running"}, headers=h).json()["changed"]


def test_dispatch_request_has_no_secrets_in_inputs(cfg):
    cfg.gh_repo, cfg.gh_token, cfg.gh_workflow, cfg.gh_ref = "me/repo", "ghp_SECRET", "opencode-run.yml", "main"
    url, headers, body = dsp.dispatch_request(cfg, {"id": "job_1", "project_id": "prj_1"}, "https://b.example")
    assert url == "https://api.github.com/repos/me/repo/actions/workflows/opencode-run.yml/dispatches"
    assert headers["Authorization"] == "Bearer ghp_SECRET"
    assert "ghp_SECRET" not in json.dumps(body) and cfg.runner_secret not in json.dumps(body)
    assert body == {"ref": "main", "inputs": {"job_id": "job_1", "project_id": "prj_1", "backend_url": "https://b.example"}}


# NOTE: the next two use httpx.MockTransport. They verify how we REACT to GitHub's responses,
# not that GitHub accepts the request (that is verified separately against the real API).
def test_dispatch_failure_marks_job_failed_with_reason(client, cfg):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    cfg.gh_repo, cfg.gh_token, cfg.dispatch_mode = "me/repo", "t", "github"
    db = client.app.state.db
    dsp.dispatch(cfg, db, j, "https://b", transport=httpx.MockTransport(lambda r: httpx.Response(404, text="Not Found")))
    job = db.one("SELECT * FROM jobs WHERE id=?", (j["id"],))
    assert job["status"] == "failed" and "404" in job["error"]


def test_dispatch_success_sets_starting(client, cfg):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    cfg.gh_repo, cfg.gh_token, cfg.dispatch_mode = "me/repo", "t", "github"
    db = client.app.state.db
    dsp.dispatch(cfg, db, j, "https://b", transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    assert db.one("SELECT status FROM jobs WHERE id=?", (j["id"],))["status"] == "starting"


def test_unconfigured_github_fails_loudly(client, cfg):
    cfg.dispatch_mode = "github"
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    assert j["status"] == "failed" and "not configured" in j["error"]


def test_recover_stale_marks_failed_and_retries(client, cfg):
    p, c = mk(client)
    j = client.post(f"/api/v1/conversations/{c['id']}/messages", json={"content": "x"}).json()["job"]
    db = client.app.state.db
    db.set_job_status(j["id"], "running")
    db.x("UPDATE job_events SET ts=ts-1000 WHERE job_id=?", (j["id"],))
    out = recover_stale(db, cfg, "https://b", stale_seconds=60)
    assert out and out[0]["failed"] == j["id"] and out[0]["retry"]
    assert db.one("SELECT status,error FROM jobs WHERE id=?", (j["id"],))["error"].startswith("runner lost")
    assert db.one("SELECT attempt FROM jobs WHERE id=?", (out[0]["retry"],))["attempt"] == 2


def test_settings_general_validation_and_persist(client):
    assert client.patch("/api/v1/settings/general", json={"fps": 500}).status_code == 400
    assert client.patch("/api/v1/settings/general", json={"bogus": 1}).status_code == 400
    assert client.patch("/api/v1/settings/general", json={"resolution": "wide"}).status_code == 400
    assert client.patch("/api/v1/settings/general", json={"opencode_model": "bad model"}).status_code == 400
    r = client.patch("/api/v1/settings/general", json={"fps": 24, "opencode_model": "openrouter/some/model"})
    assert r.json()["fps"] == 24
    assert client.get("/api/v1/settings/status").json()["general"]["fps"] == 24


def test_settings_status_never_leaks_secrets(client, cfg):
    cfg.gh_token = "ghp_VERYSECRET"
    s = json.dumps(client.get("/api/v1/settings/status").json())
    assert "ghp_VERYSECRET" not in s and "owner-token-test" not in s and "runner-secret-test" not in s


def test_secret_sealing_roundtrip():
    from nacl import public, encoding
    import base64
    sk = public.PrivateKey.generate()
    sealed = secrets_gh.seal(sk.public_key.encode(encoding.Base64Encoder()).decode(), "hunter2")
    assert public.SealedBox(sk).decrypt(base64.b64decode(sealed)) == b"hunter2"
    import pytest
    with pytest.raises(ValueError):
        secrets_gh.put_secret(type("C", (), {"gh_repo": "a/b", "gh_token": "t"})(), "PATH", "x")


def test_telegram_request_shape(cfg):
    cfg.telegram_bot_token, cfg.telegram_chat_id = "123:abc", "42"
    seen = {}
    def h(req): seen["url"], seen["body"] = str(req.url), json.loads(req.content); return httpx.Response(200)
    assert notify.notify(cfg, "hi", transport=httpx.MockTransport(h))
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage" and seen["body"] == {"chat_id": "42", "text": "hi"}
