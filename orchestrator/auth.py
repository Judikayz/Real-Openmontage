"""Owner auth (admin token -> signed, expiring session cookie) and per-job runner HMAC."""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Depends, HTTPException, Request

COOKIE = "om_session"
SESSION_TTL = 7 * 24 * 3600


def _sign(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, ttl: int = SESSION_TTL) -> str:
    exp = str(int(time.time()) + ttl)
    return f"{exp}.{_sign(secret, 'session:' + exp)}"


def check_session(secret: str, value: str) -> bool:
    try:
        exp, sig = value.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(secret, "session:" + exp))
    except Exception:
        return False


def runner_signature(runner_secret: str, job_id: str) -> str:
    return _sign(runner_secret, "runner:" + job_id)


def require_user(request: Request):
    cfg = request.app.state.cfg
    if not cfg.admin_token:
        raise HTTPException(503, "ORCH_ADMIN_TOKEN is not configured on the server")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:], cfg.admin_token):
        return "owner"
    cookie = request.cookies.get(COOKIE, "")
    if cookie and check_session(cfg.session_secret, cookie):
        return "owner"
    raise HTTPException(401, "authentication required")


def require_runner(job_id: str, request: Request):
    cfg = request.app.state.cfg
    if not cfg.runner_secret:
        raise HTTPException(503, "ORCH_RUNNER_SECRET is not configured on the server")
    sig = request.headers.get("x-runner-signature", "")
    if not hmac.compare_digest(sig, runner_signature(cfg.runner_secret, job_id)):
        raise HTTPException(401, "bad runner signature")
    return job_id
