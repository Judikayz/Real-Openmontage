#!/usr/bin/env bash
# usage: annotate.sh <title> <file>  -> a ::notice annotation readable through the check-runs API
set -euo pipefail
body=$(head -c 6000 "$2" | python3 -c "import sys,urllib.parse as u;t=sys.stdin.read();print(t.replace('%','%25').replace('\r','%0D').replace('\n','%0A'))")
echo "::notice title=$1::$body"
