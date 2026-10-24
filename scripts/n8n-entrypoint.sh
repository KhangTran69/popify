#!/bin/sh
set -eu
# Preserve the existing database and encryption key. Workflow IDs make updates idempotent.
marker=/home/node/.n8n/commerceops-workflows-sha
fingerprint=$(cat /workflows/*.json | sha256sum | cut -d ' ' -f 1)
if [ ! -f "$marker" ] || [ "$(cat "$marker")" != "$fingerprint" ]; then
  if [ -f /home/node/.n8n/database.sqlite ]; then
    n8n export:workflow --all --output=/home/node/.n8n/commerceops-before-update.json
  fi
  n8n import:workflow --separate --input=/workflows
  for workflow_id in $(node -e 'const fs=require("fs"); for (const file of fs.readdirSync("/workflows")) if(file.endsWith(".json")) console.log(JSON.parse(fs.readFileSync("/workflows/"+file)).id)'); do
    n8n publish:workflow --id="$workflow_id"
  done
  printf '%s' "$fingerprint" > "$marker"
fi
exec n8n start
