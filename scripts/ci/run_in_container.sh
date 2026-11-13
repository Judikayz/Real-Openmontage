#!/usr/bin/env bash
# Run the runner inside the Docker image. Only secrets named in config/runner_secrets.txt reach the container
# (infrastructure secrets such as CLOUDFLARE_API_TOKEN / GH_DISPATCH_TOKEN / ORCH_ADMIN_TOKEN are never forwarded to the AI).
# Secret values are passed by NAME (-e NAME), never on the command line.
set -euo pipefail
IMAGE="${IMAGE:-om-runner:ci}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
gpu_flag=""
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 && docker info 2>/dev/null | grep -qi nvidia; then
  gpu_flag="--gpus all"; echo "execution path: GPU ($(nvidia-smi -L | head -1))"
else
  echo "execution path: CPU (no usable GPU on this runner)"
fi
envs=()
for n in JOB_ID BACKEND_URL ORCH_RUNNER_SECRET GITHUB_RUN_ID GITHUB_REPOSITORY; do [ -n "${!n:-}" ] && envs+=(-e "$n"); done
if [ -n "${SECRETS_JSON:-}" ]; then
  forwarded=0
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    v=$(SECRETS_JSON="$SECRETS_JSON" python3 -c 'import json,os,sys;print(json.loads(os.environ["SECRETS_JSON"]).get(sys.argv[1],""),end="")' "$n")
    if [ -n "$v" ]; then export "$n=$v"; envs+=(-e "$n"); forwarded=$((forwarded+1)); fi
  done < "$HERE/config/runner_secrets.txt"
  echo "forwarded $forwarded tool/provider key(s) to the container"
  unset SECRETS_JSON
fi
# shellcheck disable=SC2086
exec docker run --rm $gpu_flag "${envs[@]}" -v "$PWD:/work" -w /work "$IMAGE" "$@"
