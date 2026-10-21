#!/usr/bin/env bash
# One-shot setup after `docker compose up -d`:
#   1. pulls the embedding model into the bundled Ollama
#   2. creates/updates the n8n credentials from .env (fixed IDs)
#   3. imports the workflows from n8n/workflows/
#   4. publishes (activates) the trigger workflows
# Re-running is safe: credentials and workflows with the same ID are overwritten.
#
#   ./scripts/bootstrap.sh                 # everything
#   ./scripts/bootstrap.sh --credentials   # only step 2
#   ./scripts/bootstrap.sh --workflows     # only steps 3-4
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env - copy .env.example first." >&2; exit 1; }
# Read one KEY from .env without sourcing it (values may contain spaces)
envget() {
  local v
  v="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "${v:-${2:-}}"
}

DC="docker compose"
STEP_MODEL=1; STEP_CREDS=1; STEP_WF=1
case "${1:-}" in
  --credentials) STEP_MODEL=0; STEP_WF=0 ;;
  --workflows)   STEP_MODEL=0; STEP_CREDS=0 ;;
  "") ;;
  *) echo "unknown option $1" >&2; exit 2 ;;
esac

ACTIVE_WORKFLOWS="SZ7TINtKAighDsQ8 RDinGXe0wJt4PzQq jKTCtmJwN60foGZz rhV3vPpq1DSy2Y3E"

echo "Waiting for n8n to become healthy..."
for i in $(seq 1 60); do
  [ "$($DC ps --format '{{.Health}}' n8n 2>/dev/null)" = "healthy" ] && break
  sleep 5; [ "$i" = 60 ] && { echo "n8n is not healthy - check: docker compose logs n8n" >&2; exit 1; }
done

if [ "$STEP_MODEL" = 1 ]; then
  echo "Pulling embedding model $(envget EMBEDDING_MODEL nomic-embed-text)..."
  $DC exec -T ollama ollama pull "$(envget EMBEDDING_MODEL nomic-embed-text)"
fi

if [ "$STEP_CREDS" = 1 ]; then
  TMP="$(mktemp)"; trap 'rm -f "$TMP"; $DC exec -T n8n rm -f /tmp/creds.json >/dev/null 2>&1 || true' EXIT
  python3 scripts/render_credentials.py .env > "$TMP"
  $DC cp "$TMP" n8n:/tmp/creds.json
  $DC exec -T n8n n8n import:credentials --input=/tmp/creds.json
  $DC exec -T n8n rm -f /tmp/creds.json
  rm -f "$TMP"
fi

if [ "$STEP_WF" = 1 ]; then
  $DC exec -T n8n rm -rf /tmp/wf
  $DC cp n8n/workflows n8n:/tmp/wf
  $DC exec -T n8n n8n import:workflow --separate --input=/tmp/wf
  $DC exec -T n8n rm -rf /tmp/wf
  for id in $ACTIVE_WORKFLOWS; do
    $DC exec -T n8n n8n publish:workflow --id="$id"
  done
  echo "Restarting n8n so the published workflows register their triggers..."
  $DC restart n8n >/dev/null
  for i in $(seq 1 60); do
    [ "$($DC ps --format '{{.Health}}' n8n 2>/dev/null)" = "healthy" ] && break; sleep 5
  done
fi
echo "Done. Open $(envget WEBHOOK_URL) and check the workflows."
