#!/usr/bin/env bash
# Signed call to the backend's runner API without needing python on the host.
# usage: call_backend.sh <job-or-purpose-id> <METHOD> <path> [json-body]
set -euo pipefail
: "${BACKEND_URL:?}" "${ORCH_RUNNER_SECRET:?}"
id="$1"; method="$2"; path="$3"; body="${4:-}"
sig=$(printf 'runner:%s' "$id" | openssl dgst -sha256 -hmac "$ORCH_RUNNER_SECRET" -hex | sed 's/^.* //')
args=(-sS -m 30 -X "$method" "${BACKEND_URL%/}${path}" -H "X-Runner-Signature: $sig" -w '\nHTTP %{http_code}\n')
[ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
curl "${args[@]}"
