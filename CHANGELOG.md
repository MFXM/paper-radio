# Changelog

## 1.0.1 - 2026-10-27

- TTS service: synchronous single-utterance synthesis for other apps, `POST /v1/synthesize`
  (WAV) and `GET /v1/voices`, optionally protected by `TTS_SYNTH_TOKEN`. The routes share the
  model with the podcast worker (which now locks per chapter) and never touch the feed.

## 1.0.0 – 2026-10-21

First public release.

- Docker Compose stack: n8n, Postgres/pgvector, Qdrant, Ollama, FlareSolverr,
  Zotero translation server, PDF converter, Kokoro TTS, nginx feed.
- Six n8n workflows: arXiv Scout, Podcasts Generation, Mattermost Control,
  Error Workflow, Overflow Flush, Feed Re-index. All settings come from `.env`.
- `scripts/bootstrap.sh` creates credentials, imports and publishes workflows.
- TTS service: escaped transcripts and feed items, atomic feed updates, `/health`.
- PDF converter: host allowlist and size limit.
