"""Post-deploy smoke test of a LIVE Backlot Worker, driven exactly like the browser app does.
Reads BASE_URL and ORCH_ADMIN_TOKEN from the environment. Prints only non-secret facts.
usage: smoke_deployed.py [--no-wait] [--timeout SECONDS] [--model provider/model]"""
import argparse, http.cookiejar, json, os, sys, time, urllib.error, urllib.request

ap = argparse.ArgumentParser(); ap.add_argument("--no-wait", action="store_true"); ap.add_argument("--timeout", type=int, default=1500)
ap.add_argument("--model", default="opencode/big-pickle"); args = ap.parse_args()
BASE = os.environ["BASE_URL"].rstrip("/"); TOKEN = os.environ["ORCH_ADMIN_TOKEN"]
jar = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
lines = []; failed = []
def say(m): print(m, flush=True); lines.append(m)
def call(method, path, body=None, auth=True):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(), headers={"content-type": "application/json", "user-agent": "backlot-smoke"})
    try:
        with (op if auth else urllib.request.build_opener()).open(req, timeout=60) as r: raw = r.read(); return r.status, raw
    except urllib.error.HTTPError as e: return e.code, e.read()
    except Exception as e: return 0, str(e).encode()
def j(raw):
    try: return json.loads(raw)
    except Exception: return {"_raw": raw[:200].decode("utf-8", "replace")}
def check(name, ok, detail=""):
    say(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok: failed.append(name)

st, raw = call("GET", "/app", auth=False); check("GET /app serves the frontend", st == 200 and b"Backlot" in raw, f"HTTP {st}")
st, raw = call("GET", "/api/v1/me", auth=False); check("API is protected when signed out", st == 401, f"HTTP {st}")
st, raw = call("POST", "/api/v1/auth/login", {"token": "definitely-wrong"}, auth=False); check("wrong token is rejected", st == 401, f"HTTP {st}")
st, raw = call("POST", "/api/v1/auth/login", {"token": TOKEN}); check("login with ORCH_ADMIN_TOKEN", st == 200, f"HTTP {st}")
if st != 200: say("cannot continue without a session"); open("/tmp/smoke.txt", "w").write("\n".join(lines)); sys.exit(1)
st, raw = call("GET", "/api/v1/settings/status"); s = j(raw)
if st == 200:
    gh, stg, db, au = s["github"], s["storage"], s["database"], s["auth"]
    check("GitHub dispatch configured", gh["configured"] or gh["dispatch_mode"] != "github", f"repo={gh['repo']} workflow={gh['workflow']} mode={gh['dispatch_mode']}")
    check("database (D1)", db["ok"], db["engine"])
    check("auth secrets set on the Worker", au["admin_token_set"] and au["runner_secret_set"], f"admin={au['admin_token_set']} runner={au['runner_secret_set']}")
    say(f"INFO  file storage: backend={stg.get('backend')} ok={stg.get('ok')} {stg.get('error', '')[:160]}")
    say(f"INFO  max concurrent jobs: {s['limits']['max_concurrent_jobs']}")
else: check("settings status", False, f"HTTP {st}")
st, raw = call("PATCH", "/api/v1/settings/general", {"opencode_model": args.model, "resolution": "1280x720"}); check("save settings (model + resolution)", st == 200, f"HTTP {st}")
st, raw = call("GET", "/api/v1/settings/status"); check("settings persisted", st == 200 and j(raw)["general"]["opencode_model"] == args.model)
st, raw = call("POST", "/api/v1/projects", {"name": "Deploy smoke test"}); pid = j(raw).get("id"); check("create project", st == 200 and pid, f"HTTP {st}")
st, raw = call("POST", f"/api/v1/projects/{pid}/conversations", {}); cid = j(raw).get("id"); check("create conversation", st == 200 and cid, f"HTTP {st}")
st, raw = call("POST", f"/api/v1/projects/{pid}/uploads", {"name": "t.txt", "size": 5}); u = j(raw)
say(f"INFO  upload start -> HTTP {st} {u.get('detail', '')[:120]}")
st, raw = call("POST", f"/api/v1/conversations/{cid}/messages", {"content": "Say hello in one sentence, then list the files in this project folder."}); r = j(raw); job = r.get("job") or {}
check("send chat message -> job created", st == 200 and job.get("id"), f"HTTP {st} status={job.get('status')} {r.get('detail', '')[:160]}")
if job.get("status") == "failed": check("job was dispatched to GitHub", False, (job.get("error") or "")[:300])
elif job.get("id") and not args.no_wait:
    t0 = time.time(); last = None; jid = job["id"]
    while time.time() - t0 < args.timeout:
        st, raw = call("GET", f"/api/v1/jobs/{jid}"); jb = j(raw); cur = (jb.get("status"), jb.get("stage"))
        if cur != last: last = cur; say(f"JOB   [{int(time.time()-t0):4d}s] {cur[0]} / {cur[1]}")
        if jb.get("status") in ("completed", "failed", "cancelled"): break
        time.sleep(10)
    else: say("JOB   timed out waiting")
    check("job completed on a runner", jb.get("status") == "completed", (jb.get("error") or "")[:300])
    st, raw = call("GET", f"/api/v1/jobs/{jid}/events.json"); ev = j(raw) if st == 200 else []
    kinds = {}
    for e in ev if isinstance(ev, list) else []: kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    say(f"INFO  events: {kinds}")
    for e in ev if isinstance(ev, list) else []:
        if e["type"] in ("error", "warning", "opencode_error"): say(f"INFO  {e['type']}: {json.dumps(e['data'])[:300]}")
        if e["type"] == "runner_started": say(f"INFO  runner: {json.dumps(e['data'].get('hardware'))}")
        if e["type"] == "run_info": say(f"INFO  github run: {e['data'].get('run_url')}")
    st, raw = call("GET", f"/api/v1/conversations/{cid}/messages"); ms = j(raw)
    reply = next((m["content"] for m in reversed(ms) if m["role"] == "assistant"), None) if isinstance(ms, list) else None
    say(f"INFO  assistant reply: {(reply or '(none)')[:500]!r}")
call("PATCH", f"/api/v1/projects/{pid}", {"archived": True})
say(f"RESULT {'ALL CHECKS PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}")
open("/tmp/smoke.txt", "w").write("\n".join(lines)); sys.exit(0 if not failed else 1)
