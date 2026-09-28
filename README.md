# Telegram-to-X Poster Bot

A single-operator Telegram bot that drafts and publishes to X/Twitter. Text or media sent to the bot is stored in a local SQLite database and shown back as an inline preview card; text over the 280-character X limit is condensed by Groq first, and a long post can be split into a numbered thread. From the card the post is either published immediately or scheduled, in which case an APScheduler date job later runs the same publish pipeline (Tweepy v2 `create_tweet`, v1.1 media upload, thread replies chained through `in_reply_to_tweet_id`). An optional RSS poller turns feed entries into draft cards, and a read-only local HTTP dashboard serves the `posts` table from the same database file. Only Telegram user IDs listed in the admin variables can use the bot.

## Features

- Draft flow: text -> preview card with Confirm / Edit / Regenerate / Cancel / Duplicate / Tag / Media buttons, plus Templates when snippets exist; the published card adds Duplicate and Stats (`handlers/drafts.py`, `utils/ui.py`).
- Media intake: photos, videos and animations are downloaded to `MEDIA_DIR`; max 4 images per tweet, a video/gif never mixes with other files, and Telegram albums keep appending to the same draft for 10 minutes (`handlers/threads.py`).
- Groq: over-limit text is condensed in up to 3 attempts (targets 250/230/210 characters) with a word-boundary trim as fallback, then offered with Post Now / Edit / Make Thread; `make_thread()` asks Groq for a JSON array of parts and prefixes each with `i/n`, truncating any part still over 280 (`utils/groq_helper.py`, `handlers/editor.py`).
- Thread builder: `/thread` opens a builder (parts separated by `---` or quoted text), up to 25 tweets, minimum 2 to post, per-tweet text and per-tweet media slots (`handlers/threads.py`).
- Scheduling: month calendar, hour and minute pickers, typed custom time (`HH:MM` or `YYYY-MM-DD HH:MM`), `+30 min` quick slots, and Post Now; jobs are persisted by `AsyncIOScheduler` + `SQLAlchemyJobStore` in `data/jobs.db` with `coalesce`, `max_instances=1` and a 1-hour misfire grace, and posts that came due while the bot was offline are re-armed at startup (`handlers/calendar_kb.py`, `handlers/scheduler_flow.py`, `scheduler/jobs.py`).
- Publishing: markdown is stripped at publish time only (raw draft text is never mutated), every tweet is length-guarded, already-published thread tweets are skipped and their IDs reused, and success/failure is reported to the admin chat; a tweepy 429 does not fail the post but pushes it 15 minutes out and re-arms the job, and failures come back as Retry / Edit & Retry / Discard / "Retry (skip bad media)" buttons (`scheduler/publisher.py`, `handlers/drafts.py`).
- RSS auto-drafts: interval poll (floored at 5 minutes), SHA-1 link dedupe stored in the settings table, `RSS_MAX_PER_DAY` quota reset by a midnight cron job, heuristic tags (crypto / AI / meme / other), and Approve / Reject / Edit cards (`utils/rss.py`, `scheduler/jobs.py`).
- Templates and settings: named snippets in SQLite injected into a draft body or one chosen thread tweet (`handlers/templates.py`); IANA timezone picker plus typed input, auto-format toggle, and pause/resume that re-arms held jobs (`handlers/settings.py`).
- Queue and report: paginated scheduled/draft list with Edit, Reschedule, Delete (10-second undo window) and Post Now (`handlers/queue.py`); `/report` builds a digest from the local DB only (published counts for 7/30 days, queue, open drafts, failed, paused) with no X API calls (`handlers/report.py`).
- Single-instance guard and dashboard: exclusive OS lock on `data/bot.lock`, webhook removal plus `get_updates` preflight at startup, and 409 conflict classification that exits after `CONFLICT_ABORT_AFTER` consecutive conflicts (`utils/single_instance.py`, `main.py`); a stdlib `ThreadingHTTPServer` serves `/`, `/api/posts` and `/media/<name>` (GET/OPTIONS only) to a static Tailwind UI that polls the API every 3 seconds (`web/server.py`, `web/static/index.html`).

## Tech Stack

| Component | Choice | Version |
|---|---|---|
| Language | Python (`zoneinfo`; `db/models.py` evaluates `X \| None` at runtime, so 3.10+ is required) | 3.10+ (developed on 3.12.10) |
| Telegram framework | python-telegram-bot, async, long polling | 20.7 |
| ORM / storage | SQLAlchemy over SQLite with WAL and additive `ALTER TABLE` migration for `posts.media_ids` | 2.0.23 |
| Scheduler | APScheduler `AsyncIOScheduler` + `SQLAlchemyJobStore` | 3.10.4 |
| X API client | tweepy (v2 `Client` for tweets, v1.1 `API` for media upload) | 4.14.0 |
| LLM | Groq async chat completions (`groq` package is unpinned) | not pinned |
| RSS parsing | feedparser | 6.0.10 |
| HTTP transport for PTB | httpx | 0.25.2 |
| Env loading | python-dotenv | 1.0.0 |
| Timezones | stdlib `zoneinfo` + `tzdata` (Windows only) | — |
| Dashboard server | stdlib `http.server` (`ThreadingHTTPServer`) | — |
| Dashboard front end | `web/static/index.html`; Tailwind CSS and Font Awesome 6.4.0 loaded from CDNs | — |
| Pinned but unused | croniter — no import anywhere in the code | 2.0.1 |

## Architecture

Telegram handlers collect input and render preview cards; the database is the single source of truth; the scheduler re-runs the same publish path for anything that was queued; the dashboard only reads the database.

```text
Telegram (long polling, drop_pending_updates=True)
   |
   v
main.py  -- builds the PTB Application, registers handlers, token preflight, instance lock
   |
   v
handlers/  drafts.py (draft cards) · editor.py (edit flow, Groq card) · threads.py (builder, media)
           scheduler_flow.py + calendar_kb.py (calendar -> hour -> minute -> save) · queue.py
           settings.py · templates.py · report.py
   |
   +--> utils/groq_helper (compress_tweet / make_thread) --> Groq API
   |
   v
db/crud.py -> db/models.py -> SQLAlchemy -> SQLite data/bot.db
   |         (posts, settings, templates, threads, thread_tweets)
   |
   +--> scheduler/jobs.py (AsyncIOScheduler + SQLAlchemyJobStore data/jobs.db)
   |         |
   |         v
   |    scheduler/publisher.publish_post --> utils/twitter (tweepy) --> X API
   |         |                              v1.1 media upload + v2 create_tweet (thread replies)
   |         +--> success / failure / rate-limit messages to the admin chat
   |
   +--> utils/rss.py (interval poll) --> draft rows --> preview cards in Telegram
   |
   +--> web/server.py (read-only) --> GET / (index.html) · GET /api/posts · GET /media/<name>
```

Startup order in `main.py`: `load_dotenv()` -> single-instance lock -> `init_db()` -> `build_application()` -> `jobs.bootstrap(app)` -> `run_polling()`. Shutdown (`post_shutdown`) stops the scheduler and dashboard and releases the lock.

## Project Structure

```text
.
├── main.py                      Entry point: env load, handlers, error classification, startup hooks
├── requirements.txt             Pinned dependencies
├── .env.example                 Configuration template (copy to .env)
├── .gitignore                   Ignores .env, data/, logs/, virtualenvs
├── README.md                    This file
├── data/                        Runtime artifacts, created automatically, not committed
│   ├── bot.db                   Main SQLite database (posts/settings/templates/threads/thread_tweets)
│   ├── jobs.db                  APScheduler persistent job store
│   ├── bot.lock                 Single-instance lock file (OS lock on byte 0, holder PID after it)
│   └── media/                   Telegram downloads staged for upload (th_<hex>.<ext>, media_<msg_id>.jpg)
├── db/
│   ├── __init__.py              Package exports (engine, get_session, init_db, models)
│   ├── database.py              Engine/session bootstrap, WAL pragma, additive column migration
│   ├── models.py                Post, Setting, Template, Thread, ThreadTweet ORM models
│   └── crud.py                  CRUD helpers, thread helpers, settings, limits (25 tweets, 4 media)
├── handlers/
│   ├── __init__.py              Package marker (empty)
│   ├── drafts.py                Draft creation, preview-card callbacks, RSS draft push, failure actions
│   ├── editor.py                Edit-content / custom-time flows, media captions, Groq compression card
│   ├── threads.py               Thread builder, /thread command, media attach slots, album appending
│   ├── scheduler_flow.py        Calendar-day/hour/minute flow, save + reschedule
│   ├── calendar_kb.py           Month grid, hour and minute keyboards, local->UTC conversion
│   ├── queue.py                 Paginated queue, delete with 10 s undo, post-now, reschedule entry
│   ├── settings.py              /start /help /settings /pause /resume /set_tz, timezone handling
│   ├── templates.py             Template CRUD and injection (single drafts and thread tweets)
│   └── report.py                /report local health digest
├── scheduler/
│   ├── __init__.py              Package marker (empty)
│   ├── jobs.py                  Scheduler bootstrap, publish jobs, RSS poll, midnight quota reset
│   └── publisher.py             Publish pipeline: formatting, threads, notifications, 429 backoff
├── utils/
│   ├── __init__.py              Package marker (empty)
│   ├── config.py                Environment configuration and capability checks
│   ├── formatting.py            Markdown stripping, URL-aware length, truncation, preview HTML
│   ├── generator.py             Deterministic draft variants for the Regenerate button
│   ├── groq_helper.py           Groq compression and thread splitting
│   ├── rss.py                   Feed fetch, link dedupe, draft text generation, tag guessing
│   ├── single_instance.py       Cross-platform exclusive lock (msvcrt / fcntl)
│   ├── timeutil.py              Admin timezone helpers, naive-UTC storage convention
│   ├── twitter.py               tweepy wrapper: media upload, publish, optional stats
│   └── ui.py                    Admin guard, keyboards, preview rendering, in-memory undo store
├── web/
│   ├── __init__.py              Package marker (empty)
│   ├── server.py                Read-only dashboard server (/, /api/posts, /media/<name>)
│   └── static/
│       └── index.html           Dashboard UI (polls /api/posts every 3 s; mock-data toggle)
└── scripts/                     Offline smoke checks, no credentials required
    ├── check_single_instance.py Lock refusal, bypass, release, lock-file bytes
    ├── check_conflict_policy.py Preflight and 409 / InvalidToken classification
    ├── check_dashboard.py       Start/stop, busy-port warning, HTTP routes
    └── check_media_flow.py      Migration, media CRUD, album rules, previews, dashboard payload
```

## Configuration

`.env` is loaded before configuration is read. "Required" means the bot exits or refuses to publish without it. Placeholders only — never commit real values.

| Variable | Purpose | Required | Example |
|---|---|---|---|
| `BOT_TOKEN` | Telegram bot token used for long polling | yes (startup aborts) | `123456789:AA...` |
| `ADMIN_ID` / `ADMIN_IDS` | Allowed Telegram user IDs; `ADMIN_IDS` is comma-separated and read first, `ADMIN_ID` is the legacy single-value form | yes (startup aborts) | `123456789,987654321` |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | OAuth 1.0a user-context credentials; `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_SECRET` are honoured as fallbacks | yes to publish | `your_consumer_key` |
| `X_BEARER_TOKEN` / `TWITTER_BEARER_TOKEN` | Read-only token for the optional stats lookup; falls back to the user-context client | no | `AAAA...` |
| `DRY_RUN` | `1`/`true`/`yes` returns fake tweet and media IDs instead of calling X | no (default off) | `0` |
| `GROQ_API_KEY` | Enables Groq compression and thread splitting; empty disables both | no | `gsk_...` |
| `GROQ_MODEL` | Groq model name; the default differs per module (`llama-3.3-70b-versatile` in `config.py`, `qwen/qwen3.8-27b` in `groq_helper.py`, the latter being what runs) | no | `llama-3.3-70b-versatile` |
| `RSS_URL` | Feed polled for auto-drafts; empty disables RSS. Seeded into the `rss_url` setting once, after which the DB value wins | no | `https://example.com/feed.xml` |
| `RSS_MAX_PER_DAY` | Max RSS drafts per calendar day | no (default 5) | `5` |
| `RSS_POLL_MINUTES` | RSS poll interval; floored at 5 minutes | no (default 30) | `30` |
| `DASHBOARD_HOST` | Interface the dashboard binds to | no (default `127.0.0.1`) | `127.0.0.1` |
| `DASHBOARD_PORT` | Dashboard port; when busy the bot logs a warning and keeps polling | no (default `3000`) | `3000` |
| `CONFLICT_ABORT_AFTER` | Consecutive Telegram 409s tolerated before exiting; `0` warns forever | no (default 10) | `10` |
| `TIMEZONE` | IANA name seeded into the `timezone` setting on first start; later changed through `/settings` | no (default `UTC`) | `Africa/Lagos` |
| `MEDIA_DIR` | Download folder for Telegram files; the lock file is created in its parent | no (default `data/media`) | `C:\xposter\media` |
| `DB_PATH` (read in `db/database.py`, not `config.py`) | Main SQLite file location | no (default `data/bot.db`) | `C:\xposter\bot.db` |
| `ALLOW_MULTI_INSTANCE` (read in `utils/single_instance.py`) | Truthy value skips the single-instance lock | no | `1` |

Runtime-mutable state lives in the `settings` table, not in `.env`: `timezone`, `paused`, `auto_format`, `rss_url`, `rss_count_today`, plus one `rss_seen_<sha1>` key per processed feed link.

## Installation and Run (Windows)

```powershell
cd c:\Users\MAYOR\Desktop\Telegram-Twitter-Bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1            # if blocked: Set-ExecutionPolicy -Scope Process Bypass
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env                            # BOT_TOKEN, ADMIN_ID, X_* credentials, optional GROQ_API_KEY
python main.py
```

The first run creates `data/bot.db`, `data/jobs.db` and `data/media/`. Send `/start` to the bot to check it answers; the dashboard listens on `http://127.0.0.1:3000` unless `DASHBOARD_PORT` says otherwise. Stop with Ctrl+C. Offline smoke checks need no credentials and use isolated temp databases: `python scripts/check_media_flow.py`, `python scripts/check_dashboard.py`, `python scripts/check_single_instance.py`, `python scripts/check_conflict_policy.py`.

## X API Note

Publishing consumes prepaid credits on the X Developer project (pay-per-use billing). With a depleted balance X rejects write calls with HTTP 402 (payment required), and nothing is posted until credits are added in the X Developer Console.

What the code does, from `scheduler/publisher.py`:

- Only rate limits are special-cased: a tweepy `TooManyRequests`, or `HTTPException` with status 429, moves the post 15 minutes out, re-arms the job and notifies the admin.
- There is no 402 branch. Every other failure marks the row `status="failed"`, stores the API message in `posts.error`, and sends the failure card (Retry / Edit & Retry / Discard).
- `DRY_RUN=1` exercises the whole flow with fake IDs and no X calls at all.

## Known Limitations

- Thread routing is not automatic: over-limit text is compressed by Groq, but making a thread requires pressing the "Make Thread" button on the compression card, or starting a thread draft via `/thread` / the media prompt. RSS drafts are always single tweets.
- The "publish thread" branch behind the compression card (`action == "post_thread"` in `handlers/editor.py`) uses a direct `publish_tweet` loop: no 429 backoff, no per-tweet persistence, media on the first tweet only. No card currently emits that callback, so it is effectively unreachable; the DB-backed thread path through the scheduler handles all three.
- No media thumbnails: the dashboard loads the stored file (for a video, `th_<hex>.mp4` in `data/media/`) straight into a `<video>` element with a static "Video added" fallback, and the lightbox always assigns URLs to an `<img>`, so videos never open in the lightbox.
- Extension guessing can mislabel media: `_guess_ext()` falls back to `.jpg` when Telegram reports neither a file name nor a MIME type, so a video can be stored under an image extension and then be refused by `upload_media()`. Plain document messages are not routed at all — `main.py` registers photo, video/animation and text handlers only, although the media helpers can classify documents.
- Stats field mismatch: `utils/twitter.get_tweet_stats()` returns `impressions`, while the `st:` handler reads `stats.get("views", 0)`, so views always show 0 (likes, replies and retweets match). The lookup also depends on the API tier allowing it; otherwise the built-in `STATS_UNAVAILABLE_TEXT` message ("X API Free Tier does not support view/impression metrics") is sent.
- The dashboard is read-only: `web/server.py` implements GET and OPTIONS only, so the compose modal in `index.html` (and its mock-data toggle with hardcoded sample posts) only simulates posts in the browser. Nothing there is saved to the DB or published.
- In-memory state: edit, custom-time, thread-attach, template and timezone flows live in `context.user_data`; the calendar cursor is a module-level dict; the queue-delete undo snapshot is a dict. PTB persistence is not enabled, so a restart clears all of them.
- Length handling is asymmetric: a single tweet over 280 characters is silently trimmed at a word boundary before publishing, while an over-limit thread tweet raises an error and blocks the whole thread.
- Posts that came due while the bot was offline, or that are past the 1-hour misfire grace, fire immediately at startup instead of at their original time.
- RSS: the first poll runs right after boot, so many unseen entries can produce a burst up to the daily cap. The feed URL has no UI control — change `.env` or the `rss_url` setting.
- `GROQ_MODEL` defaults disagree between `utils/config.py` and `utils/groq_helper.py`; set `GROQ_MODEL` explicitly. (Not verified against a live Groq model list.)
- Solo design: SQLite single-writer, one long-poller per bot token (the local lock refuses a second instance), and the dashboard binds localhost with no authentication — do not expose it to a network.

## License

No license file is included in this repository. Add a `LICENSE` file (MIT, Apache-2.0, or another of your choice) to state the terms; until then all rights are reserved by the repository owner.

