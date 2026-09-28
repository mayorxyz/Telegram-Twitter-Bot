# How to use this bot

A hands-on walkthrough for setting up and running this project with your own accounts. Follow it top to bottom; at the end you will have posted your first live tweet.

Note on button names: this bot's buttons carry a leading icon (a check mark, a pencil, and so on). This guide writes the label text only, for example `Confirm` or `Post Now`. Match them by their words.

## 1. What this does and who it is for

This bot lets you compose, review, schedule, and publish posts to X from Telegram chat, using Groq to shorten text that is over X's 280-character limit and to split long text into a thread. It runs as a Python process on your own machine and keeps everything (drafts, scheduled posts, threads, templates, settings) in a local SQLite file. You talk to your own Telegram bot and you publish to your own X account through your own X developer app.

What you need:

- A computer to run it on (this guide uses Windows and PowerShell).
- Python 3.10 or newer. The code uses `zoneinfo` and evaluates `str | None` style annotations at runtime in `db/models.py`, so 3.10 is the minimum. It was developed on 3.12.10.
- A Telegram account, so you can create a bot with @BotFather.
- An X account with access to the X Developer Console, a project and app with Read and Write permissions, and prepaid credits loaded (posting is billed per post).
- A Groq API key (optional, but recommended). Without it, over-limit text is trimmed instead of compressed, and the "Make Thread" button on the compression card cannot generate a thread.
- The credentials stay on your machine in a `.env` file next to the code. Only Telegram user IDs you list in `ADMIN_ID` or `ADMIN_IDS` can use the bot; everyone else is rejected.

## 2. Step 1: Create your Telegram bot

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`.
3. Answer BotFather's questions: a display name, then a username that must end in `bot`.
4. BotFather replies with a token that looks like `123456789:AAExampleTokenText`. Copy it.
5. Put it in your `.env` file as `BOT_TOKEN`, for example:
   ```
   BOT_TOKEN=123456789:AAExampleTokenText
   ```
6. Now find your own numeric Telegram user id. Start a chat with the bot you just created and send it any message (for example `hello`), then open this URL in a browser, replacing the token with yours:
   ```
   https://api.telegram.org/bot123456789:AAExampleTokenText/getUpdates
   ```
   In the JSON reply, find `"message"` -> `"from"` -> `"id"`. That number is your user id.
   If the bot is not running by you yet, this is the only process reading updates, so the call works. Do not run `python main.py` in another window while you are doing this, because Telegram allows only one consumer per bot token.
7. Put that number in `.env` as `ADMIN_ID`:
   ```
   ADMIN_ID=123456789
   ```
   For several admins, use `ADMIN_IDS` with comma-separated ids instead (it takes priority over `ADMIN_ID`):
   ```
   ADMIN_IDS=123456789,987654321
   ```

Your bot will only answer these ids. Everyone else gets "This bot is private." or "Not authorized".

## 3. Step 2: Set up your X developer app

1. Sign in to the X Developer Console with the X account you want to post from. (The console is reached from X's developer site; the account needs developer access.)
2. Create a project, then create an app inside that project.
3. Open the app's user authentication settings and set the app permissions to Read and Write. Saving this is what allows the bot to create tweets.
4. Do step 3 before generating tokens. If you change permissions later, the access tokens you already generated stop working and you must regenerate the access token and access token secret.
5. Generate the API key and API secret, and put them in `.env`:
   ```
   X_API_KEY=your_api_key
   X_API_SECRET=your_api_secret
   ```
6. Generate the access token and access token secret (only possible after Write permission is set), and put them in `.env`:
   ```
   X_ACCESS_TOKEN=your_access_token
   X_ACCESS_TOKEN_SECRET=your_access_token_secret
   ```
   The older `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_SECRET` names still work as fallbacks, but use the `X_` names.
7. Load credits on the project (pay-per-use). When the balance is empty, X rejects writes with HTTP 402 and nothing is published until you top up.
8. Optional: generate a bearer token if you want the `Stats` button to have a read-only path. Put it in `.env`:
   ```
   TWITTER_BEARER_TOKEN=AAAAAAAAAAAAAAAAAAAAA
   ```

All four OAuth 1.0a values are required before the bot will publish anything. If any of them is missing, publishing stops with "Twitter credentials are not configured in .env".

## 4. Step 3: Get your Groq key

1. Sign in to the Groq console (console.groq.com) with your Groq account.
2. Open the API keys section and create a key.
3. Copy it into `.env`:
   ```
   GROQ_API_KEY=gsk_your_key_here
   ```
4. Decide the model. `GROQ_MODEL` is the model name the bot sends to Groq. It is read from the environment when a request is made, and if it is not set the code falls back to `qwen/qwen3.8-27b` in `utils/groq_helper.py` (while `utils/config.py` lists `llama-3.3-70b-versatile` as its own default, which is not what runs). Set it explicitly to a model your Groq account can use:
   ```
   GROQ_MODEL=llama-3.3-70b-versatile
   ```
5. Leave `GROQ_API_KEY` empty if you do not want AI features. Then:
   - Over-limit text is cut at a word boundary instead of compressed.
   - The "Make Thread" button reports that AI compression is disabled and does not create a thread.

Groq is only called when text is longer than 280 characters, when you press "Make Thread", or when a tweet you type into the thread builder is over 280 characters.

## 5. Step 4: Install and configure

1. Open PowerShell and go to where you want the project. To clone it:
   ```powershell
   cd $env:USERPROFILE\Desktop
   git clone https://github.com/mayorxyz/Telegram-Twitter-Bot.git Telegram-Twitter-Bot
   cd Telegram-Twitter-Bot
   ```
   If you already have the folder, just `cd` into it:
   ```powershell
   cd c:\Users\MAYOR\Desktop\Telegram-Twitter-Bot
   ```
2. Check your Python version (must be 3.10 or newer):
   ```powershell
   python --version
   ```
3. Create and activate a virtual environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks the activation script, run this once in the same window and try again:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   ```
4. Install the dependencies:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
5. Create your configuration file from the template:
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```
6. Fill in `.env`. The complete set of variables the code reads, with the required ones first:
   ```
   # Required
   BOT_TOKEN=123456789:AAExampleTokenText
   ADMIN_ID=123456789
   # ADMIN_IDS=123456789,987654321   # optional: comma-separated admins, takes priority over ADMIN_ID

   # X / Twitter credentials (all four are required to publish)
   X_API_KEY=your_api_key
   X_API_SECRET=your_api_secret
   X_ACCESS_TOKEN=your_access_token
   X_ACCESS_TOKEN_SECRET=your_access_token_secret
   TWITTER_BEARER_TOKEN=              # optional: read-only token for the Stats button
   # TWITTER_API_KEY / TWITTER_API_SECRET / TWITTER_ACCESS_TOKEN /
   # TWITTER_ACCESS_SECRET are legacy aliases for the X_* names above

   # Groq (optional: AI compression and thread splitting)
   GROQ_API_KEY=gsk_your_key_here     # empty disables AI features
   GROQ_MODEL=llama-3.3-70b-versatile # model name you want Groq to use

   # RSS auto-drafts (optional)
   RSS_URL=                           # feed URL, empty disables RSS
   RSS_MAX_PER_DAY=5                  # max RSS drafts per day (default 5)
   RSS_POLL_MINUTES=30                # poll interval in minutes, minimum 5 (default 30)

   # Behaviour
   TIMEZONE=UTC                       # IANA name, e.g. Africa/Lagos (default UTC)
   DRY_RUN=0                          # 1 = simulate posts, no X calls
   CONFLICT_ABORT_AFTER=10            # Telegram 409 conflicts before exiting; 0 = never exit

   # Local dashboard
   DASHBOARD_HOST=127.0.0.1           # default 127.0.0.1
   DASHBOARD_PORT=3000                # default 3000

   # Paths (optional, read by db/database.py and utils/config.py)
   DB_PATH=./data/bot.db
   MEDIA_DIR=./data/media

   # Development only: skips the single-instance lock
   ALLOW_MULTI_INSTANCE=0
   ```
7. Never share or commit `.env`. The repository's `.gitignore` already ignores `.env`, `data/` and `logs/`; if you copy the code into a new repository, make sure those three entries are in its `.gitignore`. You can check with:
   ```powershell
   git status --short
   ```
   `.env` must not appear in that list.


## 6. Step 5: First run

1. Start the bot from the project folder, with the virtual environment active:
   ```powershell
   python main.py
   ```
2. Leave that window open. A healthy startup looks like this (paths and numbers are yours):
   ```text
   2026-09-29 10:00:01 INFO     utils.single_instance: single-instance lock held (C:\Users\MAYOR\Desktop\Telegram-Twitter-Bot\data\bot.lock)
   2026-09-29 10:00:01 INFO     scheduler.jobs: scheduler started; 0 scheduled posts restored
   2026-09-29 10:00:02 INFO     telegram.ext.Application: Application started
   2026-09-29 10:00:02 INFO     main: startup: no webhook set — long polling owns this token
   2026-09-29 10:00:02 INFO     main: Bot command menu set.
   2026-09-29 10:00:02 INFO     main: startup reload: 0 scheduled posts re-armed
   2026-09-29 10:00:02 INFO     web.server: dashboard: read-only UI at http://127.0.0.1:3000
   2026-09-29 10:00:02 INFO     main: Dashboard running at http://127.0.0.1:3000
   ```
   What each line means:
   - `single-instance lock held (data\bot.lock)` - this process holds the lock that stops a second copy from polling the same bot token.
   - `scheduler started; N scheduled posts restored` - APScheduler started and reloaded its persistent job store (`data/jobs.db`).
   - `Application started` - python-telegram-bot finished initialising.
   - `no webhook set - long polling owns this token` - any webhook on the token was removed and long polling now owns it. If a webhook existed, you instead see `startup: removed webhook ...`.
   - `Bot command menu set.` - the Telegram command menu was written for your bot.
   - `startup reload: N scheduled posts re-armed` - scheduled rows in `data/bot.db` got their scheduler jobs back.
   - `dashboard: read-only UI at http://...` and `Dashboard running at http://...` - the local dashboard is listening.
   If one of these is missing, or a warning line appears instead, see section 13.
3. In Telegram, open the chat with your bot and send `/start`.
4. The bot answers with its help text and a persistent keyboard: `New Tweet`, `New Thread`, `Queue`, `Settings`, `Report`, `Templates`.
5. Worth trying now: `/report` (local database only), `/settings`, `/queue`.
6. To stop the bot, press Ctrl+C in the terminal. Durable state is in SQLite, so nothing is lost.


## 7. Step 6: Make your first post

### 7.1 A short text post

1. Send the bot a normal message, for example:
   ```text
   Testing my new bot. This is my first post through Telegram.
   ```
2. The bot stores it as a draft and replies with a preview card showing the text, a character line, and these buttons: `Confirm`, `Edit`, `Regenerate`, `Cancel`, `Duplicate`, `Tag`, `Media` (plus `Templates` if you have saved any).
3. Tap `Confirm`. The card turns into the scheduling view: `Schedule post #N` with `Pick Date`, `Post Now`, `Cancel`.
4. Tap `Post Now`. The card is replaced by a publishing notice, then by the published card: `Published! Post #N` with a link to the tweet and the local time, plus `Duplicate` and `Stats` buttons.
5. Confirm on X: tap the link in the card (it opens `https://x.com/i/web/status/<tweet id>`) or open your X profile in a browser. The post is live.

### 7.2 A post longer than 280 characters

1. Send a text longer than 280 characters. The bot replies:
   ```text
   Text is N chars (limit: 280). Compressing with Groq AI...
   ```
2. It then shows the compressed version and three buttons:
   - `Post Now` - publishes the compressed text immediately.
   - `Edit` - turns the compressed text into a normal draft with the usual card, so you can edit, tag, or schedule it.
   - `Make Thread` - asks Groq to split the original text into a thread, creates a thread draft, and opens the thread builder.
3. If you pressed `Make Thread`, review the numbered tweets in the builder and use `Done - preview draft` to get back to the normal card, then `Confirm` and either `Post Now` or `Pick Date`. Rules the bot enforces: 2 to 25 tweets, at least 2 to continue, and no empty tweets.
4. To build a thread yourself, send `/thread` (or tap `New Thread`). The builder opens with two empty tweets and these buttons: `Done - preview draft`, `Post Now`, `Add tweet`, `Set text`, `Attach media`, `Remove media`, `Drop tweet`. Text you send while the builder waits goes into the next empty tweet. You can also pass the whole thread in one command, separating tweets with `---`:
   ```text
   /thread First tweet --- Second tweet --- Third tweet
   ```
   Without a Groq key the automatic split is unavailable; this manual route always works.

### 7.3 A post with an image or a video

1. Send a photo, with or without a caption. The bot replies `Media received. How would you like to use it?` with `Single Tweet`, `New Thread`, `Cancel`. If a draft is already collecting media, the first button reads `Add to draft #N (n/4)` instead.
2. Tap `Single Tweet`. The bot downloads the file into `data/media/`, replies with the draft number and the media count, and shows the preview card for that draft. The photo message itself carries the card buttons.
3. Send up to three more photos within 10 minutes to add them to the same draft. Telegram albums arrive as separate messages and are grouped automatically. Limits the bot enforces: 4 files per tweet, and a video or GIF never mixes with images.
4. Caption, if you did not send one with the photo: tap `Edit` on the card, then send the text. It replaces the draft text and the attached media stays.
5. Tap `Confirm`, then `Post Now`. The media is uploaded to X together with the tweet.

### 7.4 A scheduled post

1. Send or open any draft and tap `Confirm`.
2. Tap `Pick Date`. A month calendar appears, with the current day marked and past days disabled. The arrows change the month.
3. Tap a day. An hour grid appears (24 slots) plus `Custom time...` and `Month`.
4. Tap an hour, then a minute from `:00`, `:15`, `:30`, `:45`, or tap `now+30m` to schedule 30 minutes out in one tap.
5. The bot confirms with `Scheduled! Post #N will fire <day> <date> <time>` followed by your timezone, and buttons `Reschedule`, `Post Now`, `Cancel`.
6. Instead of the grid you can type a time: tap `Custom time...` and send `HH:MM` for the selected day, or a full `YYYY-MM-DD HH:MM`. Times in the past are rejected.
7. At that moment APScheduler fires the job, the bot publishes the post, and you get the same published card as in 7.1.



## 8. Step 7: Make it yours

- Timezone: send `/set_tz Africa/Lagos`, or open `/settings` and tap `Change Timezone (...)`, then pick one of the listed zones or `Type custom timezone` and type an IANA name. The value is stored in the `settings` table and used for the calendar, every timestamp on the cards, and every time you type. Typed times are interpreted in your timezone and stored as UTC.
- `TIMEZONE` in `.env` only seeds that setting on the first start. After that the stored value wins, so change your zone with `/set_tz` or `/settings`, not by editing `.env`.
- Admin ids: `ADMIN_IDS` (comma-separated, takes priority) or `ADMIN_ID` in `.env`, then restart. Only those ids can use the bot.
- RSS feeds: set `RSS_URL` in `.env`. Polling starts right after each startup and repeats every `RSS_POLL_MINUTES` (the code floors this at 5 minutes). `RSS_MAX_PER_DAY` caps how many drafts a day the feed can produce (default 5). Each new item arrives in chat as an `RSS draft` card with `Approve`, `Reject`, `Edit`, and `Regenerate`. Links already seen are remembered, so the same item is not drafted twice. Note: `RSS_URL` is copied into the `rss_url` setting on the first start only, and no command changes it later, so editing `.env` afterwards has no effect unless you clear that setting row in `data/bot.db`.
- Auto-format: `/settings`, then toggle the auto-format line. It is ON by default and applies at publish time only: markdown markers (bold, italic, strikethrough, spoilers, headings, blockquotes, code fences, `[label](url)` links) are converted to plain text on the way to X, while the stored draft keeps what you typed.
- Pause and resume: `/pause` and `/resume`, or the same toggle in `/settings`. While paused, scheduled posts do not fire (each due job re-arms itself for 5 minutes later) and the RSS poller skips its runs. `/queue` shows a PAUSED banner. Nothing is dropped.
- Compression target: there is no environment variable for it. Compression asks Groq for under 250 characters, then 230, then 210 on retries; thread parts target 250 characters; the hard X limit is 280 (`X_CHAR_LIMIT` in `utils/config.py`, `THREAD_PART_TARGET` in `utils/groq_helper.py`). To change the targets, edit those two files.
- Groq model: `GROQ_MODEL` in `.env`, then restart. Set it explicitly; if you leave it empty the code falls back to `qwen/qwen3.8-27b`.
- Dry runs: `DRY_RUN=1` makes publishing and media uploads return fake ids without calling X, which is useful for testing the chat flows. Set it back to `0` before real posting.
- Telegram conflicts: `CONFLICT_ABORT_AFTER` (default 10) is how many consecutive 409 conflicts the bot tolerates before it stops cleanly. `0` means it only warns and never stops.
- Dashboard: `DASHBOARD_PORT` (default 3000) and `DASHBOARD_HOST` (default 127.0.0.1). If the port is in use, the bot logs a warning and keeps running without the dashboard.
- Paths: `DB_PATH` (default `data/bot.db`) and `MEDIA_DIR` (default `data/media`). Keep `MEDIA_DIR` the same across restarts, because drafts store absolute file paths into it.
- Tags and templates: the `Tag` button on a card assigns one of `crypto`, `AI`, `meme`, `other`. `/template` opens template management; tap `Add New` and send `Name | Content` to save a snippet, then use the `Templates` button on a card to append it to a draft (thread drafts ask which tweet to append it to).

## 9. Everyday workflow

1. Draft: send text or media to the bot, or send `/thread` for a multi-tweet post.
2. Review the card: `Regenerate` for a different opener and closing line, `Edit` to replace the text, `Tag` to label it, `Duplicate` to clone an existing post, `Templates` to append a saved snippet.
3. Choose when: `Confirm` then `Post Now`, or `Pick Date` and walk through the calendar, or `Custom time...` to type an exact time.
4. Track: `/queue` lists scheduled and draft posts page by page with per-row buttons, and the dashboard shows the same rows from the same database.
5. Failures: a failed publish arrives as a card naming the post and the X error.
   - `Retry` - publishes again as-is. For a thread it resumes from the first tweet that has no tweet id yet, so already-published tweets of that thread are not repeated.
   - `Edit & Retry` - for a single post it opens the edit flow; for a thread it opens the builder so you can fix individual tweets.
   - `Discard` - deletes the row, its scheduler job, and its local media files.
   - `Retry (skip bad media)` - appears when a media upload was the problem; it drops the files that failed and retries the rest.
6. Stats: the published card's `Stats` button asks X for public metrics. Without a bearer token, or on a tier that does not expose them, the bot answers with its built-in "X API Free Tier does not support view/impression metrics" notice.
7. Paused: it is a hold, not a loss. Scheduled posts stay scheduled, due posts wait and retry, RSS stops drafting, and `/resume` re-arms everything (posts whose time already passed fire immediately).

## 10. Dashboard

- URL: `http://127.0.0.1:3000` by default (`DASHBOARD_PORT` changes the port, `DASHBOARD_HOST` the interface). The `Dashboard running at http://...` line in the startup log tells you the real address.
- It shows your posts from `GET /api/posts`: id, status, tag, scheduled time, tweet id for published posts, error text for failed posts, a thread's individual tweets, and the media attached to each.
- Filters: `All Posts`, `Draft Posts`, `Scheduled Posts`, `Published Posts`, `Failed Posts`. The page refreshes itself every 3 seconds.
- Media is served from `GET /media/<file name>` out of `MEDIA_DIR`; images render inline and videos are loaded into a player. No thumbnails are generated.
- It is read-only: `web/server.py` implements GET and OPTIONS only, so nothing can be changed from the browser.
- It binds to localhost by default and has no authentication. Keep it local.
- The compose modal and the "Use Mock Data" toggle in the page are front-end simulations: they build a post object in the browser and load hardcoded sample posts. They do not write to the database and do not publish anything. Use Telegram for real posting.

## 11. How it works end to end

1. You send a message in Telegram. `main.py` checks `ADMIN_IDS` and routes the update: text to `ingest_text`, photos to `ingest_photo`, videos and animations to `ingest_video`.
2. Short text becomes a row. `handlers/drafts.py` calls `crud.create_post()` and renders the preview card with `utils/ui.py`.
3. Long text is offered compression. `handlers/editor.py` calls `utils/groq_helper.py` (`compress_tweet` or `make_thread`), then shows the compressed card or the thread builder.
4. A thread draft lives in two tables. `handlers/threads.py` keeps the per-tweet rows (`Thread` plus `ThreadTweet` in `db/models.py`) through `db/crud.py`.
5. `Confirm` chooses when. `handlers/scheduler_flow.py` plus `handlers/calendar_kb.py` either publish immediately or save `status="scheduled"` with a UTC time.
6. Scheduled posts wait in two places. The row waits in `data/bot.db` and the job waits in `data/jobs.db`, managed by `scheduler/jobs.py` (APScheduler with `SQLAlchemyJobStore`).
7. Publishing runs one pipeline. `scheduler/publisher.py` strips markdown at publish time only (`utils/formatting.py`), uploads media and creates tweets with `utils/twitter.py` (tweepy v1.1 upload plus v2 `create_tweet`).
8. The result comes back to Telegram. Success edits the card to published with the tweet id; a rate limit re-arms the job 15 minutes later; any other failure marks the row failed with the X error and offers retry actions.
9. RSS drafts enter at step 2. `utils/rss.py` fetches the feed on the interval from `scheduler/jobs.py`, and each new link becomes a draft row with an approval card.

How the parts connect:

- Telegram and bot: long polling with `drop_pending_updates=True`. One token, one running copy, guarded by the lock in `utils/single_instance.py` (`data/bot.lock`).
- Bot and Groq: HTTPS chat completions only for compression and thread splitting. No Groq key means those two cards never appear; everything else works.
- Bot and SQLite: `db/database.py` opens `data/bot.db` (WAL mode). Every card, job, and the dashboard read the same rows.
- Bot and X API: tweepy user-context calls with your four `X_*` credentials. Text posts use v2; media uses v1.1 upload first, then attaches the returned media ids.
- Dashboard and DB: `web/server.py` serves `GET /`, `GET /api/posts`, and `GET /media/<name>` from the same database and media folder. Read-only.

## 12. Costs and limits

- X posting is billed as prepaid credits on your X developer project (this pricing is not in the code; confirm current prices in the X Developer Console before posting): $0.015 for a plain post, $0.20 for a post with a link. Prices are subject to change by X.
- Posting with no remaining credits fails with HTTP 402 and nothing is published. Top up credits in the X Developer Console before retrying.
- Set a spending limit in the X Developer Console on the same billing page where you buy credits, so automated posting cannot exceed a budget you choose.
- Groq usage depends on your Groq plan. Each compression or thread split is one or more chat completion calls; short drafts and scheduled posts without AI cost nothing on the Groq side.
- Hard limits enforced by the code: 280 characters per tweet (`X_CHAR_LIMIT` in `utils/config.py`), 25 tweets per thread and 4 media files per tweet (`db/crud.py`), RSS quota `RSS_MAX_PER_DAY` per day.

## 13. Troubleshooting table

| Symptom | Cause | Fix |
|---|---|---|
| `402` / payment error, post marked failed | X prepaid credits depleted | Add credits in the X Developer Console, then tap `Retry` on the failure card |
| `429` / rate limit message, post pushed 15 minutes out | X rate limit hit | Do nothing; the bot re-arms the job automatically. Do not tap `Retry` immediately |
| `Message to edit not found` in the log | The card message was deleted or is stale | Send `/queue`, open the post again, continue from the fresh card |
| `Media file missing: ...th_*.mp4` or media upload failed | The file in `data/media/` was deleted, moved, or saved under a wrong extension | Keep `data/` intact across restarts; re-attach the media to the draft, or tap `Retry (skip bad media)` to post text-only |
| `Another instance is already running` at startup, exit code 2 | A second copy is polling the same token, or a stale `data/bot.lock` | Stop the other copy. If none is running, stop the bot, delete `data/bot.lock`, start again. Never run two copies on one token |
| `BOT_TOKEN is not set` or `ADMIN_ID or ADMIN_IDS is not set`, exit code 1 | `.env` missing or the variable empty | Copy `.env.example` to `.env`, fill in the value, restart. Check for stray spaces or quotes |
| Bot does not respond in Telegram | Wrong token, wrong admin id, bot not started, or another copy holds the token | Check the startup log for the lock and polling lines; verify `BOT_TOKEN` and that your numeric id is in `ADMIN_ID` / `ADMIN_IDS`; send `/start` from the admin account |
| Every post shows failed right after tokens were regenerated | App permissions were changed after tokens were issued, or old tokens pasted | In the X console set Read and Write permissions first, regenerate the access token and secret, paste all four `X_*` values into `.env`, restart, then `Retry` |
| Groq card says compression or thread generation failed | Missing or invalid `GROQ_API_KEY`, or the model name is wrong | Set `GROQ_API_KEY`, set `GROQ_MODEL` explicitly to a model your Groq account supports, restart |
| `... is not a valid IANA timezone` | Typo in the timezone string | Send `/set_tz Africa/Lagos` with a valid IANA name, or pick one from `/settings` |
| Time in the past rejected at scheduling | The chosen day and time already passed in your timezone | Pick a future slot; check `/settings` shows the timezone you expect |
| Dashboard page does not load | Wrong host or port, or the port is busy | Open the URL from the `Dashboard running at http://...` startup line. If the log says the port is in use, free it or set another `DASHBOARD_PORT` and restart |

## 14. Safety checklist

- Never paste `.env`, tokens, or API keys into screenshots, chat logs, or public repos. `.env` stays on your machine only.
- Keep a spending limit on your X developer project so a queue of scheduled posts cannot surprise you.
- If any token or key leaks, revoke it in its console (Telegram via BotFather, X in the developer portal, Groq in its dashboard), generate a new one, update `.env`, and restart the bot.
- Run one instance per bot token. The lock file refuses a second copy for a reason: two pollers fight over the same updates.
- Keep the dashboard on localhost (`127.0.0.1`). It has no login, so do not expose its port to a network.
- Keep `data/` backed up if your drafts matter: it holds the database, the scheduler store, and every staged media file.
