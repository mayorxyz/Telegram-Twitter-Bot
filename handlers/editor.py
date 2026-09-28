"""Edit & custom-time input flows (conversation-based, state in DB).

States stored in context.user_data['flow']:
  ('edit_content', post_id, rss:bool)   – waiting for replacement text
  ('custom_time', post_id, date_iso, from_queue:bool) – waiting for HH:MM / full datetime
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from db import crud
from utils import config
from utils.formatting import (effective_length, escape_html, format_preview,
                              twitter_len, truncate_to_limit)
from utils.timeutil import from_admin_local, utc_now_aware
from utils.ui import is_admin, update_preview

log = logging.getLogger(__name__)


async def start_editing(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        post, rss: bool = False) -> None:
    """Prompt the admin to type replacement text; live char-count comes with each edit."""
    cq = update.callback_query
    context.user_data["flow"] = ("edit_content", post.id, rss)
    kb = _exit_keyboard(post.id, rss)
    hint = (f"✏️ <b>Edit post #{post.id}</b>\n"
            f"Send the new text — I'll replace it in place.\n"
            f"(current: {effective_length(post.content or '')}/{config.X_CHAR_LIMIT} chars)")
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=cq.message.message_id,
            text=hint, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=hint, reply_markup=kb, parse_mode="HTML")


def _exit_keyboard(post_id: int, rss: bool) -> "telegram.InlineKeyboardMarkup":
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    token = f"r{post_id}" if rss else str(post_id)
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Cancel edit",
                                                       callback_data=f"d:{token}:back")]])


async def start_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            post_id: int, date_iso: str | None,
                            from_queue: bool = False) -> None:
    cq = update.callback_query
    context.user_data["flow"] = ("custom_time", post_id, date_iso or "", from_queue)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    token = f"q{post_id}" if from_queue else str(post_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "↩️ Back", callback_data=f"cal:{token}:open")]])
    text = ("🕐 Send a custom time:\n"
            "• <code>HH:MM</code> (for the selected day)\n"
            "• <code>YYYY-MM-DD HH:MM</code>")
    if not date_iso:
        text += "\n(no day selected — use full datetime)"
    try:
        await cq.edit_message_text(text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=text, reply_markup=kb, parse_mode="HTML")


async def handle_flow_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called by the text/photo handlers first. Returns True if a flow consumed the msg."""
    flow = context.user_data.get("flow")
    if not flow or not update.message:
        return False
    msg = update.message
    chat_id = msg.chat.id

    if flow[0] == "edit_content":
        _, post_id, rss = flow
        post = crud.get_post(post_id)
        if post is None:
            context.user_data.pop("flow", None)
            await msg.reply_text("ℹ️ That draft no longer exists.")
            return True
        new_text = msg.text or ""
        if not new_text.strip():
            await msg.reply_text("⚠️ Empty text — send real content or ↩️ Cancel edit.")
            return True
        # Over the X limit → offer AI compress / thread-split before saving raw.
        if twitter_len(new_text) > config.X_CHAR_LIMIT:
            context.user_data.pop("flow", None)
            await _handle_long_text(update, context, post, new_text, chat_id, rss)
            return True
        post = crud.update_post(post_id, content=new_text)
        context.user_data.pop("flow", None)
        n = effective_length(new_text)
        extra = (f"✏️ <b>Updated</b> · {n}/{config.X_CHAR_LIMIT} chars"
                 + (" ⚠️ over limit!" if n > config.X_CHAR_LIMIT else ""))
        await update_preview(context, chat_id, post, text_extra=extra, rss=rss)
        return True

    if flow[0] == "custom_time":
        _, post_id, date_iso, from_queue = flow
        raw = (msg.text or "").strip()
        when = _parse_custom(raw, date_iso)
        if when is None:
            await msg.reply_text("❌ Couldn't parse. Use HH:MM or YYYY-MM-DD HH:MM.")
            return True
        if when <= utc_now_aware():
            await msg.reply_text("⏰ That's in the past — pick a future time.")
            return True
        context.user_data.pop("flow", None)
        from handlers.scheduler_flow import reschedule_existing, save_schedule

        if from_queue:
            await reschedule_existing(context, chat_id, post_id, when)
        else:
            await save_schedule(context, chat_id, post_id, when)
        return True

    return False


def _parse_custom(raw: str, date_iso: str) -> datetime | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if m and date_iso:
        naive = datetime.fromisoformat(date_iso).replace(hour=int(m.group(1)),
                                                         minute=int(m.group(2)))
        return from_admin_local(naive)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", raw)
    if m:
        naive = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)))
        return from_admin_local(naive)
    return None


# ------------------------------------------------------------- media ------

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo attached while editing a draft → attach to that draft; else new draft."""
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    msg = update.message
    photo = msg.photo[-1] if msg.photo else None
    if photo is None:
        return
    file = await context.bot.get_file(photo.file_id)
    fname = f"media_{msg.message_id}.jpg"
    path = os.path.join(config.MEDIA_DIR, fname)
    await file.download_to_drive(path)

    flow = context.user_data.get("flow")
    if flow and flow[0] == "edit_content":
        _, post_id, rss = flow
        post = crud.add_post_media(post_id, [path])
        caption = (msg.caption or "").strip()
        if caption:
            if twitter_len(caption) > config.X_CHAR_LIMIT:
                context.user_data.pop("flow", None)
                await _handle_long_text(update, context, post, caption, msg.chat.id, rss)
                return
            post = crud.update_post(post_id, content=caption)
            n = effective_length(caption)
            extra = (f"📎 <b>Media attached & caption updated</b> · {n}/{config.X_CHAR_LIMIT} chars"
                     + (" ⚠️ over limit!" if n > config.X_CHAR_LIMIT else ""))
            await update_preview(context, msg.chat.id, post, text_extra=extra, rss=rss)
            context.user_data.pop("flow", None)
            return
        await update_preview(context, msg.chat.id, post, text_extra="📎 Media attached.")
        return
    # Caption-less media drafts store empty content: the card renders "📎 Media post"
    # and publish sends the files with no text. (Telegram puts photo captions in
    # msg.caption, never msg.text.)
    caption = (msg.caption or msg.text or "").strip()
    from handlers.drafts import new_draft_message

    await new_draft_message(update, context, caption, media_ids=[path])


# ------------------------------------------------- inline editor callbacks --

async def editor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles ed_save:<id>, ed_cancel:<id> and custom_time:<token>:<date_iso>."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    parts = cq.data.split(":")
    head = parts[0]
    chat_id = update.effective_chat.id

    if head == "ed_save":
        post_id = int(parts[1])
        rss = len(parts) > 2 and parts[2] == "r"
        draft = context.user_data.get("edit_draft", {}).get(str(post_id))
        post = crud.get_post(post_id)
        if post is None:
            await cq.edit_message_text("ℹ️ Post not found.")
            return
        if draft:
            post = crud.update_post(post_id, content=draft)
            context.user_data.get("edit_draft", {}).pop(str(post_id), None)
        n = effective_length(post.content or "")
        extra = (f"💾 <b>Saved</b> · {n}/{config.X_CHAR_LIMIT} chars"
                 + (" ⚠️ over limit!" if n > config.X_CHAR_LIMIT else ""))
        context.user_data.pop("flow", None)
        await update_preview(context, chat_id, post, text_extra=extra, rss=rss)
        return

    if head == "ed_cancel":
        post_id = int(parts[1])
        rss = len(parts) > 2 and parts[2] == "r"
        context.user_data.pop("flow", None)
        context.user_data.get("edit_draft", {}).pop(str(post_id), None)
        post = crud.get_post(post_id)
        if post is None:
            await cq.edit_message_text("ℹ️ Post not found.")
            return
        await update_preview(context, chat_id, post, text_extra="↩️ Edit cancelled.", rss=rss)
        return

    if head == "custom_time":
        token, date_iso = parts[1], parts[2]
        from_queue = token.startswith("q")
        post_id = int(token[1:] if from_queue else token)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⏱ +30 min", callback_data=f"quick:{token}:30"),
            InlineKeyboardButton("📅 Calendar", callback_data=f"cal:{token}:open"),
            InlineKeyboardButton("↩️ Cancel", callback_data=f"d:{post_id}:back")]])
        context.user_data["flow"] = ("custom_time", post_id, date_iso, from_queue)
        await cq.edit_message_text(
            "🕐 Send a custom time:\n"
            "• <code>HH:MM</code> (for the selected day)\n"
            "• <code>YYYY-MM-DD HH:MM</code>",
            reply_markup=kb, parse_mode="HTML")
        return

    if head == "comp" or head.startswith("comp_"):
        await _handle_compression_callback(update, context, head, parts)
        return


async def _handle_long_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           post=None, text: str = "", chat_id: int | None = None,
                           rss: bool = False) -> None:
    """Handle tweet text > 280 chars: compress via Groq AI & present action buttons."""
    if isinstance(post, str) and not text:
        text = post
        post = None

    text = (text or "").strip()
    text_len = twitter_len(text)
    cid = chat_id or (update.effective_chat.id if update.effective_chat else 0)
    post_id = post.id if post else 0

    status_text = f"⚠️ Text is {text_len} chars (limit: 280). Compressing with Groq AI..."
    msg = None
    if update.callback_query:
        try:
            msg = await update.callback_query.edit_message_text(status_text)
        except Exception:
            msg = await context.bot.send_message(chat_id=cid, text=status_text)
    elif update.message:
        msg = await update.message.reply_text(status_text)
    else:
        msg = await context.bot.send_message(chat_id=cid, text=status_text)

    from utils.groq_helper import compress_tweet, GroqError

    try:
        compressed = await compress_tweet(text)
    except GroqError as exc:
        log.warning("Groq compression failed: %s — using fallback trim", exc)
        compressed = truncate_to_limit(text, config.X_CHAR_LIMIT)
    except Exception as exc:
        log.exception("Unexpected error in compress_tweet: %s", exc)
        compressed = truncate_to_limit(text, config.X_CHAR_LIMIT)

    comp_len = twitter_len(compressed)

    if post:
        post = crud.update_post(post.id, content=compressed)

    context.user_data["comp_state"] = {
        "original_text": text,
        "compressed_text": compressed,
        "post_id": post.id if post else post_id,
        "rss": rss,
    }

    # If post has attached media, render full draft preview card with media!
    if post and getattr(post, "media", None) and len([p for p in post.media if p]):
        from utils.ui import show_preview
        if msg:
            try:
                await msg.delete()
            except Exception:
                pass
        await show_preview(
            context, cid, post,
            text_extra=f"✨ <b>Caption compressed with Groq AI ({comp_len}/280)!</b>",
            rss=rss
        )
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    reply_text = f"✅ <b>Compressed ({comp_len}/280):</b>\n\n{escape_html(compressed)}"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Post Now", callback_data=f"comp:post:{post.id if post else post_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"comp:edit:{post.id if post else post_id}"),
            InlineKeyboardButton("🧵 Make Thread", callback_data=f"comp:thread:{post.id if post else post_id}"),
        ]
    ])

    try:
        if msg:
            await msg.edit_text(reply_text, reply_markup=kb, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id=cid, text=reply_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=cid, text=reply_text, reply_markup=kb, parse_mode="HTML")


async def _handle_compression_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                        head: str, parts: list[str]) -> None:
    """Callback handler for compression flow buttons: comp:post, comp:edit, comp:thread, comp:post_thread."""
    cq = update.callback_query
    action = ""
    post_id = 0

    if head.startswith("comp_"):
        action = head[5:]
        if len(parts) > 1 and parts[1].isdigit():
            post_id = int(parts[1])
    elif head == "comp" and len(parts) > 1:
        action = parts[1]
        if len(parts) > 2 and parts[2].isdigit():
            post_id = int(parts[2])

    state = context.user_data.get("comp_state", {})
    original_text = state.get("original_text", "")
    compressed_text = state.get("compressed_text", "")
    rss = state.get("rss", False)
    chat_id = update.effective_chat.id

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from utils.groq_helper import GroqError, make_thread
    from utils.twitter import PublishError, publish_tweet
    from utils.ui import show_preview

    if action == "post":
        post = crud.get_post(post_id) if post_id else None
        text_to_post = compressed_text or (post.content if post else "")
        if not text_to_post:
            await cq.edit_message_text("❌ No text found to post.")
            return

        # Hard validate before publish
        if twitter_len(text_to_post) > config.X_CHAR_LIMIT:
            text_to_post = truncate_to_limit(text_to_post, config.X_CHAR_LIMIT)

        media_paths = list(post.media) if post and getattr(post, "media", None) else None

        try:
            tweet_id = publish_tweet(text_to_post, media_paths=media_paths)
            if post:
                post = crud.update_post(post_id, content=text_to_post, status="published", tweet_id=tweet_id)
            else:
                post = crud.create_post(content=text_to_post)
                post = crud.update_post(post.id, status="published", tweet_id=tweet_id)
            
            await show_preview(context, chat_id, post, text_extra=f"🚀 <b>Published to X!</b> (Tweet ID: <code>{escape_html(str(tweet_id))}</code>)")
        except PublishError as exc:
            await cq.edit_message_text(f"❌ Failed to publish: {escape_html(str(exc))}", parse_mode="HTML")
        return

    if action == "edit":
        text_to_edit = compressed_text
        if not text_to_edit and post_id:
            p = crud.get_post(post_id)
            text_to_edit = p.content if p else ""

        if post_id:
            post = crud.update_post(post_id, content=text_to_edit)
        else:
            post = crud.create_post(content=text_to_edit)

        # Show standard draft preview card so user gets full interactivity
        await show_preview(context, chat_id, post, text_extra="📝 <b>Compressed draft created.</b> Edit, schedule, or post below:")
        return

    if action == "thread":
        if not original_text and post_id:
            p = crud.get_post(post_id)
            if p:
                original_text = p.content or ""

        if not original_text:
            await cq.edit_message_text("❌ Original text missing for thread creation.")
            return

        await cq.edit_message_text("⏳ Splitting text into thread with Groq AI...")
        try:
            thread_parts = await make_thread(original_text)
        except GroqError as exc:
            await cq.edit_message_text(f"❌ Thread generation failed: {escape_html(str(exc))}", parse_mode="HTML")
            return
        except Exception as exc:
            log.exception("make_thread failed")
            await cq.edit_message_text(f"❌ Thread generation failed: {escape_html(str(exc))}", parse_mode="HTML")
            return

        if isinstance(thread_parts, str) and thread_parts.startswith("Error"):
            await cq.edit_message_text(f"❌ {escape_html(thread_parts)}", parse_mode="HTML")
            return

        # Create real thread in DB so it integrates into the bot's thread builder
        if post_id:
            post = crud.get_post(post_id)
            if post:
                post = crud.update_post(post_id, content=thread_parts[0])
            else:
                post = crud.create_post(content=thread_parts[0])
        else:
            post = crud.create_post(content=thread_parts[0])

        crud.create_thread(post.id, [{"content": c, "media_ids": []} for c in thread_parts])

        from handlers.threads import _show_builder
        await _show_builder(context, chat_id, post, note="🧵 <b>Thread created with Groq AI!</b> Review, edit tweets, or post:")
        return

    if action == "post_thread":
        thread_parts = context.user_data.get("comp_thread_parts", [])
        post = crud.get_post(post_id) if post_id else None

        if not thread_parts and post and getattr(post, "thread", None):
            thread_parts = [t.content for t in sorted(post.thread.tweets, key=lambda x: x.position)]

        if not thread_parts:
            await cq.edit_message_text("❌ No thread content found to publish.")
            return

        await cq.edit_message_text("🚀 Publishing thread to X...")
        prev_id = None
        published_ids = []
        media_paths = list(post.media) if post and getattr(post, "media", None) else None

        for idx, tweet_text in enumerate(thread_parts):
            if twitter_len(tweet_text) > config.X_CHAR_LIMIT:
                tweet_text = truncate_to_limit(tweet_text, config.X_CHAR_LIMIT)

            curr_media = media_paths if (idx == 0 and media_paths) else None
            try:
                tid = publish_tweet(tweet_text, media_paths=curr_media, reply_to=prev_id)
                prev_id = tid
                published_ids.append(tid)
            except PublishError as exc:
                await cq.edit_message_text(f"❌ Thread posting failed at tweet {idx+1}: {escape_html(str(exc))}", parse_mode="HTML")
                return

        if post:
            crud.update_post(post_id, status="published", tweet_id=published_ids[0])

        await cq.edit_message_text(f"🚀 Published thread of {len(published_ids)} tweets to X!", parse_mode="HTML")
        return


