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
├── .gitignore                 # Ignores .env, __pycache__/, data/, logs/
├── README.md                  # This document
├── data/                      # Runtime artifacts (created automatically; not committed)
│   ├── bot.db                 #   Main SQLite database (posts/settings/templates/threads)
│   ├── jobs.db                #   APScheduler persistent job store
│   ├── bot.lock               #   Single-instance lock (OS-held; pid written after byte 0)
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
├── scripts/                   # Dev-only smoke checks (no network, no credentials)
│   ├── check_single_instance.py  # Second instance refused / bypass / release
│   ├── check_conflict_policy.py  # Token preflight + 409 policy (stubbed bot)
│   └── check_dashboard.py        # Dashboard start/stop/port-busy + HTTP routes
├── web/
│   ├── __init__.py            # Package marker
│   ├── server.py              # Read-only local dashboard (stdlib HTTP, localhost:3000)
│   │                          #   start_dashboard()/stop_dashboard(), JSON + static UI
│   └── static/
│       └── index.html         # Dashboard front end (drafts/queue/failures view)
└── utils/
    ├── __init__.py            # Package marker
    ├── config.py              # Reads env vars into typed module constants
    ├── single_instance.py     # One-poller-per-token guard: OS lock on data/bot.lock
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
| `MEDIA_DIR`                                   | no       | `./data/media`   | Where Telegram downloads are staged before upload. The single-instance lock file `bot.lock` is created in this folder's parent (`data/`) | `/srv/media`   |
| `DASHBOARD_HOST`                              | no       | `127.0.0.1`      | Interface the read-only dashboard binds to                              | `127.0.0.1`              |
| `DASHBOARD_PORT`                              | no       | `3000`           | Dashboard port; if busy the bot logs a warning and keeps polling        | `3000`                   |
| `ALLOW_MULTI_INSTANCE`                        | no       | unset            | `1`/`true` bypasses the single-instance lock (debug boots only)         | `1`                      |
| `CONFLICT_ABORT_AFTER`                        | no       | `10`             | Consecutive 409 conflicts tolerated before the bot logs and stops cleanly (`0` = warn forever, never stop) | `10` |

**Runtime-mutable settings** (stored in the `settings` table, changed via UI, not `.env`): `timezone`, `paused`, `auto_format`, `rss_url`, `rss_count_today`, and per-link RSS dedupe keys.

## 8. How to Run

```bash
python main.py
```

**Startup sequence:**

1. `load_dotenv()` reads `.env`; logging is configured (httpx noise suppressed).
2. `single_instance.acquire_default()` takes an exclusive OS lock on `data/bot.lock`. A second live instance logs the holder's PID and exits with code 2 instead of starting a doomed second poller (see §12).
3. `init_db()` creates/verifies all tables (`Base.metadata.create_all`).
4. `Application.builder().token(BOT_TOKEN)` constructs the PTB app with `post_init`/`post_shutdown` hooks, then `add_error_handler` classifies polling errors (409 `Conflict`, timeouts, network blips).
5. Handlers register in fixed order: commands → photo/text `MessageHandler`s → pattern-scoped `CallbackQueryHandler`s (drafts → scheduler → queue → delete steps → editor → settings → templates → threads). Patterns are mutually exclusive; ordering prevents shadowing.
6. `post_init` runs the **token preflight**: it clears any leftover webhook, then probes `getUpdates` once — a `Conflict` here aborts startup with one actionable line instead of minutes of retry noise.
7. `post_init` seeds missing `timezone` / `auto_format` / `paused` / `rss_url` settings, re-arms every `status="scheduled"` post as a scheduler date job, and starts the read-only dashboard on `DASHBOARD_PORT` (default `3000`).
8. `jobs.bootstrap(app)` attaches the application to the scheduler (guarded by `_bootstrapped` so it cannot double-start), restores/validates jobs from `data/jobs.db`, catches up missed publishes, registers `rss_poll` (interval) and `rss_reset` (cron 00:00), then starts the `AsyncIOScheduler`.
9. `app.run_polling(drop_pending_updates=True)` begins long-polling.
10. Ctrl-C triggers `post_shutdown`: scheduler stopped, dashboard socket closed, `data/bot.lock` released (the OS releases it on any exit path, including crashes).

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
- **Telegram 409 `Conflict`:** Classified by the global error handler, not dumped as a traceback: the first occurrence logs the single actionable hint, every 10th repeats a short warning, and a streak lasting `CONFLICT_ABORT_AFTER` (default 10) consecutive conflicts stops the bot cleanly so a supervisor can restart it after the cause is fixed. A quiet gap of 2 minutes resets the streak.
- **Invalid or revoked `BOT_TOKEN`:** Aborts startup *before* polling with one line (`Telegram rejected BOT_TOKEN …`) and exit code 1 — no traceback. Fix `BOT_TOKEN` in `.env` (see §12.2).
- **Another local instance running:** Refused at startup with the holder's PID and exit code 2 (see §12.2).
- **Startup exit codes:** `0` normal stop, `1` preflight/startup failure (token or conflict detected), `2` single-instance lock held by another live process.

## 12. Troubleshooting

### 12.1 `409 Conflict: terminated by other getUpdates request`

**Symptom.** The log repeats *"Conflict: terminated by other getUpdates request"* while the bot looks alive, and messages are answered late, twice, or not at all.

**Cause.** Telegram routes `getUpdates` to exactly **one** consumer per bot token. A second consumer makes *both* sessions conflict; PTB retries forever, so the log floods while polling is effectively dead.

**Likely culprits, most common first:**

1. A second copy of this bot is running (another terminal, a leftover process after a crash, a second machine).
2. Another project/deployment reuses the same `BOT_TOKEN` — the token is the identity, not the folder.
3. A webhook was set on the token (e.g. by another tool); webhooks and `getUpdates` are mutually exclusive. `post_init` clears it automatically at startup, so a webhook set *after* boot is the dangerous case.
4. The token was shared (screenshot, chat, commit) and someone else is polling it.

**Find the local twin (Windows PowerShell):**

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CommandLine |
  Where-Object { $_.CommandLine -like '*main.py*' }
```

**Find the local twin (Linux/macOS):**

```bash
ps -ef | grep '[m]ain.py'
```

Then stop all but one (`Stop-Process -Id <pid>` / `kill <pid>`).

**Check for a webhook** (empty `url` = fine):

```bash
curl "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"
```

Delete it with:

```bash
curl "https://api.telegram.org/bot$BOT_TOKEN/deleteWebhook?drop_pending_updates=false"
```

**If the token ever leaked:** open @BotFather → `/mybots` → *API Token* → **Revoke current token**, then update `BOT_TOKEN` in `.env` and on the one legitimate deployment. Revoking is the only fix — the leaked token cannot be "secured" while someone else polls it.

**Built-in protections (already in this repo):**

| Layer | Behavior |
|-------|----------|
| Local lock | `data/bot.lock` is locked with an exclusive OS lock at startup. A second local instance logs the holder's PID and exits with code **2** before touching Telegram. Crashes leave no stale lock — the OS releases it. |
| Token preflight | `post_init` deletes any webhook and probes `getUpdates` once. An active remote poller aborts startup immediately with an actionable message instead of retry noise. |
| Error classification | Runtime 409s log one hint (then every 10th), and stop the process cleanly after `CONFLICT_ABORT_AFTER` (default 10) consecutive conflicts. |
| Escape hatch | `ALLOW_MULTI_INSTANCE=1` skips the local lock for a deliberate short-lived debug boot. It does **not** make Telegram accept two pollers — expect 409s while both run. |

**Verify the fix.** With one instance running, the log shows `single-instance lock held (...bot.lock)` and no 409s for several minutes; sending `/start` gets exactly one reply. Starting `python main.py` again prints the "another instance ... is already running (pid=...)" message and exits 2.

### 12.2 `python main.py` exits immediately with code 2 (or 1)

- **Exit code 2 — another instance holds the lock.** The message names the holder's PID. Stop that process, or set `ALLOW_MULTI_INSTANCE=1` for an intentional parallel debug boot.
- **Exit code 1 — startup preflight failed.** The single logged line says which: `Telegram rejected BOT_TOKEN …` (revoked/mistyped/whitespace-padded token, or one whose whitespace/quoting in `.env` broke it — the value must be exactly `123456:AAH…`) or `409 Conflict …` (a twin is polling the token; see §12.1).

### 12.3 Dashboard not reachable on `http://127.0.0.1:3000`

- Look for `dashboard: cannot bind 127.0.0.1:3000` in the log — another service owns the port. Change `DASHBOARD_PORT` in `.env` (the bot itself keeps polling without the dashboard).
- Confirm the port in the log line `Dashboard running at http://127.0.0.1:<port>`; both the log line and the server read `utils/config.DASHBOARD_HOST`/`DASHBOARD_PORT`, so they cannot disagree.
- The dashboard is read-only and deliberately bound to localhost with no authentication — never expose it to a network.

### 12.4 X/Twitter publish failures

See §11: 429s are re-queued automatically (+15 min); auth/validation/network failures are marked `failed` with retry, edit-and-retry, and discard actions in chat.

### 12.5 Smoke-check the hardening (no credentials needed)

Both checks are offline — they stub `telegram.Bot` and only touch `data/bot.lock`:

```bash
python scripts/check_single_instance.py   # lock refusal, bypass, release, lock bytes
python scripts/check_conflict_policy.py   # preflight + 409/InvalidToken classification
python scripts/check_dashboard.py         # start/stop, port-busy warning, HTTP routes
```

Each prints one line per check and ends with `RESULT: ALL CHECKS PASSED` (exit code 0).

### 12.6 Reset local state

Stopping the bot is always safe: everything durable lives in SQLite. To start over, stop the bot and delete `data/bot.db*` and `data/jobs.db*` (this drops all posts, templates, and settings); `data/bot.lock` can be deleted too — it is recreated on the next start and holds no state.

## 13. Known Limitations

- **Free Tier caps:** ~500 writes/month (hard monthly ceiling — the bot cannot see or enforce it beyond backoff), 1 posted tweet per user per 24h for signups without verified org binding depending on current tier terms, media upload limits apply per tweet (≤4 images or ≤1 video/clip). No engagement metrics — 📊 Stats is intentionally a stub message.
- **Thread resumption gap:** Retrying a partially published thread currently republishes from scratch rather than continuing from the last successful tweet id (duplicate risk if the first tweets landed).
- **Undo store is in-memory snapshots:** Deleting a post and restarting the bot inside the 10 s window loses the undo snapshot (the row itself is already deleted; queue re-syncs fine).
- **Scheduler drift:** Jobs missed by more than the 1 h misfire grace fire immediately on startup rather than at their original time.
- **No LLM regeneration:** 🔄 Regenerate uses deterministic hook/CTA templates, not semantic rewriting.
- **SQLite single-writer:** Adequate for solo use; not designed for multiple concurrent bot instances sharing one DB file.
- **Minor:** Calendar cursor state is per-process memory (cosmetic only); RSS first poll runs at boot which can produce bursts if the feed has many unseen items (bounded by `RSS_MAX_PER_DAY`).

## 14. Future Enhancements (ranked by value)

1. **Thread-aware retry/resume** — Persist last published tweet id per `ThreadTweet` and continue chains on retry instead of reposting (eliminates the duplicate-post edge case).
2. **Monthly write-quota tracker** — Count publishes locally against the Free-Tier ceiling and warn at 80/100%.
3. **Per-tweet scheduling metadata in queue view** — Show thread previews (`1/5…`) inline in `/queue`.
4. **Media library** — Reuse previously uploaded Telegram files across drafts without re-download.
5. **LLM-backed regenerate** — Pluggable provider for true paraphrase variants while keeping the deterministic fallback.
6. **Webhook mode option** — `run_webhook` behind TLS reverse proxy for lower latency and no long-poll drops.
7. **Export/import** — `/export` the DB as CSV/JSON for archival; `/import` templates.
8. **Multi-account posting** — Map several credential sets to tags (blocked today by single OAuth pair).
9. **Draft expiry policy** — Auto-archive stale drafts older than N days.
10. **Tests + CI** — pytest suite around `crud`, `publisher` (mocked tweepy), and callback dispatch; GitHub Actions lint/type-check. Offline smoke checks for the polling guard already exist in `scripts/` (§12.5).