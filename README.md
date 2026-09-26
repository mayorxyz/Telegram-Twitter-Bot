```markdown
# Telegram-to-X Automation Bot

A single-user (solo) Telegram bot that drafts, schedules, and publishes content to X/Twitter via the API v2 Free Tier. Content is composed in Telegram chat, reviewed as preview cards with inline buttons, queued in SQLite, and fired on time by APScheduler. Supports single tweets with media, multi-tweet threads with per-tweet media attachment, RSS auto-drafts, reusable text templates, a global pause kill switch, and local health reporting.

## 1. Project Overview

- **Audience:** One operator (matched against `ADMIN_ID`). All other users are rejected at handler level; there is no multi-tenant state.
- **Core loop:** Send text/photo → draft stored in DB → inline review card (`Confirm` / `Edit` / `Regenerate` / `Cancel`) → scheduling (calendar picker or "Post Now") → APScheduler date job → tweepy publish → success/failure notification with recovery actions.
- **Persistence-first:** Every piece of durable state (posts, threads, settings, templates, scheduled jobs) lives in SQLite. The bot can be killed and restarted without losing the queue; due posts missed while offline are caught up on startup.
- **Free-Tier discipline:** Only `POST /2/tweets` (with `reply_to_id` chaining for threads) and v1.1 media upload are used. No metrics/search endpoints. `/report` and stats stubs are computed locally.

## 2. Tech Stack

| Component              | Choice                                              | Version (pinned in `requirements.txt`) |
|------------------------|-----------------------------------------------------|----------------------------------------|
| Language               | Python                                              | 3.10+ (uses `X \| None` typing, `zoneinfo`) |
| Telegram framework     | python-telegram-bot (async)                         | 20.7                                   |
| Scheduler              | APScheduler (`AsyncIOScheduler` + `SQLAlchemyJobStore`) | 3.10.4                             |
| ORM / storage          | SQLAlchemy over SQLite (WAL mode)                   | 2.0.23                                 |
| X API client           | tweepy (v2 Client + v1.1 `API` for media upload)    | 4.14.0                                 |
| RSS parsing            | feedparser                                          | 6.0.10                                 |
| HTTP (PTB transport)   | httpx                                               | 0.25.2                                 |
| Cron trigger support   | croniter                                            | 2.0.1                                  |
| Env loading            | python-dotenv                                       | 1.0.0                                  |
| Timezones              | stdlib `zoneinfo` (+ `tzdata` on Windows)           | —                                      |

## 3. Project Structure

```text
.
├── main.py                    # Entry point: loads .env, init_db(), builds Application,
│                              #   registers all handlers, bootstraps scheduler, run_polling()
├── requirements.txt           # Pinned dependencies
├── .env.example               # Configuration template (copy to .env)
├── .gitignore                 # Ignores __pycache__/, data/jobs.db, etc.
├── README.md                  # This document
├── data/                      # Runtime artifacts (created automatically)
│   ├── bot.db                 #   Main SQLite database (posts/settings/templates/threads)
│   ├── jobs.db                #   APScheduler persistent job store
│   └── media/                 #   Downloaded Telegram photos/videos staged for upload
├── db/
│   ├── __init__.py            # Package marker
│   ├── database.py            # Engine/session factory, WAL pragma, init_db() create_all()
│   ├── models.py              # ORM models: Post, Setting, Template, Thread, ThreadTweet
│   └── crud.py                # All DB access helpers (posts, settings, templates, threads)
├── handlers/
│   ├── __init__.py            # Package marker
│   ├── drafts.py              # Draft creation, review card callbacks (d:/tag:/dup:/f:/st:),
│   │                          #   RSS approve/reject, template injection into drafts/threads
│   ├── editor.py              # Conversation flows: edit replacement text, custom-time input,
│   │                          #   media attach/remove, char-count feedback
│   ├── calendar_kb.py         # Inline keyboard builders: month grid, day/hour/minute slots
│   ├── scheduler_flow.py      # Scheduling state machine (cal:/day:/hr:/mn:/now:/quick:),
│   │                          #   save_schedule(), reschedule_existing(), open_queue_picker()
│   ├── queue.py               # /queue paginated list, per-item actions, 10s undo delete
│   ├── settings.py            # /settings menu, /pause /resume kill switch, timezone handling
│   ├── templates.py           # /template CRUD UI + tpl: callbacks (add/list/del/inject/use)
│   ├── threads.py             # /thread builder: compose ≤25 tweets, per-tweet media attach
│   └── report.py              # /report health digest from local DB queries only
├── scheduler/
│   ├── __init__.py            # Package marker
│   ├── jobs.py                # AsyncIOScheduler + SQLAlchemyJobStore, publish_job, rss_poll_job,
│   │                          #   reset_rss_count cron, schedule_post_job/unschedule_post_job,
│   │                          #   idempotent bootstrap(application)
│   └── publisher.py           # publish_post(): single & thread publishing, rate-limit backoff,
│                              #   failure notifications with Retry/Edit&Retry/Discard
└── utils/
    ├── __init__.py            # Package marker
    ├── config.py              # Reads env vars into typed module constants
    ├── twitter.py             # tweepy wrappers: upload_media, publish_tweet, thread chaining
    ├── formatting.py          # Markdown→plain-text stripping, effective_length (280), HTML escape
    ├── generator.py           # Deterministic non-LLM "regenerate" variants (hooks/CTAs)
    ├── rss.py                 # feedparser polling, dedupe by link hash, daily draft counter
    ├── timeutil.py            # UTC↔admin-timezone conversion helpers
    └── ui.py                  # Admin guard, preview rendering/edit-in-place, keyboards, undo store
```

## 4. Features

- **Draft & Review Flow** — Sending text or a photo creates a `status="draft"` row and an editable preview card with ✅ Confirm / ✏️ Edit / 🔄 Regenerate / ❌ Cancel. Edit replaces content in place; Regenerate rewrites presentation deterministically; Cancel deletes the draft.
- **Scheduling with Date/Time Picker** — After confirm: inline month-grid calendar with ◀️/▶️ navigation → day → hour slots → minute slots, plus custom HH:MM/full-datetime input. Times are interpreted in the admin timezone and stored as naive UTC in `posts.scheduled_at`; an APScheduler date job is armed immediately.
- **Queue Management** — `/queue` lists scheduled + draft posts, 5 per page with ◀️ Prev / Next ▶️. Each item has ✏️ Edit, 🕐 Reschedule (reopens the picker and updates the existing row/job), 🗑 Delete (two-step with a 10-second undo window tracked in `context.bot_data["pending_delete"]`), and ⚡ Post Now (immediate manual publish).
- **Publishing + Error Handling** — On success: `status="published"`, `tweet_id` and `published_at` recorded, admin notified with a link-style confirmation. On failure: `status="failed"` with the error string, and a notification carrying 🔁 Retry / ✏️ Edit & Retry / 🗑 Discard (plus skip-bad-media-and-retry when only uploads failed).
- **Auto-Format Toggle** — `/settings` shows `[✓ Auto-Format: ON/OFF]`, persisted in the settings table. When ON, markdown is stripped/converted to plain text at publish time only; the raw draft content in the DB is never mutated.
- **Kill Switch** — `/pause` sets `paused=true` and reports how many posts are on hold; the publish job and RSS poller check the flag before acting (due posts are re-armed +5 min instead of firing). `/resume` clears it and re-arms held jobs. `/queue` renders a ⏸ PAUSED banner while active.
- **RSS Auto-Draft** — A recurring interval job polls `RSS_URL` with feedparser, dedupes by SHA-1 of the link (persisted in settings), generates a draft, and pushes it to the admin with ✅ Approve / ❌ Reject / ✏️ Edit. Daily output is capped by `RSS_MAX_PER_DAY` using `rss_count_today`.
- **Supporting Buttons** — 🔁 Duplicate clones any post into a new draft; 🏷 Tag opens an inline picker (crypto / AI / meme / other); live character-count warnings (`n/280`, over-limit flag) during editing; 📎 Toggle Media attaches/removes images on drafts; 📊 Stats answers with the Free-Tier limitation notice (no premium endpoint calls).
- **Solo-Use Enforcement** — Every command, message, and callback passes through `utils.ui.is_admin(update)` comparing against `ADMIN_ID`; non-admins get a polite rejection and no state change.
- **Restart Survival / Startup Reload** — `on_startup()` seeds default settings and re-arms every `status="scheduled"` post from the DB; the persistent SQLAlchemyJobStore plus catch-up logic (missed jobs fire ASAP unless paused) means nothing is lost across restarts.
- **Smart Rate-Limit Backoff** — A 429 from X does not fail the post: it is rescheduled +15 minutes, the job is re-armed, and the admin receives an HTML warning protecting the Free-Tier quota.
- **Draft Templates / Snippets** — `/template` manages saved snippets (unique name + content). A 📋 Templates button on draft cards opens a picker; selecting one appends its content to the draft (works for individual thread tweets too) and re-renders the preview.
- **Bot Health Digest** — `/report` renders published counts for last 7/30 days, queue size, open drafts, failed count, and Active/Paused status — purely from SQLite queries.
- **Thread Support** — `/thread` opens a builder for up to 25 tweets (minimum 2): add/remove tweets, replace a tweet's text inline, per-tweet char counters. Stored as a Thread row (1:1 with the parent Post) plus ordered ThreadTweet rows.
- **Selective Per-Tweet Media** — In the thread builder each tweet has its own media slot (`th:media:<post>:<position>`); sending a photo/video while the slot is armed downloads it into `data/media/` and records the path in that tweet's `media_ids` JSON column. Publishing walks tweets in order, uploading only each tweet's own media and chaining via `reply_to_id`.
- **Single Tweet with Media** — Non-thread posts accept one or more downloaded media files; uploads go through tweepy v1.1 media upload and the resulting ids are passed to `client.create_tweet(media_ids=[...])`.

## 5. Prerequisites

- Python ≥ 3.10 on Linux/macOS/Windows (Windows additionally needs the `tzdata` package — already conditional in `requirements.txt`).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Your numeric Telegram user ID (e.g., from [@userinfobot](https://t.me/userinfobot)) — this becomes `ADMIN_ID`.
- An X developer app on the Free Tier with read+write posting permissions, generating OAuth 1.0a consumer key/secret and user access token/secret for the account that will post.
- Outbound HTTPS access to `api.telegram.org` and `api.twitter.com` / `upload.twitter.com`.
- Optional: a public RSS/Atom feed URL if you want auto-drafts.

## 6. Setup & Installation

```bash
# 1. Clone and enter the project
git clone <repo-url> x-poster-bot
cd x-poster-bot

# 2. Create an isolated environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install pinned dependencies
pip install -r requirements.txt

# 4. Configure secrets
cp .env.example .env
# then edit .env with your real values (see section 7)

# 5. Run
python main.py
```

On first run `init_db()` creates `data/bot.db` (tables: `posts`, `settings`, `templates`, `threads`, `thread_tweets`) and `data/jobs.db` (APScheduler job store). `data/media/` is created automatically for staged uploads. Send `/start` to your bot to verify it responds.

## 7. Configuration Reference

All variables are read once at import time by `utils/config.py` (via python-dotenv).

| Variable                                      | Required | Default          | Purpose                                                                 | Example                  |
|-----------------------------------------------|----------|------------------|-------------------------------------------------------------------------|--------------------------|
| `BOT_TOKEN`                                   | yes      | —                | Telegram bot token from BotFather                                       | `123456:AAH...xyz`       |
| `ADMIN_ID`                                    | yes      | `0`              | Numeric Telegram ID of the sole operator; all other users are rejected  | `5551234`                |
| `TIMEZONE`                                    | no       | `UTC`            | IANA timezone seeded into the settings table on first start; drives the calendar picker and display times | `Africa/Lagos`     |
| `X_API_KEY` / `TWITTER_API_KEY`               | yes*     | —                | OAuth 1.0a consumer key (*legacy alias also honored)                    | `abcdEFGH...`            |
| `X_API_SECRET` / `TWITTER_API_SECRET`         | yes*     | —                | OAuth 1.0a consumer secret                                              | `hijklMNOP...`           |
| `X_ACCESS_TOKEN` / `TWITTER_ACCESS_TOKEN`     | yes*     | —                | OAuth 1.0a access token of the posting account                          | `222-qrst...`            |
| `X_ACCESS_TOKEN_SECRET` / `TWITTER_ACCESS_SECRET` | yes* | —                | OAuth 1.0a access token secret                                          | `uvwxyz...`              |
| `TWITTER_BEARER_TOKEN`                        | no       | —                | Read-only bearer token (unused on Free Tier; reserved)                  | `AAAAAAAA...`            |
| `RSS_URL`                                     | no       | —                | Feed polled for auto-drafts; empty disables the feature                 | `https://example.com/feed.xml` |
| `RSS_MAX_PER_DAY`                             | no       | `5`              | Cap on RSS-generated drafts per calendar day                            | `5`                      |
| `RSS_POLL_MINUTES`                            | no       | `30`             | Poll interval (floored at 5 minutes)                                    | `30`                     |
| `DB_PATH`                                     | no       | `./data/bot.db`  | Override location of the main SQLite DB                                 | `/var/lib/xposter/bot.db` |
| `MEDIA_DIR`                                   | no       | `./data/media`   | Where Telegram downloads are staged before upload                       | `/srv/media`             |

**Runtime-mutable settings** (stored in the `settings` table, changed via UI, not `.env`): `timezone`, `paused`, `auto_format`, `rss_url`, `rss_count_today`, and per-link RSS dedupe keys.

## 8. How to Run

```bash
python main.py
```

**Startup sequence:**

1. `load_dotenv()` reads `.env`; logging is configured (httpx noise suppressed).
2. `init_db()` creates/verifies all tables (`Base.metadata.create_all`).
3. `Application.builder().token(BOT_TOKEN).build()` constructs the PTB app.
4. Handlers register in fixed order: commands → photo/text `MessageHandler`s → pattern-scoped `CallbackQueryHandler`s (drafts → scheduler → queue → delete steps → editor → settings → templates → threads). Patterns are mutually exclusive; ordering prevents shadowing.
5. `app.post_init = on_startup`: seeds missing `timezone` / `auto_format` / `paused` / `rss_url` settings and re-arms every `status="scheduled"` post as a scheduler date job.
6. `jobs.bootstrap(app)` attaches the application to the scheduler (guarded by `_bootstrapped` so it cannot double-start), restores/validates jobs from `data/jobs.db`, catches up missed publishes, registers `rss_poll` (interval) and `rss_reset` (cron 00:00), then starts the `AsyncIOScheduler`.
7. `app.run_polling(drop_pending_updates=True)` begins long-polling. Ctrl-C stops cleanly.

For production use behind supervision, e.g.:

```bash
systemd-run --unit=xposter --working-directory="$PWD" .venv/bin/python main.py
```

## 9. How It Works

### Single-post lifecycle

```text
User sends text/photo
   └─ main.ingest_text / ingest_photo → admin guard → flow routing
        ├─ active editor/timezone/template/thread flow? → that handler consumes it
        └─ otherwise → drafts.new_draft_message
             └─ crud.create_post(status="draft", content, media_path?) → preview card
                  [✅ Confirm][✏️ Edit][🔄 Regenerate][❌ Cancel]  (callbacks d:<id>:*)
Confirm → scheduler_flow.enter_scheduling → entry keyboard
   [📅 Pick Date] → calendar_kb month grid (cal:/day:/hr:/mn:) → build_dt_utc (admin tz → UTC)
   [⚡ Post Now]  → immediate manual publish
save_schedule → crud.update_post(status="scheduled", scheduled_at=UTC)
              → jobs.schedule_post_job(id, when)  (job id publish_<id>, replace_existing)
[APScheduler fires publish_job]
   ├─ paused? → re-arm +5 min, skip
   └─ publisher.publish_post(manual=False)
        ├─ final_text_for(post): strip_markdown applied here ONLY if auto_format
        ├─ twitter.upload_media (v1.1) → twitter.publish_tweet (v2 create_tweet)
        ├─ OK   → status="published", tweet_id/published_at set, notify ✅ (+ 📊 Stats)
        ├─ 429  → status stays "scheduled", scheduled_at=now+15min, job re-armed, ⚠️ notify
        └─ else → status="failed", error saved, notify with 🔁/✏️/🗑 (f:<id>:*)
```

### Thread lifecycle

```text
/thread → prompt: "Send tweets separated by |" (or line-by-line flow)
   → Post(status="draft") + Thread row + ThreadTweet rows (position 0..N-1, ≤25, ≥2)
Builder card per tweet: text preview, n/280 counter, [📎 Attach media], [✏️ Text], [🗑 Remove]
   th:media:<post>:<pos> arms context.user_data['th_attach']
   next photo/video → download_telegram_file → data/media/<uuid>.<ext> → appended to
   that tweet's media_ids JSON column (th:rmm:<post>:<pos> removes one file)
Confirm → same scheduling flow as single posts
publish_post detects the linked Thread → _publish_thread:
   for each ThreadTweet in position order:
       upload only that tweet's media files → media_ids
       client.create_tweet(text, reply_to_id=<previous tweet id>)   # first tweet unchained
   partial failure marks the post failed with the failing position reported;
   Retry resumes from the notification actions (Edit & Retry / Skip bad media & Retry)
```

All state transitions above are written to SQLite synchronously, so a crash between steps replays correctly after restart.

## 10. Command Reference

| Command                          | Description |
|----------------------------------|-------------|
| `/start`                         | Greeting + usage summary (admin only). |
| `/help`                          | Full command/button reference. |
| `/queue`                         | Paginated scheduled+draft list (5/page) with Edit / Reschedule / Delete / Post Now per item; shows ⏸ PAUSED banner when paused. |
| `/pause`                         | Kill switch ON: `paused=true`; replies "⏸ Paused — N posts on hold". Due posts are deferred, RSS polling halts. |
| `/resume`                        | Kill switch OFF: `paused=false`, held jobs re-armed; replies "▶️ Resumed". |
| `/settings`                      | Menu with `[✓ Auto-Format: ON/OFF]`, `[🌍 Change Timezone]`, pause toggle. |
| `/set_tz <IANA>` (or pick via settings) | Validates against `zoneinfo` and persists `timezone` in the settings table. |
| `/template`                      | Manage snippets: ➕ Add New (`Name \| Content`), 🗑 delete, list. |
| `/thread`                        | Start a multi-tweet thread builder (2–25 tweets, per-tweet media). |
| `/report`                        | Local DB health digest: 7/30-day published counts, queue, drafts, failures, Active/Paused. |
| plain text                       | Creates a draft (or feeds an open Edit / custom-time / template-add / thread flow). |
| photo/video                      | Attaches media to an open edit/thread slot, or starts a photo draft. |

**Inline buttons:** draft cards (Confirm/Edit/Regenerate/Cancel/Tag/Media/Duplicate/Templates/Stats), calendar (◀️ day hr mn, quick offsets +30m/+1h/+3h), queue rows, failure actions (Retry/Edit & Retry/Discard), delete two-step (Confirm Delete/Undo), thread builder (add/remove/edit-text/attach-remove media/approve).

## 11. Error Handling & Recovery

- **X API 429 (rate limit):** Caught via `tweepy.errors.TooManyRequests` / `HTTPException(status_code==429)`. The post is not marked failed: `scheduled_at ← now + 15 min`, job re-armed (`replace_existing`), admin notified with the exact new UTC time. `wait_on_rate_limit=True` additionally makes tweepy sleep on soft limits.
- **Other publish errors (auth, validation, network):** `status="failed"`, error text stored on the row, admin notified with 🔁 Retry (republish as-is), ✏️ Edit & Retry (fix content first), 🗑 Discard (delete row + remove job). Media-upload-only failures offer skip-bad-media-and-retry.
- **Missed jobs while offline:** Job store persistence + 1-hour misfire grace + startup catch-up re-fires overdue posts immediately (unless paused, in which case they defer +5 min until resume).
- **Accidental delete:** 🗑 requires ✅ Confirm Delete; ↩️ Undo within 10 seconds restores the full row snapshot and re-arms its scheduler job; after the window closes the action is terminal ("⏱ Undo window expired.").
- **Paused pipeline:** `/pause` freezes publishing and RSS drafting globally; nothing is dropped — everything waits in the DB and re-arms on `/resume`.
- **Non-admin traffic:** Rejected in every handler before any DB write.
- **Duplicate RSS items:** Link-hash dedupe keys in settings prevent re-drafting the same article across restarts.

## 12. Known Limitations

- **Free Tier caps:** ~500 writes/month (hard monthly ceiling — the bot cannot see or enforce it beyond backoff), 1 posted tweet per user per 24h for signups without verified org binding depending on current tier terms, media upload limits apply per tweet (≤4 images or ≤1 video/clip). No engagement metrics — 📊 Stats is intentionally a stub message.
- **Thread resumption gap:** Retrying a partially published thread currently republishes from scratch rather than continuing from the last successful tweet id (duplicate risk if the first tweets landed).
- **Undo store is in-memory snapshots:** Deleting a post and restarting the bot inside the 10 s window loses the undo snapshot (the row itself is already deleted; queue re-syncs fine).
- **Scheduler drift:** Jobs missed by more than the 1 h misfire grace fire immediately on startup rather than at their original time.
- **No LLM regeneration:** 🔄 Regenerate uses deterministic hook/CTA templates, not semantic rewriting.
- **SQLite single-writer:** Adequate for solo use; not designed for multiple concurrent bot instances sharing one DB file.
- **Minor:** Calendar cursor state is per-process memory (cosmetic only); RSS first poll runs at boot which can produce bursts if the feed has many unseen items (bounded by `RSS_MAX_PER_DAY`).

## 13. Future Enhancements (ranked by value)

1. **Thread-aware retry/resume** — Persist last published tweet id per `ThreadTweet` and continue chains on retry instead of reposting (eliminates the duplicate-post edge case).
2. **Monthly write-quota tracker** — Count publishes locally against the Free-Tier ceiling and warn at 80/100%.
3. **Per-tweet scheduling metadata in queue view** — Show thread previews (`1/5…`) inline in `/queue`.
4. **Media library** — Reuse previously uploaded Telegram files across drafts without re-download.
5. **LLM-backed regenerate** — Pluggable provider for true paraphrase variants while keeping the deterministic fallback.
6. **Webhook mode option** — `run_webhook` behind TLS reverse proxy for lower latency and no long-poll drops.
7. **Export/import** — `/export` the DB as CSV/JSON for archival; `/import` templates.
8. **Multi-account posting** — Map several credential sets to tags (blocked today by single OAuth pair).
9. **Draft expiry policy** — Auto-archive stale drafts older than N days.
10. **Tests + CI** — pytest suite around `crud`, `publisher` (mocked tweepy), and callback dispatch; GitHub Actions lint/type-check.
```