"""Scheduling + concurrency tests against the real Cloudflare Worker runtime (workerd, local D1).
The cron handler is triggered through wrangler's /__scheduled endpoint, i.e. the same code path Cloudflare cron uses."""
import datetime as dt, json, os, signal, socket, subprocess, time
from pathlib import Path
import httpx, pytest

ROOT = Path(__file__).resolve().parents[2]; WDIR = ROOT / "worker"
H = {"Authorization": "Bearer sched-admin"}
MIN_CRON = "*+*+*+*+*"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wrangler(args, check=True):
    """Run wrangler and include its stderr/stdout in the failure message (so CI failures are diagnosable)."""
    r = subprocess.run(["npx", "wrangler", *args], cwd=WDIR, capture_output=True, text=True, timeout=240)
    if check and r.returncode != 0:
        raise RuntimeError(f"wrangler {' '.join(args[:3])} failed (rc={r.returncode}):\nSTDOUT: {r.stdout[-1500:]}\nSTDERR: {r.stderr[-1500:]}")
    return r


@pytest.fixture(scope="module")
def w(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sched"); persist = str(tmp / "st"); port = free_port()
    (WDIR / ".dev.vars").write_text("ORCH_ADMIN_TOKEN=sched-admin\nORCH_RUNNER_SECRET=r\nSESSION_SECRET=s\nDISPATCH_MODE=none\n")
    wrangler(["d1", "migrations", "apply", "DB", "--local", "--persist-to", persist])
    p = subprocess.Popen(["npx", "wrangler", "dev", "--local", "--test-scheduled", "--port", str(port), "--persist-to", persist], cwd=WDIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(90):
        try:
            if httpx.get(base + "/api/v1/me", headers=H).status_code == 200: break
        except httpx.HTTPError: pass
        time.sleep(0.5)
    else: p.kill(); pytest.fail("worker did not start")
    c = httpx.Client(base_url=base, headers=H, timeout=60)
    def sql(q): wrangler(["d1", "execute", "DB", "--local", "--persist-to", persist, "--command", q])
    def tick(): r = httpx.get(f"{base}/__scheduled?cron={MIN_CRON}", timeout=60); assert r.status_code == 200, r.text; time.sleep(1.5)
    yield c, sql, tick
    try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception: p.terminate()
    p.wait(15)


@pytest.fixture
def proj(w):
    c, *_ = w; return c.post("/api/v1/projects", json={"name": "S"}).json()["id"]


def mk(c, pid, **kw):
    body = {"name": "Nightly", "prompt": "make the daily video", "kind": "daily", "time": "09:00", **kw}
    r = c.post(f"/api/v1/projects/{pid}/schedules", json=body); assert r.status_code == 200, r.text; return r.json()


def jobs_for(c, sch): return [j for j in c.get("/api/v1/jobs").json() if j["conversation_id"] == sch["conversation_id"]]


def test_requires_auth(w, proj):
    c, *_ = w; assert httpx.get(str(c.base_url) + f"/api/v1/projects/{proj}/schedules").status_code == 401


def test_default_timezone_is_lagos_and_next_run_is_correct(w, proj):
    c, *_ = w; s = mk(c, proj)
    assert s["spec"]["tz"] == "Africa/Lagos" and "daily at 09:00 (Africa/Lagos)" in s["summary"]
    nxt = dt.datetime.fromtimestamp(s["next_run_at"], dt.timezone.utc); assert (nxt.hour, nxt.minute) == (8, 0)  # 09:00 Lagos == 08:00 UTC
    assert nxt.timestamp() > time.time()


@pytest.mark.parametrize("bad", [{"kind": "hourly"}, {"time": "99:00"}, {"kind": "weekly", "weekdays": []}, {"kind": "monthly", "day_of_month": 40, "time": "09:00"},
                                  {"tz": "Mars/Base"}, {"kind": "once", "run_at": "2020-01-01T09:00"}, {"name": ""}, {"prompt": ""}])
def test_bad_input_rejected(w, proj, bad):
    c, *_ = w; body = {"name": "x", "prompt": "p", "kind": "daily", "time": "09:00", **bad}
    assert c.post(f"/api/v1/projects/{proj}/schedules", json=body).status_code == 422


def test_weekly_monthly_once_creation(w, proj):
    c, *_ = w
    assert mk(c, proj, kind="weekly", weekdays=[1, 3], time="18:30")["next_run_at"] > time.time()
    assert mk(c, proj, kind="monthly", day_of_month=31, time="10:00")["next_run_at"] > time.time()
    fut = (dt.datetime.utcnow() + dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    assert mk(c, proj, kind="once", run_at=fut)["spec"]["run_at"] == fut


def test_due_schedule_fires_once_and_advances(w, proj):
    c, sql, tick = w; s = mk(c, proj)
    sql(f"UPDATE schedules SET next_run_at={time.time() - 5} WHERE id='{s['id']}'"); tick()
    js = jobs_for(c, s); assert len(js) == 1
    msgs = c.get(f"/api/v1/conversations/{s['conversation_id']}/messages").json(); assert msgs[0]["content"] == "make the daily video"
    after = [x for x in c.get(f"/api/v1/projects/{proj}/schedules").json() if x["id"] == s["id"]][0]
    assert after["next_run_at"] > time.time() and after["last_run_at"]
    runs = c.get(f"/api/v1/schedules/{s['id']}/runs").json(); assert runs[0]["status"] == "started" and runs[0]["job_id"] == js[0]["id"]
    tick(); assert len(jobs_for(c, s)) == 1  # not due again -> no duplicate


def test_missed_runs_are_recovered_once_with_a_count(w, proj):
    c, sql, tick = w; s = mk(c, proj)  # worker was "down" for ~3 days
    sql(f"UPDATE schedules SET next_run_at={time.time() - 3 * 86400 - 600} WHERE id='{s['id']}'"); tick()
    assert len(jobs_for(c, s)) == 1  # coalesced into ONE catch-up run, not a burst of 3
    r = c.get(f"/api/v1/schedules/{s['id']}/runs").json()[0]
    assert r["status"] == "missed_catchup" and "late" in r["note"] and "missed" in r["note"]


def test_pause_resume_does_not_catch_up_paused_period(w, proj):
    c, sql, tick = w; s = mk(c, proj)
    p = c.patch(f"/api/v1/schedules/{s['id']}", json={"status": "paused"}).json(); assert p["status"] == "paused" and p["next_run_at"] is None
    tick(); assert jobs_for(c, s) == []
    r = c.patch(f"/api/v1/schedules/{s['id']}", json={"status": "active"}).json(); assert r["status"] == "active" and r["next_run_at"] > time.time()
    tick(); assert jobs_for(c, s) == []


def test_edit_changes_next_run(w, proj):
    c, *_ = w; s = mk(c, proj); e = c.patch(f"/api/v1/schedules/{s['id']}", json={"time": "14:15", "name": "Renamed"}).json()
    assert e["name"] == "Renamed" and e["spec"]["time"] == "14:15"
    n = dt.datetime.fromtimestamp(e["next_run_at"], dt.timezone.utc); assert (n.hour, n.minute) == (13, 15)  # 14:15 Lagos
    assert c.patch(f"/api/v1/schedules/{s['id']}", json={"time": "nope"}).status_code == 422


def test_run_now_then_overlap_is_skipped_and_logged(w, proj):
    c, sql, tick = w; s = mk(c, proj)
    r = c.post(f"/api/v1/schedules/{s['id']}/run-now"); assert r.status_code == 200 and r.json()["job"]["status"] == "queued"
    assert c.post(f"/api/v1/schedules/{s['id']}/run-now").status_code == 409  # previous run still active
    sql(f"UPDATE schedules SET next_run_at={time.time() - 5} WHERE id='{s['id']}'"); tick()
    assert len(jobs_for(c, s)) == 1  # scheduled run did not pile up behind the active one
    runs = c.get(f"/api/v1/schedules/{s['id']}/runs").json(); assert {x["status"] for x in runs} >= {"manual", "skipped"}


def test_one_time_schedule_runs_then_completes(w, proj):
    c, sql, tick = w; fut = (dt.datetime.utcnow() + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"); s = mk(c, proj, kind="once", run_at=fut)
    sql(f"UPDATE schedules SET next_run_at={time.time() - 5} WHERE id='{s['id']}'"); tick()
    assert len(jobs_for(c, s)) == 1
    done = [x for x in c.get(f"/api/v1/projects/{proj}/schedules").json() if x["id"] == s["id"]][0]; assert done["status"] == "completed" and done["next_run_at"] is None
    tick(); assert len(jobs_for(c, s)) == 1
    assert c.patch(f"/api/v1/schedules/{s['id']}", json={"status": "active"}).status_code == 200  # run_at still in the future: legitimately re-armable
    past = json.dumps({"kind": "once", "tz": "Africa/Lagos", "run_at": "2020-01-01T09:00"})
    sql(f"UPDATE schedules SET status='completed', next_run_at=NULL, spec='{past}' WHERE id='{s['id']}'")
    assert c.patch(f"/api/v1/schedules/{s['id']}", json={"status": "active"}).status_code == 409  # run_at in the past: cannot be resumed


def test_delete_removes_schedule_and_history(w, proj):
    c, *_ = w; s = mk(c, proj); assert c.delete(f"/api/v1/schedules/{s['id']}").status_code == 200
    assert c.get(f"/api/v1/schedules/{s['id']}/runs").status_code == 404 and s["id"] not in [x["id"] for x in c.get(f"/api/v1/projects/{proj}/schedules").json()]


def test_concurrency_limit_is_20_by_default(w):
    c, *_ = w; assert c.get("/api/v1/settings/status").json()["limits"]["max_concurrent_jobs"] == 20


def test_schedules_survive_in_database_across_ticks_independent_of_any_client(w, proj):
    c, sql, tick = w; s = mk(c, proj); tick(); tick()  # no browser/phone involved at all: cron + D1 only
    assert s["id"] in [x["id"] for x in c.get(f"/api/v1/projects/{proj}/schedules").json()]
