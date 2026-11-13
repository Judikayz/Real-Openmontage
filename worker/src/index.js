// Backlot control plane on Cloudflare Workers: D1 (state) + R2 (files) + GitHub Actions dispatch.
// Heavy work (Docker, OpenCode, OpenMontage) never runs here; it runs on GitHub runners.
import { AwsClient } from "aws4fetch";
import RUNNER_SECRETS from "./runner_secrets.json";
import { nextRun, normalise, countOccurrences, describe, DEFAULT_TZ } from "./schedule.js";

const enc = new TextEncoder();
const TERMINAL = ["completed", "failed", "cancelled"];
const ACTIVE = ["queued", "starting", "running", "waiting_for_input"];
const UPLOAD_TYPES = {
  video: [".mp4", ".mov", ".webm", ".mkv", ".m4v"], audio: [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"],
  image: [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"], document: [".pdf", ".txt", ".md", ".json", ".csv", ".srt", ".vtt", ".docx"],
};
const GENERAL_KEYS = { resolution: "string", aspect_ratio: "string", fps: "number", output_format: "string", opencode_model: "string", opencode_agent: "string", production_preferences: "string" };
const SECRET_NAMES = ["ORCH_RUNNER_SECRET", ...RUNNER_SECRETS];  // shared allowlist: config/runner_secrets.txt

class HttpError extends Error { constructor(status, msg) { super(msg); this.status = status; } }
const now = () => Date.now() / 1000;
const newId = p => `${p}_${crypto.randomUUID().replace(/-/g, "").slice(0, 16)}`;
const json = (d, status = 200, headers = {}) => new Response(JSON.stringify(d), { status, headers: { "content-type": "application/json", ...headers } });
const need = (row, msg = "not found") => { if (!row) throw new HttpError(404, msg); return row; };
const safeName = n => (n.split(/[\\/]/).pop().replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 120)) || "file";
const ext = n => (n.lastIndexOf(".") >= 0 ? n.slice(n.lastIndexOf(".")).toLowerCase() : "");
const classify = n => Object.keys(UPLOAD_TYPES).find(k => UPLOAD_TYPES[k].includes(ext(n))) || null;

// ---------- crypto ----------
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
async function hmac(secret, msg) { const k = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]); return hex(await crypto.subtle.sign("HMAC", k, enc.encode(msg))); }
async function safeEq(a, b) { const [x, y] = await Promise.all([hmac("cmp", a || ""), hmac("cmp", b || "")]); return x === y; }

// ---------- db ----------
const q = async (env, sql, ...a) => (await env.DB.prepare(sql).bind(...a).all()).results;
const one = async (env, sql, ...a) => (await q(env, sql, ...a))[0] || null;
const run = async (env, sql, ...a) => env.DB.prepare(sql).bind(...a).run();
async function addEvent(env, jobId, type, data = {}) { const r = await run(env, "INSERT INTO job_events(job_id,ts,type,data) VALUES(?,?,?,?)", jobId, now(), type, JSON.stringify(data)); return r.meta.last_row_id; }
async function setStatus(env, id, status, error = null, stage = null) {
  const cur = await one(env, "SELECT status FROM jobs WHERE id=?", id);
  if (!cur || TERMINAL.includes(cur.status)) return false;
  const sets = ["status=?"], args = [status];
  if (status === "running") { sets.push("started_at=COALESCE(started_at,?)"); args.push(now()); }
  if (TERMINAL.includes(status)) { sets.push("finished_at=?"); args.push(now()); }
  if (error !== null) { sets.push("error=?"); args.push(String(error).slice(0, 4000)); }
  if (stage !== null) { sets.push("stage=?"); args.push(stage); }
  await run(env, `UPDATE jobs SET ${sets.join(",")} WHERE id=?`, ...args, id);
  await addEvent(env, id, "status", { status, error, stage });
  return true;
}

// ---------- auth ----------
const COOKIE = "om_session", TTL = 7 * 24 * 3600;
const sessSecret = env => env.SESSION_SECRET || env.ORCH_ADMIN_TOKEN;
async function makeSession(env) { const exp = String(Math.floor(now()) + TTL); return `${exp}.${await hmac(sessSecret(env), "session:" + exp)}`; }
async function checkSession(env, v) { const [exp, sig] = (v || "").split("."); if (!exp || !sig || +exp <= now()) return false; return safeEq(sig, await hmac(sessSecret(env), "session:" + exp)); }
const cookies = req => Object.fromEntries((req.headers.get("cookie") || "").split(";").map(c => c.trim().split(/=(.*)/s).slice(0, 2)).filter(c => c[0]));
async function requireUser(req, env) {
  if (!env.ORCH_ADMIN_TOKEN) throw new HttpError(503, "ORCH_ADMIN_TOKEN is not configured");
  const a = req.headers.get("authorization") || "";
  if (a.toLowerCase().startsWith("bearer ") && await safeEq(a.slice(7), env.ORCH_ADMIN_TOKEN)) return;
  const c = cookies(req)[COOKIE];
  if (c && await checkSession(env, c)) return;
  throw new HttpError(401, "authentication required");
}
async function requireRunner(req, env, id) {
  if (!env.ORCH_RUNNER_SECRET) throw new HttpError(503, "ORCH_RUNNER_SECRET is not configured");
  if (!await safeEq(req.headers.get("x-runner-signature") || "", await hmac(env.ORCH_RUNNER_SECRET, "runner:" + id))) throw new HttpError(401, "bad runner signature");
}

// ---------- storage (R2 binding; presigned S3 URLs when R2 keys exist, signed proxy URLs otherwise) ----------
const s3 = env => (env.S3_ACCESS_KEY_ID && env.S3_SECRET_ACCESS_KEY && env.R2_ACCOUNT_ID && env.R2_BUCKET_NAME)
  ? new AwsClient({ accessKeyId: env.S3_ACCESS_KEY_ID, secretAccessKey: env.S3_SECRET_ACCESS_KEY, service: "s3", region: "auto" }) : null;
const objUrl = (env, key) => `https://${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com/${env.R2_BUCKET_NAME}/${key.split("/").map(encodeURIComponent).join("/")}`;
async function presign(env, req, op, key, mime = "application/octet-stream", ttl = 3600) {
  const c = s3(env);
  if (c) {
    const headers = op === "put" ? { "Content-Type": mime } : {};
    const signed = await c.sign(new Request(`${objUrl(env, key)}?X-Amz-Expires=${ttl}`, { method: op === "put" ? "PUT" : "GET", headers }), { aws: { signQuery: true } });
    return signed.url;
  }
  const exp = Math.floor(now()) + ttl; const sig = await hmac(sessSecret(env), `${op}:${key}:${exp}`);
  return `${new URL(req.url).origin}/api/v1/storage/${op}?key=${encodeURIComponent(key)}&exp=${exp}&sig=${sig}`;
}
const head = async (env, key) => { const o = await env.BUCKET.head(key); return o ? { size: o.size } : null; };

// ---------- GitHub dispatch ----------
const ghHeaders = env => ({ Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "backlot-worker" });
async function rememberOrigin(env, req) {
  const o = new URL(req.url).origin; if (/^https?:\/\/(127\.0\.0\.1|localhost)/.test(o) && !env.PUBLIC_URL) { /* local dev: still remember it for cron tests */ }
  const cur = await one(env, "SELECT value FROM settings WHERE key='public_url'");
  if (!cur || cur.value !== o) await run(env, "INSERT INTO settings(key,value) VALUES('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", o);
}
async function resolveUrl(env) { if (env.PUBLIC_URL) return env.PUBLIC_URL.replace(/\/$/, ""); const r = await one(env, "SELECT value FROM settings WHERE key='public_url'"); return r ? r.value : ""; }
const backendUrl = (env, req) => (env.PUBLIC_URL || (req ? new URL(req.url).origin : "")).replace(/\/$/, "");
async function dispatch(env, job, url) {
  const mode = env.DISPATCH_MODE || "github";
  if (mode === "none") return addEvent(env, job.id, "dispatch_skipped", { reason: "DISPATCH_MODE=none; job stays queued" });
  if (mode === "webhook") {
    try {
      const r = await fetch(env.DISPATCH_WEBHOOK_URL, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ job_id: job.id, project_id: job.project_id, backend_url: url }) });
      if (!r.ok) return setStatus(env, job.id, "failed", `webhook dispatch rejected: HTTP ${r.status}`);
      await setStatus(env, job.id, "starting", null, "waiting for runner");
      return addEvent(env, job.id, "dispatched", { mode: "webhook (development)", ...(await r.json().catch(() => ({}))) });
    } catch (e) { return setStatus(env, job.id, "failed", `webhook dispatch error: ${e}`); }
  }
  if (!env.GITHUB_REPO || !env.GITHUB_DISPATCH_TOKEN) return setStatus(env, job.id, "failed", "GitHub dispatch not configured: set GITHUB_REPO and the GITHUB_DISPATCH_TOKEN secret on the Worker");
  try {
    const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`, {
      method: "POST", headers: ghHeaders(env), body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs: { job_id: job.id, project_id: job.project_id, backend_url: url } }) });
    if (r.status !== 204) return setStatus(env, job.id, "failed", `GitHub dispatch rejected: HTTP ${r.status} ${(await r.text()).slice(0, 300)}`);
  } catch (e) { return setStatus(env, job.id, "failed", `GitHub dispatch network error: ${e}`); }
  await setStatus(env, job.id, "starting", null, "waiting for runner");
  await addEvent(env, job.id, "dispatched", { mode: "github-actions", workflow: env.GITHUB_WORKFLOW, ref: env.GITHUB_REF });
}
async function cancelGithubRun(env, runUrl) {
  const m = /\/actions\/runs\/(\d+)/.exec(runUrl || ""); if (!m || !env.GITHUB_DISPATCH_TOKEN) return false;
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/runs/${m[1]}/cancel`, { method: "POST", headers: ghHeaders(env) });
  return [202, 409].includes(r.status);
}

// ---------- queue / recovery ----------
async function createJob(env, projectId, convId, messageId, parent = null, attempt = 1) {
  const id = newId("job");
  await run(env, "INSERT INTO jobs(id,project_id,conversation_id,message_id,status,attempt,parent_job_id,created_at) VALUES(?,?,?,?,?,?,?,?)", id, projectId, convId, messageId, "queued", attempt, parent, now());
  await addEvent(env, id, "queued", { attempt });
  return one(env, "SELECT * FROM jobs WHERE id=?", id);
}
async function drainQueue(env, url) {
  const max = parseInt(env.MAX_CONCURRENT_JOBS || "2", 10), started = [];
  if ((env.DISPATCH_MODE || "github") === "none") {
    for (const j of await q(env, "SELECT id FROM jobs WHERE status='queued'")) if (!await one(env, "SELECT 1 x FROM job_events WHERE job_id=? AND type='dispatch_skipped'", j.id)) await addEvent(env, j.id, "dispatch_skipped", { reason: "DISPATCH_MODE=none; job stays queued" });
    return started;
  }
  let active = (await one(env, "SELECT COUNT(*) c FROM jobs WHERE status IN ('starting','running')")).c;
  const queued = await q(env, "SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 AND id NOT IN (SELECT job_id FROM job_events WHERE type='dispatched') ORDER BY created_at LIMIT 6");
  for (const j of queued) {
    if (active >= max) { if (!await one(env, "SELECT 1 x FROM job_events WHERE job_id=? AND type='deferred'", j.id)) await addEvent(env, j.id, "deferred", { reason: `${active} job(s) already running (limit ${max}); will start automatically` }); continue; }
    await dispatch(env, j, url); started.push(j.id);
    if (["starting", "running"].includes((await one(env, "SELECT status FROM jobs WHERE id=?", j.id)).status)) active++;
  }
  return started;
}
async function recoverStale(env, url, staleSeconds = 300, maxAttempts = 3) {
  const out = [], t = now();
  for (const j of await q(env, "SELECT * FROM jobs WHERE status IN ('starting','running')")) {
    const last = (await one(env, "SELECT MAX(ts) t FROM job_events WHERE job_id=?", j.id)).t || j.created_at;
    if (t - last < staleSeconds * (j.status === "starting" ? 6 : 1)) continue;
    await setStatus(env, j.id, "failed", `runner lost: no heartbeat for ${Math.floor(t - last)}s`);
    const item = { failed: j.id, retry: null };
    if (j.attempt < maxAttempts && !j.cancel_requested) item.retry = (await createJob(env, j.project_id, j.conversation_id, j.message_id, j.id, j.attempt + 1)).id;
    out.push(item);
  }
  await drainQueue(env, url);
  return out;
}

// ---------- settings ----------
const GENERAL_DEFAULT = { resolution: "1920x1080", aspect_ratio: "16:9", fps: 30, output_format: "mp4", opencode_model: "", opencode_agent: "", production_preferences: "" };
async function general(env) { const r = await one(env, "SELECT value FROM settings WHERE key='general'"); return { ...GENERAL_DEFAULT, ...(r ? JSON.parse(r.value) : {}) }; }
async function listSecretNames(env) {
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/secrets?per_page=100`, { headers: ghHeaders(env) });
  if (!r.ok) throw new HttpError(502, `GitHub: HTTP ${r.status}`);
  return (await r.json()).secrets.map(s => s.name).sort();
}
async function putSecret(env, name, value) {
  if (!SECRET_NAMES.includes(name)) throw new HttpError(400, `secret name not allowed: ${name}`);
  const k = await (await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/secrets/public-key`, { headers: ghHeaders(env) })).json();
  const sodium = (await import("libsodium-wrappers")).default; await sodium.ready;
  const sealed = sodium.to_base64(sodium.crypto_box_seal(sodium.from_string(value), sodium.from_base64(k.key, sodium.base64_variants.ORIGINAL)), sodium.base64_variants.ORIGINAL);
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/secrets/${name}`, { method: "PUT", headers: ghHeaders(env), body: JSON.stringify({ encrypted_value: sealed, key_id: k.key_id }) });
  if (![201, 204].includes(r.status)) throw new HttpError(502, `GitHub: HTTP ${r.status}`);
}

// ---------- router ----------
const routes = [];
const on = (method, pattern, handler, auth = "user") => routes.push({ method, re: new RegExp("^" + pattern.replace(/:(\w+)/g, "(?<$1>[^/]+)") + "$"), handler, auth });
const body = async req => { try { return await req.json(); } catch { throw new HttpError(422, "invalid JSON body"); } };
const parseJson = s => { try { return JSON.parse(s || "{}"); } catch { return {}; } };

// auth
on("POST", "/api/v1/auth/login", async ({ req, env }) => {
  const b = await body(req);
  if (!env.ORCH_ADMIN_TOKEN || !await safeEq(b.token || "", env.ORCH_ADMIN_TOKEN)) throw new HttpError(401, "invalid token");
  await rememberOrigin(env, req);
  const secure = new URL(req.url).protocol === "https:" ? "; Secure" : "";
  return json({ ok: true }, 200, { "set-cookie": `${COOKIE}=${await makeSession(env)}; Max-Age=${TTL}; Path=/; HttpOnly; SameSite=Strict${secure}` });
}, "none");
on("POST", "/api/v1/auth/logout", async () => json({ ok: true }, 200, { "set-cookie": `${COOKIE}=; Max-Age=0; Path=/` }), "none");
on("GET", "/api/v1/me", async ({ req, env }) => { await rememberOrigin(env, req); return json({ user: "owner" }); });

// projects / conversations / messages
on("GET", "/api/v1/projects", async ({ env, url }) => json(await q(env, "SELECT * FROM projects WHERE archived=? ORDER BY updated_at DESC", url.searchParams.get("archived") === "true" ? 1 : 0)));
on("POST", "/api/v1/projects", async ({ req, env }) => {
  const b = await body(req); const name = (b.name || "").trim(); if (!name || name.length > 120) throw new HttpError(422, "name required (max 120 chars)");
  const id = newId("prj"), t = now(); await run(env, "INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)", id, name, t, t);
  return json(await one(env, "SELECT * FROM projects WHERE id=?", id));
});
on("PATCH", "/api/v1/projects/:pid", async ({ req, env, p }) => {
  need(await one(env, "SELECT 1 x FROM projects WHERE id=?", p.pid)); const b = await body(req);
  if (b.name != null) { if (!b.name.trim() || b.name.length > 120) throw new HttpError(422, "bad name"); await run(env, "UPDATE projects SET name=?,updated_at=? WHERE id=?", b.name.trim(), now(), p.pid); }
  if (b.archived != null) await run(env, "UPDATE projects SET archived=?,updated_at=? WHERE id=?", b.archived ? 1 : 0, now(), p.pid);
  return json(await one(env, "SELECT * FROM projects WHERE id=?", p.pid));
});
on("GET", "/api/v1/projects/:pid/conversations", async ({ env, p }) => json(await q(env, "SELECT id,project_id,title,created_at FROM conversations WHERE project_id=? ORDER BY created_at DESC", p.pid)));
on("POST", "/api/v1/projects/:pid/conversations", async ({ req, env, p }) => {
  need(await one(env, "SELECT 1 x FROM projects WHERE id=?", p.pid), "project not found"); const b = await req.json().catch(() => ({}));
  const id = newId("cnv"); await run(env, "INSERT INTO conversations(id,project_id,title,created_at) VALUES(?,?,?,?)", id, p.pid, String(b.title || "New conversation").slice(0, 120), now());
  return json(await one(env, "SELECT id,project_id,title,created_at FROM conversations WHERE id=?", id));
});
on("GET", "/api/v1/conversations/:cid/messages", async ({ env, p }) => json((await q(env, "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", p.cid)).map(m => ({ ...m, attachments: parseJson(m.attachments || "[]") || [] }))));
on("POST", "/api/v1/conversations/:cid/messages", async ({ req, env, p }) => {
  const conv = need(await one(env, "SELECT * FROM conversations WHERE id=?", p.cid), "conversation not found"); const b = await body(req);
  const content = (b.content || "").toString(); if (!content || content.length > 20000) throw new HttpError(422, "content required (max 20000 chars)");
  if (await one(env, `SELECT id FROM jobs WHERE conversation_id=? AND status IN (${ACTIVE.map(() => "?").join(",")})`, p.cid, ...ACTIVE)) throw new HttpError(409, "a task is already active in this conversation; wait for it or cancel it");
  const attachments = [];
  for (const aid of b.asset_ids || []) {
    const a = need(await one(env, "SELECT id,name,kind,size,status,project_id FROM assets WHERE id=?", aid), `asset ${aid} not found`);
    if (a.project_id !== conv.project_id || a.status !== "uploaded") throw new HttpError(400, `asset ${aid} is not an uploaded asset of this project`);
    await run(env, "UPDATE assets SET conversation_id=? WHERE id=?", p.cid, aid); attachments.push({ id: a.id, name: a.name, kind: a.kind, size: a.size });
  }
  const mid = newId("msg"); await run(env, "INSERT INTO messages(id,conversation_id,role,content,attachments,created_at) VALUES(?,?,?,?,?,?)", mid, p.cid, "user", content, JSON.stringify(attachments), now());
  if (conv.title === "New conversation") await run(env, "UPDATE conversations SET title=? WHERE id=?", content.slice(0, 60), p.cid);
  const job = await createJob(env, conv.project_id, p.cid, mid);
  await run(env, "UPDATE messages SET job_id=? WHERE id=?", job.id, mid); await run(env, "UPDATE projects SET updated_at=? WHERE id=?", now(), conv.project_id);
  await drainQueue(env, backendUrl(env, req));
  return json({ message_id: mid, job: await one(env, "SELECT * FROM jobs WHERE id=?", job.id) });
});

// jobs
on("GET", "/api/v1/jobs", async ({ env, url }) => {
  let sql = "SELECT * FROM jobs WHERE 1=1"; const a = [];
  if (url.searchParams.get("project_id")) { sql += " AND project_id=?"; a.push(url.searchParams.get("project_id")); }
  if (url.searchParams.get("status")) { sql += " AND status=?"; a.push(url.searchParams.get("status")); }
  return json(await q(env, sql + " ORDER BY created_at DESC LIMIT 200", ...a));
});
on("GET", "/api/v1/jobs/:jid", async ({ env, p }) => json(need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid))));
on("POST", "/api/v1/jobs/:jid/cancel", async ({ env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)); if (TERMINAL.includes(job.status)) return json(job);
  await run(env, "UPDATE jobs SET cancel_requested=1 WHERE id=?", p.jid); await addEvent(env, p.jid, "cancel_requested", {});
  if (job.status === "queued" || !job.started_at) { await cancelGithubRun(env, job.github_run_url); await setStatus(env, p.jid, "cancelled", null, "cancelled before start"); }
  return json(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid));
});
on("POST", "/api/v1/jobs/:jid/retry", async ({ req, env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)); if (!["failed", "cancelled"].includes(job.status)) throw new HttpError(409, "only failed or cancelled jobs can be retried");
  const nj = await createJob(env, job.project_id, job.conversation_id, job.message_id, p.jid, job.attempt + 1); await drainQueue(env, backendUrl(env, req));
  return json(await one(env, "SELECT * FROM jobs WHERE id=?", nj.id));
});
const eventsAfter = (env, jid, after) => q(env, "SELECT seq,ts,type,data FROM job_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 500", jid, after);
const evOut = r => ({ seq: r.seq, ts: r.ts, type: r.type, data: parseJson(r.data) });
on("GET", "/api/v1/jobs/:jid/events.json", async ({ env, p, url }) => json((await eventsAfter(env, p.jid, +(url.searchParams.get("after") || 0))).map(evOut)));
on("GET", "/api/v1/jobs/:jid/events", async ({ req, env, p, url }) => {
  need(await one(env, "SELECT 1 x FROM jobs WHERE id=?", p.jid));
  let last = Math.max(+(url.searchParams.get("after") || 0), +(req.headers.get("last-event-id") || 0));
  const { readable, writable } = new TransformStream(), w = writable.getWriter();
  (async () => {
    try {
      // Free-plan D1 allows ~50 queries per invocation, so each stream lives ~40s; EventSource reconnects and resumes via Last-Event-ID.
      for (let i = 0; i < 28; i++) {
        const rows = await eventsAfter(env, p.jid, last);
        for (const r of rows) { last = r.seq; await w.write(enc.encode(`id: ${r.seq}\nevent: ${r.type}\ndata: ${JSON.stringify(evOut(r))}\n\n`)); }
        if (!rows.length) {
          const j = await one(env, "SELECT status FROM jobs WHERE id=?", p.jid);
          if (j && TERMINAL.includes(j.status)) { await w.write(enc.encode("event: end\ndata: {}\n\n")); break; }
          if (i % 10 === 9) await w.write(enc.encode(": heartbeat\n\n"));
        }
        await new Promise(r => setTimeout(r, rows.length ? 300 : 1500));
      }
    } catch (e) { /* client went away */ } finally { try { await w.close(); } catch {} }
  })();
  return new Response(readable, { headers: { "content-type": "text/event-stream", "cache-control": "no-cache", "x-accel-buffering": "no" } });
});

// uploads / assets
on("POST", "/api/v1/projects/:pid/uploads", async ({ req, env, p }) => {
  need(await one(env, "SELECT 1 x FROM projects WHERE id=?", p.pid), "project not found"); const b = await body(req);
  const name = (b.name || "").toString(), size = +b.size, mime = (b.mime || "application/octet-stream").toString();
  if (!name || !(size > 0)) throw new HttpError(422, "name and positive size required");
  const kind = classify(name); if (!kind) throw new HttpError(415, `unsupported file type: ${ext(name) || name}`);
  if (size > +(env.MAX_UPLOAD_BYTES || 2147483648)) throw new HttpError(413, "file too large");
  const aid = newId("ast"), key = `projects/${p.pid}/uploads/${aid}/${safeName(name)}`;
  await run(env, "INSERT INTO assets(id,project_id,name,kind,mime,size,storage_key,source,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", aid, p.pid, safeName(name), kind, mime, size, key, "upload", "pending", now());
  return json({ asset_id: aid, kind, put_url: await presign(env, req, "put", key, mime), storage_key: key });
});
on("POST", "/api/v1/assets/:aid/complete", async ({ env, p }) => {
  const a = need(await one(env, "SELECT * FROM assets WHERE id=?", p.aid)); const h = await head(env, a.storage_key);
  if (!h) throw new HttpError(409, "object not found in storage; upload did not finish");
  if (h.size !== a.size) throw new HttpError(409, `size mismatch: declared ${a.size}, stored ${h.size}`);
  await run(env, "UPDATE assets SET status='uploaded' WHERE id=?", p.aid); return json(await one(env, "SELECT * FROM assets WHERE id=?", p.aid));
});
on("GET", "/api/v1/projects/:pid/assets", async ({ env, p, url }) => {
  let sql = "SELECT id,name,kind,mime,size,source,status,scene_id,meta,created_at FROM assets WHERE project_id=? AND status='uploaded'"; const a = [p.pid];
  if (url.searchParams.get("source")) { sql += " AND source=?"; a.push(url.searchParams.get("source")); }
  return json((await q(env, sql + " ORDER BY created_at DESC", ...a)).map(r => ({ ...r, meta: parseJson(r.meta) })));
});
on("GET", "/api/v1/assets/:aid/url", async ({ req, env, p }) => {
  const a = need(await one(env, "SELECT storage_key,status FROM assets WHERE id=?", p.aid)); if (a.status !== "uploaded") throw new HttpError(409, "asset not available");
  return json({ url: await presign(env, req, "get", a.storage_key) });
});
on("GET", "/api/v1/projects/:pid/storyboard", async ({ env, p }) => {
  const a = await one(env, "SELECT * FROM assets WHERE project_id=? AND kind='plan' AND status='uploaded' ORDER BY created_at DESC LIMIT 1", p.pid);
  if (!a) throw new HttpError(404, "no storyboard yet; ask the AI to plan the video");
  const obj = await env.BUCKET.get(a.storage_key); return json({ asset_id: a.id, created_at: a.created_at, validation: parseJson(a.meta), storyboard: JSON.parse(await obj.text()) });
});

// signed proxy storage (used only when R2 S3 keys are not configured, e.g. local dev)
async function checkSigned(env, op, url) {
  const key = url.searchParams.get("key"), exp = +url.searchParams.get("exp"), sig = url.searchParams.get("sig");
  if (!key || !(exp > now()) || !await safeEq(sig, await hmac(sessSecret(env), `${op}:${key}:${exp}`))) throw new HttpError(403, "invalid or expired signature");
  return key;
}
on("PUT", "/api/v1/storage/put", async ({ req, env, url }) => {
  const key = await checkSigned(env, "put", url);
  const o = await env.BUCKET.put(key, req.body, { httpMetadata: { contentType: req.headers.get("content-type") || "application/octet-stream" } });
  return json({ size: o.size });
}, "none");
on("GET", "/api/v1/storage/get", async ({ req, env, url }) => {
  const key = await checkSigned(env, "get", url); const range = req.headers.get("range");
  const o = await env.BUCKET.get(key, range ? { range: req.headers } : undefined); if (!o) throw new HttpError(404, "object not found");
  const h = new Headers({ "content-type": o.httpMetadata?.contentType || "application/octet-stream", "accept-ranges": "bytes", etag: o.httpEtag });
  if (range && o.range) { const off = o.range.offset ?? 0, len = o.range.length ?? o.size - off; h.set("content-range", `bytes ${off}-${off + len - 1}/${o.size}`); h.set("content-length", String(len)); return new Response(o.body, { status: 206, headers: h }); }
  h.set("content-length", String(o.size)); return new Response(o.body, { headers: h });
}, "none");

// settings
on("GET", "/api/v1/settings/status", async ({ req, env }) => {
  const health = await one(env, "SELECT value FROM settings WHERE key='runner_health'"); let st;
  try { await env.BUCKET.head("__ping__"); st = { backend: "r2", ok: true, bucket: env.R2_BUCKET_NAME || "backlot-storage", presigned_urls: !!s3(env) }; } catch (e) { st = { backend: "r2", ok: false, error: String(e).slice(0, 200) }; }
  return json({
    github: { configured: !!(env.GITHUB_REPO && env.GITHUB_DISPATCH_TOKEN), repo: env.GITHUB_REPO || "", ref: env.GITHUB_REF || "main", workflow: env.GITHUB_WORKFLOW || "opencode-run.yml", dispatch_mode: env.DISPATCH_MODE || "github", token_present: !!env.GITHUB_DISPATCH_TOKEN },
    storage: st, database: { ok: !!await one(env, "SELECT 1 AS ok"), engine: "cloudflare-d1" },
    auth: { admin_token_set: !!env.ORCH_ADMIN_TOKEN, runner_secret_set: !!env.ORCH_RUNNER_SECRET }, notifications: { telegram_configured: false },
    runner_health: health ? JSON.parse(health.value) : null, general: await general(env), limits: { max_concurrent_jobs: +(env.MAX_CONCURRENT_JOBS || 2) } });
});
on("PATCH", "/api/v1/settings/general", async ({ req, env }) => {
  const b = await body(req), cur = await general(env);
  for (const [k, v] of Object.entries(b)) {
    if (!(k in GENERAL_KEYS) || typeof v !== GENERAL_KEYS[k]) throw new HttpError(400, `invalid setting: ${k}`);
    if (k === "fps" && !(Number.isInteger(v) && v >= 1 && v <= 120)) throw new HttpError(400, "fps must be 1-120");
    if (k === "resolution" && !/^\d{3,4}x\d{3,4}$/.test(v)) throw new HttpError(400, "resolution must look like 1920x1080");
    if (k === "opencode_model" && v && !/^[\w.-]+\/[\w.:/-]+$/.test(v)) throw new HttpError(400, "model must be provider/model");
    cur[k] = v;
  }
  await run(env, "INSERT INTO settings(key,value) VALUES('general',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", JSON.stringify(cur)); return json(cur);
});
on("GET", "/api/v1/settings/github-secrets", async ({ env }) => {
  if (!env.GITHUB_REPO || !env.GITHUB_DISPATCH_TOKEN) throw new HttpError(409, "GitHub not configured");
  return json({ present: await listSecretNames(env), settable: SECRET_NAMES });
});
on("PUT", "/api/v1/settings/github-secrets", async ({ req, env }) => {
  if (!env.GITHUB_REPO || !env.GITHUB_DISPATCH_TOKEN) throw new HttpError(409, "GitHub not configured");
  const b = await body(req); if (!b.value) throw new HttpError(422, "value required"); await putSecret(env, b.name, String(b.value)); return json({ ok: true, name: b.name });
});

// runner API (per-job HMAC)
const R = (method, pattern, handler) => on(method, pattern, async c => { await requireRunner(c.req, c.env, c.p.jid || c.p.rid); return handler(c); }, "none");
on("POST", "/api/runner/recover", async ({ req, env, url }) => { await requireRunner(req, env, "recover"); return json({ recovered: await recoverStale(env, backendUrl(env, req), +(url.searchParams.get("stale_seconds") || 300)) }); }, "none");
on("POST", "/api/runner/health", async ({ req, env }) => { await requireRunner(req, env, "health"); const b = await body(req);
  await run(env, "INSERT INTO settings(key,value) VALUES('runner_health',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", JSON.stringify({ ...b, reported_at: now() })); return json({ ok: true }); }, "none");
const latest = (env, col, id, kind) => one(env, `SELECT * FROM checkpoints WHERE ${col}=? AND kind=? ORDER BY created_at DESC LIMIT 1`, id, kind);
R("GET", "/api/runner/jobs/:jid/context", async ({ req, env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)), conv = await one(env, "SELECT * FROM conversations WHERE id=?", job.conversation_id);
  const msgs = (await q(env, "SELECT id,role,content,attachments,created_at FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", conv.id)).map(m => ({ ...m, attachments: parseJson(m.attachments) || [] }));
  const assets = await q(env, "SELECT id,name,kind,mime,size,storage_key,source,scene_id FROM assets WHERE project_id=? AND status='uploaded'", job.project_id);
  for (const a of assets) a.url = await presign(env, req, "get", a.storage_key);
  const state = await latest(env, "project_id", job.project_id, "project_state"), sess = await latest(env, "conversation_id", conv.id, "opencode_session");
  return json({ job, project: await one(env, "SELECT * FROM projects WHERE id=?", job.project_id), conversation: { id: conv.id, opencode_session_id: conv.opencode_session_id }, messages: msgs, assets, message_id: job.message_id,
    state_url: state ? await presign(env, req, "get", state.storage_key) : null, session_url: sess ? await presign(env, req, "get", sess.storage_key) : null, general: await general(env) });
});
R("POST", "/api/runner/jobs/:jid/events", async ({ req, env, p }) => {
  need(await one(env, "SELECT 1 x FROM jobs WHERE id=?", p.jid)); const b = await body(req);
  if (!b.type || String(b.type).length > 64) throw new HttpError(422, "bad type");
  if (b.type === "run_info" && b.data?.run_url) await run(env, "UPDATE jobs SET github_run_url=? WHERE id=?", b.data.run_url, p.jid);
  const seq = await addEvent(env, p.jid, b.type, b.data || {});
  return json({ seq, cancel_requested: !!(await one(env, "SELECT cancel_requested c FROM jobs WHERE id=?", p.jid)).c });
});
R("POST", "/api/runner/jobs/:jid/status", async ({ req, env, p }) => {
  const b = await body(req); if (!["running", "waiting_for_input", "completed", "failed", "cancelled"].includes(b.status)) throw new HttpError(400, "invalid status");
  need(await one(env, "SELECT 1 x FROM jobs WHERE id=?", p.jid)); const changed = await setStatus(env, p.jid, b.status, b.error ?? null, b.stage ?? null);
  if (changed && TERMINAL.includes(b.status)) await drainQueue(env, backendUrl(env, req)); return json({ changed });
});
R("POST", "/api/runner/jobs/:jid/messages", async ({ req, env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)); const b = await body(req); if (!b.content) throw new HttpError(422, "content required");
  const mid = newId("msg"); await run(env, "INSERT INTO messages(id,conversation_id,role,content,job_id,created_at) VALUES(?,?,?,?,?,?)", mid, job.conversation_id, "assistant", String(b.content).slice(0, 50000), p.jid, now());
  await addEvent(env, p.jid, "assistant_message", { message_id: mid }); return json({ message_id: mid });
});
R("POST", "/api/runner/jobs/:jid/checkpoint/presign", async ({ req, env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)), b = await body(req); if (!["project_state", "opencode_session"].includes(b.kind)) throw new HttpError(400, "invalid checkpoint kind");
  const key = `projects/${job.project_id}/checkpoints/${b.kind}/${p.jid}-${Math.floor(now())}.${b.kind === "project_state" ? "tar.gz" : "json"}`;
  return json({ key, put_url: await presign(env, req, "put", key) });
});
R("POST", "/api/runner/jobs/:jid/checkpoint/commit", async ({ req, env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)), b = await body(req);
  if (!String(b.key || "").startsWith(`projects/${job.project_id}/checkpoints/`)) throw new HttpError(400, "key outside project");
  if (!await head(env, b.key)) throw new HttpError(409, "checkpoint object missing in storage");
  await run(env, "INSERT INTO checkpoints(id,project_id,conversation_id,job_id,kind,storage_key,created_at) VALUES(?,?,?,?,?,?,?)", newId("ckp"), job.project_id, job.conversation_id, p.jid, b.kind, b.key, now());
  if (b.opencode_session_id) await run(env, "UPDATE conversations SET opencode_session_id=? WHERE id=?", b.opencode_session_id, job.conversation_id);
  await addEvent(env, p.jid, "checkpoint", { kind: b.kind }); return json({ ok: true });
});
R("POST", "/api/runner/jobs/:jid/outputs", async ({ req, env, p }) => {
  const job = need(await one(env, "SELECT * FROM jobs WHERE id=?", p.jid)), b = await body(req); const aid = newId("ast"), key = `projects/${job.project_id}/outputs/${aid}/${safeName(b.name || "output")}`;
  await run(env, "INSERT INTO assets(id,project_id,conversation_id,name,kind,mime,size,storage_key,source,status,scene_id,meta,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", aid, job.project_id, job.conversation_id, safeName(b.name || "output"), b.kind || "video", b.mime || "application/octet-stream", 0, key, "generated", "pending", b.scene_id || null, JSON.stringify(b.meta || {}), now());
  return json({ asset_id: aid, put_url: await presign(env, req, "put", key, b.mime || "application/octet-stream") });
});
R("POST", "/api/runner/jobs/:jid/outputs/:aid/complete", async ({ env, p }) => {
  const a = need(await one(env, "SELECT * FROM assets WHERE id=?", p.aid)), h = await head(env, a.storage_key); if (!h) throw new HttpError(409, "output object missing in storage");
  await run(env, "UPDATE assets SET status='uploaded', size=? WHERE id=?", h.size, p.aid);
  await addEvent(env, p.jid, "output_ready", { asset_id: p.aid, name: a.name, kind: a.kind, size: h.size, meta: parseJson(a.meta) }); return json({ ok: true, size: h.size });
});

// ---------- schedules (cron-driven; Africa/Lagos by default) ----------
const specOf = r => ({ ...JSON.parse(r.spec) });
const schedOut = r => ({ id: r.id, project_id: r.project_id, conversation_id: r.conversation_id, name: r.name, prompt: r.prompt, status: r.status, spec: JSON.parse(r.spec),
  summary: describe(JSON.parse(r.spec)), next_run_at: r.next_run_at, last_run_at: r.last_run_at, created_at: r.created_at });
async function logRun(env, sid, due, jobId, status, note) {
  await run(env, "INSERT INTO schedule_runs(id,schedule_id,due_at,started_at,job_id,status,note) VALUES(?,?,?,?,?,?,?)", newId("srn"), sid, due, now(), jobId, status, note || null);
}
/** Start one run of a schedule: posts the prompt into the schedule's own conversation and queues a job. */
async function fireSchedule(env, sch, due, url, status = "started", note = null) {
  const active = await one(env, `SELECT id FROM jobs WHERE conversation_id=? AND status IN (${ACTIVE.map(() => "?").join(",")})`, sch.conversation_id, ...ACTIVE);
  if (active) { await logRun(env, sch.id, due, active.id, "skipped", "previous run still active"); return null; }
  const mid = newId("msg"); await run(env, "INSERT INTO messages(id,conversation_id,role,content,attachments,created_at) VALUES(?,?,?,?,?,?)", mid, sch.conversation_id, "user", sch.prompt, "[]", now());
  const job = await createJob(env, sch.project_id, sch.conversation_id, mid); await run(env, "UPDATE messages SET job_id=? WHERE id=?", job.id, mid);
  await run(env, "UPDATE projects SET updated_at=? WHERE id=?", now(), sch.project_id);
  await logRun(env, sch.id, due, job.id, status, note); return job;
}
async function runDueSchedules(env, nowMs = Date.now(), maxPerTick = 4) {
  const url = await resolveUrl(env), t = nowMs / 1000, out = [];
  const due = await q(env, "SELECT * FROM schedules WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at LIMIT ?", t, maxPerTick);
  for (const sch of due) {
    const spec = specOf(sch), dueMs = sch.next_run_at * 1000, lateS = Math.floor(t - sch.next_run_at);
    const skipped = Math.max(0, countOccurrences(spec, dueMs, nowMs) ); // later occurrences also missed (coalesced into this single run)
    const late = lateS > 120; const note = late ? `ran ${lateS}s late${skipped ? `; ${skipped} later occurrence(s) also missed and coalesced into this run` : ""}` : null;
    const job = await fireSchedule(env, sch, sch.next_run_at, url, late ? "missed_catchup" : "started", note);
    const nx = spec.kind === "once" ? null : nextRun(spec, nowMs);  // a one-time schedule always completes once it has fired
    await run(env, "UPDATE schedules SET last_run_at=?, next_run_at=?, status=?, updated_at=? WHERE id=?", t, nx === null ? null : nx / 1000, nx === null ? "completed" : "active", t, sch.id);
    out.push({ schedule: sch.id, job: job && job.id, late: lateS });
  }
  if (out.length) await drainQueue(env, url);
  return out;
}
const needSched = async (env, sid) => need(await one(env, "SELECT * FROM schedules WHERE id=?", sid), "schedule not found");
function cleanName(v) { const n = String(v || "").trim(); if (!n || n.length > 120) throw new HttpError(422, "name required (max 120 chars)"); return n; }
function cleanPrompt(v) { const n = String(v || "").trim(); if (!n || n.length > 20000) throw new HttpError(422, "prompt required (max 20000 chars)"); return n; }
function specFrom(input) { try { return normalise(input, Date.now()); } catch (e) { throw new HttpError(422, e.message); } }

on("GET", "/api/v1/projects/:pid/schedules", async ({ env, p }) => json((await q(env, "SELECT * FROM schedules WHERE project_id=? ORDER BY created_at DESC", p.pid)).map(schedOut)));
on("POST", "/api/v1/projects/:pid/schedules", async ({ req, env, p }) => {
  need(await one(env, "SELECT 1 x FROM projects WHERE id=?", p.pid), "project not found"); const b = await body(req);
  const name = cleanName(b.name), prompt = cleanPrompt(b.prompt), spec = specFrom(b), id = newId("sch"), cid = newId("cnv"), t = now();
  await run(env, "INSERT INTO conversations(id,project_id,title,created_at) VALUES(?,?,?,?)", cid, p.pid, `⏱ ${name}`.slice(0, 120), t);
  await run(env, "INSERT INTO schedules(id,project_id,conversation_id,name,prompt,spec,status,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", id, p.pid, cid, name, prompt, JSON.stringify(spec), "active", nextRun(spec, Date.now()) / 1000, t, t);
  await rememberOrigin(env, req); return json(schedOut(await needSched(env, id)));
});
on("PATCH", "/api/v1/schedules/:sid", async ({ req, env, p }) => {
  const cur = await needSched(env, p.sid), b = await body(req); let spec = specOf(cur), status = cur.status, next = cur.next_run_at;
  const specKeys = ["kind", "time", "weekdays", "day_of_month", "run_at", "tz"], touched = specKeys.some(k => k in b);
  if (touched) { spec = specFrom({ ...spec, ...b }); const n = nextRun(spec, Date.now()); next = n === null ? null : n / 1000; if (status === "completed") status = "active"; }
  if (b.status != null) {
    if (!["active", "paused"].includes(b.status)) throw new HttpError(422, "status must be active or paused");
    if (b.status === "active" && status !== "active") { const n = nextRun(spec, Date.now()); next = n === null ? null : n / 1000; if (next === null) throw new HttpError(409, "one-time schedule is in the past; edit run_at first"); }  // resume: no catch-up for the paused period
    status = b.status;
  }
  const name = b.name != null ? cleanName(b.name) : cur.name, prompt = b.prompt != null ? cleanPrompt(b.prompt) : cur.prompt;
  await run(env, "UPDATE schedules SET name=?,prompt=?,spec=?,status=?,next_run_at=?,updated_at=? WHERE id=?", name, prompt, JSON.stringify(spec), status, status === "active" ? next : (status === "paused" ? null : next), now(), p.sid);
  return json(schedOut(await needSched(env, p.sid)));
});
on("DELETE", "/api/v1/schedules/:sid", async ({ env, p }) => { await needSched(env, p.sid); await run(env, "DELETE FROM schedule_runs WHERE schedule_id=?", p.sid); await run(env, "DELETE FROM schedules WHERE id=?", p.sid); return json({ ok: true }); });
on("POST", "/api/v1/schedules/:sid/run-now", async ({ req, env, p }) => {
  const sch = await needSched(env, p.sid); const url = backendUrl(env, req) || await resolveUrl(env);
  const job = await fireSchedule(env, sch, now(), url, "manual", "run now"); if (!job) throw new HttpError(409, "previous run is still active");
  await drainQueue(env, url); return json({ job: await one(env, "SELECT * FROM jobs WHERE id=?", job.id) });
});
on("GET", "/api/v1/schedules/:sid/runs", async ({ env, p }) => { await needSched(env, p.sid); return json(await q(env, "SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY started_at DESC LIMIT 50", p.sid)); });

// ---------- entry ----------
export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(req);
    try {
      for (const r of routes) {
        if (r.method !== req.method) continue; const m = r.re.exec(url.pathname); if (!m) continue;
        if (r.auth === "user") await requireUser(req, env);
        return await r.handler({ req, env, ctx, url, p: m.groups || {} });
      }
      return json({ detail: "not found" }, 404);
    } catch (e) {
      if (e instanceof HttpError) return json({ detail: e.message }, e.status);
      console.error(e); return json({ detail: "internal error" }, 500);
    }
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const url = await resolveUrl(env);
      if (event.cron === "*/5 * * * *") { await recoverStale(env, url); return; }
      await runDueSchedules(env, event.scheduledTime || Date.now()); await drainQueue(env, url);
    })());
  },
};
