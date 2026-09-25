"""Draft flow: content in -> preview card with Confirm/Edit/Regenerate/Cancel."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from db import crud
from utils import config
from utils.formatting import escape_html, format_preview, strip_markdown
from utils.generator import generate_draft
from utils.ui import (
    draft_keyboard,
    is_admin,
    media_keyboard,
    show_preview,
    tag_keyboard,
    update_preview,
)

log = logging.getLogger(__name__)


def _is_rss(post_id_token: str) -> tuple[bool, int]:
    """callback data uses d:r<id>:action for RSS drafts."""
    if post_id_token.startswith("r"):
        return True, int(post_id_token[1:])
    return False, int(post_id_token)


async def new_draft_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            text: str, media_path: str | None = None) -> int:
    """Create a draft row and push the preview card. Returns message id."""
    post = crud.create_post(content=text, media_path=media_path,
                            auto_format=crud.get_bool("auto_format", True))
    mid = await show_preview(context, update.effective_chat.id, post,
                             text_extra="📝 <b>New draft</b> — confirm, edit or cancel:")
    return mid


async def push_rss_draft(bot, post) -> None:
    """Send an RSS-generated draft to admin with Approve/Reject/Edit."""
    html = f"📡 <b>RSS draft</b> ({crud.count_posts('draft')} drafts pending)\n\n{format_preview(post)}"
    kb = draft_keyboard(post.id, rss=True)
    sent = await bot.send_message(chat_id=config.ADMIN_ID, text=html,
                                  reply_markup=kb, parse_mode="HTML")
    crud.update_post(post.id, tg_message_id=sent.message_id)


STATS_UNAVAILABLE_TEXT = (
    "📊 Stats: X API Free Tier does not support view/impression metrics. "
    "Upgrade to Basic tier to enable."
)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles d:, tag:, dup:, f: and st: callbacks on draft/preview cards."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    parts = cq.data.split(":")            # <head>:<token>:<action>[:<arg>]

    # ---- st:<post_id> — tweet stats ------------------------------------------
    if parts[0] == "st":
        post_id = int(parts[1])
        post = crud.get_post(post_id)
        if post is None:
            await cq.answer("Post not found", show_alert=True)
            return
        stats = None
        if post.tweet_id:
            from utils.twitter import get_tweet_stats

            try:
                stats = get_tweet_stats(post.tweet_id)
            except Exception:
                stats = None
        if stats:
            await cq.answer(
                f"👁 {stats.get('views', 0):,} · 💬 {stats.get('replies', 0)} · "
                f"🔁 {stats.get('retweets', 0)} · ❤️ {stats.get('likes', 0)}",
                show_alert=True)
        else:
            await cq.message.reply_text(STATS_UNAVAILABLE_TEXT)
        return

    # ---- f:<post_id>:<retry|editretry|discard> — failure actions --------------
    if parts[0] == "f":
        await _failure_action(update, context, int(parts[1]), parts[2])
        return

    # ---- bare tag:<post_id>:<tag> / dup:<post_id> aliases ---------------------
    if parts[0] == "tag":
        post = crud.get_post(int(parts[1]))
        if post is None:
            await cq.edit_message_text("ℹ️ This draft no longer exists.")
            return
        post = crud.update_post(post.id, tag=parts[2])
        await update_preview(context, update.effective_chat.id, post,
                             text_extra=f"🏷 Tagged <code>{parts[2]}</code>")
        return
    if parts[0] == "dup":
        clone = crud.duplicate_post(int(parts[1]))
        if clone is None:
            await cq.edit_message_text("ℹ️ Source post no longer exists.")
            return
        await show_preview(context, update.effective_chat.id, clone,
                           text_extra=f"🔁 <b>Duplicated from #{parts[1]}</b>")
        return

    rss_flag, post_id = _is_rss(parts[1])
    action = parts[2]
    chat_id = update.effective_chat.id

    post = crud.get_post(post_id)
    if post is None:
        await cq.edit_message_text("ℹ️ This draft no longer exists.")
        return

    if action == "confirm":
        from handlers.scheduler_flow import enter_scheduling

        await enter_scheduling(context, chat_id, post)
    elif action == "edit":
        from handlers.editor import start_editing

        await start_editing(update, context, post, rss=rss_flag)
    elif action == "regen":
        new_content = generate_draft(post.content, post.id)
        post = crud.update_post(post_id, content=new_content)
        await update_preview(context, chat_id, post,
                             text_extra="🔄 <b>Regenerated</b> — same slot, fresh take:",
                             rss=rss_flag)
    elif action == "cancel":
        crud.delete_post(post_id)
        try:
            await cq.edit_message_text(f"🗑 Draft #{post_id} cancelled.")
        except Exception:
            pass
    elif action == "dup":
        clone = crud.duplicate_post(post_id)
        if clone:
            await show_preview(context, chat_id, clone,
                               text_extra=f"🔁 <b>Duplicated from #{post_id}</b>")
    elif action == "tags":
        await cq.edit_message_reply_markup(reply_markup=tag_keyboard(post_id))
    elif action == "tag":
        tag = parts[3]
        post = crud.update_post(post_id, tag=tag)
        await update_preview(context, chat_id, post, text_extra=f"🏷 Tagged <code>{tag}</code>",
                             rss=rss_flag)
    elif action == "media":
        await cq.edit_message_reply_markup(
            reply_markup=media_keyboard(post_id, bool(post.media_path)))
    elif action == "unmedia":
        post = crud.update_post(post_id, media_path=None)
        await update_preview(context, chat_id, post, text_extra="📎 Media removed.",
                             rss=rss_flag)
    elif action == "approve":  # RSS approve -> scheduling step
        from handlers.scheduler_flow import enter_scheduling

        await enter_scheduling(context, chat_id, post)
    elif action == "reject":  # RSS reject -> discard
        crud.delete_post(post_id)
        try:
            await cq.edit_message_text(f"❌ Rejected RSS draft #{post_id}.")
        except Exception:
            pass
    elif action == "back":
        await update_preview(context, chat_id, post, rss=rss_flag)


async def _failure_action(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          post_id: int, action: str) -> None:
    """f:<post_id>:<retry|editretry|discard> — actions on the failure notice card."""
    cq = update.callback_query
    chat_id = update.effective_chat.id
    post = crud.get_post(post_id)
    if post is None:
        await cq.edit_message_text("ℹ️ Post not found.")
        return

    if action == "retry":
        from scheduler.publisher import publish_post

        await cq.edit_message_text(f"🔁 Retrying post #{post_id}…")
        await publish_post(context.application, post_id, manual=True)
    elif action == "editretry":
        from handlers.editor import start_editing

        await start_editing(update, context, post, rss=False)
    elif action == "discard":
        from scheduler.jobs import unschedule_post_job

        unschedule_post_job(post_id)
        crud.delete_post(post_id)
        try:
            await cq.edit_message_text("🗑 Discarded.")
        except Exception:
            pass
    else:
        await cq.answer("Unknown action", show_alert=True)


# ----------------------------------------------------- template injection --

def has_templates() -> bool:
    return bool(crud.get_templates())


async def templates_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                             post_id: int, message_id: int) -> None:
    """[📋 Templates] pressed on a draft card → inline picker of saved snippets."""
    from handlers.templates import render_picker

    await render_picker(context.bot, chat_id, post_id, message_id=message_id)


async def use_template(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                       post_id: int, tpl_id: int, message_id: int | None = None) -> None:
    """Append template content to the draft's raw text and re-render its preview."""
    from handlers.templates import inject_template

    await inject_template(context, chat_id, post_id, tpl_id, message_id=message_id)
