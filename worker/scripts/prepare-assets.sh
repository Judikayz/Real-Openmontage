#!/usr/bin/env bash
# Copy the Backlot UI into the Worker's static assets directory.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf public && mkdir -p public/ui
cp ../backlot/ui/app.html public/app.html
for f in app.js app.css board.css; do cp ../backlot/ui/$f public/ui/$f; done
echo "assets ready: $(find public -type f | wc -l) files"
