"""Verify the execution environment inside the container; optionally report to the backend."""
from __future__ import annotations

import hashlib, hmac, json, os, shutil, subprocess, sys

import httpx


def ver(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
    except Exception:
        return None


def collect() -> dict:
    gpu = ver(["nvidia-smi", "-L"]) if shutil.which("nvidia-smi") else None
    d = shutil.disk_usage("/work" if os.path.isdir("/work") else ".")
    return {"python": sys.version.split()[0], "node": ver(["node", "--version"]), "ffmpeg": ver(["ffmpeg", "-version"]),
            "ffprobe": ver(["ffprobe", "-version"]), "opencode": ver(["opencode", "--version"]), "git": ver(["git", "--version"]),
            "gpu": gpu, "execution_path": "gpu" if gpu else "cpu", "cpus": os.cpu_count(),
            "disk_free_gb": round(d.free / 1e9, 1), "run_url": os.environ.get("GITHUB_RUN_ID") and
            f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"}


def main() -> int:
    h = collect()
    print(json.dumps(h, indent=2))
    missing = [k for k in ("node", "ffmpeg", "ffprobe", "opencode", "git") if not h[k]]
    h["ok"], h["missing"] = not missing, missing
    url, secret = os.environ.get("BACKEND_URL", ""), os.environ.get("ORCH_RUNNER_SECRET", "")
    if url and secret:
        sig = hmac.new(secret.encode(), b"runner:health", hashlib.sha256).hexdigest()
        try:
            r = httpx.post(url.rstrip("/") + "/api/runner/health", json=h, headers={"X-Runner-Signature": sig}, timeout=30)
            print("reported to backend:", r.status_code)
        except httpx.HTTPError as e:
            print("could not report to backend:", e)
    else:
        print("BACKEND_URL / ORCH_RUNNER_SECRET not set; not reporting")
    if missing: print("MISSING:", missing, file=sys.stderr)
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
