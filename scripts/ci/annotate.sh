#!/usr/bin/env bash
# usage: annotate.sh <title> <file>  -> a ::notice annotation readable through the check-runs API.
# Long token-like strings are redacted first so a secret can never leak through an annotation.
set -euo pipefail
body=$(tail -c 6000 "$2" | python3 -c "
import sys,re
t=sys.stdin.read()
t=re.sub(r'(?:gh[pousr]_|github_pat_|sk-|xox[abp]-)[A-Za-z0-9_\-]{8,}','[redacted]',t)
t=re.sub(r'[A-Za-z0-9_\-]{40,}','[redacted]',t)
print(t.replace('%','%25').replace('\r','%0D').replace('\n','%0A'))")
echo "::notice title=$1::$body"
