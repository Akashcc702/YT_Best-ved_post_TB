"""
YT AI Digest Bot -- single-file build

Daily pipeline: search YouTube -> rule filter -> AI score (OpenRouter/Gemini)
-> pick top N -> post to a Telegram channel. Runs as an aiohttp web service
(health check + Telegram webhook) with an APScheduler cron job inside it.

Sections (search for the "# ==" headers to jump around):
  CONFIG | LOGGING & RETRY | DATABASE | YOUTUBE | AI RANKING
  | TELEGRAM FORMATTER | TELEGRAM BOT | SCHEDULER | ENTRYPOINT
"""
from __future__ import annotations

# ---- stdlib ----
import asyncio
import json
import logging
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, List, Literal, Optional

# ---- third-party ----
import httpx
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("ytdigest")



# ==============================================================================
# CONFIG
# ==============================================================================

"""Centralized application configuration.

All runtime configuration is read from environment variables (see .env.example).
Using pydantic-settings gives us validation + sane errors on startup instead of
random KeyErrors three modules deep.
"""




class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_channel_id: str = Field(..., alias="TELEGRAM_CHANNEL_ID")
    telegram_admin_ids: str = Field(default="", alias="TELEGRAM_ADMIN_IDS")

    # --- YouTube (via the free, keyless Invidious API — see YOUTUBE CLIENT section) ---
    # Comma-separated list of Invidious instance base URLs, tried in order with automatic
    # failover since public instances go up/down. Put your own/self-hosted instance first
    # for reliability. See https://api.invidious.io/ for a live list of current public
    # instances if these defaults stop responding.
    invidious_instances: str = Field(
        default="https://invidious.jing.rocks,https://yewtu.be,https://invidious.nerdvpn.de,https://inv.nadeko.net",
        alias="INVIDIOUS_INSTANCES",
    )

    # --- AI ranking ---
    ai_provider: Literal["openrouter", "gemini"] = Field(default="openrouter", alias="AI_PROVIDER")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="meta-llama/llama-3.1-8b-instruct:free", alias="OPENROUTER_MODEL")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_MODEL")

    # --- Behaviour ---
    schedule_timezone: str = Field(default="Asia/Kolkata", alias="SCHEDULE_TIMEZONE")
    schedule_time: str = Field(default="09:00", alias="SCHEDULE_TIME")
    top_n: int = Field(default=3, alias="TOP_N")
    search_max_candidates: int = Field(default=40, alias="SEARCH_MAX_CANDIDATES")
    search_queries: str = Field(
        default="AI automation tutorial,python project tutorial",
        alias="SEARCH_QUERIES",
    )

    # --- Web server ---
    port: int = Field(default=10000, alias="PORT")
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    webhook_secret: str = Field(default="change-me", alias="WEBHOOK_SECRET")

    # --- Database ---
    database_path: str = Field(default="./data/app.db", alias="DATABASE_PATH")

    # --- Logging ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("telegram_admin_ids")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def admin_id_list(self) -> List[int]:
        return [int(x) for x in self.telegram_admin_ids.split(",") if x.strip()]

    @property
    def query_list(self) -> List[str]:
        return [q.strip() for q in self.search_queries.split(",") if q.strip()]

    @property
    def invidious_instance_list(self) -> List[str]:
        return [i.strip().rstrip("/") for i in self.invidious_instances.split(",") if i.strip()]

    @property
    def schedule_hour_minute(self) -> tuple[int, int]:
        h, m = self.schedule_time.split(":")
        return int(h), int(m)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ==============================================================================
# LOGGING & RETRY
# ==============================================================================

"""Application-wide logging configuration."""



def setup_logging(level: str = "INFO", log_dir: str = "./logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    try:
        file_handler = logging.FileHandler(Path(log_dir) / "app.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # Render's free tier disk is ephemeral; if the log dir isn't writable
        # for some reason, fall back to stdout-only logging instead of crashing.
        root.warning("Could not open log file, continuing with stdout logging only")

    # Quiet down noisy third-party libraries
    for noisy in ("httpx", "apscheduler", "telegram"):
        logging.getLogger(noisy).setLevel("WARNING")

"""Shared retry policy for flaky network calls (YouTube, AI provider, Telegram)."""


network_retry = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException, ConnectionError)),
)


# ==============================================================================
# DATABASE
# ==============================================================================

"""SQLite persistence layer.

Tables
------
videos          every candidate video we ever scored, with its metadata + AI scores
posted_videos   the subset of videos actually posted to Telegram (drives dedup)
logs            structured run log (one row per scheduler/command run)
settings        simple key/value store for runtime-tunable settings
"""



SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    channel_title       TEXT,
    description         TEXT,
    published_at        TEXT,
    duration_seconds    INTEGER,
    view_count          INTEGER,
    like_count          INTEGER,
    comment_count       INTEGER,
    thumbnail_url       TEXT,
    category_id         TEXT,
    tags                TEXT,               -- JSON list
    search_query        TEXT,
    ai_scores_json       TEXT,              -- full JSON blob from the AI scorer
    overall_score       REAL,
    summary             TEXT,
    skills_learned      TEXT,               -- JSON list
    github_repos        TEXT,               -- JSON list
    difficulty          TEXT,
    fetched_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posted_videos (
    video_id     TEXT PRIMARY KEY,
    rank         INTEGER,
    posted_at    TEXT DEFAULT (datetime('now')),
    message_id   INTEGER,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type    TEXT,           -- 'scheduled' | 'manual'
    status      TEXT,           -- 'success' | 'error' | 'empty'
    detail      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        logger.info("Database schema ready at %s", self.path)

    # ---------- videos ----------

    def upsert_video(self, video: dict[str, Any]) -> None:
        fields = (
            "video_id", "title", "channel_title", "description", "published_at",
            "duration_seconds", "view_count", "like_count", "comment_count",
            "thumbnail_url", "category_id", "tags", "search_query",
            "ai_scores_json", "overall_score", "summary", "skills_learned",
            "github_repos", "difficulty",
        )
        row = {f: video.get(f) for f in fields}
        for json_field in ("tags", "skills_learned", "github_repos"):
            if isinstance(row.get(json_field), (list, dict)):
                row[json_field] = json.dumps(row[json_field])
        if isinstance(row.get("ai_scores_json"), dict):
            row["ai_scores_json"] = json.dumps(row["ai_scores_json"])

        placeholders = ", ".join(f":{f}" for f in fields)
        columns = ", ".join(fields)
        updates = ", ".join(f"{f}=excluded.{f}" for f in fields if f != "video_id")
        sql = (
            f"INSERT INTO videos ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(video_id) DO UPDATE SET {updates}"
        )
        with self._conn() as conn:
            conn.execute(sql, row)

    def is_already_posted(self, video_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("SELECT 1 FROM posted_videos WHERE video_id = ?", (video_id,))
            return cur.fetchone() is not None

    def mark_posted(self, video_id: str, rank: int, message_id: Optional[int] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO posted_videos (video_id, rank, message_id) VALUES (?, ?, ?)",
                (video_id, rank, message_id),
            )

    def recent_posted(self, limit: int = 15) -> list[sqlite3.Row]:
        with self._conn() as conn:
            cur = conn.execute(
                """
                SELECT p.rank, p.posted_at, v.title, v.video_id, v.overall_score
                FROM posted_videos p JOIN videos v ON v.video_id = p.video_id
                ORDER BY p.posted_at DESC LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def last_top_n(self, n: int = 3) -> list[sqlite3.Row]:
        with self._conn() as conn:
            cur = conn.execute(
                """
                SELECT v.*, p.rank, p.posted_at FROM posted_videos p
                JOIN videos v ON v.video_id = p.video_id
                WHERE p.posted_at = (SELECT MAX(posted_at) FROM posted_videos)
                ORDER BY p.rank ASC LIMIT ?
                """,
                (n,),
            )
            return cur.fetchall()

    # ---------- logs ----------

    def add_log(self, run_type: str, status: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO logs (run_type, status, detail) VALUES (?, ?, ?)",
                (run_type, status, detail[:2000]),
            )

    def recent_logs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT ?", (limit,))
            return cur.fetchall()

    # ---------- settings ----------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


# ==============================================================================
# YOUTUBE FILTERS
# ==============================================================================

"""Rule-based pre-filtering.

This runs BEFORE the (rate-limited, slower) AI scoring step, so it's cheap
regex/keyword matching to throw out the obvious junk. It is intentionally
conservative — a keyword filter alone can't reliably tell "coding tutorial"
from "coding news roundup", so the AI scorer applies a second, smarter pass
on whatever survives this one.
"""


REJECT_KEYWORDS = [
    "reaction", "react to", "reacting to", "podcast", "interview", "vs code review",
    "unboxing", "news update", "breaking news", "celebrity", "politics",
    "election", "gossip", "drama", "clickbait", "you won't believe",
    "shocking", "exposed", "review of", "opinion:", "rant",
]

ACCEPT_KEYWORDS = [
    "tutorial", "how to", "guide", "build", "project", "step by step",
    "walkthrough", "automation", "workflow", "agent", "rag", "llm",
    "ollama", "huggingface", "docker", "python", "github", "open source",
    "ai automation", "mcp", "n8n", "api", "coding", "linux", "self-hosted",
    "local ai", "voice ai", "vision ai", "ocr", "free tool", "no code",
]

# Invidious exposes YouTube's category as a human-readable "genre" string
# (e.g. "Education", "Science & Technology", "News & Politics") rather than
# the numeric category id the official Data API used, so this is now a
# name-based reject list instead of an id-based one.
REJECT_GENRES = {"news & politics", "entertainment", "comedy", "people & blogs", "music"}


def passes_keyword_filter(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()

    if any(re.search(rf"\b{re.escape(k)}\b", text) for k in REJECT_KEYWORDS):
        return False

    # Require at least one positive signal so we don't forward totally
    # unrelated content (e.g. cooking, gaming) to the (costlier) AI step.
    return any(k in text for k in ACCEPT_KEYWORDS)


def passes_category_filter(genre: str | None) -> bool:
    if not genre:
        return True
    return genre.strip().lower() not in REJECT_GENRES


# ==============================================================================
# YOUTUBE CLIENT (via Invidious — free, keyless, no quota)
# ==============================================================================

"""Thin async wrapper around the Invidious API (https://docs.invidious.io/api/).

Invidious is an open-source, privacy-focused front end for YouTube. Its public
instances mirror YouTube's data and expose it over a REST API that needs no
API key, no Google account, and has no daily quota — unlike the official
YouTube Data API v3. The tradeoff: public instances are run by volunteers and
occasionally go down or rate-limit, so this client tries a list of instances
in order and fails over automatically. For production reliability, put a
self-hosted instance first in INVIDIOUS_INSTANCES (self-hosting Invidious is
itself free — see https://docs.invidious.io/installation/).

Note: Piped (https://piped.video) is a very similar keyless alternative built
the same way (public instances, no key, no quota) and could be swapped in by
reimplementing this class against Piped's `/search` and `/streams/{id}`
endpoints — worth trying if Invidious instances are unreliable for you.

Endpoints used:
  GET /api/v1/search?q=..&type=video&sort_by=upload_date&date=today
  GET /api/v1/videos/{id}
"""


class YouTubeClient:
    """Kept the original class name so the rest of the pipeline (and the
    variable names `yt`, etc. elsewhere) didn't need to change — only what's
    inside changed, from Google's API to Invidious."""

    def __init__(self, instances: list[str]):
        if not instances:
            raise ValueError("At least one Invidious instance URL is required")
        self.instances = instances
        self._client = httpx.AsyncClient(timeout=20)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """Try each configured instance in order until one responds successfully."""
        last_error: Exception | None = None
        for base in self.instances:
            try:
                resp = await self._client.get(f"{base}{path}", params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                logger.warning("Invidious instance %s failed for %s: %s", base, path, exc)
                last_error = exc
                continue
        raise RuntimeError(f"All Invidious instances failed for {path}") from last_error

    async def search_recent(
        self, query: str, max_results: int = 15, hours_back: int = 24
    ) -> list[dict[str, Any]]:
        """Search for videos, newest first, then keep only ones published within
        `hours_back` hours (Invidious's own `date` filter is coarse — hour/today/
        week/month/year — so we ask for `date=today` as a first pass and then
        apply the precise cutoff ourselves using each result's `published` unix
        timestamp)."""
        date_filter = "hour" if hours_back <= 1 else "today"
        params = {
            "q": query,
            "type": "video",
            "sort_by": "upload_date",
            "date": date_filter,
            "page": 1,
        }
        items = await self._get("/api/v1/search", params=params)
        if not isinstance(items, list):
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        fresh = [
            item for item in items
            if item.get("published") and datetime.fromtimestamp(item["published"], tz=timezone.utc) >= cutoff
        ]
        return fresh[:max_results]

    async def get_video_details(self, video_ids: list[str]) -> list[dict[str, Any]]:
        """Invidious has no batch endpoint, so fetch one at a time with bounded
        concurrency to be a reasonable citizen of free public instances."""
        sem = asyncio.Semaphore(5)
        results: list[dict[str, Any]] = []

        async def _fetch_one(vid: str) -> None:
            async with sem:
                try:
                    data = await self._get(f"/api/v1/videos/{vid}", params={})
                    results.append(data)
                except Exception:
                    logger.warning("Could not fetch details for video %s, skipping", vid)

        await asyncio.gather(*(_fetch_one(v) for v in video_ids))
        return results

    @staticmethod
    def normalize(item: dict[str, Any], search_query: str) -> dict[str, Any]:
        thumbs = item.get("videoThumbnails", []) or []
        thumbnail = thumbs[0].get("url") if thumbs else None
        for t in thumbs:
            if t.get("quality") in ("high", "hqdefault", "sddefault"):
                thumbnail = t.get("url")
                break

        published_ts = item.get("published")
        published_at = (
            datetime.fromtimestamp(published_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if published_ts else ""
        )

        return {
            "video_id": item.get("videoId"),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "channel_title": item.get("author", ""),
            "published_at": published_at,
            # Invidious calls YouTube's category "genre" (a human-readable
            # string, not a numeric id) — stored in the same DB column.
            "category_id": item.get("genre"),
            "tags": item.get("keywords", []),
            "thumbnail_url": thumbnail,
            "view_count": int(item.get("viewCount", 0) or 0),
            "like_count": int(item.get("likeCount", 0) or 0),
            # Invidious doesn't reliably expose a comment count on the main
            # video endpoint (it lives behind a separate paginated /comments
            # call), so this is left at 0 rather than spending an extra
            # request per candidate on a field the AI scorer doesn't rely on.
            "comment_count": 0,
            "duration_seconds": int(item.get("lengthSeconds", 0) or 0),
            "search_query": search_query,
        }


# ==============================================================================
# AI RANKING PROMPTS
# ==============================================================================

"""Prompt construction for the AI video-scoring step."""


SYSTEM_PROMPT = """You are a strict technical-content curator for a channel that teaches \
practical AI, automation, Python, and open-source developer skills. You evaluate \
YouTube videos from metadata only (title, description, channel, stats) and decide \
whether each one is genuinely educational and reproducible with free/open-source tools.

You are skeptical by default: clickbait titles, vague descriptions, pure news/opinion/reaction \
content, and anything requiring a paid API or paid course score low or get rejected.

You MUST respond with ONLY a single JSON object, no markdown fences, no commentary."""

SCORING_INSTRUCTIONS = """For the video below, return a JSON object with exactly this shape:

{
  "is_educational": true/false,
  "reject_reason": "" ,                     // short reason if is_educational is false, else ""
  "scores": {
    "educational_value": 0-10,
    "practical_value": 0-10,
    "career_value": 0-10,
    "portfolio_value": 0-10,
    "implementation_quality": 0-10,
    "beginner_friendliness": 0-10,
    "business_potential": 0-10,
    "open_source_score": 0-10,
    "reproducibility": 0-10,
    "cost_score": 0-10,                     // 10 = fully free, 0 = requires paid tools/APIs
    "documentation_quality": 0-10
  },
  "overall_score": 0-100,                   // your holistic weighted judgement, not a plain average
  "summary": "2-3 sentence plain-English summary of what the video teaches",
  "why_selected": "1-2 sentences on why this is worth a viewer's time today",
  "skills_learned": ["skill1", "skill2"],
  "prerequisites": ["prereq1"],
  "difficulty": "beginner" | "intermediate" | "advanced",
  "estimated_time_minutes": 0,
  "github_repos_mentioned": ["url or name if any, else empty list"],
  "huggingface_models_mentioned": [],
  "ollama_models_mentioned": [],
  "apis_used": [],
  "free_alternatives": ["if the video uses a paid tool, name a free alternative"],
  "startup_ideas": ["1 short idea"],
  "resume_bullet": "one resume-ready bullet point describing the skill gained"
}

Video metadata:
Title: {title}
Channel: {channel}
Published: {published_at}
Duration (seconds): {duration_seconds}
Views: {views} | Likes: {likes} | Comments: {comments}
Description (truncated): {description}
"""


def build_user_prompt(video: dict[str, Any]) -> str:
    description = (video.get("description") or "")[:1200]
    return SCORING_INSTRUCTIONS.format(
        title=video.get("title", ""),
        channel=video.get("channel_title", ""),
        published_at=video.get("published_at", ""),
        duration_seconds=video.get("duration_seconds", 0),
        views=video.get("view_count", 0),
        likes=video.get("like_count", 0),
        comments=video.get("comment_count", 0),
        description=description,
    )


# ==============================================================================
# AI RANKING SCORER
# ==============================================================================

"""AI-backed scoring of candidate videos.

Two interchangeable free backends are supported:
  - OpenRouter (AI_PROVIDER=openrouter) — call any model, including free-tier
    models such as `meta-llama/llama-3.1-8b-instruct:free`.
  - Gemini (AI_PROVIDER=gemini) — Google's free-tier Gemini API.

Both return the same normalized dict shape so the rest of the app never has
to know which provider produced a score.
"""





OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Models sometimes wrap JSON in markdown fences despite instructions; strip those."""
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: grab the first {...} block
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return None


class AIScorer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=45)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def score_video(self, video: dict[str, Any]) -> Optional[dict[str, Any]]:
        prompt = build_user_prompt(video)
        try:
            if self.settings.ai_provider == "openrouter":
                raw = await self._call_openrouter(prompt)
            else:
                raw = await self._call_gemini(prompt)
        except httpx.HTTPStatusError as exc:
            logger.warning("AI provider HTTP error for %s: %s", video.get("video_id"), exc)
            return None
        except httpx.HTTPError as exc:
            logger.warning("AI provider network error for %s: %s", video.get("video_id"), exc)
            return None

        if raw is None:
            return None
        parsed = _extract_json(raw)
        if parsed is None:
            logger.warning("Could not parse AI JSON response for %s", video.get("video_id"))
            return None
        return parsed

    @network_retry
    async def _call_openrouter(self, prompt: str) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        resp = await self._client.post(OPENROUTER_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return None
        return choices[0]["message"]["content"]

    @network_retry
    async def _call_gemini(self, prompt: str) -> Optional[str]:
        url = GEMINI_URL_TMPL.format(model=self.settings.gemini_model, key=self.settings.gemini_api_key)
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None
        return parts[0].get("text")


def flatten_ai_result(video: dict[str, Any], ai_result: dict[str, Any]) -> dict[str, Any]:
    """Merge the raw AI JSON into the flat dict shape the database expects."""
    scores = ai_result.get("scores", {})
    merged = dict(video)
    merged.update(
        {
            "ai_scores_json": ai_result,
            "overall_score": float(ai_result.get("overall_score", 0)),
            "summary": ai_result.get("summary", ""),
            "skills_learned": ai_result.get("skills_learned", []),
            "github_repos": ai_result.get("github_repos_mentioned", []),
            "difficulty": ai_result.get("difficulty", "unknown"),
        }
    )
    merged["_scores_detail"] = scores
    merged["_why_selected"] = ai_result.get("why_selected", "")
    merged["_is_educational"] = ai_result.get("is_educational", False)
    merged["_reject_reason"] = ai_result.get("reject_reason", "")
    return merged


# ==============================================================================
# AI RANKING PIPELINE
# ==============================================================================

"""End-to-end pipeline: search YouTube -> filter -> AI score -> pick top N.

This is the single function both the daily scheduler and the /today, /search
commands call, so behaviour never drifts between "automatic" and "manual" runs.
"""





async def collect_candidates(
    yt: YouTubeClient, queries: list[str], max_candidates: int, hours_back: int = 24
) -> list[dict[str, Any]]:
    """Search all configured queries, dedupe, and hydrate with full video details."""
    seen: dict[str, dict[str, Any]] = {}

    for query in queries:
        try:
            items = await yt.search_recent(query, max_results=15, hours_back=hours_back)
        except Exception:
            logger.exception("YouTube search failed for query %r", query)
            continue
        for item in items:
            vid = item.get("id", {}).get("videoId")
            if vid and vid not in seen:
                seen[vid] = {"_search_query": query}
        if len(seen) >= max_candidates:
            break

    if not seen:
        return []

    video_ids = list(seen.keys())[:max_candidates]
    details = await yt.get_video_details(video_ids)

    normalized = []
    for item in details:
        vid = item.get("id")
        query_used = seen.get(vid, {}).get("_search_query", "")
        normalized.append(YouTubeClient.normalize(item, query_used))
    return normalized


def apply_rule_filters(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    survivors = []
    for c in candidates:
        if not passes_category_filter(c.get("category_id")):
            continue
        if not passes_keyword_filter(c.get("title", ""), c.get("description", "")):
            continue
        survivors.append(c)
    return survivors


async def score_candidates(
    scorer: AIScorer, candidates: list[dict[str, Any]], concurrency: int = 3
) -> list[dict[str, Any]]:
    """Score candidates with the AI provider, bounded concurrency to respect rate limits."""
    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []

    async def _score_one(video: dict[str, Any]) -> None:
        async with sem:
            ai_result = await scorer.score_video(video)
            if ai_result is None:
                return
            if not ai_result.get("is_educational", False):
                logger.info(
                    "Rejected %s: %s", video.get("title"), ai_result.get("reject_reason", "")
                )
                return
            results.append(flatten_ai_result(video, ai_result))

    await asyncio.gather(*(_score_one(v) for v in candidates))
    return results


async def run_pipeline(
    settings: Settings,
    db: Database,
    queries: list[str] | None = None,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """Full run: returns the selected top-N videos (already persisted to the DB)."""
    queries = queries or settings.query_list
    top_n = top_n or settings.top_n

    yt = YouTubeClient(settings.invidious_instance_list)
    scorer = AIScorer(settings)
    try:
        candidates = await collect_candidates(
            yt, queries, settings.search_max_candidates
        )
        logger.info("Collected %d raw candidates", len(candidates))

        filtered = apply_rule_filters(candidates)
        logger.info("%d candidates survived keyword/category filters", len(filtered))

        # Skip videos already posted before spending AI calls on them
        filtered = [c for c in filtered if not db.is_already_posted(c["video_id"])]

        if not filtered:
            return []

        scored = await score_candidates(scorer, filtered)
        logger.info("%d candidates scored as educational", len(scored))

        for video in scored:
            db.upsert_video(video)

        scored.sort(key=lambda v: v.get("overall_score", 0), reverse=True)
        return scored[:top_n]
    finally:
        await yt.aclose()
        await scorer.aclose()


# ==============================================================================
# TELEGRAM FORMATTER
# ==============================================================================

"""Builds the Telegram digest message from a list of scored, ranked videos."""


MEDALS = ["🥇", "🥈", "🥉"]


def escape_md(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    special = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", text or "")


def format_video_block(video: dict[str, Any], index: int) -> str:
    medal = MEDALS[index] if index < len(MEDALS) else "▫️"
    title = escape_md(video.get("title", "Untitled"))
    channel = escape_md(video.get("channel_title", ""))
    url = f"https://www.youtube.com/watch?v={video.get('video_id')}"
    duration_min = round((video.get("duration_seconds") or 0) / 60)
    difficulty = escape_md(str(video.get("difficulty", "unknown")).title())
    summary = escape_md(video.get("summary", ""))
    why = escape_md(video.get("_why_selected", ""))
    score = video.get("overall_score", 0)

    skills = video.get("skills_learned") or []
    if isinstance(skills, str):
        skills = [skills]
    skills_txt = escape_md(", ".join(skills[:6]))

    repos = video.get("github_repos") or []
    if isinstance(repos, str):
        repos = [repos]
    repos_txt = escape_md(", ".join(repos[:3])) if repos else escape_md("None mentioned")

    return (
        f"{medal} *{title}*\n"
        f"📺 Channel: {channel}\n"
        f"⏱ Duration: {duration_min} min  •  🎯 Difficulty: {difficulty}  •  ⭐ Score: {escape_md(str(round(score, 1)))}/100\n\n"
        f"📝 {summary}\n\n"
        f"✅ Why watch: {why}\n"
        f"🧠 Skills: {skills_txt}\n"
        f"🔗 GitHub: {repos_txt}\n\n"
        f"▶️ [Watch Now]({url})"
    )


def format_digest(videos: list[dict[str, Any]]) -> str:
    if not videos:
        return "No qualifying educational videos were found in the last 24 hours\\. Try again later\\."

    header = "🏆 *Today's Best AI/Automation Learning Videos*\n" + "━" * 12 + "\n\n"
    blocks = [format_video_block(v, i) for i, v in enumerate(videos)]
    footer = "\n\n" + "━" * 12 + f"\n_Auto\\-curated daily digest • {len(videos)} of today's videos selected_"
    return header + ("\n\n" + "━" * 12 + "\n\n").join(blocks) + footer


def format_history(rows: list[Any]) -> str:
    if not rows:
        return "No videos have been posted yet\\."
    lines = ["🗂 *Recent posts*\n"]
    for r in rows:
        title = escape_md(r["title"])
        date = escape_md(str(r["posted_at"]))
        score = escape_md(str(round(r["overall_score"] or 0, 1)))
        url = f"https://www.youtube.com/watch?v={r['video_id']}"
        lines.append(f"• [{title}]({url}) — {date} — {score}/100")
    return "\n".join(lines)


# ==============================================================================
# TELEGRAM BOT
# ==============================================================================

"""Telegram bot: command handlers + the function that posts the daily digest."""






def _is_admin(settings: Settings, user_id: int) -> bool:
    admins = settings.admin_id_list
    return not admins or user_id in admins  # if no admins configured, allow everyone


async def post_digest(settings: Settings, db: Database, app: Application) -> list[dict]:
    """Runs the full pipeline and posts the result to the configured channel.
    Used by both the scheduler and /today. Returns the selected videos."""
    try:
        videos = await run_pipeline(settings, db)
    except Exception as exc:
        logger.exception("Pipeline run failed")
        db.add_log("scheduled", "error", str(exc))
        raise

    if not videos:
        db.add_log("scheduled", "empty", "No qualifying videos found")
        await app.bot.send_message(
            chat_id=settings.telegram_channel_id,
            text="No qualifying educational videos were found in the last 24 hours\\. "
                 "We'll check again tomorrow\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return []

    message = format_digest(videos)
    # Telegram hard limit is 4096 chars per message; split if needed.
    chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)] or [message]

    sent_message_id = None
    for chunk in chunks:
        sent = await app.bot.send_message(
            chat_id=settings.telegram_channel_id,
            text=chunk,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=False,
        )
        sent_message_id = sent.message_id

    for i, video in enumerate(videos):
        db.mark_posted(video["video_id"], rank=i + 1, message_id=sent_message_id)

    db.add_log("scheduled", "success", f"Posted {len(videos)} videos")
    return videos


# ---------- Command handlers ----------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *AI Learning Digest Bot*\n\n"
        "/today \\- run the pipeline now and post today's top videos\n"
        "/top \\- show the last posted top videos\n"
        "/history \\- show recent posting history\n"
        "/search <query> \\- manually search \\+ rank a topic \\(admin only\\)\n"
        "/settings \\- show current configuration\n"
        "/help \\- this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    if not _is_admin(settings, update.effective_user.id):
        await update.message.reply_text("This command is restricted to admins.")
        return
    await update.message.reply_text("Running today's search + ranking now, this can take a minute...")
    videos = await post_digest(settings, db, context.application)
    if videos:
        await update.message.reply_text(f"Posted {len(videos)} videos to the channel.")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]
    rows = db.last_top_n(settings.top_n)
    if not rows:
        await update.message.reply_text("No digest has been posted yet.")
        return
    videos = [dict(r) for r in rows]
    for v in videos:
        v.setdefault("skills_learned", [])
        v.setdefault("github_repos", [])
    text = format_digest(videos)
    await update.message.reply_text(text[:4000], parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    rows = db.recent_posted(limit=15)
    await update.message.reply_text(format_history(rows), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    if not _is_admin(settings, update.effective_user.id):
        await update.message.reply_text("This command is restricted to admins.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /search <topic>")
        return
    query = " ".join(context.args)
    await update.message.reply_text(f"Searching and ranking videos for: {query}")
    videos = await run_pipeline(settings, db, queries=[query], top_n=5)
    if not videos:
        await update.message.reply_text("No qualifying educational videos found for that query.")
        return
    text = format_digest(videos)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i : i + 4000], parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    text = (
        "⚙️ *Current settings*\n"
        f"Schedule: {settings.schedule_time} {settings.schedule_timezone}\n"
        f"Top N: {settings.top_n}\n"
        f"AI provider: {settings.ai_provider}\n"
        f"Search queries: {len(settings.query_list)} configured\n"
        f"Max candidates/day: {settings.search_max_candidates}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


def build_application(settings: Settings, db: Database) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("settings", cmd_settings))
    return app


# ==============================================================================
# SCHEDULER
# ==============================================================================

"""Daily digest job registration using APScheduler's async scheduler."""






def start_scheduler(settings: Settings, db: Database, app: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.schedule_timezone)
    hour, minute = settings.schedule_hour_minute

    async def _job() -> None:
        logger.info("Running scheduled daily digest job")
        try:
            await post_digest(settings, db, app)
        except Exception:
            logger.exception("Scheduled digest run failed")

    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="daily_digest",
        misfire_grace_time=3600,  # if Render was asleep/restarting, still run within an hour
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Scheduler started: daily digest at %02d:%02d %s", hour, minute, settings.schedule_timezone)
    return scheduler


# ==============================================================================
# ENTRYPOINT
# ==============================================================================

"""Entrypoint.

Runs a single aiohttp web server that:
  - answers Render's health checks on GET /health
  - receives Telegram updates via webhook on POST /webhook/<secret>
  - hosts the APScheduler job that posts the daily digest

Using a webhook (instead of long-polling) is what lets this run as a normal
Render *web service* — Render's free tier only keeps web services alive on
inbound HTTP traffic, and a webhook naturally provides that. See README.md
for the free-tier "keep-alive ping" caveat and a GitHub Actions alternative.
"""






async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_webhook(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    secret_in_path = request.match_info.get("secret", "")
    if secret_in_path != settings.webhook_secret:
        return web.Response(status=403, text="forbidden")

    application = request.app["telegram_app"]
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return web.Response(status=200)


async def on_startup(app: web.Application) -> None:
    settings = app["settings"]
    db = app["db"]
    telegram_app = app["telegram_app"]

    await telegram_app.initialize()
    await telegram_app.start()

    if settings.public_base_url:
        webhook_url = f"{settings.public_base_url.rstrip('/')}/webhook/{settings.webhook_secret}"
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram webhook registered at %s", webhook_url)
    else:
        logger.warning(
            "PUBLIC_BASE_URL is not set — Telegram commands will NOT work until it is configured. "
            "The daily digest scheduler will still run."
        )

    app["scheduler"] = start_scheduler(settings, db, telegram_app)
    logger.info("Application startup complete")


async def on_cleanup(app: web.Application) -> None:
    scheduler = app.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    telegram_app = app["telegram_app"]
    await telegram_app.stop()
    await telegram_app.shutdown()
    logger.info("Application shutdown complete")


def create_app() -> web.Application:
    settings = get_settings()
    setup_logging(settings.log_level)

    db = Database(settings.database_path)
    telegram_app = build_application(settings, db)

    web_app = web.Application()
    web_app["settings"] = settings
    web_app["db"] = db
    web_app["telegram_app"] = telegram_app

    web_app.router.add_get("/health", handle_health)
    web_app.router.add_get("/", handle_health)
    web_app.router.add_post("/webhook/{secret}", handle_webhook)

    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)
    return web_app


def main() -> None:
    settings = get_settings()
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
