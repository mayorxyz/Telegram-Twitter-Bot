"""Calendar + time-slot keyboards for the scheduling flow.

Callback grammar:
  cal:<post_id>:<YYYY-MM|prev|next|today>   – month grid navigation
  day:<post_id>:<YYYY-MM-DD>                – pick a day -> hour slots
  hr:<post_id>:<YYYY-MM-DD>:<HH>            – pick an hour -> minute slots
  mn:<post_id>:<YYYY-MM-DD>:<HH>:<MM|c>     – pick minutes / custom input
  sd:<post_id>:<iso-utc-datetime>           – final set (used internally)
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.timeutil import admin_tz, from_admin_local, utc_now_aware

WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def entry_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Pick Date", callback_data=f"cal:{post_id}:open"),
            InlineKeyboardButton("⚡ Post Now", callback_data=f"now:{post_id}"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"d:{post_id}:cancel")],
    ])


def _month_label(d: datetime) -> str:
    return f"{calendar.month_name[d.month]} {d.year}"


def calendar_keyboard(post_id: int, year: int, month: int) -> InlineKeyboardMarkup:
    today = utc_now_aware().astimezone(admin_tz()).date()
    cur = datetime(year, month, 1)
    first_weekday = cur.weekday()  # Monday=0
    days_in_month = calendar.monthrange(year, month)[1]

    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton("◀️", callback_data=f"cal:{post_id}:prev"),
        InlineKeyboardButton(_month_label(cur), callback_data=f"cal:{post_id}:noop"),
        InlineKeyboardButton("▶️", callback_data=f"cal:{post_id}:next"),
    ]]
    rows.append([InlineKeyboardButton(w, callback_data=f"cal:{post_id}:noop") for w in WEEKDAYS])

    week: list[InlineKeyboardButton] = [InlineKeyboardButton("", callback_data=f"cal:{post_id}:noop")
                                        for _ in range(first_weekday)]
    for day_num in range(1, days_in_month + 1):
        d = datetime(year, month, day_num).date()
        label = f"{day_num} ✅" if d == today else f"{day_num}"
        disabled = d < today
        cb = f"day:{post_id}:{d.isoformat()}" if not disabled else f"cal:{post_id}:noop"
        week.append(InlineKeyboardButton(label, callback_data=cb))
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        while len(week) < 7:
            week.append(InlineKeyboardButton("", callback_data=f"cal:{post_id}:noop"))
        rows.append(week)
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"d:{post_id}:back")])
    return InlineKeyboardMarkup(rows)


def hours_keyboard(post_id: int, date_iso: str) -> InlineKeyboardMarkup:
    """24 hourly slots in a 6x4 grid."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for h in range(24):
        row.append(InlineKeyboardButton(f"{h:02d}:00", callback_data=f"hr:{post_id}:{date_iso}:{h:02d}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([
        InlineKeyboardButton("🕐 Custom time…", callback_data=f"mn:{post_id}:{date_iso}:c:c"),
        InlineKeyboardButton("↩️ Month", callback_data=f"cal:{post_id}:open"),
    ])
    return InlineKeyboardMarkup(rows)


def minutes_keyboard(post_id: int, date_iso: str, hour: int) -> InlineKeyboardMarkup:
    mins = [InlineKeyboardButton(f":{m:02d}", callback_data=f"mn:{post_id}:{date_iso}:{hour:02d}:{m:02d}")
            for m in (0, 15, 30, 45)]
    mins.insert(2, InlineKeyboardButton("⏱ now+30m", callback_data=f"quick:{post_id}:30"))
    return InlineKeyboardMarkup([
        mins,
        [InlineKeyboardButton("↩️ Hours", callback_data=f"day:{post_id}:{date_iso}")],
    ])


def build_dt_utc(date_iso: str, hour: int, minute: int) -> datetime:
    """Local (admin tz) wall time -> aware UTC datetime."""
    naive_local = datetime.fromisoformat(date_iso).replace(hour=hour, minute=minute)
    return from_admin_local(naive_local)


def shift_month(dt: datetime, delta: int) -> tuple[int, int]:
    m = dt.month - 1 + delta
    y = dt.year + m // 12
    m = m % 12 + 1
    return y, m
