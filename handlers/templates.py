"""Draft templates / snippets: save recurring text and inject it into drafts.

Callback namespace: tpl:*
  tpl:add                              – start "Name | Content" input flow
  tpl:list                             – re-render the management list
  tpl:del:<template_id>                – delete a template + re-render
  tpl:inject:<post_id>[:r]             – show picker of templates for a draft
  tpl:use:<post_id>:<template_id>[:r]  – append template content to the post

State (pending add / pending inject) lives in context.user_data so the flows
survive across messages within the same conversation; templates themselves are
persisted in SQLite.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import crud
from utils.formatting import escape_html
from utils.ui import is_admin

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers -

def _chunk(items: list, size: int = 3) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _token(post_id: int, rss: bool) -> str:
    return f"r{post_id}" if rss else str(post_id)


def _parse_token(token: str) -> tuple[int, bool]:
    rss = token.startswith("r")
    return int(token[1:] if rss else token), rss


async def _render_list(bot, chat_id: int, message_id: int | None = None,
                       note: str = "") -> None:
    """Render the /template management list (optionally editing an existing msg)."""
    templates = crud.get_templates()
    lines = ["🧩 <b>Templates</b>", ""]
    if note:
        lines.insert(1, note)
        lines.insert(2, "")
    if not templates:
        lines.append("<i>No templates yet — tap ➕ Add New, then send:</i>")
        lines.append("<code>Name | Content</code>")
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates:
        snippet = escape_html((t.content or "").replace("\n", " ")[:48])
        lines.append(f"<b>{escape_html(t.name)}</b> #{t.id} · {snippet}")
    lines.append("")
    for t in templates:
        rows.append([
            InlineKeyboardButton(f"🗑 {t.name}", callback_data=f"tpl:del:{t.id}"),
        ])
    rows.append([InlineKeyboardButton("➕ Add New", callback_data="tpl:add")])
    kb = InlineKeyboardMarkup(rows)
    html = "\n".join(lines)

    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=html, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            log.debug("template list edit failed, sending fresh")
    await bot.send_message(chat_id=chat_id, text=html, reply_markup=kb, parse_mode="HTML")


async def _render_picker(bot, chat_id: int, post_id: int, rss: bool,
                         message_id: int | None = None) -> None:
    """Show all templates as inject buttons for the given draft."""
    templates = crud.get_templates()
    token = _token(post_id, rss)
    lines = [f"📋 <b>Inject template into draft #{post_id}</b>", "",
             "Tap one — its text is appended to the draft."]
    rows: list[list[InlineKeyboardButton]] = []
    if not templates:
        lines.append("")
        lines.append("<i>No templates saved yet. Use /template → ➕ Add New.</i>")
    for t in templates:
        rows.append([InlineKeyboardButton(t.name,
                                          callback_data=f"tpl:use:{token}:{t.id}")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"d:{token}:back")])
    kb = InlineKeyboardMarkup(rows)
    html = "\n".join(lines)

    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=html, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            log.debug("picker edit failed, sending fresh")
    await bot.send_message(chat_id=chat_id, text=html, reply_markup=kb, parse_mode="HTML")


async def _inject(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                  post_id: int, tpl_id: int, rss: bool) -> None:
    """Append a template's content to a post's raw text and re-render the preview."""
    from utils.ui import update_preview

    post = crud.get_post(post_id)
    tpl = crud.get_template(tpl_id)
    if post is None:
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"ℹ️ Draft #{post_id} no longer exists.")
        return
    if tpl is None:
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"ℹ️ Template #{tpl_id} no longer exists.")
        return
    base = (post.content or "").rstrip()
    sep = "\n\n" if base else ""
    post = crud.update_post(post_id, content=base + sep + tpl.content)
    context.user_data.pop("tpl_flow", None)
    await update_preview(context, chat_id, post,
                         text_extra=f"📋 Appended template <b>{escape_html(tpl.name)}</b>:",
                         rss=rss)


# public aliases used by handlers/drafts.py and handlers/editor.py ------------

async def render_picker(bot, chat_id: int, post_id: int,
                        message_id: int | None = None, rss: bool = False) -> None:
    await _render_picker(bot, chat_id, post_id, rss, message_id=message_id)


async def inject_template(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                          post_id: int, tpl_id: int,
                          message_id: int | None = None, rss: bool = False) -> None:
    await _inject(context, chat_id, post_id, tpl_id, rss)


# ----------------------------------------------------------------- commands -

async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/template — manage saved snippets."""
    from utils.ui import deny

    if not is_admin(update):
        await deny(update, context)
        return
    await _render_list(context.bot, update.effective_chat.id)


# ---------------------------------------------------------------- callbacks -

async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles every tpl:* inline keyboard press."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return

    chat_id = update.effective_chat.id
    msg_id = cq.message.message_id if cq.message else None
    parts = cq.data.split(":")
    head = parts[1] if len(parts) > 1 else ""

    if head == "add":
        context.user_data["tpl_flow"] = "add_name_content"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "↩️ Cancel", callback_data="tpl:list")]])
        try:
            await cq.edit_message_text(
                "🆕 Send the new template as:\n\n<code>Name | Content</code>\n\n"
                "e.g. <code>NFA | DYOR, not financial advice.</code>",
                reply_markup=kb, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id=chat_id,
                                           text="🆕 Send: Name | Content",
                                           reply_markup=kb)
        return

    if head == "list":
        context.user_data.pop("tpl_flow", None)
        await _render_list(context.bot, chat_id, message_id=msg_id)
        return

    if head == "del":
        tpl_id = int(parts[2])
        ok = crud.delete_template(tpl_id)
        note = (f"🗑 Template #{tpl_id} deleted." if ok
                else f"ℹ️ Template #{tpl_id} not found.")
        await _render_list(context.bot, chat_id, message_id=msg_id, note=note)
        return

    if head == "inject":
        post_id, rss = _parse_token(parts[2])
        context.user_data["tpl_inject_from"] = {"post_id": post_id, "rss": rss,
                                                "message_id": msg_id}
        await _render_picker(context.bot, chat_id, post_id, rss)
        return

    if head == "use":
        post_id, rss = _parse_token(parts[2])
        tpl_id = int(parts[3])
        await _inject(context, chat_id, post_id, tpl_id, rss)
        return


# ----------------------------------------------------- text-input handling --

async def handle_template_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume 'Name | Content' while the add-template flow is active. True = handled."""
    if context.user_data.get("tpl_flow") != "add_name_content" or not update.message:
        return False
    msg = update.message
    raw = (msg.text or "").strip()
    chat_id = msg.chat.id

    if "|" not in raw:
        await msg.reply_text("❌ Wrong format. Send exactly:\n<code>Name | Content</code>",
                             parse_mode="HTML")
        return True
    name, _, content = raw.partition("|")
    tpl = crud.create_template(name.strip(), content.strip())
    if tpl is None:
        await msg.reply_text(f"⚠️ Could not save — empty fields or duplicate name "
                             f"<b>{escape_html(name.strip())}</b>.", parse_mode="HTML")
        return True
    context.user_data.pop("tpl_flow", None)
    await msg.reply_text(f"✅ Template <b>{escape_html(tpl.name)}</b> saved (#{tpl.id}).",
                         parse_mode="HTML")
    await _render_list(context.bot, chat_id)
    return True
