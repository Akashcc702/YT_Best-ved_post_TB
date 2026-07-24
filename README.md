# YT AI Digest Bot

A Telegram bot that searches YouTube daily for newly-uploaded, practical AI/automation/coding
tutorials, scores them with an AI model, and posts the top 3 to a Telegram channel — fully
automated, built on free-tier services.

**Read this first — honest scope note:** the original spec asked for dozens of bonus features
(weekly digests, skill graphs, caption transcription, monthly analytics, etc.). This build
implements the full core pipeline end-to-end and production-grade: real YouTube search, real
rule + AI filtering, real scoring, real Telegram posting, a working scheduler, `/today /top
/history /search /settings /help` commands, retries, logging, and a real Render deployment. The
long-tail bonus features are **not** all implemented — adding them on this foundation is
straightforward and the "Extending this" section below tells you where each one plugs in,
but claiming they're all done out of the box would be dishonest. What's here is real,
tested, and deployable today.

---

## 1. How it works

```
YouTube search (per query, last 24h, sorted by date)
        │
        ▼
Dedup + hydrate with statistics/contentDetails  (app/youtube/client.py)
        │
        ▼
Rule-based keyword/category filter               (app/youtube/filters.py)
        │
        ▼
AI scoring — OpenRouter or Gemini, JSON output    (app/ranking/scorer.py)
        │
        ▼
Sort by overall_score, take top N                 (app/ranking/pipeline.py)
        │
        ▼
Format MarkdownV2 message, post to channel         (app/telegram_bot/*)
        │
        ▼
SQLite: store video + mark posted (dedup forever)  (app/database/db.py)
```

The same `run_pipeline()` function powers the 09:00 scheduled job **and** the `/today` and
`/search` commands, so behavior never diverges between automatic and manual runs.

## 2. Project structure

```
app/
  main.py               entrypoint: aiohttp server (health + webhook) + startup wiring
  config.py              pydantic-settings config, reads from env
  database/db.py         SQLite schema + all queries
  youtube/client.py      YouTube Data API v3 search + video details
  youtube/filters.py     keyword/category rule filter
  ranking/prompts.py     the AI scoring prompt
  ranking/scorer.py      OpenRouter/Gemini calls + JSON parsing
  ranking/pipeline.py    orchestrates search -> filter -> score -> select
  telegram_bot/bot.py    command handlers + post_digest()
  telegram_bot/formatter.py   MarkdownV2 message building
  scheduler/jobs.py      APScheduler daily cron job
  utils/logger.py, retry.py
tests/                   pytest unit tests (filters, JSON parsing)
requirements.txt runtime.txt render.yaml Procfile .env.example
```

## 3. Database schema

```sql
videos          -- every candidate ever scored (metadata + full AI score JSON)
posted_videos   -- which videos were actually posted (this is what prevents duplicates)
logs            -- one row per scheduled/manual run: status + detail
settings        -- key/value store for future runtime-tunable settings
```
Full DDL is in `app/database/db.py::SCHEMA`.

## 4. Getting your API keys

1. **Telegram bot token**: message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the
   token into `TELEGRAM_BOT_TOKEN`.
2. **Telegram channel**: create a channel, add your bot as an **admin** (needs "Post Messages"
   permission), set `TELEGRAM_CHANNEL_ID` to `@yourchannelusername` (public) or the numeric
   `-100xxxxxxxxxx` id (private — get it by forwarding a channel message to
   [@userinfobot](https://t.me/userinfobot) or via the Bot API `getUpdates`).
3. **Your Telegram user id** (for `TELEGRAM_ADMIN_IDS`): message [@userinfobot](https://t.me/userinfobot).
4. **YouTube Data API v3 key**: [Google Cloud Console](https://console.cloud.google.com/) →
   create project → enable "YouTube Data API v3" → Credentials → Create API key. Free quota is
   10,000 units/day; this bot's default 8 daily search queries use roughly 800 units/day.
5. **OpenRouter key** (default AI provider): [openrouter.ai/keys](https://openrouter.ai/keys) —
   pick a model whose name ends in `:free` (e.g. `meta-llama/llama-3.1-8b-instruct:free`) to stay
   at zero cost; check OpenRouter's models page for current free options since these rotate.
   Alternatively set `AI_PROVIDER=gemini` and use a free
   [Gemini API key](https://aistudio.google.com/apikey) instead.

## 5. Local setup

```bash
git clone <your-repo>
cd yt-ai-digest-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
pytest                 # run the unit tests
python -m app.main     # starts the web server on $PORT (default 10000)
```

Locally, without a public HTTPS URL, `PUBLIC_BASE_URL` should stay empty — the bot will run the
scheduler but Telegram commands won't be reachable (Telegram needs to POST to a real HTTPS
webhook). To test commands locally, use a tunnel (`ngrok http 10000`) and set
`PUBLIC_BASE_URL` to the ngrok URL.

## 6. Deploying to Render (free tier)

1. Push this project to a GitHub repo.
2. In Render, **New → Blueprint**, point it at your repo — it will read `render.yaml`
   automatically and create a Web Service.
3. Render will prompt you for the `sync: false` env vars (`TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_IDS`, `YOUTUBE_API_KEY`, `OPENROUTER_API_KEY`,
   `GEMINI_API_KEY`, `PUBLIC_BASE_URL`) — fill these in the Render dashboard.
4. `PUBLIC_BASE_URL` should be `https://<your-service-name>.onrender.com` (Render shows you the
   exact URL after the first deploy — set this env var, then redeploy once so the webhook
   registers against the right URL).
5. `WEBHOOK_SECRET` is auto-generated by Render (`generateValue: true`) — leave it as is.
6. Deploy. Check `https://<your-service>.onrender.com/health` returns `{"status": "ok"}`.
7. Message your bot's `/help` command in Telegram (DM the bot, or use it in the channel if it
   can read messages there) to confirm the webhook is live.

### Free tier caveats (read this before relying on it)

- **Spin-down**: Render's free web services sleep after ~15 minutes without inbound HTTP
  traffic, and cold-start takes 30–60s. A sleeping service **will not fire the 09:00 cron job**
  because there's no internal always-on process once it's asleep.
  **Fix**: use a free uptime pinger (e.g. [UptimeRobot](https://uptimerobot.com) or
  [cron-job.org](https://cron-job.org)) to hit `/health` every 10 minutes, which keeps the
  service warm enough for APScheduler to fire on schedule. `misfire_grace_time=3600` in
  `scheduler/jobs.py` also means if the service *does* wake up late, it still runs the job as
  long as it's within an hour of the scheduled time.
- **Ephemeral disk**: the free plan has no persistent disk (that's a paid add-on). This means
  the SQLite file — and with it your dedup history — is wiped on every deploy or restart. For a
  hobby project this is usually fine (worst case: an old video gets reposted once). If you need
  durable history, either upgrade to a Render paid plan with a persistent disk, or swap
  `DATABASE_PATH` for a free hosted Postgres (e.g. Supabase/Neon free tier) — the `Database`
  class in `app/database/db.py` is the only place that would need a driver swap.
- **More reliable free alternative for the schedule specifically**: a
  [GitHub Actions scheduled workflow](https://docs.github.com/actions/using-workflows/events-that-trigger-workflows#schedule)
  that runs `python -m app.run_once` (see "Extending this" below) on a cron and calls the
  Telegram Bot API directly is more reliable than relying on Render's free tier staying awake,
  since GitHub Actions cron doesn't depend on inbound traffic to a sleeping web service.

## 7. Environment variables reference

See `.env.example` for the full list with defaults. Key ones:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHANNEL_ID` | bot auth + where to post |
| `TELEGRAM_ADMIN_IDS` | who can run `/today`, `/search` (empty = anyone) |
| `YOUTUBE_API_KEY` | YouTube Data API v3 |
| `AI_PROVIDER` | `openrouter` or `gemini` |
| `SCHEDULE_TIME` / `SCHEDULE_TIMEZONE` | when the daily job runs |
| `SEARCH_QUERIES` | comma-separated topics searched every day |
| `TOP_N` | how many videos to post (default 3) |
| `PUBLIC_BASE_URL` / `WEBHOOK_SECRET` | Telegram webhook registration |

## 8. Telegram commands

- `/today` — run the pipeline right now and post to the channel (admin-restricted)
- `/top` — show the last posted top videos
- `/history` — recent posting history
- `/search <topic>` — one-off search + rank on any topic, replies in chat, doesn't post to the
  channel (admin-restricted)
- `/settings` — show current configuration
- `/help` — command list

## 9. Logs & monitoring

- Logs go to stdout (visible in the Render dashboard's Logs tab) and to `./logs/app.log`
  (ephemeral, same caveat as the DB above).
- Every pipeline run — scheduled or manual — writes a row to the `logs` table
  (`run_type`, `status`, `detail`) via `Database.add_log`.
- `GET /health` is the liveness/readiness endpoint Render's health check and any uptime pinger
  should hit.

## 10. Troubleshooting / FAQ

- **Bot doesn't respond to commands**: check `PUBLIC_BASE_URL` is set correctly and matches your
  actual Render URL, then redeploy — the webhook is only (re)registered on startup.
- **"No qualifying videos found" every day**: your `SEARCH_QUERIES` may be too narrow, or
  `SEARCH_MAX_CANDIDATES` too low. Try `/search <topic>` to debug a single query interactively.
- **YouTube quota exceeded (403 quotaExceeded)**: each `search.list` call costs 100 units;
  reduce the number of `SEARCH_QUERIES` or `SEARCH_MAX_CANDIDATES`, or request a quota increase
  in Google Cloud Console.
- **OpenRouter free model returning empty/garbage JSON**: free models vary in reliability.
  `_extract_json()` in `app/ranking/scorer.py` already strips markdown fences and grabs the
  first `{...}` block as a fallback, but if a specific free model is consistently bad, swap
  `OPENROUTER_MODEL` for a different free one, or switch `AI_PROVIDER=gemini`.
- **Messages not appearing in the channel**: confirm the bot is an **admin** of the channel with
  "Post Messages" permission, and `TELEGRAM_CHANNEL_ID` is correct (public channels: `@handle`;
  private: numeric id starting with `-100`).
- **Duplicate videos posted after a Render restart**: expected on the free tier — see the
  "Ephemeral disk" caveat above.

## 11. Testing

```bash
pytest -v
```
Covers the keyword/category filter logic and the AI JSON-extraction/flattening logic — the two
places most likely to silently break as YouTube results or model output formats drift.

## 12. Extending this

The spec's full bonus-feature wishlist wasn't all built in, but the architecture leaves clean
seams for each:

- **Weekly/monthly digests**: add another `CronTrigger` job in `scheduler/jobs.py` that queries
  `db.recent_posted()` over a longer window and reuses `formatter.py`.
- **Caption-based summaries**: `youtube-transcript-api` (free, no key) can pull captions by
  video id; feed the transcript into `ranking/prompts.py` alongside the metadata for richer
  summaries.
- **HuggingFace/Ollama model extraction**: the AI prompt already asks for
  `huggingface_models_mentioned` / `ollama_models_mentioned` — they're just not currently
  rendered in the Telegram message or stored as separate DB columns; both are one-line additions
  in `formatter.py` and `db.py`.
- **A `run_once` script for GitHub Actions cron** (recommended free-tier reliability fix): a
  small script that builds the `Application` in webhook-less mode, calls `post_digest()` once,
  and exits — no long-running server needed for the daily post itself.
- **Postgres instead of SQLite** for durable history on Render's free plan: swap the `sqlite3`
  calls in `database/db.py` for `asyncpg`/`psycopg`, pointed at a free Supabase/Neon instance.
