"""HTTP API. Browser routes: /api/v1/*  (owner auth).  Runner routes: /api/runner/*  (per-job HMAC)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import dispatch as dsp
from . import secrets_gh
from .auth import COOKIE, SESSION_TTL, make_session, require_runner, require_user
from .config import classify_upload
from .db import TERMINAL, new_id
from .notify import notify

router = APIRouter()
ACTIVE = ("queued", "starting", "running", "waiting_for_input")
GENERAL_KEYS = {"resolution": str, "aspect_ratio": str, "fps": int, "output_format": str,
                "opencode_model": str, "opencode_agent": str, "production_preferences": str}


def _d(request): return request.app.state.db
def _cfg(request): return request.app.state.cfg
def _st(request): return request.app.state.storage


def _backend_url(request: Request) -> str:
    return (_cfg(request).public_url or str(request.base_url)).rstrip("/")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:120] or "file"


def _need(row, what="not found"):
    if not row:
        raise HTTPException(404, what)
    return row


# ---------------- auth ----------------
class Login(BaseModel):
    token: str


@router.post("/api/v1/auth/login")
def login(body: Login, request: Request):
    import hmac
    cfg = _cfg(request)
    if not cfg.admin_token or not hmac.compare_digest(body.token, cfg.admin_token):
        raise HTTPException(401, "invalid token")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, make_session(cfg.session_secret), max_age=SESSION_TTL, httponly=True,
                    samesite="strict", secure=request.url.scheme == "https")
    return resp


@router.post("/api/v1/auth/logout")
def logout():
    resp = JSONResponse({"ok": True}); resp.delete_cookie(COOKIE); return resp


@router.get("/api/v1/me")
def me(user=Depends(require_user)):
    return {"user": user}


# ---------------- projects / conversations ----------------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


@router.get("/api/v1/projects")
def list_projects(request: Request, archived: bool = False, user=Depends(require_user)):
    return _d(request).q("SELECT * FROM projects WHERE archived=? ORDER BY updated_at DESC", (int(archived),))


@router.post("/api/v1/projects")
def create_project(body: ProjectIn, request: Request, user=Depends(require_user)):
    db, now = _d(request), time.time()
    pid = new_id("prj")
    db.x("INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)", (pid, body.name, now, now))
    return db.one("SELECT * FROM projects WHERE id=?", (pid,))


@router.patch("/api/v1/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch, request: Request, user=Depends(require_user)):
    db = _d(request); _need(db.one("SELECT 1 FROM projects WHERE id=?", (pid,)))
    if body.name is not None:
        db.x("UPDATE projects SET name=?,updated_at=? WHERE id=?", (body.name, time.time(), pid))
    if body.archived is not None:
        db.x("UPDATE projects SET archived=?,updated_at=? WHERE id=?", (int(body.archived), time.time(), pid))
    return db.one("SELECT * FROM projects WHERE id=?", (pid,))


@router.get("/api/v1/projects/{pid}/conversations")
def list_conversations(pid: str, request: Request, user=Depends(require_user)):
    return _d(request).q("SELECT id,project_id,title,created_at FROM conversations WHERE project_id=? ORDER BY created_at DESC", (pid,))


class ConvIn(BaseModel):
    title: str = "New conversation"


@router.post("/api/v1/projects/{pid}/conversations")
def create_conversation(pid: str, body: ConvIn, request: Request, user=Depends(require_user)):
    db = _d(request); _need(db.one("SELECT 1 FROM projects WHERE id=?", (pid,)), "project not found")
    cid = new_id("cnv")
    db.x("INSERT INTO conversations(id,project_id,title,created_at) VALUES(?,?,?,?)", (cid, pid, body.title[:120], time.time()))
    return db.one("SELECT id,project_id,title,created_at FROM conversations WHERE id=?", (cid,))


@router.get("/api/v1/conversations/{cid}/messages")
def list_messages(cid: str, request: Request, user=Depends(require_user)):
    rows = _d(request).q("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (cid,))
    for r in rows:
        r["attachments"] = json.loads(r["attachments"] or "[]")
    return rows


# ---------------- chat -> job ----------------
class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    asset_ids: list[str] = []


def _create_job(db, project_id, conversation_id, message_id, parent=None, attempt=1) -> dict:
    jid = new_id("job")
    db.x("INSERT INTO jobs(id,project_id,conversation_id,message_id,status,attempt,parent_job_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
         (jid, project_id, conversation_id, message_id, "queued", attempt, parent, time.time()))
    db.add_event(jid, "queued", {"attempt": attempt})
    return db.one("SELECT * FROM jobs WHERE id=?", (jid,))


@router.post("/api/v1/conversations/{cid}/messages")
def send_message(cid: str, body: MessageIn, request: Request, user=Depends(require_user)):
    db = _d(request)
    conv = _need(db.one("SELECT * FROM conversations WHERE id=?", (cid,)), "conversation not found")
    if db.one(f"SELECT id FROM jobs WHERE conversation_id=? AND status IN ({','.join('?'*len(ACTIVE))})", (cid, *ACTIVE)):
        raise HTTPException(409, "a task is already active in this conversation; wait for it or cancel it")
    attachments = []
    for aid in body.asset_ids:
        a = _need(db.one("SELECT id,name,kind,size,status,project_id FROM assets WHERE id=?", (aid,)), f"asset {aid} not found")
        if a["project_id"] != conv["project_id"] or a["status"] != "uploaded":
            raise HTTPException(400, f"asset {aid} is not an uploaded asset of this project")
        db.x("UPDATE assets SET conversation_id=? WHERE id=?", (cid, aid))
        attachments.append({k: a[k] for k in ("id", "name", "kind", "size")})
    mid = new_id("msg")
    db.x("INSERT INTO messages(id,conversation_id,role,content,attachments,created_at) VALUES(?,?,?,?,?,?)",
         (mid, cid, "user", body.content, json.dumps(attachments), time.time()))
    if conv["title"] == "New conversation":
        db.x("UPDATE conversations SET title=? WHERE id=?", (body.content[:60], cid))
    job = _create_job(db, conv["project_id"], cid, mid)
    db.x("UPDATE messages SET job_id=? WHERE id=?", (job["id"], mid))
    db.x("UPDATE projects SET updated_at=? WHERE id=?", (time.time(), conv["project_id"]))
    dsp.dispatch(_cfg(request), db, job, _backend_url(request))
    return {"message_id": mid, "job": db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))}


# ---------------- jobs ----------------
@router.get("/api/v1/jobs")
def list_jobs(request: Request, project_id: str | None = None, status: str | None = None, user=Depends(require_user)):
    sql, args = "SELECT * FROM jobs WHERE 1=1", []
    if project_id: sql += " AND project_id=?"; args.append(project_id)
    if status: sql += " AND status=?"; args.append(status)
    return _d(request).q(sql + " ORDER BY created_at DESC LIMIT 200", tuple(args))


@router.get("/api/v1/jobs/{jid}")
def get_job(jid: str, request: Request, user=Depends(require_user)):
    return _need(_d(request).one("SELECT * FROM jobs WHERE id=?", (jid,)))


@router.post("/api/v1/jobs/{jid}/cancel")
def cancel_job(jid: str, request: Request, user=Depends(require_user)):
    db = _d(request); job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    if job["status"] in TERMINAL:
        return job
    db.x("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
    db.add_event(jid, "cancel_requested", {})
    if job["status"] == "queued" or not job["started_at"]:
        dsp.cancel_github_run(_cfg(request), job["github_run_url"])
        db.set_job_status(jid, "cancelled", stage="cancelled before start")
    return db.one("SELECT * FROM jobs WHERE id=?", (jid,))


@router.post("/api/v1/jobs/{jid}/retry")
def retry_job(jid: str, request: Request, user=Depends(require_user)):
    db = _d(request); job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    if job["status"] not in ("failed", "cancelled"):
        raise HTTPException(409, "only failed or cancelled jobs can be retried")
    nj = _create_job(db, job["project_id"], job["conversation_id"], job["message_id"], parent=jid, attempt=job["attempt"] + 1)
    dsp.dispatch(_cfg(request), db, nj, _backend_url(request))
    return db.one("SELECT * FROM jobs WHERE id=?", (nj["id"],))


def _events_after(db, jid, after):
    return db.q("SELECT seq,ts,type,data FROM job_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 500", (jid, after))


@router.get("/api/v1/jobs/{jid}/events")
async def job_events(jid: str, request: Request, after: int = 0, user=Depends(require_user)):
    """SSE, resumable via ?after=<seq> or the Last-Event-ID header. Only real, persisted events are sent."""
    db = _d(request); _need(db.one("SELECT 1 FROM jobs WHERE id=?", (jid,)))
    last = max(after, int(request.headers.get("last-event-id", "0") or 0))

    async def stream():
        nonlocal last
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            rows = await asyncio.to_thread(_events_after, db, jid, last)
            for r in rows:
                last = r["seq"]; idle = 0
                yield f"id: {r['seq']}\nevent: {r['type']}\ndata: {json.dumps({'seq': r['seq'], 'ts': r['ts'], 'type': r['type'], 'data': json.loads(r['data'] or '{}')})}\n\n"
            if not rows:
                job = await asyncio.to_thread(db.one, "SELECT status FROM jobs WHERE id=?", (jid,))
                if job and job["status"] in TERMINAL:
                    yield "event: end\ndata: {}\n\n"; return
                idle += 1
                if idle % 15 == 0:
                    yield ": heartbeat\n\n"
            await asyncio.sleep(0.5 if rows else 1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/v1/jobs/{jid}/events.json")
def job_events_json(jid: str, request: Request, after: int = 0, user=Depends(require_user)):
    """Polling fallback for clients without SSE."""
    rows = _events_after(_d(request), jid, after)
    return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "data": json.loads(r["data"] or "{}")} for r in rows]


# ---------------- uploads / assets ----------------
class UploadIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    mime: str = "application/octet-stream"


@router.post("/api/v1/projects/{pid}/uploads")
def start_upload(pid: str, body: UploadIn, request: Request, user=Depends(require_user)):
    db, cfg = _d(request), _cfg(request)
    _need(db.one("SELECT 1 FROM projects WHERE id=?", (pid,)), "project not found")
    kind = classify_upload(body.name)
    if not kind:
        raise HTTPException(415, f"unsupported file type: {Path(body.name).suffix or body.name}")
    if body.size > cfg.max_upload_bytes:
        raise HTTPException(413, f"file too large (max {cfg.max_upload_bytes} bytes)")
    aid = new_id("ast")
    key = f"projects/{pid}/uploads/{aid}/{_safe_name(body.name)}"
    db.x("INSERT INTO assets(id,project_id,name,kind,mime,size,storage_key,source,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
         (aid, pid, _safe_name(body.name), kind, body.mime, body.size, key, "upload", "pending", time.time()))
    return {"asset_id": aid, "kind": kind, "put_url": _st(request).presign_put(key, body.mime), "storage_key": key}


@router.post("/api/v1/assets/{aid}/complete")
def complete_upload(aid: str, request: Request, user=Depends(require_user)):
    db = _d(request); a = _need(db.one("SELECT * FROM assets WHERE id=?", (aid,)))
    head = _st(request).head(a["storage_key"])
    if not head:
        raise HTTPException(409, "object not found in storage; upload did not finish")
    if head["size"] != a["size"]:
        raise HTTPException(409, f"size mismatch: declared {a['size']}, stored {head['size']}")
    db.x("UPDATE assets SET status='uploaded' WHERE id=?", (aid,))
    return db.one("SELECT * FROM assets WHERE id=?", (aid,))


@router.get("/api/v1/projects/{pid}/assets")
def list_assets(pid: str, request: Request, source: str | None = None, user=Depends(require_user)):
    sql, args = "SELECT id,name,kind,mime,size,source,status,scene_id,meta,created_at FROM assets WHERE project_id=? AND status='uploaded'", [pid]
    if source: sql += " AND source=?"; args.append(source)
    rows = _d(request).q(sql + " ORDER BY created_at DESC", tuple(args))
    for r in rows: r["meta"] = json.loads(r["meta"] or "{}")
    return rows


@router.get("/api/v1/assets/{aid}/url")
def asset_url(aid: str, request: Request, user=Depends(require_user)):
    a = _need(_d(request).one("SELECT storage_key,status FROM assets WHERE id=?", (aid,)))
    if a["status"] != "uploaded":
        raise HTTPException(409, "asset not available")
    return {"url": _st(request).presign_get(a["storage_key"])}


# ---------------- local-storage endpoints (dev/tests; S3 uses presigned URLs directly) ----------------
@router.put("/api/v1/storage/put")
async def local_put(key: str, exp: int, sig: str, request: Request):
    st = _st(request)
    if st.kind != "local" or not st.verify("put", key, exp, sig):
        raise HTTPException(403, "invalid or expired signature")
    n = 0
    tmp = st.open_path(key).with_suffix(".part"); tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk); n += len(chunk)
    tmp.replace(st.open_path(key))
    return {"size": n}


@router.get("/api/v1/storage/get")
def local_get(key: str, exp: int, sig: str, request: Request):
    st = _st(request)
    if st.kind != "local" or not st.verify("get", key, exp, sig):
        raise HTTPException(403, "invalid or expired signature")
    p = st.open_path(key)
    if not p.is_file():
        raise HTTPException(404, "object not found")
    return FileResponse(p)


# ---------------- settings ----------------
@router.get("/api/v1/settings/status")
def settings_status(request: Request, user=Depends(require_user)):
    cfg, db = _cfg(request), _d(request)
    health = db.one("SELECT value FROM settings WHERE key='runner_health'")
    return {
        "github": {"configured": cfg.github_configured(), "repo": cfg.gh_repo, "ref": cfg.gh_ref, "workflow": cfg.gh_workflow,
                   "dispatch_mode": cfg.dispatch_mode, "token_present": bool(cfg.gh_token)},
        "storage": _st(request).status(),
        "database": {"ok": bool(db.one("SELECT 1 AS ok")), "engine": "sqlite"},
        "auth": {"admin_token_set": bool(cfg.admin_token), "runner_secret_set": bool(cfg.runner_secret)},
        "notifications": {"telegram_configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id)},
        "runner_health": json.loads(health["value"]) if health else None,
        "general": _general(db),
    }


def _general(db) -> dict:
    row = db.one("SELECT value FROM settings WHERE key='general'")
    base = {"resolution": "1920x1080", "aspect_ratio": "16:9", "fps": 30, "output_format": "mp4",
            "opencode_model": "", "opencode_agent": "", "production_preferences": ""}
    return {**base, **(json.loads(row["value"]) if row else {})}


@router.patch("/api/v1/settings/general")
def patch_general(body: dict, request: Request, user=Depends(require_user)):
    db = _d(request); cur = _general(db)
    for k, v in body.items():
        if k not in GENERAL_KEYS or not isinstance(v, GENERAL_KEYS[k]) or isinstance(v, bool):
            raise HTTPException(400, f"invalid setting: {k}")
        if k == "fps" and not 1 <= v <= 120: raise HTTPException(400, "fps must be 1-120")
        if k == "resolution" and not re.fullmatch(r"\d{3,4}x\d{3,4}", v): raise HTTPException(400, "resolution must look like 1920x1080")
        if k == "opencode_model" and v and not re.fullmatch(r"[\w.-]+/[\w.:/-]+", v): raise HTTPException(400, "model must be provider/model")
        cur[k] = v
    db.x("INSERT INTO settings(key,value) VALUES('general',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cur),))
    return cur


@router.get("/api/v1/settings/github-secrets")
def github_secret_names(request: Request, user=Depends(require_user)):
    cfg = _cfg(request)
    if not cfg.github_configured():
        raise HTTPException(409, "GitHub not configured")
    return {"present": secrets_gh.list_secret_names(cfg), "settable": sorted(secrets_gh.ALLOWED_SECRET_NAMES)}


class SecretIn(BaseModel):
    name: str
    value: str = Field(min_length=1, max_length=8192)


@router.put("/api/v1/settings/github-secrets")
def set_github_secret(body: SecretIn, request: Request, user=Depends(require_user)):
    cfg = _cfg(request)
    if not cfg.github_configured():
        raise HTTPException(409, "GitHub not configured")
    try:
        secrets_gh.put_secret(cfg, body.name, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "name": body.name}  # value is never echoed or stored


# ---------------- recovery ----------------
def recover_stale(db, cfg, backend_url: str, stale_seconds: int = 300, max_attempts: int = 3) -> list[dict]:
    now, out = time.time(), []
    rows = db.q("SELECT * FROM jobs WHERE status IN ('starting','running')")
    for j in rows:
        last = db.one("SELECT MAX(ts) AS t FROM job_events WHERE job_id=?", (j["id"],))["t"] or j["created_at"]
        # a job that never got a runner gets longer (queue wait on GitHub) than one whose runner went silent
        limit = stale_seconds * (6 if j["status"] == "starting" else 1)
        if now - last < limit:
            continue
        db.set_job_status(j["id"], "failed", f"runner lost: no heartbeat for {int(now - last)}s")
        item = {"failed": j["id"], "retry": None}
        if j["attempt"] < max_attempts and not j["cancel_requested"]:
            nj = _create_job(db, j["project_id"], j["conversation_id"], j["message_id"], parent=j["id"], attempt=j["attempt"] + 1)
            dsp.dispatch(cfg, db, nj, backend_url)
            item["retry"] = nj["id"]
        out.append(item)
    return out


@router.post("/api/runner/recover")
def runner_recover(request: Request, stale_seconds: int = 300):
    require_runner("recover", request)
    return {"recovered": recover_stale(_d(request), _cfg(request), _backend_url(request), stale_seconds)}


@router.post("/api/runner/health")
async def runner_health(request: Request):
    require_runner("health", request)
    body = await request.json()
    _d(request).x("INSERT INTO settings(key,value) VALUES('runner_health',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps({**body, "reported_at": time.time()}),))
    return {"ok": True}


# ---------------- runner API ----------------
def _abs(request, url: str) -> str:
    return url if url.startswith("http") else _backend_url(request) + url


def _latest(db, cid_or_pid, kind, by="conversation_id"):
    return db.one(f"SELECT * FROM checkpoints WHERE {by}=? AND kind=? ORDER BY created_at DESC LIMIT 1", (cid_or_pid, kind))


@router.get("/api/runner/jobs/{jid}/context")
def runner_context(jid: str, request: Request):
    require_runner(jid, request)
    db, st = _d(request), _st(request)
    job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    conv = db.one("SELECT * FROM conversations WHERE id=?", (job["conversation_id"],))
    msgs = db.q("SELECT id,role,content,attachments,created_at FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (conv["id"],))
    for m in msgs: m["attachments"] = json.loads(m["attachments"] or "[]")
    assets = db.q("SELECT id,name,kind,mime,size,storage_key,source,scene_id FROM assets WHERE project_id=? AND status='uploaded'", (job["project_id"],))
    for a in assets: a["url"] = _abs(request, st.presign_get(a["storage_key"]))
    state, sess = _latest(db, job["project_id"], "project_state", "project_id"), _latest(db, conv["id"], "opencode_session")
    return {"job": job, "project": db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],)),
            "conversation": {"id": conv["id"], "opencode_session_id": conv["opencode_session_id"]},
            "messages": msgs, "assets": assets, "message_id": job["message_id"],
            "state_url": _abs(request, st.presign_get(state["storage_key"])) if state else None,
            "session_url": _abs(request, st.presign_get(sess["storage_key"])) if sess else None,
            "general": _general(db)}


class EventIn(BaseModel):
    type: str = Field(max_length=64)
    data: dict = {}


@router.post("/api/runner/jobs/{jid}/events")
def runner_event(jid: str, body: EventIn, request: Request):
    require_runner(jid, request)
    db = _d(request); job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    if body.type == "run_info" and body.data.get("run_url"):
        db.x("UPDATE jobs SET github_run_url=? WHERE id=?", (body.data["run_url"], jid))
    seq = db.add_event(jid, body.type, body.data)
    return {"seq": seq, "cancel_requested": bool(db.one("SELECT cancel_requested c FROM jobs WHERE id=?", (jid,))["c"])}


class StatusIn(BaseModel):
    status: str
    error: str | None = None
    stage: str | None = None


@router.post("/api/runner/jobs/{jid}/status")
def runner_status(jid: str, body: StatusIn, request: Request):
    require_runner(jid, request)
    if body.status not in ("running", "waiting_for_input", "completed", "failed", "cancelled"):
        raise HTTPException(400, "invalid status")
    db, cfg = _d(request), _cfg(request)
    _need(db.one("SELECT 1 FROM jobs WHERE id=?", (jid,)))
    changed = db.set_job_status(jid, body.status, body.error, body.stage)
    if changed and body.status in ("completed", "failed", "waiting_for_input"):
        notify(cfg, f"Job {jid}: {body.status}" + (f" — {body.error}" if body.error else ""))
    return {"changed": changed}


class AssistantMsg(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


@router.post("/api/runner/jobs/{jid}/messages")
def runner_message(jid: str, body: AssistantMsg, request: Request):
    require_runner(jid, request)
    db = _d(request); job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    mid = new_id("msg")
    db.x("INSERT INTO messages(id,conversation_id,role,content,job_id,created_at) VALUES(?,?,?,?,?,?)",
         (mid, job["conversation_id"], "assistant", body.content, jid, time.time()))
    db.add_event(jid, "assistant_message", {"message_id": mid})
    return {"message_id": mid}


class CkptIn(BaseModel):
    kind: str


@router.post("/api/runner/jobs/{jid}/checkpoint/presign")
def ckpt_presign(jid: str, body: CkptIn, request: Request):
    require_runner(jid, request)
    if body.kind not in ("project_state", "opencode_session"):
        raise HTTPException(400, "invalid checkpoint kind")
    job = _need(_d(request).one("SELECT * FROM jobs WHERE id=?", (jid,)))
    ext = "tar.gz" if body.kind == "project_state" else "json"
    key = f"projects/{job['project_id']}/checkpoints/{body.kind}/{jid}-{int(time.time())}.{ext}"
    return {"key": key, "put_url": _abs(request, _st(request).presign_put(key))}


class CkptCommit(BaseModel):
    kind: str
    key: str
    opencode_session_id: str | None = None


@router.post("/api/runner/jobs/{jid}/checkpoint/commit")
def ckpt_commit(jid: str, body: CkptCommit, request: Request):
    require_runner(jid, request)
    db = _d(request); job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    if not body.key.startswith(f"projects/{job['project_id']}/checkpoints/"):
        raise HTTPException(400, "key outside project")
    if not _st(request).head(body.key):
        raise HTTPException(409, "checkpoint object missing in storage")
    db.x("INSERT INTO checkpoints(id,project_id,conversation_id,job_id,kind,storage_key,created_at) VALUES(?,?,?,?,?,?,?)",
         (new_id("ckp"), job["project_id"], job["conversation_id"], jid, body.kind, body.key, time.time()))
    if body.opencode_session_id:
        db.x("UPDATE conversations SET opencode_session_id=? WHERE id=?", (body.opencode_session_id, job["conversation_id"]))
    db.add_event(jid, "checkpoint", {"kind": body.kind})
    return {"ok": True}


class OutputIn(BaseModel):
    name: str
    mime: str = "application/octet-stream"
    kind: str = "video"
    scene_id: str | None = None
    meta: dict = {}


@router.post("/api/runner/jobs/{jid}/outputs")
def output_start(jid: str, body: OutputIn, request: Request):
    require_runner(jid, request)
    db = _d(request); job = _need(db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    aid = new_id("ast"); key = f"projects/{job['project_id']}/outputs/{aid}/{_safe_name(body.name)}"
    db.x("INSERT INTO assets(id,project_id,conversation_id,name,kind,mime,size,storage_key,source,status,scene_id,meta,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (aid, job["project_id"], job["conversation_id"], _safe_name(body.name), body.kind, body.mime, 0, key, "generated", "pending", body.scene_id, json.dumps(body.meta), time.time()))
    return {"asset_id": aid, "put_url": _abs(request, _st(request).presign_put(key, body.mime))}


@router.post("/api/runner/jobs/{jid}/outputs/{aid}/complete")
def output_complete(jid: str, aid: str, request: Request):
    require_runner(jid, request)
    db = _d(request); a = _need(db.one("SELECT * FROM assets WHERE id=?", (aid,)))
    head = _st(request).head(a["storage_key"])
    if not head:
        raise HTTPException(409, "output object missing in storage")
    db.x("UPDATE assets SET status='uploaded', size=? WHERE id=?", (head["size"], aid))
    db.add_event(jid, "output_ready", {"asset_id": aid, "name": a["name"], "kind": a["kind"], "size": head["size"], "meta": json.loads(a["meta"] or "{}")})
    return {"ok": True, "size": head["size"]}
