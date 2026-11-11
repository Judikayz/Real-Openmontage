# PROJECT_MEMORY

## Decisions
- Execution happens on GitHub Actions runners inside `Dockerfile.runner`; the repo checkout is bind-mounted so the latest commit always runs.
- Backend = `orchestrator/` (FastAPI, SQLite schema portable to D1), mounted in Backlot when `ORCH_ENABLE=1`.
- Runner auth: per-job HMAC of the shared `ORCH_RUNNER_SECRET` computed on the runner. No secret ever travels in workflow inputs.
- Persistence: object storage (S3/R2) holds uploads, outputs, a tar of `projects/<id>` and an `opencode export` of the session after every job. A new runner restores both. If the session is missing, context is rebuilt from stored messages.
- OpenCode CLI surface verified against v1.19.5: `run --format json --session --auto --dir -m --agent`, `export`, `import`. JSON-lines events carry `type`, `timestamp`, `sessionID`.

## Verified (executed)
- 22 API/unit tests + 8 end-to-end tests (real HTTP server, real runner subprocesses on empty workspaces, real ffmpeg QC) pass locally and on a GitHub-hosted runner.
- Docker image builds on a GitHub runner; toolchain check and ffmpeg+QC smoke test pass inside it.
- Real GitHub `workflow_dispatch` API accepted (HTTP 204).

## Not yet verified / not built
- A real LLM run through OpenCode (needs a provider key). Tests use `tests/orchestrator/fake_opencode.py`, a stand-in that mimics the CLI.
- That `opencode import` preserves session ids (fallback to message history exists and is tested).
- Cloudflare R2 (S3Storage code exists; only the local backend has been exercised).
- Backend deployed at a public URL + a full dispatch -> runner -> backend round trip on GitHub.
- Telegram notifications live (request shape unit-tested only).
- Backlot UI: chat panel, uploads, jobs, settings (API exists; UI not built).
- Storyboard/timeline director skill, remote board state for Backlot, GPU path (CPU image only).
