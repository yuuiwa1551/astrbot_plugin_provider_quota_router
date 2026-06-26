from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class UsageWindow:
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime
    window_id: str


def parse_reset_time(value: str) -> time:
    raw = str(value or "00:00").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid reset_time: {value!r}, expected HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid reset_time: {value!r}, expected HH:MM")
    return time(hour=hour, minute=minute)


def current_window(
    *,
    timezone_name: str,
    reset_time: str,
    now: datetime | None = None,
) -> UsageWindow:
    tz = ZoneInfo(str(timezone_name or "Asia/Shanghai"))
    now_local = now.astimezone(tz) if now else datetime.now(tz)
    reset = parse_reset_time(reset_time)
    start_local = now_local.replace(
        hour=reset.hour,
        minute=reset.minute,
        second=0,
        microsecond=0,
    )
    if now_local < start_local:
        start_local -= timedelta(days=1)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return UsageWindow(
        start_local=start_local,
        end_local=end_local,
        start_utc=start_utc,
        end_utc=end_utc,
        window_id=start_local.strftime("%Y%m%dT%H%M%S%z"),
    )


def window_for_local_date(
    *,
    timezone_name: str,
    reset_time: str,
    local_date: date,
) -> UsageWindow:
    tz = ZoneInfo(str(timezone_name or "Asia/Shanghai"))
    reset = parse_reset_time(reset_time)
    start_local = datetime.combine(local_date, reset, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return UsageWindow(
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        window_id=start_local.strftime("%Y%m%dT%H%M%S%z"),
    )
