# Changelog

## Unreleased

- TTS service: `POST /v1/episode` publishes audio another app has already rendered (multipart WAV + title,
  transcript, TL;DR, link). It is encoded to MP3 at once, with transcript and feed item, without queueing behind
  `/podcast` jobs. `DELETE /v1/episode/<slug>` removes such an upload again; it only accepts the `episode_<ts>_<id>`
  slugs of uploads, never generated episodes. Same `TTS_SYNTH_TOKEN` as the other `/v1` routes; upload limit
  `TTS_MAX_EPISODE_MB` (200).

## 1.1.0 - 2026-10-29

- Mattermost Control: webhooks for other apps (used by the QKB knowledge base). `podcast-file` makes an episode
  from an uploaded PDF (multipart `File`, `podcast_type`, `doi`), `curate-paper-direct` curates a paper without the
  form (arXiv id, or title/authors/abstract for DOI-only papers; an already curated DOI-only paper only gets its tier
  changed instead of a duplicate row), and `curation-status` returns the reference library as JSON. The Mattermost
  notice for `podcast-file` runs in parallel and cannot block generation.
- Fix: curating a paper without an arXiv id (Manual Entry) stored the text `'null'` as its `arxiv_id`, so the next such
  paper overwrote it via `ON CONFLICT (arxiv_id)`. Missing ids are now stored as NULL. Existing rows can be cleaned with
  `UPDATE paper_embeddings SET arxiv_id = NULL WHERE arxiv_id = 'null'` (and the same for `doi`).

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
