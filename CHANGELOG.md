# Changelog

## 1.0.0 – 2026-10-21

First public release.

- Docker Compose stack: n8n, Postgres/pgvector, Qdrant, Ollama, FlareSolverr,
  Zotero translation server, PDF converter, Kokoro TTS, nginx feed.
- Six n8n workflows: arXiv Scout, Podcasts Generation, Mattermost Control,
  Error Workflow, Overflow Flush, Feed Re-index. All settings come from `.env`.
- `scripts/bootstrap.sh` creates credentials, imports and publishes workflows.
- TTS service: escaped transcripts and feed items, atomic feed updates, `/health`.
- PDF converter: host allowlist and size limit.
