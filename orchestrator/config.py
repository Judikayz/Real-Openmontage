"""Environment-driven configuration. Secrets are only ever read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    db_path: str = field(default_factory=lambda: _env("ORCH_DB_PATH", ".orchestrator/orchestrator.db"))
    admin_token: str = field(default_factory=lambda: _env("ORCH_ADMIN_TOKEN"))
    runner_secret: str = field(default_factory=lambda: _env("ORCH_RUNNER_SECRET"))
    session_secret: str = field(default_factory=lambda: _env("ORCH_SESSION_SECRET") or _env("ORCH_ADMIN_TOKEN"))
    public_url: str = field(default_factory=lambda: _env("ORCH_PUBLIC_URL"))
    allowed_hosts: list = field(default_factory=lambda: [h for h in _env("ORCH_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",") if h])
    # storage: "local" (dev/tests) or "s3" (Cloudflare R2 / any S3-compatible)
    storage: str = field(default_factory=lambda: _env("ORCH_STORAGE", "local"))
    local_storage_dir: str = field(default_factory=lambda: _env("ORCH_LOCAL_STORAGE_DIR", ".orchestrator/objects"))
    s3_endpoint: str = field(default_factory=lambda: _env("S3_ENDPOINT"))
    s3_bucket: str = field(default_factory=lambda: _env("S3_BUCKET"))
    s3_access_key_id: str = field(default_factory=lambda: _env("S3_ACCESS_KEY_ID"))
    s3_secret_access_key: str = field(default_factory=lambda: _env("S3_SECRET_ACCESS_KEY"))
    s3_region: str = field(default_factory=lambda: _env("S3_REGION", "auto"))
    # GitHub dispatch
    gh_repo: str = field(default_factory=lambda: _env("GITHUB_DISPATCH_REPO"))  # owner/name
    gh_ref: str = field(default_factory=lambda: _env("GITHUB_DISPATCH_REF", "main"))
    gh_workflow: str = field(default_factory=lambda: _env("GITHUB_DISPATCH_WORKFLOW", "opencode-run.yml"))
    gh_token: str = field(default_factory=lambda: _env("GITHUB_DISPATCH_TOKEN"))
    # "github" (normal) | "local" (dev/tests only: spawn the runner as a local subprocess) | "none"
    dispatch_mode: str = field(default_factory=lambda: _env("ORCH_DISPATCH_MODE", "github"))
    max_concurrent_jobs: int = field(default_factory=lambda: int(_env("ORCH_MAX_CONCURRENT_JOBS", "2")))
    max_upload_bytes: int = field(default_factory=lambda: int(_env("ORCH_MAX_UPLOAD_BYTES", str(2 * 1024**3))))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))

    def github_configured(self) -> bool:
        return bool(self.gh_repo and self.gh_token)

    def storage_configured(self) -> bool:
        if self.storage == "local":
            return True
        return all([self.s3_endpoint, self.s3_bucket, self.s3_access_key_id, self.s3_secret_access_key])


ALLOWED_UPLOAD_TYPES = {
    "video": {".mp4", ".mov", ".webm", ".mkv", ".m4v"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"},
    "document": {".pdf", ".txt", ".md", ".json", ".csv", ".srt", ".vtt", ".docx"},
}


def classify_upload(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    for kind, exts in ALLOWED_UPLOAD_TYPES.items():
        if ext in exts:
            return kind
    return None
