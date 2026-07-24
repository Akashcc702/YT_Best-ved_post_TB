# YT AI Digest Bot -- compact build (5 files)

Same bot, condensed into 5 files. **YouTube access was switched from the official YouTube
Data API v3 (needs a Google Cloud API key + has a daily quota) to the [Invidious](https://docs.invidious.io/api/)
API — free, keyless, no quota, no Google account.** Everything else is unchanged.

**Files in this delivery:**

| File | What's in it |
|---|---|
| `app.py` | Everything: config, logging/retry, SQLite database, YouTube search + filters (now via Invidious), AI scoring (OpenRouter/Gemini), Telegram message formatting + bot commands, APScheduler daily job, aiohttp entrypoint (health check + webhook). Sections in order: CONFIG → LOGGING & RETRY → DATABASE → YOUTUBE FILTERS → YOUTUBE CLIENT → AI RANKING (prompts/scorer/pipeline) → TELEGRAM FORMATTER → TELEGRAM BOT → SCHEDULER → ENTRYPOINT. |
| `test_app.py` | 12 unit tests — filters, AI JSON parsing, message formatting, and the new Invidious field-mapping. Run with `pytest test_app.py`. |
| `requirements.txt` | Trimmed to exactly what `app.py` imports. |
| `.env.example` | Every environment variable the app reads, with comments. |
| `render.yaml` | Render Blueprint deploy config (`startCommand: python app.py`). |
| `runtime.txt` | Pins the Python version Render builds with. **Required** — without it Render may pick a newer Python (e.g. 3.14) for which `pydantic-core` has no prebuilt wheel yet, causing a Rust/maturin build failure (see "Fixed: build failure" below). |

## Why Invidious (of the 5 options you listed)

- **Piped** — also keyless and viable; I noted it in the code as a drop-in alternative if
  Invidious instances give you trouble. I picked Invidious over it because its `/api/v1/search`
  endpoint has an explicit `sort_by=upload_date` + `date=` filter that maps directly onto this
  project's "newest videos, last 24h" requirement, and `/api/v1/videos/{id}` returns view count,
  like count, duration, description, and category in one call — closely matching what the
  pipeline needs.
- **YouTube Operational API** — good for channel-level stats (subscriber counts, full upload
  lists for a known channel), not built for open-ended topic search across all of YouTube,
  which is what "find today's best AI/Python videos" needs.
- **yt-dlp** — excellent, no quota, but it's a scraping-style extraction tool, not a search
  index; you'd still need something to search *by topic* first, then hand it URLs. Good fit as
  an add-on later (e.g. richer metadata or fallback fetch), not as the primary search source.
- **RapidAPI third-party APIs** — free tiers are typically capped very low (e.g. 500
  requests/month), tighter than the official API's own free quota, so it doesn't actually solve
  the "avoid limits" goal.

## What changed in the code

Only the **YOUTUBE CLIENT** and **YOUTUBE FILTERS** sections, plus config:

- `YouTubeClient` now calls Invidious's `/api/v1/search` and `/api/v1/videos/{id}` instead of
  Google's `search.list`/`videos.list`. Same class name, same methods (`search_recent`,
  `get_video_details`, `normalize`), same output shape — the rest of the pipeline
  (`collect_candidates`, `apply_rule_filters`, `score_candidates`, `run_pipeline`) needed **zero
  changes**.
- **Multi-instance failover**: `INVIDIOUS_INSTANCES` is a comma-separated list; the client tries
  each in order and moves on if one is down. Tested with a mocked dead-instance scenario —
  failover works and logs the failed instance before succeeding on the next.
- **Category filter** changed from numeric YouTube category ids (`"25"` = News) to Invidious's
  human-readable `genre` string (`"News & Politics"`), since that's the field Invidious actually
  returns.
- `YOUTUBE_API_KEY` is gone from config, `.env.example`, and `render.yaml` — replaced by
  `INVIDIOUS_INSTANCES` with sensible public defaults baked in, so **you can deploy without
  signing up for anything YouTube/Google-related at all.**
- Comment counts are no longer collected (Invidious doesn't expose them on the main video
  endpoint without an extra paginated call per video) — the AI scorer never used that field for
  its verdict anyway, so this doesn't change ranking quality, just drops an unused DB value to 0.

Verified after the change: `python -m py_compile app.py` (clean), full `pytest test_app.py` (12/12
pass, **run without any `YOUTUBE_API_KEY` set** to confirm it's no longer required), and a mocked
failover test showing the client correctly skips a dead instance and succeeds on the next.

## Fixed: build failure (`pydantic-core` / maturin / Rust, exit code 1)

If you saw this in your Render build log:

```
Using Python version 3.14.3 (default)
...
Collecting pydantic-core==2.23.4
  Preparing metadata (pyproject.toml): finished with status 'error'
  💥 maturin failed
    Caused by: Cargo metadata failed...
  error: failed to create directory `/usr/local/cargo/registry/cache/...`
  Caused by: Read-only file system (os error 30)
error: metadata-generation-failed
```

**What happened:** Render defaulted to Python 3.14 for the build. `pydantic-core==2.23.4` (a
dependency of `pydantic`) has no prebuilt wheel for Python 3.14 yet, so pip fell back to
compiling it from source with Rust/maturin — and that compile needs to write to a Cargo cache
directory that's read-only in Render's build environment, so it failed outright.

**The fix:** this delivery now includes **`runtime.txt`** containing `python-3.12.6`, which
pins Render to a Python version `pydantic-core` already ships a prebuilt wheel for — no Rust
compile needed. `render.yaml` also sets `PYTHON_VERSION=3.12.6` as a second, redundant pin (in
case your service was created as a plain Web Service rather than through the Blueprint, where
`render.yaml`'s env vars aren't read).

**What to do:**
1. Add `runtime.txt` (included in this delivery) to your repo root, alongside `app.py`.
2. Commit and push it.
3. On Render: **Manual Deploy → Clear build cache & deploy** (a plain redeploy can reuse a
   stale cached environment — clearing the cache forces it to re-read `runtime.txt`).
4. Check the top of the new build log — it should now say `Using Python version 3.12.6`
   instead of `3.14.3`, and the `pydantic-core` step should download a wheel instead of trying
   to build one.

## Also changed: default AI model

`OPENROUTER_MODEL` now defaults to **`openrouter/free`** — OpenRouter's own Free Models Router,
which automatically picks a working free model for each request instead of pointing at one
specific model name that could be discontinued or renamed later. If you'd rather pin a specific
free model instead (for more consistent behavior between runs), set `OPENROUTER_MODEL` to any
model whose id ends in `:free` from https://openrouter.ai/models.



```bash
pip install -r requirements.txt
cp .env.example .env      # fill in your keys — no YouTube key needed anymore
pytest test_app.py        # 12 tests should pass
python app.py             # starts the web server on $PORT (default 10000)
```

## Deploying to Render (free tier) — step by step

### 1. Get your (now shorter) list of keys
- **Telegram bot token**: [@BotFather](https://t.me/BotFather) → `/newbot`.
- **Telegram channel id**: create a channel, add the bot as admin with "Post Messages"
  permission; use `@handle` for public channels or the numeric `-100...` id for private ones.
- **Your admin user id**: [@userinfobot](https://t.me/userinfobot).
- **OpenRouter key**: [openrouter.ai/keys](https://openrouter.ai/keys) — pick a model ending in
  `:free`.
- ~~YouTube API key~~ — **not needed anymore.** `INVIDIOUS_INSTANCES` already has working
  public defaults in `render.yaml`; you only need to touch it if those defaults go down (check
  https://api.invidious.io/ for current public instances).

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/yt-ai-digest-bot.git
git push -u origin main
```
Don't push a real `.env` file — only `.env.example`. Make sure **`runtime.txt`** is included —
it's what prevents the Python-3.14/pydantic-core build failure described above.

### 3. Create the Render Blueprint
1. [render.com](https://render.com) → sign up with GitHub.
2. Dashboard → **New → Blueprint** → select your repo → authorize.
3. Render reads `render.yaml` and creates a Web Service automatically.

### 4. Fill in the secrets Render asks for
Only 4 secrets now (down from 5): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`,
`TELEGRAM_ADMIN_IDS`, `OPENROUTER_API_KEY`. Leave `PUBLIC_BASE_URL` empty for now.
Click **Apply** — first deploy starts.

### 5. Set `PUBLIC_BASE_URL` and redeploy
1. After the first deploy, copy your service's URL from the Render dashboard (looks like
   `https://yt-ai-digest-bot-xxxx.onrender.com`).
2. Service → **Environment** tab → set `PUBLIC_BASE_URL` to that exact URL (no trailing slash).
3. Save — Render redeploys automatically, and the Telegram webhook registers against this URL
   on startup.

### 6. Verify
1. `https://<your-service>.onrender.com/health` → should return `{"status": "ok"}`.
2. Render **Logs** tab → look for `Telegram webhook registered at https://.../webhook/...`.
3. DM the bot `/help` on Telegram → it should reply.
4. DM `/today` (if you're in `TELEGRAM_ADMIN_IDS`) → runs the pipeline now and posts to the
   channel.

### 7. Keep it awake (free tier only)
Render's free web service sleeps after ~15 min idle, which can make it miss the scheduled
09:00 run. Add a free monitor on [UptimeRobot](https://uptimerobot.com) or
[cron-job.org](https://cron-job.org) hitting `/health` every 5–10 minutes to keep it warm.

## Free tier caveats
- **Sleep/wake**: see step 7 above. `misfire_grace_time=3600` in the scheduler also means a late
  wake-up still runs the job as long as it's within an hour of the scheduled time.
- **No persistent disk**: SQLite dedup history resets on every deploy/restart on the free plan.
  Fine for a hobby bot; for durable history, point the DATABASE section at a free hosted
  Postgres instead.
- **Invidious instance uptime**: public instances are volunteer-run and occasionally go down.
  `INVIDIOUS_INSTANCES` already lists several as fallbacks; if all of them are ever down at
  once, that day's search returns empty rather than crashing the app, and it just tries again
  tomorrow. For a more permanent fix, self-host one instance (free) and put it first in the list.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't respond to commands | Check `PUBLIC_BASE_URL` is correct and a redeploy happened after setting it |
| "No qualifying videos found" every day | Try `/search <topic>` to debug one query; widen `SEARCH_QUERIES` |
| `RuntimeError: All Invidious instances failed` in logs | All configured instances are down/unreachable right now — check https://api.invidious.io/ for currently healthy public instances and update `INVIDIOUS_INSTANCES` |
| Messages not posting to the channel | Bot must be a channel **admin** with "Post Messages" permission |
| Duplicate videos after a restart | Expected on free tier — see "No persistent disk" above |
| Build fails on `pydantic-core` / maturin / Rust / read-only file system | `runtime.txt` is missing or Render used a cached build — see "Fixed: build failure" section above |

## Telegram commands
`/today` (admin) run now + post • `/top` last posted set • `/history` recent posts •
`/search <topic>` (admin) one-off search, replies in chat only • `/settings` current config •
`/help` command list.

## Honest scope note
This still implements the core pipeline for real — search, filter, AI-score, post, dedup,
schedule, commands — not the entire original bonus wishlist (weekly/monthly digests, caption
transcription, skill graphs). Those extension points are unchanged; see the code's
SCHEDULER/ENTRYPOINT sections in `app.py` for where a second cron job would plug in.
