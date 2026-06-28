from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import func, select

from astrbot.core.db.po import ProviderStat

from .time_window import UsageWindow, parse_reset_time, window_for_local_date


@dataclass(frozen=True)
class UsageRecord:
    quota_key: str
    db_tokens: int
    pending_tokens: int
    overlay_tokens: int

    @property
    def effective_tokens(self) -> int:
        return self.db_tokens + self.pending_tokens + self.overlay_tokens


class QuotaLedger:
    def __init__(self, db_helper, *, count_cached_input_tokens: bool = True) -> None:
        self.db_helper = db_helper
        self.count_cached_input_tokens = count_cached_input_tokens

    async def query_usage(
        self,
        *,
        quota_key: str,
        quota_key_mode: str,
        window: UsageWindow,
    ) -> int:
        token_expr = ProviderStat.token_input_other + ProviderStat.token_output
        if self.count_cached_input_tokens:
            token_expr = token_expr + ProviderStat.token_input_cached

        filters = [
            ProviderStat.agent_type == "internal",
            ProviderStat.created_at >= window.start_utc,
            ProviderStat.created_at < window.end_utc,
        ]
        if quota_key_mode == "provider_id":
            filters.append(ProviderStat.provider_id == quota_key)
        else:
            filters.append(ProviderStat.provider_model == quota_key)

        async with self.db_helper.get_db() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(token_expr), 0)).where(*filters)
            )
            return int(result.scalar_one() or 0)

    async def query_daily_model_usage(
        self,
        *,
        start_date: date,
        end_date: date,
        timezone_name: str,
        reset_time: str,
        model_filter: str = "",
    ) -> dict:
        """Aggregate ProviderStat rows into local daily model/provider usage.

        The bucket boundary follows the same timezone + reset_time window as quota
        routing. This keeps the report aligned with the daily quota checks instead
        of relying on SQLite date functions or server-local timezone assumptions.
        """
        if end_date < start_date:
            start_date, end_date = end_date, start_date

        first_window = window_for_local_date(
            timezone_name=timezone_name,
            reset_time=reset_time,
            local_date=start_date,
        )
        last_window = window_for_local_date(
            timezone_name=timezone_name,
            reset_time=reset_time,
            local_date=end_date,
        )
        token_columns = (
            ProviderStat.token_input_other,
            ProviderStat.token_input_cached,
            ProviderStat.token_output,
        )
        filters = [
            ProviderStat.agent_type == "internal",
            ProviderStat.created_at >= first_window.start_utc,
            ProviderStat.created_at < last_window.end_utc,
        ]

        async with self.db_helper.get_db() as session:
            result = await session.execute(
                select(
                    ProviderStat.provider_id,
                    ProviderStat.provider_model,
                    *token_columns,
                    ProviderStat.created_at,
                ).where(*filters)
            )
            raw_rows = result.all()

        tz = ZoneInfo(str(timezone_name or "Asia/Shanghai"))
        reset = parse_reset_time(reset_time)
        wanted_model = str(model_filter or "").strip()
        days = _date_range(start_date, end_date)
        day_set = set(days)
        by_day_model: dict[str, dict[str, dict]] = {item.isoformat(): {} for item in days}
        model_totals: dict[str, dict] = {}
        daily_totals: dict[str, dict] = {
            item.isoformat(): {"date": item.isoformat(), "tokens": 0, "calls": 0}
            for item in days
        }

        for row in raw_rows:
            provider_id = str(row[0] or "")
            provider_model = str(row[1] or provider_id or "unknown")
            if wanted_model and wanted_model not in {provider_model, provider_id}:
                continue
            created_at = _as_utc_datetime(row[5])
            bucket = _window_local_date(created_at, tz=tz, reset_time=reset)
            if bucket not in day_set:
                continue

            tokens = int(row[2] or 0) + int(row[4] or 0)
            if self.count_cached_input_tokens:
                tokens += int(row[3] or 0)

            day_key = bucket.isoformat()
            model_entry = by_day_model[day_key].setdefault(
                provider_model,
                {
                    "date": day_key,
                    "provider_model": provider_model,
                    "tokens": 0,
                    "calls": 0,
                    "provider_ids": set(),
                },
            )
            model_entry["tokens"] += tokens
            model_entry["calls"] += 1
            if provider_id:
                model_entry["provider_ids"].add(provider_id)

            total_entry = model_totals.setdefault(
                provider_model,
                {
                    "provider_model": provider_model,
                    "tokens": 0,
                    "calls": 0,
                    "provider_ids": set(),
                },
            )
            total_entry["tokens"] += tokens
            total_entry["calls"] += 1
            if provider_id:
                total_entry["provider_ids"].add(provider_id)

            daily_totals[day_key]["tokens"] += tokens
            daily_totals[day_key]["calls"] += 1

        sorted_models = sorted(
            model_totals.values(),
            key=lambda item: (-int(item["tokens"]), str(item["provider_model"])),
        )
        model_names = [str(item["provider_model"]) for item in sorted_models]
        daily_rows: list[dict] = []
        for day in days:
            day_key = day.isoformat()
            for model_name in model_names:
                entry = by_day_model[day_key].get(model_name)
                if not entry:
                    daily_rows.append(
                        {
                            "date": day_key,
                            "provider_model": model_name,
                            "tokens": 0,
                            "calls": 0,
                            "provider_ids": [],
                        }
                    )
                    continue
                daily_rows.append(_serialize_usage_entry(entry))

        series = [
            {
                "provider_model": model_name,
                "tokens": [
                    int(by_day_model[day.isoformat()].get(model_name, {}).get("tokens") or 0)
                    for day in days
                ],
                "calls": [
                    int(by_day_model[day.isoformat()].get(model_name, {}).get("calls") or 0)
                    for day in days
                ],
                "total_tokens": int(model_totals[model_name]["tokens"]),
                "total_calls": int(model_totals[model_name]["calls"]),
                "provider_ids": sorted(model_totals[model_name]["provider_ids"]),
            }
            for model_name in model_names
        ]

        grand_total = sum(int(item["tokens"]) for item in model_totals.values())
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": timezone_name,
            "reset_time": reset_time,
            "count_cached_input_tokens": self.count_cached_input_tokens,
            "days": [item.isoformat() for item in days],
            "daily_totals": [daily_totals[item.isoformat()] for item in days],
            "model_totals": [
                {
                    **_serialize_usage_entry(item),
                    "percent": (
                        round(int(item["tokens"]) / grand_total * 100, 4)
                        if grand_total
                        else 0
                    ),
                }
                for item in sorted_models
            ],
            "series": series,
            "daily_rows": daily_rows,
            "summary": {
                "total_tokens": grand_total,
                "total_calls": sum(int(item["calls"]) for item in model_totals.values()),
                "model_count": len(model_names),
                "day_count": len(days),
            },
        }


def _date_range(start_date: date, end_date: date) -> list[date]:
    days = (end_date - start_date).days
    return [start_date + timedelta(days=idx) for idx in range(days + 1)]


def _as_utc_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _window_local_date(created_at: datetime, *, tz: ZoneInfo, reset_time) -> date:
    local_dt = created_at.astimezone(tz)
    bucket = local_dt.date()
    if local_dt.time().replace(tzinfo=None) < reset_time:
        bucket -= timedelta(days=1)
    return bucket


def _serialize_usage_entry(entry: dict) -> dict:
    return {
        "date": entry.get("date"),
        "provider_model": entry.get("provider_model"),
        "tokens": int(entry.get("tokens") or 0),
        "calls": int(entry.get("calls") or 0),
        "provider_ids": sorted(entry.get("provider_ids") or []),
    }
