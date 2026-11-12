(() => {
const $ = s => document.querySelector(s);
const S = { project: null, conv: null, job: null, es: null, attach: [], lastSeq: 0, projects: [] };
const esc = (t, el = document.createElement('div')) => (el.textContent = t, el);
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  if (r.status === 401) { show(false); throw new Error('auth'); }
  if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch (e) {} throw new Error(d || r.statusText); }
  return r.status === 204 ? null : r.json();
}
function toast(m) { const t = $('#toast'); t.textContent = m; t.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => t.style.display = 'none', 4000); }
function show(on) { $('#login').hidden = on; $('#app').hidden = !on; }
const bytes = n => n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.ceil(n / 1e3) + ' KB';
// ---- auth
$('#loginForm').onsubmit = async e => { e.preventDefault(); try { await api('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ token: $('#tok').value }) }); $('#tok').value = ''; start(); } catch (x) { $('#loginErr').textContent = 'Invalid token'; } };
$('#logout').onclick = async () => { await fetch('/api/v1/auth/logout', { method: 'POST' }); location.reload(); };
$('#menuBtn').onclick = () => $('#side').classList.toggle('open');
// ---- projects
async function loadProjects() {
  S.projects = await api('/api/v1/projects?archived=' + $('#showArch').checked);
  const ul = $('#projects'); ul.replaceChildren();
  S.projects.forEach(p => {
    const li = document.createElement('li'); if (S.project && S.project.id === p.id) li.className = 'sel';
    const n = document.createElement('span'); n.textContent = p.name; n.onclick = () => openProject(p);
    const r = document.createElement('button'); r.textContent = '✎'; r.onclick = async () => { const v = prompt('Rename project', p.name); if (v) { await api('/api/v1/projects/' + p.id, { method: 'PATCH', body: JSON.stringify({ name: v }) }); loadProjects(); } };
    const a = document.createElement('button'); a.textContent = p.archived ? 'Restore' : 'Archive'; a.onclick = async () => { await api('/api/v1/projects/' + p.id, { method: 'PATCH', body: JSON.stringify({ archived: !p.archived }) }); if (S.project && S.project.id === p.id) S.project = null; loadProjects(); };
    li.append(n, r, a); ul.append(li);
  });
}
$('#showArch').onchange = loadProjects;
$('#newProj').onclick = async () => { const n = prompt('Project name'); if (!n) return; const p = await api('/api/v1/projects', { method: 'POST', body: JSON.stringify({ name: n }) }); await loadProjects(); openProject(p); };
async function openProject(p) {
  S.project = p; S.conv = null; S.attach = []; $('#title').textContent = p.name; $('#side').classList.remove('open'); closeStream();
  await loadProjects(); await loadConvs(); tab(currentTab());
}
// ---- conversations
async function loadConvs() {
  const cs = await api(`/api/v1/projects/${S.project.id}/conversations`);
  const sel = $('#convs'); sel.replaceChildren(...cs.map(c => Object.assign(document.createElement('option'), { value: c.id, textContent: c.title })));
  if (!cs.length) return newConv();
  S.conv = (S.conv && cs.find(c => c.id === S.conv)) ? S.conv : cs[0].id; sel.value = S.conv; await loadMsgs();
}
$('#convs').onchange = e => { S.conv = e.target.value; closeStream(); loadMsgs(); };
async function newConv() { if (!S.project) return toast('Create or open a project first'); const c = await api(`/api/v1/projects/${S.project.id}/conversations`, { method: 'POST', body: '{}' }); S.conv = c.id; closeStream(); await loadConvs(); }
$('#newConv').onclick = newConv;
async function loadMsgs() {
  const ms = await api(`/api/v1/conversations/${S.conv}/messages`); const box = $('#msgs'); box.replaceChildren();
  ms.forEach(m => { const d = document.createElement('div'); d.className = 'm ' + m.role; d.textContent = m.content;
    if (m.attachments && m.attachments.length) { const s = document.createElement('small'); s.textContent = '📎 ' + m.attachments.map(a => a.name).join(', '); d.append(s); } box.append(d); });
  box.scrollTop = box.scrollHeight;
  const jobs = await api('/api/v1/jobs?project_id=' + S.project.id); const mine = jobs.filter(j => j.conversation_id === S.conv);
  if (mine.length) trackJob(mine[0]); else { $('#jobbar').hidden = true; }
}
// ---- attachments (direct-to-storage upload with progress)
$('#file').onchange = e => { [...e.target.files].forEach(uploadFile); e.target.value = ''; };
function uploadFile(f) {
  if (!S.project) return toast('Open a project first');
  const chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = `${f.name} (${bytes(f.size)}) 0%`; $('#chips').append(chip);
  api(`/api/v1/projects/${S.project.id}/uploads`, { method: 'POST', body: JSON.stringify({ name: f.name, size: f.size, mime: f.type || 'application/octet-stream' }) }).then(up => {
    const x = new XMLHttpRequest(); x.open('PUT', up.put_url); x.setRequestHeader('Content-Type', f.type || 'application/octet-stream');
    x.upload.onprogress = ev => { if (ev.lengthComputable) chip.textContent = `${f.name} (${bytes(f.size)}) ${Math.round(ev.loaded / ev.total * 100)}%`; };
    x.onerror = () => { chip.className = 'chip bad'; chip.textContent = `${f.name}: upload failed`; };
    x.onload = async () => { if (x.status >= 300) { chip.className = 'chip bad'; chip.textContent = `${f.name}: upload failed (${x.status})`; return; }
      try { await api(`/api/v1/assets/${up.asset_id}/complete`, { method: 'POST' }); S.attach.push(up.asset_id); chip.textContent = `✓ ${f.name} (${bytes(f.size)})`; } catch (er) { chip.className = 'chip bad'; chip.textContent = `${f.name}: ${er.message}`; } };
    x.send(f);
  }).catch(er => { chip.className = 'chip bad'; chip.textContent = `${f.name}: ${er.message}`; });
}
// ---- send + job tracking
$('#composer').onsubmit = async e => { e.preventDefault(); const t = $('#text').value.trim(); if (!t) return; if (!S.conv) return toast('Open a project first');
  try { const r = await api(`/api/v1/conversations/${S.conv}/messages`, { method: 'POST', body: JSON.stringify({ content: t, asset_ids: S.attach }) });
    $('#text').value = ''; S.attach = []; $('#chips').replaceChildren(); await loadMsgs(); trackJob(r.job); } catch (x) { toast(x.message); } };
const ACTIVE = ['queued', 'starting', 'running', 'waiting_for_input'];
function trackJob(j) {
  S.job = j; renderJob(j); closeStream(); $('#log').textContent = ''; S.lastSeq = 0; $('#needs').hidden = true;
  const es = new EventSource(`/api/v1/jobs/${j.id}/events`); S.es = es;
  const on = (type) => es.addEventListener(type, ev => onEvent(JSON.parse(ev.data)));
  ['queued','dispatched','deferred','status','runner_started','restored','warning','heartbeat','assistant_text','assistant_message','opencode','opencode_error','qc','qc_fix_attempt','storyboard','plan_fix_attempt','needs_input','checkpoint','output_ready','error','run_info','log','runner_exit','cancel_requested'].forEach(on);
  es.addEventListener('end', async () => { closeStream(); await refreshJob(); await loadMsgs(); });
  es.onerror = () => { /* EventSource reconnects by itself and resumes from Last-Event-ID */ };
}
function closeStream() { if (S.es) { S.es.close(); S.es = null; } }
async function refreshJob() { if (S.job) { S.job = await api('/api/v1/jobs/' + S.job.id); renderJob(S.job); } }
function onEvent(e) {
  if (e.seq <= S.lastSeq) return; S.lastSeq = e.seq;
  if (e.type === 'heartbeat') return;
  const line = `[${new Date(e.ts * 1000).toLocaleTimeString()}] ${e.type} ${JSON.stringify(e.data).slice(0, 220)}`; const l = $('#log'); l.textContent += line + '\n'; l.scrollTop = l.scrollHeight;
  if (e.type === 'status') { S.job.status = e.data.status; S.job.stage = e.data.stage; S.job.error = e.data.error; renderJob(S.job); }
  if (e.type === 'needs_input') { const n = $('#needs'); n.hidden = false; n.textContent = '✋ The AI needs your input: ' + e.data.question; }
  if (e.type === 'output_ready' || e.type === 'assistant_message') { toast(e.type === 'output_ready' ? 'Video ready: ' + e.data.name : 'New reply'); if (e.type === 'assistant_message') loadMsgs(); }
}
function renderJob(j) {
  $('#jobbar').hidden = false; const active = ACTIVE.includes(j.status);
  $('#jobstat').textContent = `${j.status}${j.stage ? ' · ' + j.stage : ''}${j.attempt > 1 ? ' · attempt ' + j.attempt : ''}${j.error ? ' — ' + j.error : ''}`;
  $('#jobstat').className = j.status === 'failed' ? 'err' : j.status === 'completed' ? 'ok' : '';
  $('#cancel').hidden = !active; $('#retry').hidden = !['failed', 'cancelled'].includes(j.status);
}
$('#logbtn').onclick = () => { const l = $('#log'); l.hidden = !l.hidden; };
$('#cancel').onclick = async () => { await api(`/api/v1/jobs/${S.job.id}/cancel`, { method: 'POST' }); await refreshJob(); };
$('#retry').onclick = async () => { const j = await api(`/api/v1/jobs/${S.job.id}/retry`, { method: 'POST' }); trackJob(j); };
// ---- tabs
const currentTab = () => document.querySelector('#tabs .on').dataset.t;
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => tab(b.dataset.t));
function tab(t) {
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  ['chat', 'plan', 'out', 'jobs', 'set'].forEach(k => $('#t-' + k).hidden = k !== t);
  ({ plan: loadPlan, out: loadOutputs, jobs: loadJobs, set: loadSettings })[t]?.();
}
function card(...kids) { const c = document.createElement('div'); c.className = 'card'; kids.forEach(k => c.append(k)); return c; }
function el(tag, text, cls) { const e = document.createElement(tag); e.textContent = text; if (cls) e.className = cls; return e; }
async function loadPlan() {
  const box = $('#t-plan'); box.replaceChildren(); if (!S.project) return box.append(el('p', 'Open a project first.'));
  let r; try { r = await api(`/api/v1/projects/${S.project.id}/storyboard`); } catch (e) { return box.append(el('p', 'No storyboard yet. Ask the AI in Chat to plan your video.')); }
  const v = r.validation; box.append(el('h3', `${r.storyboard.title} — ${v.scenes} scenes, ${v.duration}s`));
  box.append(el('p', v.ok ? '✓ Plan passes validation' : '✗ Plan has problems', v.ok ? 'ok' : 'err'));
  (v.errors || []).forEach(x => box.append(el('div', x, 'err'))); (v.warnings || []).forEach(x => box.append(el('div', x, 'warn')));
  r.storyboard.scenes.forEach(s => { const head = el('h4', `${s.id} · ${s.title}  (${s.start_seconds}s–${s.end_seconds}s)`);
    const tags = document.createElement('div'); [s.type, s.layout, s.status || 'planned', ...(s.tools || [])].forEach(t => tags.append(el('span', t, 'tag')));
    box.append(card(head, tags, el('div', s.visual_description), s.narration ? el('i', '“' + s.narration + '”') : el('span', ''),
      el('small', (s.asset_refs || []).map(a => `${a.role}:${a.source}`).join(' · ')))); });
  box.append(el('p', 'To change anything, just tell the AI in Chat (e.g. "make the demo longer").'));
}
async function loadOutputs() {
  const box = $('#t-out'); box.replaceChildren(); if (!S.project) return box.append(el('p', 'Open a project first.'));
  const as = (await api(`/api/v1/projects/${S.project.id}/assets?source=generated`)).filter(a => a.kind === 'video');
  if (!as.length) return box.append(el('p', 'No rendered videos yet.'));
  for (const a of as) { const { url } = await api(`/api/v1/assets/${a.id}/url`); const m = a.meta || {};
    const v = document.createElement('video'); v.controls = true; v.preload = 'metadata'; v.playsInline = true; v.src = url;
    const dl = Object.assign(document.createElement('a'), { href: url, download: a.name, textContent: 'Download' });
    box.append(card(el('h4', a.name), v, el('small', `${m.width || '?'}×${m.height || '?'} · ${(m.duration || 0).toFixed(1)}s · ${bytes(a.size)} · QC ${m.qc_ok ? '✓' : '✗ ' + (m.qc_errors || []).join('; ')}${(m.qc_warnings || []).length ? ' · ' + m.qc_warnings.join('; ') : ''}`), dl)); }
}
async function loadJobs() {
  const box = $('#t-jobs'); box.replaceChildren(); const js = await api('/api/v1/jobs' + (S.project ? '?project_id=' + S.project.id : ''));
  if (!js.length) return box.append(el('p', 'No jobs yet.'));
  js.forEach(j => { const c = card(el('h4', `${j.status}${j.stage ? ' · ' + j.stage : ''}`), el('small', `${new Date(j.created_at * 1000).toLocaleString()} · attempt ${j.attempt}${j.error ? ' · ' + j.error : ''}`));
    if (j.github_run_url) c.append(Object.assign(document.createElement('a'), { href: j.github_run_url, target: '_blank', rel: 'noopener', textContent: ' GitHub run ↗' }));
    c.onclick = () => { S.conv = j.conversation_id; tab('chat'); loadConvs(); }; box.append(c); });
}
async function loadSettings() {
  const box = $('#t-set'); box.className = 'set'; box.replaceChildren(); const s = await api('/api/v1/settings/status');
  const stat = (n, ok, d) => card(el('h4', (ok ? '✓ ' : '✗ ') + n, ok ? 'ok' : 'err'), el('small', d));
  box.append(stat('GitHub dispatch', s.github.configured, `${s.github.repo || 'not set'} · ${s.github.workflow} @ ${s.github.ref} · mode ${s.github.dispatch_mode}`),
    stat('Storage', s.storage.ok, `${s.storage.backend}${s.storage.bucket ? ' · ' + s.storage.bucket : ''}${s.storage.error ? ' · ' + s.storage.error : ''}`),
    stat('Database', s.database.ok, s.database.engine), stat('Auth', s.auth.admin_token_set && s.auth.runner_secret_set, `admin token ${s.auth.admin_token_set ? 'set' : 'MISSING'} · runner secret ${s.auth.runner_secret_set ? 'set' : 'MISSING'}`),
    stat('Runner health', !!(s.runner_health && s.runner_health.ok), s.runner_health ? `${s.runner_health.execution_path} · opencode ${s.runner_health.opencode} · ffmpeg ok` : 'no health report yet (run the Health Check workflow)'),
    el('small', `Max concurrent jobs: ${s.limits.max_concurrent_jobs}`));
  const g = s.general, f = document.createElement('form'); f.className = 'card';
  const fields = [['resolution', 'Resolution (e.g. 1920x1080 or 1080x1920)'], ['aspect_ratio', 'Aspect ratio'], ['fps', 'Frame rate'], ['output_format', 'Output format'], ['opencode_model', 'OpenCode model (provider/model)'], ['opencode_agent', 'OpenCode agent (optional)'], ['production_preferences', 'Default production preferences']];
  fields.forEach(([k, label]) => { const l = el('label', label); const i = document.createElement('input'); i.name = k; i.value = g[k] ?? ''; if (k === 'fps') i.type = 'number'; f.append(l, i); });
  const b = el('button', 'Save'); f.append(b);
  f.onsubmit = async e => { e.preventDefault(); const body = {}; fields.forEach(([k]) => { const v = f.elements[k].value; body[k] = k === 'fps' ? parseInt(v, 10) : v; }); try { await api('/api/v1/settings/general', { method: 'PATCH', body: JSON.stringify(body) }); toast('Saved'); } catch (x) { toast(x.message); } };
  box.append(el('h4', 'Defaults'), f);
  if (s.github.configured) { let names; try { names = await api('/api/v1/settings/github-secrets'); } catch (e) { names = null; }
    if (names) { const sf = document.createElement('form'); sf.className = 'card'; sf.append(el('h4', 'Provider keys (stored as GitHub Actions secrets)'), el('small', 'Present: ' + (names.present.join(', ') || 'none')));
      const sel = document.createElement('select'); names.settable.forEach(n => sel.append(Object.assign(document.createElement('option'), { value: n, textContent: n })));
      const val = document.createElement('input'); val.type = 'password'; val.placeholder = 'Value (write-only, never shown again)'; val.autocomplete = 'off'; sf.append(sel, val, el('button', 'Save secret'));
      sf.onsubmit = async e => { e.preventDefault(); try { await api('/api/v1/settings/github-secrets', { method: 'PUT', body: JSON.stringify({ name: sel.value, value: val.value }) }); val.value = ''; toast('Secret saved'); loadSettings(); } catch (x) { toast(x.message); } };
      box.append(sf); } }
}
// ---- boot
async function start() { show(true); await loadProjects(); if (S.projects.length) openProject(S.projects[0]); else toast('Create your first project with “+ New”'); }
api('/api/v1/me').then(start).catch(() => show(false));
})();
