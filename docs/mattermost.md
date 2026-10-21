# Mattermost setup

The pipeline talks to Mattermost in two directions:

- **n8n → Mattermost:** a bot account posts messages, cards and search results
  through the REST API (`MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`).
- **Mattermost → n8n:** slash commands, interactive buttons and form links call
  n8n webhooks under `WEBHOOK_URL`.

## 1. Bot account

1. System Console → Integrations → Bot Accounts → enable *Bot Account Creation*.
2. Integrations → Bot Accounts → *Add Bot Account*, e.g. `podcast-bot`, role *Member*.
3. Copy the access token into `.env` as `MATTERMOST_BOT_TOKEN`.
4. Add the bot to the channel you want to use (`/invite @podcast-bot`).
5. Channel name → *View Info* → copy the channel ID into `MATTERMOST_CHANNEL_ID`.

Then run `./scripts/bootstrap.sh --credentials`.

## 2. Let Mattermost reach n8n

If `WEBHOOK_URL` points to a private address (LAN IP, `localhost`, a VPN name),
Mattermost refuses to call it by default. Add the host name or IP (without port)
under System Console → Environment → Developer → **Allow untrusted internal
connections to**. This applies to slash commands and to interactive buttons.

The devices on which you open the upload/curation forms must reach
`WEBHOOK_URL` too, since form links open n8n directly.

## 3. Slash commands

Integrations → Slash Commands → *Add Slash Command*, one per row. Request method
**POST**. Autocomplete is optional. Mattermost sends the command arguments
as `text`, and the workflows read `channel_id` from the request too.

| Trigger word | Request URL (append to `WEBHOOK_URL`) | Autocomplete hint |
|---|---|---|
| `briefing` | `webhook/podcast-briefing` | `[arXiv ID]` |
| `breakdown` | `webhook/podcast-breakdown` | `[arXiv ID]` |
| `deepdive` | `webhook/podcast-deepdive` | `[arXiv ID]` |
| `podcast` | `webhook/podcast-form` | upload a PDF/EPUB |
| `overflow` | `webhook/arxiv_overflow` | review the overflow queue |
| `curate` | `webhook/curate-paper` | add a reference paper |
| `curate-author` | `webhook/curate-author` | add a known author |
| `curation` | `webhook/list-curation` | list reference papers and authors |
| `search` | `webhook/paper-search` | `[query]` |

`/curation` answers in the channel directly. The other commands reply through the
bot, usually within seconds; podcast commands post "Generating Podcast…" first.

Slash-command tokens are not checked by the workflows (see *Security* in the
README). Keep n8n reachable only from your own network.

## 4. Buttons

Overflow cards, search results and error cards contain buttons that call
`webhook/overflow-callback`, `webhook/llm-search-harder`,
`webhook/mattermost-search-paginate` and `webhook/retry-execution`. They need no
configuration beyond step 2. The *Retry* button on error cards also needs
`N8N_API_KEY` (n8n → Settings → n8n API → *Create API key*), followed by
`./scripts/bootstrap.sh --credentials`.
