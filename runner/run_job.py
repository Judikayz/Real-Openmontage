"""Entry point: JOB_ID, BACKEND_URL, ORCH_RUNNER_SECRET from env. Never receives secrets via workflow inputs."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import httpx

from director.storyboard import validate_file
from runner.qc import probe, qc_video

REPO = Path(os.environ.get("OM_REPO_ROOT", Path(__file__).resolve().parent.parent))
OPENCODE = shlex.split(os.environ.get("OPENCODE_BIN", "opencode"))
RULES = """You are the production operator for this OpenMontage repository (read AGENTS.md first).
Rules: reuse the existing pipelines in pipeline_defs/ and tools in tools/; do not build a second editor.
Use supplied avatar/voice/screen-recording files as-is; never fabricate a screen recording or claim a demo you did not capture.
Renders must be written under projects/<project_id>/renders/. Report honestly what you did and what failed."""


class Backend:
    def __init__(self, url: str, job_id: str, secret: str):
        self.url, self.job_id = url.rstrip("/"), job_id
        sig = hmac.new(secret.encode(), f"runner:{job_id}".encode(), hashlib.sha256).hexdigest()
        self.c = httpx.Client(base_url=self.url, headers={"X-Runner-Signature": sig}, timeout=60)

    def _p(self, path): return f"/api/runner/jobs/{self.job_id}{path}"
    def ctx(self): r = self.c.get(self._p("/context")); r.raise_for_status(); return r.json()

    def event(self, etype, data=None):
        try:
            r = self.c.post(self._p("/events"), json={"type": etype, "data": data or {}})
            return r.json().get("cancel_requested", False) if r.status_code == 200 else False
        except httpx.HTTPError:
            return False

    def status(self, status, error=None, stage=None):
        for _ in range(3):
            try:
                self.c.post(self._p("/status"), json={"status": status, "error": error, "stage": stage}).raise_for_status(); return
            except httpx.HTTPError:
                time.sleep(2)

    def post(self, path, body):
        r = self.c.post(self._p(path), json=body); r.raise_for_status(); return r.json()

    def _abs(self, url): return url if url.startswith("http") else self.url + url

    def download(self, url, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", self._abs(url), timeout=None, follow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for ch in r.iter_bytes(1 << 20): f.write(ch)

    def upload(self, put_url, path: Path, mime="application/octet-stream"):
        def gen():
            with open(path, "rb") as f:
                while ch := f.read(1 << 20): yield ch
        r = httpx.put(self._abs(put_url), content=gen(), headers={"Content-Type": mime, "Content-Length": str(path.stat().st_size)}, timeout=None)
        r.raise_for_status()


def hardware() -> dict:
    gpu = None
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
        gpu = r.stdout.strip() or None if r.returncode == 0 else None
    return {"gpu": gpu, "cpus": os.cpu_count(), "execution_path": "gpu" if gpu else "cpu"}


def build_prompt(ctx: dict, uploads: list[dict], resumed: bool) -> str:
    pid = ctx["project"]["id"]; g = ctx["general"]
    msgs = ctx["messages"]
    current = next((m for m in msgs if m["id"] == ctx["message_id"]), msgs[-1])
    parts = [RULES, f"\nProject id: {pid}  (project dir: projects/{pid})",
             f"Defaults: {g['resolution']} {g['aspect_ratio']} {g['fps']}fps {g['output_format']}. Preferences: {g.get('production_preferences') or 'none'}"]
    if uploads:
        parts.append("\nUploaded files (already on disk; inspect before planning):")
        for u in uploads:
            m = u.get("probe") or {}
            extra = f" [{m.get('duration', 0):.1f}s {m.get('width') or ''}x{m.get('height') or ''} audio={m.get('has_audio')}]" if m.get("duration") else ""
            parts.append(f"- {u['path']} ({u['kind']}){extra}")
    if not resumed and len(msgs) > 1:
        parts.append("\nConversation so far (session could not be restored; use this as context):")
        for m in msgs[:-1][-30:]:
            parts.append(f"{m['role'].upper()}: {m['content'][:1500]}")
    parts.append(f"\nUser request:\n{current['content']}")
    return "\n".join(parts)


def extract_text(ev: dict) -> str:
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    if ev.get("type") == "text":
        return str(part.get("text") or ev.get("text") or "")
    return ""


class Run:
    def __init__(self):
        self.job_id = os.environ["JOB_ID"]
        self.be = Backend(os.environ["BACKEND_URL"], self.job_id, os.environ["ORCH_RUNNER_SECRET"])
        self.proc: subprocess.Popen | None = None
        self.cancel = threading.Event()
        self.stop_hb = threading.Event()
        self.started = time.time()
        self.oc_deadline: float | None = None
        self.timed_out = False

    def heartbeat(self):
        while not self.stop_hb.wait(float(os.environ.get("HEARTBEAT_SECONDS", "20"))):
            if self.be.event("heartbeat", {"elapsed": int(time.time() - self.started)}):
                self.cancel.set()
            if self.cancel.is_set() and self.proc and self.proc.poll() is None:
                self.proc.terminate()
            if self.oc_deadline and time.time() > self.oc_deadline and self.proc and self.proc.poll() is None:
                self.timed_out = True; self.proc.terminate()

    def on_signal(self, *_):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run_opencode(self, prompt: str, session: str | None, ctx: dict) -> dict:
        g = ctx["general"]
        cmd = [*OPENCODE, "run", "--format", "json", "--auto", "--dir", str(REPO), "--title", f"job {self.job_id}"]
        if session: cmd += ["--session", session]
        if g.get("opencode_model"): cmd += ["-m", g["opencode_model"]]
        if g.get("opencode_agent"): cmd += ["--agent", g["opencode_agent"]]
        cmd.append(prompt)
        self.oc_deadline = time.time() + float(os.environ.get("OPENCODE_TIMEOUT_SECONDS", "10800"))
        self.proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        err_buf: list[str] = []
        threading.Thread(target=lambda: err_buf.extend(self.proc.stderr), daemon=True).start()
        sid, texts, errors = None, [], []
        for line in self.proc.stdout:
            line = line.strip()
            if not line: continue
            try: ev = json.loads(line)
            except json.JSONDecodeError:
                self.be.event("log", {"line": line[:500]}); continue
            sid = ev.get("sessionID") or sid
            t = extract_text(ev)
            if t: texts.append(t); self.be.event("assistant_text", {"text": t[:2000]})
            if ev.get("type") == "error":
                errors.append(json.dumps(ev.get("error"))[:500]); self.be.event("opencode_error", {"error": ev.get("error")})
            else:
                self.be.event("opencode", {"kind": ev.get("type"), "summary": json.dumps(ev)[:400]})
        rc = self.proc.wait(); self.oc_deadline = None
        if self.timed_out:
            raise RuntimeError(f"OpenCode timed out after {os.environ.get('OPENCODE_TIMEOUT_SECONDS', '10800')}s and was stopped")
        return {"rc": rc, "session_id": sid, "text": "".join(texts).strip(), "errors": errors, "stderr": "".join(err_buf)[-1500:]}

    def snapshot(self, pid: str, sid: str | None):
        """Persist resumable state. Always attempted, even after failure/cancel."""
        try:
            pdir = REPO / "projects" / pid
            if pdir.is_dir():
                tmp = Path(tempfile.mkdtemp()) / "state.tar.gz"
                skip = {"node_modules", ".cache", "__pycache__"}
                def filt(ti):
                    parts = Path(ti.name).parts
                    if skip & set(parts) or "uploads" in parts[1:3]: return None
                    return ti
                with tarfile.open(tmp, "w:gz") as tf: tf.add(pdir, arcname=f"projects/{pid}", filter=filt)
                r = self.be.post("/checkpoint/presign", {"kind": "project_state"})
                self.be.upload(r["put_url"], tmp)
                self.be.post("/checkpoint/commit", {"kind": "project_state", "key": r["key"]})
            if sid:
                x = subprocess.run([*OPENCODE, "export", sid], capture_output=True, text=True, cwd=REPO, timeout=120)
                if x.returncode == 0 and x.stdout.strip().startswith(("{", "[")):
                    f = Path(tempfile.mkdtemp()) / "session.json"; f.write_text(x.stdout)
                    r = self.be.post("/checkpoint/presign", {"kind": "opencode_session"})
                    self.be.upload(r["put_url"], f)
                    self.be.post("/checkpoint/commit", {"kind": "opencode_session", "key": r["key"], "opencode_session_id": sid})
                else:
                    self.be.event("warning", {"msg": "could not export OpenCode session; context will be rebuilt from message history", "detail": x.stderr[-300:]})
        except Exception as e:  # noqa: BLE001
            self.be.event("warning", {"msg": f"checkpoint failed: {e}"})

    def collect_outputs(self, pid: str, general: dict) -> list[dict]:
        rdir = REPO / "projects" / pid / "renders"
        results = []
        if not rdir.is_dir(): return results
        for f in sorted(rdir.rglob("*")):
            if f.suffix.lower() not in {".mp4", ".mov", ".webm"} or f.stat().st_mtime < self.started: continue
            qc = qc_video(f, general.get("resolution"))
            self.be.event("qc", {"file": f.name, **{k: qc[k] for k in ("ok", "errors", "warnings")}})
            meta = {**qc["meta"], "qc_ok": qc["ok"], "qc_warnings": qc["warnings"], "qc_errors": qc["errors"], "aspect_ratio": general.get("aspect_ratio")}
            mime = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}[f.suffix[1:].lower()]
            r = self.be.post("/outputs", {"name": f.name, "mime": mime, "kind": "video", "meta": meta})
            self.be.upload(r["put_url"], f, mime)
            self.be.c.post(self.be._p(f"/outputs/{r['asset_id']}/complete")).raise_for_status()
            results.append({"file": f, "qc": qc})
        return results

    def collect_plan(self, pid: str) -> dict | None:
        """Validate and persist the AI-written storyboard so Backlot can show it and the next runner can resume from it."""
        pdir = REPO / "projects" / pid; f = pdir / "artifacts" / "storyboard.json"
        if not f.is_file() or f.stat().st_mtime < self.started: return None
        v = validate_file(f, pdir)
        self.be.event("storyboard", {"ok": v["ok"], "errors": v["errors"], "warnings": v["warnings"], "scenes": v["scenes"], "duration": v["duration"]})
        r = self.be.post("/outputs", {"name": "storyboard.json", "mime": "application/json", "kind": "plan", "meta": v})
        self.be.upload(r["put_url"], f, "application/json")
        self.be.c.post(self.be._p(f"/outputs/{r['asset_id']}/complete")).raise_for_status()
        return v

    def needs_input(self, pid: str) -> str | None:
        f = REPO / "projects" / pid / "artifacts" / "needs_input.json"
        if not f.is_file() or f.stat().st_mtime < self.started: return None
        try: q = json.loads(f.read_text())
        except Exception: return None
        text = str(q.get("question", "")).strip()
        if text and q.get("options"): text += "\nOptions: " + ", ".join(map(str, q["options"]))
        return text or None

    def main(self) -> int:
        for s in (signal.SIGTERM, signal.SIGINT): signal.signal(s, self.on_signal)
        be = self.be
        be.status("running", stage="restoring state")
        be.event("runner_started", {"hardware": hardware(), "opencode": OPENCODE})
        if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY"):
            be.event("run_info", {"run_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"})
        threading.Thread(target=self.heartbeat, daemon=True).start()
        pid, sid = None, None
        try:
            ctx = be.ctx(); pid = ctx["project"]["id"]; sid = ctx["conversation"]["opencode_session_id"]
            if ctx["job"]["cancel_requested"] or ctx["job"]["status"] in ("cancelled", "completed", "failed"):
                be.event("runner_exit", {"reason": "job was cancelled or finished before the runner started"})
                if ctx["job"]["status"] not in ("cancelled", "completed", "failed"):
                    be.status("cancelled", stage="cancelled before start")  # never leave a cancelled job 'running'
                return 0
            if ctx["state_url"]:
                tmp = Path(tempfile.mkdtemp()) / "state.tar.gz"; be.download(ctx["state_url"], tmp)
                with tarfile.open(tmp) as tf: tf.extractall(REPO, filter="data")
                be.event("restored", {"what": "project_state"})
            uploads = []
            for a in ctx["assets"]:
                if a["source"] != "upload": continue
                dest = REPO / "projects" / pid / "inputs" / "uploads" / f"{a['id'][-8:]}_{a['name']}"
                be.download(a["url"], dest)
                info = {"path": str(dest.relative_to(REPO)), "kind": a["kind"]}
                if a["kind"] in ("video", "audio"): info["probe"] = probe(dest)
                uploads.append(info)
            if uploads: be.event("restored", {"what": "uploads", "count": len(uploads)})
            resumed = False
            if ctx["session_url"] and sid:
                f = Path(tempfile.mkdtemp()) / "session.json"; be.download(ctx["session_url"], f)
                imp = subprocess.run([*OPENCODE, "import", str(f)], capture_output=True, text=True, cwd=REPO, timeout=120)
                resumed = imp.returncode == 0
                be.event("restored" if resumed else "warning", {"what": "opencode_session", "ok": resumed, "detail": imp.stderr[-200:]})
            be.status("running", stage="opencode working")
            res = self.run_opencode(build_prompt(ctx, uploads, resumed), sid if resumed else None, ctx)
            if resumed and res["rc"] != 0 and not res["text"]:
                be.event("warning", {"msg": "resume failed; retrying with history in prompt"})
                res = self.run_opencode(build_prompt(ctx, uploads, False), None, ctx)
            sid = res["session_id"] or sid
            if self.cancel.is_set():
                be.status("cancelled", stage="cancelled"); self.snapshot(pid, sid); return 0
            if res["rc"] != 0 and not res["text"]:
                raise RuntimeError(f"OpenCode exited {res['rc']}: {(res['errors'] or [res['stderr']])[0][:600]}")
            be.status("running", stage="checking storyboard")
            plan = self.collect_plan(pid)
            if plan and not plan["ok"] and not self.cancel.is_set():
                be.event("plan_fix_attempt", {"errors": plan["errors"][:10]})
                fixp = (f"Project id: {pid}  (project dir: projects/{pid})\nUser request:\nThe storyboard failed validation (python -m director validate projects/{pid}):\n"
                        + "\n".join(plan["errors"][:20]) + "\nFix artifacts/storyboard.json. Do not fabricate screen recordings; use real uploaded/captured recordings.")
                res["text"] += "\n\n" + self.run_opencode(fixp, sid, ctx)["text"]
                plan = self.collect_plan(pid)
            be.status("running", stage="quality control")
            outs = self.collect_outputs(pid, ctx["general"])
            bad = [o for o in outs if not o["qc"]["ok"]]
            if bad and not self.cancel.is_set():
                be.event("qc_fix_attempt", {"files": [o["file"].name for o in bad]})
                fix = f"Project id: {pid}  (project dir: projects/{pid})\nUser request:\nAutomated QC failed:\n" + "\n".join(f"{o['file'].name}: {o['qc']['errors']}" for o in bad) + "\nFix the cause and re-render."
                res2 = self.run_opencode(fix, sid, ctx); res["text"] += "\n\n" + res2["text"]
                outs = self.collect_outputs(pid, ctx["general"])
            final = res["text"] or "(OpenCode returned no text.)"
            if plan: final += f"\n\nStoryboard: {plan['scenes']} scenes, {plan['duration']}s" + ("" if plan["ok"] else " (STILL INVALID: " + "; ".join(plan["errors"][:3]) + ")")
            ask = self.needs_input(pid)
            if ask:
                final += f"\n\nI need your input: {ask}"; be.event("needs_input", {"question": ask})
            if outs:
                final += "\n\nRendered: " + ", ".join(o["file"].name + ("" if o["qc"]["ok"] else " (QC FAILED)") for o in outs)
            be.post("/messages", {"content": final})
            self.snapshot(pid, sid)
            be.status("completed", stage="done")
            return 0
        except Exception as e:  # noqa: BLE001
            be.event("error", {"message": str(e)[:1000]})
            if pid: self.snapshot(pid, sid)
            be.status("failed", error=str(e)[:1500])
            return 1
        finally:
            self.stop_hb.set()


if __name__ == "__main__":
    sys.exit(Run().main())
