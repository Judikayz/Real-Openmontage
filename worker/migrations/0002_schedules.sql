CREATE TABLE IF NOT EXISTS schedules(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, name TEXT NOT NULL, prompt TEXT NOT NULL,
  spec TEXT NOT NULL,                       -- JSON: {kind,tz,time,weekdays,day_of_month,run_at}
  status TEXT NOT NULL DEFAULT 'active',    -- active | paused | completed (one-time done)
  next_run_at REAL, last_run_at REAL, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_sched_due ON schedules(status, next_run_at);
CREATE TABLE IF NOT EXISTS schedule_runs(
  id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, due_at REAL, started_at REAL, job_id TEXT,
  status TEXT NOT NULL,                     -- started | missed_catchup | skipped | error | manual
  note TEXT);
CREATE INDEX IF NOT EXISTS idx_sruns ON schedule_runs(schedule_id, started_at);
