"""Start a job: GitHub Actions workflow_dispatch (normal) or a local subprocess (dev/tests only)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
GH_API = "https://api.github.com"


def gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def dispatch_request(cfg, job: dict, backend_url: str) -> tuple[str, dict, dict]:
    """Build the workflow_dispatch request. No secret is ever placed in `inputs`."""
    url = f"{GH_API}/repos/{cfg.gh_repo}/actions/workflows/{cfg.gh_workflow}/dispatches"
    body = {"ref": cfg.gh_ref, "inputs": {"job_id": job["id"], "project_id": job["project_id"], "backend_url": backend_url}}
    return url, gh_headers(cfg.gh_token), body


def dispatch(cfg, db, job: dict, backend_url: str, transport: httpx.BaseTransport | None = None) -> None:
    """Never pretends: on any failure the job is marked failed with the real reason."""
    jid = job["id"]
    mode = cfg.dispatch_mode
    if mode == "none":
        db.add_event(jid, "dispatch_skipped", {"reason": "ORCH_DISPATCH_MODE=none; job stays queued"})
        return
    if mode == "local":
        import tempfile
        ws = tempfile.mkdtemp(prefix="om-runner-")  # fresh, empty workspace == a brand-new runner
        env = {**os.environ, "JOB_ID": jid, "BACKEND_URL": backend_url, "ORCH_RUNNER_SECRET": cfg.runner_secret,
               "OM_REPO_ROOT": ws, "PYTHONPATH": str(REPO_ROOT)}
        p = subprocess.Popen([sys.executable, "-m", "runner.run_job"], cwd=REPO_ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        db.set_job_status(jid, "starting", stage="waiting for runner")
        db.add_event(jid, "dispatched", {"mode": "local-subprocess (development only)", "pid": p.pid, "workspace": ws})
        return
    if not cfg.github_configured():
        db.set_job_status(jid, "failed", "GitHub dispatch not configured: set GITHUB_DISPATCH_REPO and GITHUB_DISPATCH_TOKEN on the backend")
        return
    url, headers, body = dispatch_request(cfg, job, backend_url)
    try:
        with httpx.Client(timeout=20, transport=transport) as c:
            r = c.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        db.set_job_status(jid, "failed", f"GitHub dispatch network error: {e}")
        return
    if r.status_code != 204:
        db.set_job_status(jid, "failed", f"GitHub dispatch rejected: HTTP {r.status_code} {r.text[:300]}")
        return
    db.set_job_status(jid, "starting", stage="waiting for runner")
    db.add_event(jid, "dispatched", {"mode": "github-actions", "workflow": cfg.gh_workflow, "ref": cfg.gh_ref})


def run_id_from_url(url: str | None) -> str | None:
    m = re.search(r"/actions/runs/(\d+)", url or "")
    return m.group(1) if m else None


def cancel_github_run(cfg, run_url: str | None, transport: httpx.BaseTransport | None = None) -> bool:
    rid = run_id_from_url(run_url)
    if not (rid and cfg.github_configured()):
        return False
    with httpx.Client(timeout=20, transport=transport) as c:
        r = c.post(f"{GH_API}/repos/{cfg.gh_repo}/actions/runs/{rid}/cancel", headers=gh_headers(cfg.gh_token))
    return r.status_code in (202, 409)
