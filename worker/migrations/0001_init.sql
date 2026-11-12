CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT, opencode_session_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, job_id TEXT, attachments TEXT DEFAULT '[]', created_at REAL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, message_id TEXT, kind TEXT DEFAULT 'chat', status TEXT NOT NULL, stage TEXT, attempt INTEGER DEFAULT 1, parent_job_id TEXT, github_run_url TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0, created_at REAL, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS job_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts REAL, type TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, seq);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT, name TEXT, kind TEXT, mime TEXT, size INTEGER, storage_key TEXT, source TEXT, status TEXT, scene_id TEXT, meta TEXT DEFAULT '{}', created_at REAL);
CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY, project_id TEXT, conversation_id TEXT, job_id TEXT, kind TEXT, storage_key TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
