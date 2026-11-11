# GitHub Actions setup

Repository **secrets**: `ORCH_RUNNER_SECRET` (required; same value as the backend), plus the provider keys you use
(`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, `FAL_KEY`, `PEXELS_API_KEY`, ...).
Repository **variable**: `ORCH_BACKEND_URL` (public URL of the backend; used by Recovery and Health Check).

Backend dispatch token (server-side env `GITHUB_DISPATCH_TOKEN`): fine-grained PAT, this repository only, Actions read+write
(and Secrets read+write only if you want the settings page to set secrets).

Workflows: `opencode-run.yml` (dispatched per chat message), `health-check.yml` (daily/manual), `recovery.yml` (every 10 min),
`maintenance.yml` (weekly cache/log cleanup; never touches project data), `orchestrator-tests.yml` (CI).

Limits: hosted runners have no GPU (the CPU path is used; GPU is passed through only if `nvidia-smi` and the Docker nvidia runtime exist),
6 h per job, and each message pays runner start-up latency. Quotas depend on your GitHub plan.
