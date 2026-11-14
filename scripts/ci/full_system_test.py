"""FULL-SYSTEM TEST on a GitHub runner: real Worker (workerd, local D1+R2) + real runner + REAL OpenCode + real model
+ real OpenMontage repo. Drives it exactly like Backlot's UI does (HTTP API). Writes a report to $REPORT_DIR."""
import json, os, signal, socket, subprocess, sys, tempfile, threading, time, http.server
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]; WDIR = ROOT / "worker"
REPORT = Path(os.environ.get("REPORT_DIR", "/tmp/report")); REPORT.mkdir(parents=True, exist_ok=True)
MODEL = os.environ.get("TEST_MODEL", "opencode/big-pickle"); TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "2700"))
TOKEN = "full-system-test"; log = []
def say(*a):
    m = " ".join(str(x) for x in a); print(m, flush=True); log.append(m)
def sh(*a, **k): return subprocess.run(a, check=True, capture_output=True, text=True, **k)
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

PROMPT = os.environ.get("TEST_PROMPT") or (
    "Make a 15-second landscape promo video for a product called 'Backlot' (an AI video studio you control from your phone). "
    "Use my uploaded avatar clip for the intro, my uploaded screen recording for the demo section, and my uploaded voice recording as the narration. "
    "Add a title card and captions. Keep it simple and use only free local tools (FFmpeg/Remotion) — no paid APIs. "
    "First write the storyboard to projects/{pid}/artifacts/storyboard.json and check it with `python -m director validate projects/{pid}`, then render the video into projects/{pid}/renders/ and tell me what you produced.")

def main():
    port = free_port(); tmp = Path(tempfile.mkdtemp()); persist = str(tmp / "st"); base = f"http://127.0.0.1:{port}"
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            env = {**os.environ, "JOB_ID": d["job_id"], "BACKEND_URL": d["backend_url"], "ORCH_RUNNER_SECRET": "full-test-runner", "PYTHONPATH": str(ROOT),
                   "OPENCODE_BIN": "opencode", "OPENCODE_TIMEOUT_SECONDS": str(TIMEOUT - 300), "HEARTBEAT_SECONDS": "10"}
            env.pop("OM_REPO_ROOT", None)  # use the real checkout (it has pipelines, tools and AGENTS.md)
            lf = open(REPORT / f"runner_{d['job_id']}.log", "w")
            pr = subprocess.Popen([sys.executable, "-m", "runner.run_job"], cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            body = json.dumps({"pid": pr.pid}).encode(); self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=hs.serve_forever, daemon=True).start()
    (WDIR / ".dev.vars").write_text(f"ORCH_ADMIN_TOKEN={TOKEN}\nORCH_RUNNER_SECRET=full-test-runner\nSESSION_SECRET=s\nDISPATCH_MODE=webhook\nDISPATCH_WEBHOOK_URL=http://127.0.0.1:{hs.server_address[1]}/launch\n")
    sh("npx", "wrangler", "d1", "migrations", "apply", "DB", "--local", "--persist-to", persist, cwd=WDIR)
    wr = subprocess.Popen(["npx", "wrangler", "dev", "--local", "--port", str(port), "--persist-to", persist], cwd=WDIR, stdout=open(REPORT / "worker.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    H_ = {"Authorization": f"Bearer {TOKEN}"}
    for _ in range(120):
        try:
            if httpx.get(base + "/api/v1/me", headers=H_).status_code == 200: break
        except httpx.HTTPError: pass
        time.sleep(1)
    else: say("FAIL: worker did not start"); return 2
    c = httpx.Client(base_url=base, headers=H_, timeout=120); result = {"model": MODEL}
    try:
        c.patch("/api/v1/settings/general", json={"opencode_model": MODEL, "resolution": "1280x720", "fps": 30}).raise_for_status()
        p = c.post("/api/v1/projects", json={"name": "Full system test"}).json(); pid = p["id"]
        conv = c.post(f"/api/v1/projects/{pid}/conversations", json={}).json()
        # assets the way a user would supply them (synthetic stand-ins, honestly labelled)
        a = tmp / "assets"; a.mkdir()
        sh("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=4", "-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-shortest", "-pix_fmt", "yuv420p", str(a / "avatar_intro.mp4"))
        sh("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "smptebars=size=1280x720:rate=25:duration=6", "-pix_fmt", "yuv420p", str(a / "screen_recording.mp4"))
        sh("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=12", str(a / "narration.wav"))
        sh("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x3366ff:size=256x256:d=1", "-frames:v", "1", str(a / "logo.png"))
        ids = []
        for f in sorted(a.iterdir()):
            up = c.post(f"/api/v1/projects/{pid}/uploads", json={"name": f.name, "size": f.stat().st_size, "mime": "application/octet-stream"}).json()
            httpx.put(up["put_url"], content=f.read_bytes(), timeout=120).raise_for_status(); c.post(f"/api/v1/assets/{up['asset_id']}/complete").raise_for_status(); ids.append(up["asset_id"])
        say(f"uploaded {len(ids)} assets")
        t0 = time.time(); job = c.post(f"/api/v1/conversations/{conv['id']}/messages", json={"content": PROMPT.format(pid=pid), "asset_ids": ids}).json()["job"]; jid = job["id"]
        last = None
        while time.time() - t0 < TIMEOUT:
            j = c.get(f"/api/v1/jobs/{jid}").json()
            if (j["status"], j["stage"]) != last: last = (j["status"], j["stage"]); say(f"[{int(time.time()-t0)}s] {j['status']} / {j['stage']}")
            if j["status"] in ("completed", "failed", "cancelled"): break
            time.sleep(10)
        else: say("TIMEOUT waiting for job"); j = c.get(f"/api/v1/jobs/{jid}").json()
        result.update(job_status=j["status"], job_error=j.get("error"), seconds=int(time.time() - t0))
        ev = c.get(f"/api/v1/jobs/{jid}/events.json").json(); (REPORT / "events.json").write_text(json.dumps(ev, indent=1))
        types = {}
        for e in ev: types[e["type"]] = types.get(e["type"], 0) + 1
        result["event_counts"] = types
        msgs = c.get(f"/api/v1/conversations/{conv['id']}/messages").json(); (REPORT / "messages.json").write_text(json.dumps(msgs, indent=1))
        result["assistant_reply"] = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant"), None)
        assets = c.get(f"/api/v1/projects/{pid}/assets?source=generated").json(); result["outputs"] = assets
        try: sb = c.get(f"/api/v1/projects/{pid}/storyboard").json(); result["storyboard_validation"] = sb["validation"]; (REPORT / "storyboard.json").write_text(json.dumps(sb["storyboard"], indent=1))
        except Exception as e: result["storyboard_validation"] = f"none ({e})"
        vids = [x for x in assets if x["kind"] == "video"]
        if vids:
            url = c.get(f"/api/v1/assets/{vids[0]['id']}/url").json()["url"]; data = httpx.get(url, timeout=300).content; (tmp / "out.mp4").write_bytes(data); (REPORT / "final.mp4").write_bytes(data)  # keep the produced video with the report
            d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp / "out.mp4"), "-f", "null", "-"], capture_output=True, text=True)
            result["downloaded_video"] = {"bytes": len(data), "decodes_cleanly": d.returncode == 0 and not d.stderr.strip()}
        tree = subprocess.run(f"find projects/{pid} -type f | head -80", shell=True, cwd=ROOT, capture_output=True, text=True).stdout; (REPORT / "project_tree.txt").write_text(tree)
        cs = ROOT / f"projects/{pid}/renders/final-contact-sheet.png"
        if cs.is_file(): (REPORT / "contact_sheet.png").write_bytes(cs.read_bytes())
        result["project_files"] = tree.count("\n")
        # what the AI actually produced/changed, so editing bugs can be diagnosed from evidence
        art = ROOT / f"projects/{pid}/artifacts"
        if art.is_dir():
            (REPORT / "artifacts").mkdir(exist_ok=True)
            for f in art.glob("*.json"):
                if f.stat().st_size < 400_000: (REPORT / "artifacts" / f.name).write_bytes(f.read_bytes())
        diff = subprocess.run("git diff -- . ':(exclude)projects' ':(exclude)worker' | head -c 400000", shell=True, cwd=ROOT, capture_output=True, text=True).stdout
        (REPORT / "ai_repo_changes.diff").write_text(diff)
        (REPORT / "ai_repo_status.txt").write_text(subprocess.run("git status --porcelain | grep -v '^?? projects/' | grep -v 'worker/' | head -60", shell=True, cwd=ROOT, capture_output=True, text=True).stdout)
        for f in sorted((ROOT / f"projects/{pid}").rglob("*")):
            if f.is_file() and f.suffix in (".tsx", ".jsx", ".css", ".html") and f.stat().st_size < 200_000:
                d = REPORT / "project_code" / f.relative_to(ROOT / f"projects/{pid}"); d.parent.mkdir(parents=True, exist_ok=True); d.write_bytes(f.read_bytes())
    except Exception as e:
        result["harness_error"] = repr(e); say("HARNESS ERROR", repr(e))
    finally:
        for pr in (wr,):
            try: os.killpg(os.getpgid(pr.pid), signal.SIGTERM)
            except Exception: pass
    (REPORT / "result.json").write_text(json.dumps(result, indent=1, default=str)); (REPORT / "harness.log").write_text("\n".join(log))
    summ = [f"job={result.get('job_status')} in {result.get('seconds')}s model={MODEL}", f"error={result.get('job_error')}", f"events={result.get('event_counts')}",
            f"storyboard={result.get('storyboard_validation')}", f"outputs={[(o['name'], o['meta'].get('qc_ok'), o['meta'].get('duration')) for o in result.get('outputs', [])]}",
            f"downloaded={result.get('downloaded_video')}", f"reply={(result.get('assistant_reply') or '')[:900]}"]
    (REPORT / "summary.txt").write_text("\n".join(summ)); print("\n".join(summ))
    return 0 if result.get("job_status") == "completed" and result.get("downloaded_video", {}).get("decodes_cleanly") else 1

if __name__ == "__main__":
    sys.exit(main())
