"""Free X posting via web intent. No API. Standalone: does not touch the API publisher."""
import os
import re
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

INTENT_BASE = "https://x.com/intent/post"
MAX_LEN = 280
URL_LEN = 23  # X counts every link as 23 chars
_URL_RE = re.compile(r"https?://\S+")

_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".gif"}


def x_length(text: str) -> int:
    """Approximate X length: links count as 23. (Emoji/CJK weighting ignored.)"""
    return len(_URL_RE.sub("x" * URL_LEN, text))


def build_intent_link(text: str) -> str:
    return f"{INTENT_BASE}?text={quote(text, safe='')}"


def build_keyboard(text: str, post_id: str | int | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Open in X", url=build_intent_link(text))]]
    if post_id is not None:
        rows.append([InlineKeyboardButton("Mark as posted", callback_data=f"intent_done:{post_id}")])
    return InlineKeyboardMarkup(rows)


async def _send_media_files(bot, chat_id: int, paths: list[str]) -> int:
    """Forward local media files to the Telegram chat. Returns count actually sent."""
    sent = 0
    for path in paths:
        if not (path and os.path.isfile(path)):
            continue
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "rb") as fh:
                if ext in _PHOTO_EXTS:
                    await bot.send_photo(chat_id, fh)
                elif ext in _VIDEO_EXTS:
                    await bot.send_video(chat_id, fh)
                else:
                    await bot.send_document(chat_id, fh)
            sent += 1
        except Exception:
            pass  # missing / unreadable file — skip silently
    return sent


async def send_intent_post(bot, chat_id: int, text: str, post_id=None,
                           media: list[str] | None = None) -> bool:
    """Send preview + 'Open in X' button. Returns False if text exceeds 280.

    If *media* paths are supplied, each file is forwarded to the chat first so
    the user can grab them and attach manually on X (web-intent URLs cannot
    carry binary media).
    """
    n = x_length(text)
    if n > MAX_LEN:
        await bot.send_message(chat_id, f"Too long for X ({n}/{MAX_LEN}). Compress first.")
        return False
    valid_media = [p for p in (media or []) if p and os.path.isfile(p)]
    sent = await _send_media_files(bot, chat_id, valid_media) if valid_media else 0
    label = f"{text}\n\n[{n}/{MAX_LEN}]"
    if sent:
        label += f"\n\n📎 {sent} media file(s) shown above — attach manually on X"
    await bot.send_message(
        chat_id,
        label,
        reply_markup=build_keyboard(text, post_id),
        disable_web_page_preview=True,
    )
    return True


async def send_intent_thread(bot, chat_id: int, tweets: list[str], post_id=None,
                             media_lists: list[list[str]] | None = None) -> None:
    """One message per tweet, in order. Post #1 fresh, the rest as replies in X.

    *media_lists* is a parallel list of file-path lists (one entry per tweet).
    Missing or shorter entries are handled gracefully.
    """
    for i, t in enumerate(tweets, 1):
        await bot.send_message(chat_id, f"Tweet {i}/{len(tweets)}" + (" (reply to previous)" if i > 1 else ""))
        tweet_media = media_lists[i - 1] if (media_lists and i - 1 < len(media_lists)) else None
        await send_intent_post(bot, chat_id, t,
                               post_id=f"{post_id}:{i}" if post_id else None,
                               media=tweet_media)


# --- wiring (in your handlers) ---
# from intent_link import send_intent_post
# from telegram.ext import CallbackQueryHandler
#
# async def on_intent_done(update, context):
#     q = update.callback_query
#     await q.answer("Marked as posted")
#     post_id = q.data.split(":", 1)[1]
#     # TODO: update SQLite status for post_id
#     await q.edit_message_reply_markup(reply_markup=None)
#
# app.add_handler(CallbackQueryHandler(on_intent_done, pattern=r"^intent_done:"))
#
# In your post menu, add a third button, e.g. callback_data="post_intent", whose handler calls:
#     await send_intent_post(context.bot, chat_id, compressed_text, post_id, media=post.media)
