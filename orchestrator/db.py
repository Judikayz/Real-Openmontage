"""SQLite persistence (WAL). Same schema is portable to Cloudflare D1."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT, opencode_session_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, job_id TEXT, attachments TEXT DEFAULT '[]', created_at REAL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, message_id TEXT, kind TEXT DEFAULT 'chat', status TEXT NOT NULL, stage TEXT, attempt INTEGER DEFAULT 1, parent_job_id TEXT, github_run_url TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0, created_at REAL, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS job_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts REAL, type TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, seq);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT, name TEXT, kind TEXT, mime TEXT, size INTEGER, storage_key TEXT, source TEXT, status TEXT, scene_id TEXT, meta TEXT DEFAULT '{}', created_at REAL);
CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY, project_id TEXT, conversation_id TEXT, job_id TEXT, kind TEXT, storage_key TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

TERMINAL = {"completed", "failed", "cancelled"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if path == ":memory:" else None
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = self._mem or sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        if not self._mem:
            c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            if not self._mem:
                c.close()

    def q(self, sql, args=()):
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.conn() as c:
            return c.execute(sql, args).lastrowid

    # ---- domain helpers ----
    def add_event(self, job_id: str, etype: str, data: dict | None = None) -> int:
        return self.x("INSERT INTO job_events(job_id,ts,type,data) VALUES(?,?,?,?)",
                      (job_id, time.time(), etype, json.dumps(data or {})))

    def set_job_status(self, job_id: str, status: str, error: str | None = None, stage: str | None = None):
        now = time.time()
        cur = self.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        if not cur or cur["status"] in TERMINAL:
            return False  # terminal states are final
        sets, args = ["status=?"], [status]
        if status == "running":
            sets.append("started_at=COALESCE(started_at,?)"); args.append(now)
        if status in TERMINAL:
            sets.append("finished_at=?"); args.append(now)
        if error is not None:
            sets.append("error=?"); args.append(error[:4000])
        if stage is not None:
            sets.append("stage=?"); args.append(stage)
        self.x(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", (*args, job_id))
        self.add_event(job_id, "status", {"status": status, "error": error, "stage": stage})
        return True
