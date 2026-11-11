#!/usr/bin/env bash
# Run the runner inside the Docker image with only the env vars that are actually set.
set -euo pipefail
IMAGE="${IMAGE:-om-runner:ci}"
gpu_flag=""
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 && docker info 2>/dev/null | grep -qi nvidia; then
  gpu_flag="--gpus all"; echo "execution path: GPU ($(nvidia-smi -L | head -1))"
else
  echo "execution path: CPU (no usable GPU on this runner)"
fi
envs=()
for n in JOB_ID BACKEND_URL ORCH_RUNNER_SECRET GITHUB_RUN_ID GITHUB_REPOSITORY \
         OPENAI_API_KEY ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY GROQ_API_KEY \
         ELEVENLABS_API_KEY FAL_KEY PEXELS_API_KEY PIXABAY_API_KEY HEYGEN_API_KEY RUNWAY_API_KEY OPENCODE_API_KEY; do
  [ -n "${!n:-}" ] && envs+=(-e "$n")
done
# shellcheck disable=SC2086
exec docker run --rm $gpu_flag "${envs[@]}" -v "$PWD:/work" -w /work "$IMAGE" "$@"
