"""Timezone-aware helpers. All datetimes are stored in DB as naive UTC."""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from db import crud


def admin_tz() -> ZoneInfo:
    tz_name = crud.get_setting("timezone", "") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def to_admin_tz(dt_utc: datetime) -> datetime:
    """naive-or-aware UTC -> localized in admin tz."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt_timezone.utc)
    return dt_utc.astimezone(admin_tz())


def from_admin_local(naive_local: datetime) -> datetime:
    """Interpret a naive local datetime in the admin tz, return aware UTC."""
    tz = admin_tz()
    aware = naive_local.replace(tzinfo=tz)
    return aware.astimezone(dt_timezone.utc)


def utc_now_aware() -> datetime:
    return datetime.now(dt_timezone.utc)
