# YT AI Digest Bot -- compact build (5 files)

Same bot as the full modular build, condensed into fewer files on request. Functionally
identical: all tests pass, verified by running both the test suite and a live smoke test of
config loading, filters, DB, formatter, and Telegram `Application` construction.

**Files in this delivery:**

| File | What's in it |
|---|---|
| `app.py` | Everything: config, logging/retry, SQLite database, YouTube search + filters, AI scoring (OpenRouter/Gemini), Telegram message formatting + bot commands, APScheduler daily job, aiohttp entrypoint (health check + webhook). Organized into `# ===` labeled sections in this order: CONFIG → LOGGING & RETRY → DATABASE → YOUTUBE FILTERS → YOUTUBE CLIENT → AI RANKING (prompts/scorer/pipeline) → TELEGRAM FORMATTER → TELEGRAM BOT → SCHEDULER → ENTRYPOINT. |
| `test_app.py` | 11 unit tests covering the filter logic, AI JSON parsing, and message formatting — same coverage as the multi-file build. Run with `pytest test_app.py`. |
| `requirements.txt` | Trimmed to exactly what `app.py` imports. |
| `.env.example` | Every environment variable the app reads, with comments. |
| `render.yaml` | Render Blueprint deploy config, `startCommand: python app.py`, plus a Procfile-equivalent comment at the bottom for platforms that want one. |

**What changed from the original multi-file build:** only the file layout. No logic was
rewritten — I stripped each module's internal `from app.xxx import ...` lines (no longer
needed in one file) and merged the import blocks at the top; the function/class bodies are
untouched. I re-ran the full test suite plus a runtime smoke test (loading settings, running
the keyword filter, formatting a digest message, building the Telegram `Application`, hitting
the SQLite layer) against `app.py` directly to confirm nothing broke in the merge.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in your keys
pytest test_app.py        # 11 tests should pass
python app.py             # starts the web server on $PORT (default 10000)
```

## Deploying to Render (free tier)

1. Push these 5 files to a GitHub repo (root of the repo, no subfolders needed).
2. Render → New → Blueprint → point at the repo. It reads `render.yaml` automatically.
3. Fill in the `sync: false` env vars in the Render dashboard: `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_IDS`, `YOUTUBE_API_KEY`, `OPENROUTER_API_KEY` (or
   `GEMINI_API_KEY`), `PUBLIC_BASE_URL` (your Render URL, e.g.
   `https://yt-ai-digest-bot.onrender.com` — set after the first deploy, then redeploy once).
4. Confirm `https://<your-service>.onrender.com/health` returns `{"status": "ok"}`.

### Where to get each key
- **Telegram bot token**: [@BotFather](https://t.me/BotFather) → `/newbot`.
- **Telegram channel id**: create a channel, add the bot as admin with "Post Messages"
  permission; use `@handle` for public channels or the numeric `-100...` id for private ones.
- **Your admin user id**: [@userinfobot](https://t.me/userinfobot).
- **YouTube Data API v3 key**: Google Cloud Console → enable "YouTube Data API v3" → Credentials.
  Free quota 10,000 units/day; default 8 daily queries use ~800 units/day.
- **OpenRouter key**: [openrouter.ai/keys](https://openrouter.ai/keys) — pick a model ending in
  `:free`.

### Free tier caveats (same as the full build)
- Render's free web service **sleeps after ~15 min idle**, which can make it miss the 09:00
  cron fire — use a free pinger (UptimeRobot / cron-job.org) on `/health` to keep it warm, or
  run the daily post via a GitHub Actions scheduled workflow instead for more reliability.
- Free plan has **no persistent disk**, so the SQLite dedup history resets on every
  deploy/restart. Fine for a hobby bot; for durable history, point `DATABASE_PATH` logic at a
  free hosted Postgres instead (would need a driver swap in the DATABASE section of `app.py`).

## Telegram commands
`/today` (admin) run now + post • `/top` last posted set • `/history` recent posts •
`/search <topic>` (admin) one-off search, replies in chat only • `/settings` current config •
`/help` command list.

## Honest scope note
This still implements the core pipeline for real — search, filter, AI-score, post, dedup,
schedule, commands — not the entire original bonus wishlist (weekly/monthly digests, caption
transcription, skill graphs). Those extension points are unchanged from the full build; see the
code's ENTRYPOINT/SCHEDULER sections in `app.py` for where a second cron job would plug in.
