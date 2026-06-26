from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import func, select

from astrbot.core.db.po import ProviderStat

from .time_window import UsageWindow


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
