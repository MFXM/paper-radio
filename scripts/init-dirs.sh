#!/usr/bin/env bash
# Create the bind-mounted data directories with the right owner.
set -euo pipefail
cd "$(dirname "$0")/.."
# Read one KEY from .env without sourcing it (values may contain spaces)
envget() {
  local v
  v="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "${v:-${2:-}}"
}
UID_="$(envget PIPELINE_UID "$(id -u)")"; GID_="$(envget PIPELINE_GID "$(id -g)")"
mkdir -p data/n8n data/state data/podcasts data/postgres data/qdrant data/ollama models
# n8n and tts run as PIPELINE_UID; postgres/qdrant/ollama manage their own dirs
for d in data/n8n data/state data/podcasts; do
  if [ "$(stat -c %u "$d")" != "$UID_" ]; then
    echo "chown $d -> $UID_:$GID_ (may ask for sudo)"; sudo chown -R "$UID_:$GID_" "$d"
  fi
done
[ -f data/state/arxiv_overflow.json ] || echo '[]' > data/state/arxiv_overflow.json
echo "Data directories ready."
