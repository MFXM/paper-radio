# paper-radio

Turn new arXiv papers into short podcast episodes for your commute.

Every weekday night the pipeline scans arXiv and SciRate, ranks new papers against a
library of reference papers and authors *you* curated, lets an LLM pick the best
few, files them in Zotero, writes a spoken script and synthesises it locally with
[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M). Episodes land in an ordinary RSS
podcast feed. Everything is controlled from a Mattermost channel: request an
episode for any arXiv ID or uploaded PDF, curate the reference library, browse the
overflow queue, and search past episodes.

It is a Docker Compose stack around a set of [n8n](https://n8n.io) workflows.

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Quick start](#quick-start)
4. [Mattermost setup](#mattermost-setup)
5. [Daily use](#daily-use)
6. [Configuration](#configuration)
7. [Ranking papers](#ranking-papers)
8. [Episode formats](#episode-formats)
9. [Data, backups and updates](#data-backups-and-updates)
10. [Changing the workflows](#changing-the-workflows)
11. [Security](#security)
12. [Troubleshooting](#troubleshooting)
13. [Licences](#licences)
14. [Acknowledgements](#acknowledgements)

## How it works

```
             ┌──────────── arXiv Scout (cron, Mon–Fri) ────────────┐
 arXiv API ─►│ fetch new v1 papers ─┐                               │
 SciRate  ──►│ (via FlareSolverr)  ─┴► de-duplicate against Zotero  │
             │  embed title+abstract (Ollama) ─► compare with your   │
             │  reference papers + known authors (Postgres/pgvector) │
             │  ─► composite score ─► LLM curator picks ≤ 3          │
             └──────┬──────────────────────────┬─────────────────────┘
                    │ primary                   │ rest → overflow queue
                    ▼                           ▼
     Zotero (translation server)        /overflow cards in Mattermost
                    │
                    ▼
     Podcasts Generation:  arXiv HTML or PDF → text → LLM script + TL;DR
                    │                         (Briefing / Breakdown / Deep Dive / Lecture Series)
                    ▼
     TTS service (Kokoro) → MP3 with chapters + HTML transcript + feed.xml → nginx → podcast app
                    │
                    └─► transcript chunks → Qdrant  (powers /search in Mattermost)
```

| Workflow (`n8n/workflows/`) | Trigger | Purpose |
|---|---|---|
| `arxiv-scout.json` | schedule | discovery, ranking, Zotero filing, starts Briefings |
| `podcast-generation.json` | sub-workflow | text extraction, script writing, TTS, indexing |
| `mattermost-control.json` | webhooks | slash commands, forms and buttons |
| `error-workflow.json` | error trigger | posts failures to Mattermost with a *Retry* button |
| `arxiv-overflow-flush.json` | manual | turns the whole overflow queue into Briefings |
| `podcast-feed-reindex.json` | manual | rebuilds the Qdrant search index from `feed.xml` |

| Service | Image | Role |
|---|---|---|
| `n8n` | `n8nio/n8n` | workflow engine |
| `postgres` | `pgvector/pgvector:pg16` | n8n database + `pipeline` database (reference library) |
| `qdrant` | `qdrant/qdrant` | episode search index |
| `ollama` | `ollama/ollama` | local embeddings (`nomic-embed-text`, 768-d) |
| `flaresolverr` | `ghcr.io/flaresolverr/flaresolverr` | fetches SciRate |
| `zotero-translator` | `zotero/translation-server` | arXiv ID/DOI → Zotero item |
| `pdf-converter` | built from `services/pdf-converter` | PDF/EPUB → Markdown (PyMuPDF) |
| `tts` | built from `services/tts` | Kokoro TTS, MP3 + transcript + RSS |
| `podcast-feed` | `nginx` | serves `feed.xml`, MP3s and transcripts |

## Requirements

- Docker Engine with Compose v2, on `linux/amd64` or `linux/arm64`. A small
  ARM board with 8 GB RAM is enough; TTS runs on the CPU (a 5-minute Briefing
  takes a few minutes to synthesise).
- About 3 GB of disk for images and models, plus roughly 5 MB per episode.
- `bash`, `curl`, `python3` and `sha256sum` on the host (for the setup scripts).
- A **Mattermost** server where you can create a bot account and slash commands.
- A **Zotero** account (personal or group library) and an API key with write access.
- LLM access:
  - an **OpenAI-compatible** endpoint for the curator (OpenAI, NVIDIA NIM, vLLM,
    LiteLLM, …), optionally OpenRouter as fallback;
  - an **Ollama-compatible** chat endpoint for script writing. This can be the
    bundled Ollama with a local model, a remote Ollama, or a gateway such as
    [uai-bridge](https://github.com/MFXM/uai-bridge);
  - optionally a **Google Gemini** API key (used first for Briefings and TL;DRs,
    with automatic fallback to the Ollama-compatible endpoint).

## Quick start

```bash
git clone https://github.com/MFXM/paper-radio.git
cd paper-radio
cp .env.example .env
$EDITOR .env                   # at least every value marked REQUIRED
./scripts/init-dirs.sh         # creates ./data/* with the right owner
./scripts/download-models.sh   # Kokoro model files (~355 MB, checksum-verified)
docker compose up -d --build
```

Open `WEBHOOK_URL` (default `http://localhost:5678/`) and create the n8n owner
account. Then:

```bash
./scripts/bootstrap.sh
```

`bootstrap.sh` pulls the embedding model, creates the n8n credentials from
`.env`, imports the workflows and publishes the four trigger workflows. It is
safe to re-run: credentials and workflows with the same ID are overwritten, so
**edits you made in the n8n UI to these six workflows are replaced**.
`--credentials` and `--workflows` run just one half.

Finally [set up Mattermost](#mattermost-setup), add a few reference papers with
`/curate`, and subscribe to `PODCAST_PUBLIC_URL/feed.xml` in your podcast app.

> Generate the three secrets (`POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `QDRANT_API_KEY`) with `openssl rand -hex 32`. Keep `N8N_ENCRYPTION_KEY`
> safe: without it the stored credentials cannot be decrypted.

## Mattermost setup

See [docs/mattermost.md](docs/mattermost.md) for screenshots-free step-by-step
instructions. In short:

1. Create a **bot account**, copy its token into `MATTERMOST_BOT_TOKEN`, and add
   the bot to your channel. Put the channel ID into `MATTERMOST_CHANNEL_ID`.
2. Mattermost must be able to reach `WEBHOOK_URL`. If that is a private address,
   add the host to **System Console → Environment → Developer → Allow untrusted
   internal connections to** (`ServiceSettings.AllowedUntrustedInternalConnections`),
   otherwise slash commands and buttons fail with a connection error.
3. Create the slash commands (request method **POST**):

| Command | Request URL | Argument |
|---|---|---|
| `/briefing` | `<WEBHOOK_URL>webhook/podcast-briefing` | arXiv ID |
| `/breakdown` | `<WEBHOOK_URL>webhook/podcast-breakdown` | arXiv ID |
| `/deepdive` | `<WEBHOOK_URL>webhook/podcast-deepdive` | arXiv ID |
| `/podcast` | `<WEBHOOK_URL>webhook/podcast-form` | – (upload form for PDF/EPUB) |
| `/overflow` | `<WEBHOOK_URL>webhook/arxiv_overflow` | – |
| `/curate` | `<WEBHOOK_URL>webhook/curate-paper` | – (form) |
| `/curate-author` | `<WEBHOOK_URL>webhook/curate-author` | – (form) |
| `/curation` | `<WEBHOOK_URL>webhook/list-curation` | – |
| `/search` | `<WEBHOOK_URL>webhook/paper-search` | search text |

Other apps (e.g. a knowledge base) can use three more webhooks without Mattermost:

| Webhook | Method | Body | Does |
|---|---|---|---|
| `webhook/podcast-file` | POST multipart | `File` (PDF), `podcast_type` (`Briefing`/`Breakdown`/`Deep Dive`), `doi`, `title` | Episode from a PDF, like `/podcast`; the feed link is the DOI |
| `webhook/curate-paper-direct` | POST JSON | `tier`, `arxiv_id` or `doi` + `title` + `authors` + `abstract` | Adds/updates a reference paper, like `/curate` |
| `webhook/curation-status` | GET | – | `{ "papers": [{ arxiv_id, doi, tier, title }] }` |

`podcast-file` hands the PDF to *Podcasts Generation* as n8n binary data. That needs n8n's filesystem
binary-data mode, the default in n8n 2.x: do not set `N8N_DEFAULT_BINARY_DATA_MODE=default`.

Command names are up to you; only the URLs matter. Buttons call
`overflow-callback`, `llm-search-harder`, `mattermost-search-paginate` and
`retry-execution` on the same base URL and need no extra setup.

### No Mattermost server yet?

Mattermost is deliberately not part of this stack. Most people already have a chat
server, and a chat server has its own upgrade and backup cycle. If you need one:

- Follow the official [Docker deployment guide](https://docs.mattermost.com/install/install-docker.html)
  on any machine that can reach `WEBHOOK_URL`. A NAS or small VM is enough for one person.
- The official `mattermost/mattermost-team-edition` images are published for
  **amd64 only** (checked 2026-10). On ARM boards, run Mattermost on another machine.
- If you run Mattermost on the same Docker host, attach it to this project's
  `egress` network and use `http://n8n:5678/` as the webhook base in the slash
  commands. Add `n8n` to *Allow untrusted internal connections to*.
- Any self-hosted or cloud Mattermost works. The pipeline only needs a bot token,
  a channel and slash commands (see [docs/mattermost.md](docs/mattermost.md)).

Other chat tools are not supported out of the box. All Mattermost nodes sit in
`mattermost-control.json` (12), `error-workflow.json` (1) and `arxiv-scout.json` (1, the
overflow reminder). Porting to another tool mostly means swapping those nodes and the
slash-command webhooks.

## Daily use

- **Automatic:** arXiv Scout runs Mon–Fri at 02:06:32 (n8n timezone `TZ`). It posts
  a warning when the overflow queue exceeds `OVERFLOW_ALERT_THRESHOLD`.
- `/briefing 2507.05120` – a ~5 min episode for any arXiv paper.
- `/podcast` – upload a PDF or EPUB and pick any format, including *Lecture Series*.
- `/overflow` – one card per queued paper with *Generate Briefing* / *Discard*.
- `/search variational circuits` – find past episodes; *Explain* gives an LLM
  answer grounded in the episode transcript.
- `/curate`, `/curate-author`, `/curation` – manage the reference library.

Episodes appear in the feed a few minutes after the script is written (the TTS
queue is processed one job at a time).

## Configuration

All settings live in `.env`; `compose.yml` passes them to n8n, where the
workflows read them via `$env`. After changing `.env`, run
`docker compose up -d` (and `./scripts/bootstrap.sh --credentials` if you
changed a key or URL that belongs to a credential).

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_URL` | – | Public base URL of n8n (trailing `/`). Used for Mattermost callbacks and form links |
| `N8N_BIND`, `N8N_PUBLISH_PORT` | `127.0.0.1`, `5678` | Where n8n is published on the host |
| `PODCAST_PUBLIC_URL` | `http://localhost:8080` | Base URL of the feed as seen by your podcast app |
| `FEED_BIND`, `FEED_PORT` | `127.0.0.1`, `8080` | Where nginx is published |
| `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `QDRANT_API_KEY` | – | Secrets (`openssl rand -hex 32`) |
| `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`, `MATTERMOST_CHANNEL_ID` | – | Mattermost server, bot token, target channel |
| `ZOTERO_LIBRARY_TYPE` | `users` | `users` or `groups` |
| `ZOTERO_LIBRARY_ID`, `ZOTERO_COLLECTION_KEY`, `ZOTERO_API_KEY` | – | Library, collection for new papers, API key |
| `ARXIV_CATEGORIES` | `quant-ph,cs.LG` | arXiv categories to scan |
| `SCIRATE_CATEGORY`, `SCIRATE_MIN_SCITES` | `quant-ph`, `10` | SciRate listing and minimum scites |
| `SCOUT_MIN_SCORE`, `SCOUT_MAX_CANDIDATES` | `0.75`, `100` | Composite-score cut and cap before the LLM sees the list |
| `OVERFLOW_ALERT_THRESHOLD` | `25` | Queue size that triggers a reminder |
| `RESEARCH_EXPERTISE`, `RESEARCH_FOCUS`, `RESEARCH_EXCLUDE` | examples | Inserted into the curator prompt |
| `HTTP_USER_AGENT` | – | Sent to arXiv; include a contact address |
| `SCOUT_LLM_BASE_URL`, `SCOUT_LLM_API_KEY`, `SCOUT_MODEL` | – | OpenAI-compatible curator model |
| `OPENROUTER_API_KEY`, `SCOUT_FALLBACK_MODEL` | – | Optional curator fallback |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_API_KEY`, `LLM_MODEL_BRIEFING` | – | Ollama-compatible endpoint and default writing model |
| `LLM_MODEL_TLDR` … `LLM_MODEL_EXPLAIN` | `LLM_MODEL_BRIEFING` | Per-step overrides (TL;DR, Breakdown, Deep Dive outline/chapters, Lecture planning/chapters, search explanations) |
| `GEMINI_API_KEY`, `GEMINI_SCRIPT_MODEL`, `GEMINI_TLDR_MODEL` | – | Optional Gemini for Briefings and TL;DRs |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Ollama embedding model; must produce 768-d vectors (see schema) |
| `TTS_VOICE`, `TTS_SPEED` | `bm_george`, `1.0` | [Kokoro voice](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md); `b*` voices use British English |
| `TTS_SYNTH_TOKEN` | – | Optional bearer token for the synchronous synthesis routes (see below); empty = open |
| `PODCAST_TITLE`, `PODCAST_DESCRIPTION`, `PODCAST_LANGUAGE` | | Feed metadata (written when `feed.xml` is first created) |
| `N8N_API_KEY` | – | Optional; enables the *Retry* button on error cards |

The schedule and the prompts are edited in the n8n UI (open the workflow, change
the *Schedule Trigger* or the agent node). Put a `cover.png` into `data/podcasts/`
for feed artwork.

### Synchronous synthesis for other apps

Besides the podcast queue, the TTS service offers a synchronous single-utterance synthesis endpoint for other apps:

| Route | Request | Response |
|---|---|---|
| `POST /v1/synthesize` | JSON or form: `text` (required, ≤ 600 characters), `voice` (optional, default `TTS_VOICE`), `speed` (optional, 0.5–2.0, default `TTS_SPEED`) | `audio/wav`, 16-bit PCM mono at the model's sample rate |
| `GET /v1/voices` | – | `{"voices": [...], "default": "bm_george"}` |

Errors are JSON `{"error": "..."}`: 400 (empty text), 413 (text too long), 422 (unknown voice or speed out of range),
401 (token), 503 (model still loading). Send one sentence or a short paragraph per request; long texts belong in `/podcast`.
The routes share the model with the podcast worker and wait for at most one chapter of a running episode. They never
write to the podcast folder or the feed.

Apps that render their own audio can publish it as an episode:

| Route | Request | Response |
|---|---|---|
| `POST /v1/episode` | multipart form: `audio` (PCM WAV, ≤ `TTS_MAX_EPISODE_MB`, default 200), `title` (≤ 300), `text` (transcript), `tldr`, `link` (http/https) | 201 `{"filename_slug": "episode_<ts>_<id>", "duration_s", "audio_url", "transcript_url"}` |
| `DELETE /v1/episode/<slug>` | – | `{"deleted": true}`; 404 if unknown, 400 for slugs that are not uploads |

The WAV is encoded to MP3 right away (no queue), with an HTML transcript and a feed item. Prefix the title with a type
(e.g. `Concept - <title>`) to make uploads filterable in podcast apps next to `Briefing - …`. Uploads can only delete
uploads: generated episodes (`podcast_…`) are out of reach of this route.

Set `TTS_SYNTH_TOKEN` to require `Authorization: Bearer <token>` on all `/v1` routes; leave it empty to keep them open
(the service is only reachable on the internal Docker network).

    curl -s -X POST http://tts:5000/v1/synthesize -H 'Content-Type: application/json' \
         -H "Authorization: Bearer $TTS_SYNTH_TOKEN" -d '{"text":"Hello there.","voice":"bf_emma"}' -o hello.wav

## Ranking papers

For each new paper the Scout computes

```
composite = max_ref( cosine(paper, ref) × tier_weight(ref) ) × author_weight × (1 + 0.1·log10(scites + 1))
```

with tier weights 1.30 / 1.15 / 1.00 for reference papers in Tier 1 / 2 / 3 and
author weights 1.30 / 1.15 for known authors in Tier 1 / 2. Papers above
`SCOUT_MIN_SCORE` go to the LLM curator, which picks up to three for Briefings
and sends the remaining in-scope papers to the overflow queue. If fewer than
three are chosen, the queue (and an optional citation library, see below) fills
the gap.

**The reference library starts empty**: until you `/curate` some papers, every
cosine score is 0 and nothing passes `SCOUT_MIN_SCORE`. Ten to twenty papers
that define your interests are a good start. Adding a Tier 1/2 paper also adds
its authors to `known_authors`.

*Optional:* `data/state/citation_lookup_library.json` (an object of
`{key: {title, arxiv|doi|url, authors, hit_count}}`) is used as a weighted random
fallback when there are not enough new papers. The pipeline only reads and prunes
it; generating it is up to you.

## Episode formats

| Format | Length | How it is written |
|---|---|---|
| Briefing | ~5 min | one prompt (Gemini first, fallback model second) |
| Breakdown | ~15 min | one prompt, five fixed chapters |
| Deep Dive | ~45 min | LLM outline of five chapters, then one call per chapter |
| Lecture Series | several episodes | sections are grouped into 2–5 episodes, each outlined and written chapter by chapter |

The text comes from arXiv's HTML rendering when available, otherwise from the
PDF via `pdf-converter`. MP3s carry ID3 chapter markers; every episode also gets
an HTML transcript linked from the feed (`podcast:transcript`).

## Data, backups and updates

| Path | Content |
|---|---|
| `data/postgres/` | n8n database (workflows, encrypted credentials, executions) and the `pipeline` database |
| `data/n8n/` | n8n settings and binary data |
| `data/state/` | `arxiv_overflow.json` (queue), optional `citation_lookup_library.json` |
| `data/podcasts/` | MP3s, transcripts, `feed.xml` |
| `data/qdrant/`, `data/ollama/` | search index, embedding model (both can be rebuilt) |

Back up `.env`, `data/postgres` (or `pg_dumpall`), `data/state` and `data/podcasts`.
The Qdrant index can be rebuilt with the *Podcast Feed Re-index* workflow.

To update: `git pull`, `docker compose pull`, `docker compose up -d --build`, then
`./scripts/bootstrap.sh --workflows` if workflow files changed.
Image versions are pinned in `compose.yml`.

## Changing the workflows

Edit in the n8n UI, then export and check before committing:

```bash
docker compose exec -T n8n n8n export:workflow --id=<ID> --pretty --output=/tmp/wf.json
docker compose cp n8n:/tmp/wf.json n8n/workflows/<name>.json
pip install -r requirements-dev.txt && pytest
```

Before committing, remove `pinData`, `shared`, `versionMetadata`, `staticData` and any
hard-coded hosts, IDs or e-mail addresses; the tests in `tests/test_workflows.py` fail
if they find them. Settings belong in `.env` and are read with `$env.NAME`.
New variables must also be added to the `n8n` service in `compose.yml`.

## Security

- **The n8n webhooks are not authenticated.** Anyone who can reach
  `WEBHOOK_URL` can trigger episodes (which costs LLM tokens), read the curation
  list or modify the reference library. Keep n8n on a private network or VPN, or
  put an authenticating reverse proxy in front of `/webhook/` that still lets your
  Mattermost server through. Never forward port 5678 on your router.
- The workflows need `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, so anyone who can edit
  workflows in n8n can read the n8n container environment (including the database
  password and encryption key). Do not give other people editor access.
  API keys for LLMs, Zotero and Mattermost are **not** in that environment; they are
  stored encrypted as n8n credentials.
- Code nodes may use only the `fs`, `path` and `http` built-ins, and file access is
  restricted to `/data`. (The Code-node sandbox has no `URL` global; CI checks for it.)
- `pdf-converter` only downloads from `PDF_ALLOWED_HOSTS`, and uploads are capped by
  `PDF_MAX_MB`. Postgres, Qdrant, TTS and the converter are not published on the
  host. Postgres, Qdrant and TTS sit on an internal-only network.
- The feed has no authentication. Anyone with the URL can download the episodes.
  Publish it only where you are allowed to share audio derived from the papers.
- Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| Slash command: "Unable to connect" | Mattermost cannot reach `WEBHOOK_URL`; see *Allow untrusted internal connections* |
| Scout never selects anything | The reference library is empty (`/curation`) or `SCOUT_MIN_SCORE` is too high |
| "Credential … not found" | Run `./scripts/bootstrap.sh --credentials`; credential IDs are fixed |
| Episode never appears | `docker compose ps tts` (health) and `docker compose logs tts`; the log shows each finished or failed job |
| `model load failed` in TTS health | Run `./scripts/download-models.sh`; `./models` must contain both files |
| TTS log: `libespeak-ng.so: failed to map segment` | `/tmp` of the tts container must allow exec (`tmpfs: ["/tmp:exec,mode=1777"]`, as in `compose.yml`) |
| n8n log: `Failed to start Python task runner` | Harmless; no workflow uses Python Code nodes |
| SciRate empty | FlareSolverr logs; SciRate may be rate-limiting. arXiv results still work without it |
| Code node: "Cannot find module 'fs'" | `NODE_FUNCTION_ALLOW_BUILTIN` missing from the n8n environment |
| `$env` is undefined/blocked | `N8N_BLOCK_ENV_ACCESS_IN_NODE` must be `false` |
| Embedding dimension error | `EMBEDDING_MODEL` must output 768 dimensions, or change `vector(768)` in the schema |

## Licences

This repository is MIT-licensed (see [LICENSE](LICENSE)). The components it
runs have their own licences, notably: n8n (Sustainable Use License),
PyMuPDF / pymupdf4llm used by `pdf-converter` (AGPL-3.0 or commercial), Kokoro-82M
weights (Apache-2.0), kokoro-onnx (MIT), FlareSolverr (MIT), Zotero translation
server (AGPL-3.0). Check them before you offer the stack as a service to others.

Respect the [arXiv API terms of use](https://info.arxiv.org/help/api/tou.html) and
the licences of the papers you convert.

## Acknowledgements

Parts of the code and documentation were generated with the help of Anthropic's
Claude Opus and Google Gemini.
