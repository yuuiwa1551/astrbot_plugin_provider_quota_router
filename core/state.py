from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .affinity import AffinityCommitResult, AffinityLookup, RouteAffinity

STATE_VERSION = 8
DECISION_LOG_MAX_BYTES = 5 * 1024 * 1024
AFFINITY_CLEAR_WATERMARK_TTL_SECONDS = 7 * 86_400


class QuotaStateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.state_path = data_dir / "quota_state.json"
        self.decisions_path = data_dir / "route_decisions.jsonl"
        self._lock = asyncio.Lock()
        self.route_lock = asyncio.Lock()
        self.last_load_error: str | None = None
        self.last_corrupt_backup: str | None = None
        self._last_corrupt_signature: tuple[int, int] | None = None
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def reserve(
        self,
        *,
        request_id: str,
        window_id: str,
        quota_key: str,
        provider_id: str,
        provider_model: str,
        tokens: int,
        ttl_seconds: int,
    ) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            state["pending"][request_id] = {
                "window_id": window_id,
                "quota_key": quota_key,
                "provider_id": provider_id,
                "provider_model": provider_model,
                "tokens": max(0, int(tokens)),
                "created_at": now,
                "expires_at": now + max(1, int(ttl_seconds)),
            }
            self._save_state(state)

    async def retarget_reservation(
        self,
        *,
        request_id: str,
        window_id: str,
        quota_key: str,
        provider_id: str,
        provider_model: str,
        tokens: int,
        ttl_seconds: int,
    ) -> None:
        """Atomically move an in-flight reservation to the actual attempt."""
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            existing = state["pending"].get(request_id)
            created_at = (
                float(existing.get("created_at") or now)
                if isinstance(existing, dict)
                else now
            )
            state["pending"][request_id] = {
                "window_id": window_id,
                "quota_key": quota_key,
                "provider_id": provider_id,
                "provider_model": provider_model,
                "tokens": max(0, int(tokens)),
                "created_at": created_at,
                "expires_at": now + max(1, int(ttl_seconds)),
            }
            self._save_state(state)

    async def release(
        self,
        *,
        request_id: str,
        actual_tokens: int | None,
        overlay_ttl_seconds: int,
        actual_provider_id: str | None = None,
        actual_provider_model: str | None = None,
        actual_quota_key: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            pending = state["pending"].pop(request_id, None)
            if pending and actual_tokens and actual_tokens > 0:
                now = time.time()
                state["overlays"].append(
                    {
                        "request_id": request_id,
                        "window_id": pending["window_id"],
                        "quota_key": actual_quota_key or pending["quota_key"],
                        "provider_id": actual_provider_id
                        or pending["provider_id"],
                        "provider_model": actual_provider_model
                        if actual_provider_model is not None
                        else pending.get("provider_model", ""),
                        "tokens": int(actual_tokens),
                        "created_at": now,
                        "expires_at": now + max(1, int(overlay_ttl_seconds)),
                    }
                )
            self._save_state(state)
            return pending

    async def usage_overlay(self, *, quota_key: str, window_id: str) -> tuple[int, int]:
        async with self._lock:
            state = self._load_state()
            changed = self._prune_state(state)
            pending_tokens = sum(
                int(item.get("tokens") or 0)
                for item in state["pending"].values()
                if item.get("quota_key") == quota_key and item.get("window_id") == window_id
            )
            completed_tokens = sum(
                int(item.get("tokens") or 0)
                for item in state["overlays"]
                if item.get("quota_key") == quota_key and item.get("window_id") == window_id
            )
            if changed:
                self._save_state(state)
            return pending_tokens, completed_tokens

    async def get_cooldown(self, *, quota_key: str) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            changed = self._prune_state(state)
            item = state["upstream_quota_cooldowns"].get(
                quota_key
            ) or state["local_quota_cooldowns"].get(quota_key)
            if changed:
                self._save_state(state)
            return dict(item) if isinstance(item, dict) else None

    async def start_cooldown(
        self,
        *,
        quota_key: str,
        window_id: str,
        provider_id: str,
        provider_model: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            existing = state["local_quota_cooldowns"].get(quota_key)
            if isinstance(existing, dict) and existing.get("window_id") == window_id:
                self._save_state(state)
                return dict(existing)
            now = time.time()
            item = {
                "window_id": window_id,
                "quota_key": quota_key,
                "provider_id": provider_id,
                "provider_model": provider_model,
                "started_at": now,
                "expires_at": now + max(0, int(ttl_seconds)),
                "reason": "token_threshold",
                "kind": "local_quota",
            }
            state["local_quota_cooldowns"][quota_key] = item
            self._save_state(state)
            return dict(item)

    async def set_cooldown_until(
        self,
        *,
        quota_key: str,
        window_id: str,
        provider_id: str,
        provider_model: str,
        expires_at: float,
        reason: str,
    ) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            item = {
                "window_id": window_id,
                "quota_key": quota_key,
                "provider_id": provider_id,
                "provider_model": provider_model,
                "started_at": now,
                "expires_at": max(now, float(expires_at)),
                "reason": str(reason or "upstream_quota_exhausted"),
            }
            item["kind"] = "upstream_quota"
            item["next_probe_at"] = max(now, float(expires_at))
            item["probe_lease_until"] = None
            state["upstream_quota_cooldowns"][quota_key] = item
            self._save_state(state)
            return dict(item)

    async def start_upstream_quota_cooldown(
        self,
        *,
        quota_key: str,
        window_id: str,
        provider_id: str,
        provider_model: str,
        initial_delay_seconds: int,
        probe_interval_seconds: int,
        error: str,
    ) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            existing = state["upstream_quota_cooldowns"].get(quota_key)
            if isinstance(existing, dict):
                item = dict(existing)
                item.update(
                    {
                        "window_id": window_id,
                        "provider_id": provider_id,
                        "provider_model": provider_model,
                        "last_error": str(error or "")[:2000],
                        "probe_interval_seconds": max(
                            60, int(probe_interval_seconds)
                        ),
                    }
                )
                if not float(item.get("next_probe_at") or 0):
                    item["next_probe_at"] = now + max(
                        60, int(initial_delay_seconds)
                    )
            else:
                item = {
                    "window_id": window_id,
                    "quota_key": quota_key,
                    "provider_id": provider_id,
                    "provider_model": provider_model,
                    "started_at": now,
                    "expires_at": None,
                    "reason": "upstream_quota_exhausted_unknown_reset",
                    "kind": "upstream_quota",
                    "next_probe_at": now
                    + max(60, int(initial_delay_seconds)),
                    "probe_interval_seconds": max(
                        60, int(probe_interval_seconds)
                    ),
                    "probe_started_at": None,
                    "probe_lease_until": None,
                    "last_probe_at": None,
                    "last_error": str(error or "")[:2000],
                }
            state["upstream_quota_cooldowns"][quota_key] = item
            self._save_state(state)
            return dict(item)

    async def due_upstream_quota_probes(self) -> list[dict[str, Any]]:
        async with self._lock:
            state = self._load_state()
            changed = self._prune_state(state)
            now = time.time()
            result = [
                dict(item)
                for item in state["upstream_quota_cooldowns"].values()
                if isinstance(item, dict)
                and float(item.get("next_probe_at") or 0) <= now
                and float(item.get("probe_lease_until") or 0) <= now
            ]
            if changed:
                self._save_state(state)
            return sorted(
                result,
                key=lambda item: float(item.get("next_probe_at") or 0),
            )

    async def acquire_upstream_quota_probe(
        self, *, quota_key: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["upstream_quota_cooldowns"].get(quota_key)
            if not isinstance(item, dict):
                return None
            now = time.time()
            if float(item.get("next_probe_at") or 0) > now:
                return None
            if float(item.get("probe_lease_until") or 0) > now:
                return None
            item.update(
                {
                    "probe_started_at": now,
                    "probe_lease_until": now + max(1, int(lease_seconds)),
                    "last_probe_at": now,
                }
            )
            self._save_state(state)
            return dict(item)

    async def finish_upstream_quota_probe(
        self,
        *,
        quota_key: str,
        success: bool,
        error: str = "",
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["upstream_quota_cooldowns"].get(quota_key)
            if not isinstance(item, dict):
                return None
            if success:
                state["upstream_quota_cooldowns"].pop(quota_key, None)
                self._save_state(state)
                return None
            now = time.time()
            item.update(
                {
                    "next_probe_at": now
                    + max(60, int(item.get("probe_interval_seconds") or 3600)),
                    "probe_started_at": None,
                    "probe_lease_until": None,
                    "last_probe_at": now,
                    "last_error": str(error or "")[:2000],
                }
            )
            self._save_state(state)
            return dict(item)

    async def clear_legacy_cooldowns_for_provider_prefixes(
        self,
        *,
        provider_prefixes: tuple[str, ...],
        preserve_reasons: tuple[str, ...] = ("upstream_quota_exhausted",),
    ) -> int:
        normalized_prefixes = tuple(
            prefix.casefold() for prefix in provider_prefixes if prefix
        )
        if not normalized_prefixes:
            return 0
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            changed = 0
            for bucket_name in (
                "local_quota_cooldowns",
                "upstream_quota_cooldowns",
            ):
                bucket = state[bucket_name]
                for quota_key, item in list(bucket.items()):
                    if not isinstance(item, dict):
                        continue
                    provider_id = str(item.get("provider_id") or "")
                    if not provider_id.casefold().startswith(normalized_prefixes):
                        continue
                    reason = str(item.get("reason") or "")
                    if reason in preserve_reasons or reason.startswith(
                        "upstream_quota_exhausted"
                    ):
                        continue
                    bucket.pop(quota_key, None)
                    changed += 1
            self._save_state(state)
            return changed

    async def clear_cooldown(self, *, quota_key: str) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            state["local_quota_cooldowns"].pop(quota_key, None)
            state["upstream_quota_cooldowns"].pop(quota_key, None)
            self._save_state(state)

    async def get_provider_model_circuit(
        self, *, provider_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            changed = self._prune_state(state)
            item = state["provider_model_circuits"].get(provider_id)
            if changed:
                self._save_state(state)
            return dict(item) if isinstance(item, dict) else None

    async def open_provider_model_circuit(
        self,
        *,
        provider_id: str,
        provider_model: str,
        ttl_seconds: int,
        error: str,
    ) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            existing = state["provider_model_circuits"].get(provider_id)
            if (
                isinstance(existing, dict)
                and float(existing.get("retry_at") or 0) > now
            ):
                self._save_state(state)
                return dict(existing)
            item = {
                "provider_id": provider_id,
                "provider_model": provider_model,
                "status": "open",
                "reason": "provider_error",
                "started_at": now,
                "retry_at": now + max(0, int(ttl_seconds)),
                "last_error": str(error or "")[:2000],
            }
            state["provider_model_circuits"][provider_id] = item
            self._save_state(state)
            return dict(item)

    async def clear_provider_model_circuit(self, *, provider_id: str) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            state["provider_model_circuits"].pop(provider_id, None)
            self._save_state(state)

    async def get_provider_group_circuit(
        self, *, group_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            changed = self._prune_state(state)
            item = state["provider_group_circuits"].get(group_id)
            if changed:
                self._save_state(state)
            return dict(item) if isinstance(item, dict) else None

    async def open_provider_group_circuit(
        self,
        *,
        group_id: str,
        trigger_provider_id: str,
        ttl_seconds: int,
        error: str,
    ) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            item = {
                "group_id": group_id,
                "status": "open",
                "started_at": now,
                "retry_at": now + max(0, int(ttl_seconds)),
                "trigger_provider_id": trigger_provider_id,
                "last_error": str(error or "")[:2000],
                "probe_provider_id": "",
                "probe_started_at": None,
                "probe_lease_until": None,
                "last_probe_at": None,
            }
            state["provider_group_circuits"][group_id] = item
            self._save_state(state)
            return dict(item)

    async def acquire_provider_group_probe(
        self,
        *,
        group_id: str,
        provider_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["provider_group_circuits"].get(group_id)
            if not isinstance(item, dict):
                self._save_state(state)
                return None
            now = time.time()
            if float(item.get("retry_at") or 0) > now:
                self._save_state(state)
                return None
            if (
                item.get("status") == "probing"
                and float(item.get("probe_lease_until") or 0) > now
            ):
                self._save_state(state)
                return None
            item.update(
                {
                    "status": "probing",
                    "probe_provider_id": provider_id,
                    "probe_started_at": now,
                    "probe_lease_until": now + max(1, int(lease_seconds)),
                    "last_probe_at": now,
                }
            )
            state["provider_group_circuits"][group_id] = item
            self._save_state(state)
            return dict(item)

    async def finish_provider_group_probe(
        self,
        *,
        group_id: str,
        success: bool,
        cooldown_seconds: int,
        error: str = "",
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["provider_group_circuits"].get(group_id)
            if not isinstance(item, dict):
                self._save_state(state)
                return None
            if success:
                state["provider_group_circuits"].pop(group_id, None)
                self._save_state(state)
                return None
            now = time.time()
            item.update(
                {
                    "status": "open",
                    "started_at": now,
                    "retry_at": now + max(0, int(cooldown_seconds)),
                    "last_error": str(error or "")[:2000],
                    "probe_provider_id": "",
                    "probe_started_at": None,
                    "probe_lease_until": None,
                    "last_probe_at": now,
                }
            )
            state["provider_group_circuits"][group_id] = item
            self._save_state(state)
            return dict(item)

    async def defer_provider_group_probe(
        self,
        *,
        group_id: str,
        delay_seconds: int,
        error: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["provider_group_circuits"].get(group_id)
            if not isinstance(item, dict):
                self._save_state(state)
                return None
            now = time.time()
            item.update(
                {
                    "status": "open",
                    "retry_at": now + max(1, int(delay_seconds)),
                    "last_error": str(error or "")[:2000],
                    "probe_provider_id": "",
                    "probe_started_at": None,
                    "probe_lease_until": None,
                }
            )
            state["provider_group_circuits"][group_id] = item
            self._save_state(state)
            return dict(item)

    async def claim_notification(
        self,
        *,
        key: str,
        interval_seconds: int,
        detail: str = "",
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            now = time.time()
            existing = state["notification_throttles"].get(key)
            if (
                isinstance(existing, dict)
                and now - float(existing.get("claimed_at") or 0)
                < max(1, int(interval_seconds))
            ):
                self._save_state(state)
                return None
            item = {
                "key": key,
                "claimed_at": now,
                "detail": str(detail or "")[:1000],
            }
            state["notification_throttles"][key] = item
            self._save_state(state)
            return dict(item)

    async def release_notification_claim(
        self,
        *,
        key: str,
        claimed_at: float,
    ) -> bool:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            current = state["notification_throttles"].get(str(key))
            if not isinstance(current, dict) or float(
                current.get("claimed_at") or 0
            ) != float(claimed_at):
                return False
            state["notification_throttles"].pop(str(key), None)
            self._save_state(state)
            return True

    async def lookup_route_affinity(
        self,
        *,
        conversation_key: str,
        modality: str,
        now: float | None = None,
    ) -> AffinityLookup:
        async with self._lock:
            state = self._load_state()
            clock = time.time() if now is None else float(now)
            bucket = (state.get("route_affinities", {}) or {}).get(
                conversation_key,
                {},
            )
            raw = bucket.get(modality) if isinstance(bucket, dict) else None
            affinity = RouteAffinity.from_mapping(
                conversation_key=conversation_key,
                modality=modality,
                value=raw,
            )
            if raw is None:
                status = "miss"
            elif affinity is None:
                status = "invalid"
            elif affinity.expires_at <= clock:
                status = "expired"
            else:
                status = "hit"
            changed = self._prune_state(state, now=clock)
            if changed:
                self._save_state(state)
            return AffinityLookup(status=status, affinity=affinity)

    async def commit_route_affinity(
        self,
        *,
        conversation_key: str,
        modality: str,
        provider_id: str,
        provider_model: str,
        request_started_at: float,
        ttl_seconds: int,
        expected_generation: str = "",
        planned_affinity_provider_id: str = "",
        now: float | None = None,
    ) -> AffinityCommitResult:
        async with self._lock:
            state = self._load_state()
            clock = time.time() if now is None else float(now)
            self._prune_state(state, now=clock)
            clear_watermark = float(
                (state.get("route_affinity_clear_watermarks", {}) or {}).get(
                    conversation_key,
                    0,
                )
                or 0
            )
            if request_started_at <= clear_watermark:
                self._save_state(state)
                return AffinityCommitResult(status="cleared")

            affinities = state.setdefault("route_affinities", {})
            bucket = affinities.setdefault(conversation_key, {})
            current = RouteAffinity.from_mapping(
                conversation_key=conversation_key,
                modality=modality,
                value=bucket.get(modality),
            )
            if (
                current is not None
                and request_started_at < current.last_request_started_at
            ):
                self._save_state(state)
                return AffinityCommitResult(status="stale", affinity=current)

            planned_provider_id = str(planned_affinity_provider_id or "")
            is_same_planned_provider = bool(
                planned_provider_id and provider_id == planned_provider_id
            )
            if is_same_planned_provider:
                if current is None:
                    self._save_state(state)
                    return AffinityCommitResult(status="expired")
                if (
                    not expected_generation
                    or current.generation != expected_generation
                ):
                    self._save_state(state)
                    return AffinityCommitResult(status="stale", affinity=current)
                if current.provider_model == provider_model:
                    updated = RouteAffinity(
                        conversation_key=current.conversation_key,
                        modality=current.modality,
                        provider_id=current.provider_id,
                        provider_model=current.provider_model,
                        assigned_at=current.assigned_at,
                        expires_at=current.expires_at,
                        last_request_started_at=max(
                            current.last_request_started_at,
                            request_started_at,
                        ),
                        generation=current.generation,
                    )
                    bucket[modality] = updated.to_mapping()
                    self._save_state(state)
                    return AffinityCommitResult(
                        status="unchanged",
                        affinity=updated,
                    )

            if (
                current is not None
                and current.provider_id == provider_id
                and current.provider_model == provider_model
            ):
                updated = RouteAffinity(
                    conversation_key=current.conversation_key,
                    modality=current.modality,
                    provider_id=current.provider_id,
                    provider_model=current.provider_model,
                    assigned_at=current.assigned_at,
                    expires_at=current.expires_at,
                    last_request_started_at=max(
                        current.last_request_started_at,
                        request_started_at,
                    ),
                    generation=current.generation,
                )
                bucket[modality] = updated.to_mapping()
                self._save_state(state)
                return AffinityCommitResult(status="unchanged", affinity=updated)

            previous_provider_id = current.provider_id if current else None
            affinity = RouteAffinity(
                conversation_key=conversation_key,
                modality=modality,
                provider_id=provider_id,
                provider_model=provider_model,
                assigned_at=clock,
                expires_at=clock + max(60, int(ttl_seconds)),
                last_request_started_at=request_started_at,
                generation=uuid4().hex,
            )
            bucket[modality] = affinity.to_mapping()
            self._save_state(state)
            return AffinityCommitResult(
                status="replaced" if current else "created",
                affinity=affinity,
                previous_provider_id=previous_provider_id,
            )

    async def remove_route_affinity(
        self,
        *,
        conversation_key: str,
        modality: str,
        generation: str = "",
    ) -> bool:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            affinities = state.get("route_affinities", {}) or {}
            bucket = affinities.get(conversation_key)
            if not isinstance(bucket, dict) or modality not in bucket:
                return False
            current = RouteAffinity.from_mapping(
                conversation_key=conversation_key,
                modality=modality,
                value=bucket.get(modality),
            )
            if generation and (
                current is None or current.generation != generation
            ):
                return False
            bucket.pop(modality, None)
            if not bucket:
                affinities.pop(conversation_key, None)
            self._save_state(state)
            return True

    async def clear_route_affinities(
        self,
        *,
        conversation_key: str,
        now: float | None = None,
    ) -> int:
        async with self._lock:
            state = self._load_state()
            clock = time.time() if now is None else float(now)
            self._prune_state(state, now=clock)
            affinities = state.get("route_affinities", {}) or {}
            bucket = affinities.pop(conversation_key, {})
            removed = len(bucket) if isinstance(bucket, dict) else 0
            state.setdefault("route_affinity_clear_watermarks", {})[
                conversation_key
            ] = clock
            self._save_state(state)
            return removed

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            changed = self._prune_state(state)
            if changed:
                self._save_state(state)
            return self._snapshot_view(state)

    async def reset_cache(self) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            self._save_state(
                {
                    "version": STATE_VERSION,
                    "pending": {},
                    "overlays": [],
                    "local_quota_cooldowns": state.get(
                        "local_quota_cooldowns", {}
                    ),
                    "upstream_quota_cooldowns": state.get(
                        "upstream_quota_cooldowns", {}
                    ),
                    "provider_model_circuits": state.get(
                        "provider_model_circuits", {}
                    ),
                    "provider_group_circuits": state.get(
                        "provider_group_circuits", {}
                    ),
                    "notification_throttles": state.get(
                        "notification_throttles", {}
                    ),
                    "route_affinities": state.get("route_affinities", {}),
                    "route_affinity_clear_watermarks": state.get(
                        "route_affinity_clear_watermarks", {}
                    ),
                }
            )

    async def record_decision(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if (
                self.decisions_path.exists()
                and self.decisions_path.stat().st_size >= DECISION_LOG_MAX_BYTES
            ):
                rotated = self.decisions_path.with_suffix(".jsonl.1")
                if rotated.exists():
                    rotated.unlink()
                self.decisions_path.replace(rotated)
            with self.decisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_load_error = f"{type(exc).__name__}: {exc}"
            self._backup_corrupt_state()
            return self._empty_state()
        if not isinstance(data, dict):
            self.last_load_error = "ValueError: quota state root must be an object"
            self._backup_corrupt_state()
            return self._empty_state()
        self.last_load_error = None
        data["version"] = STATE_VERSION
        legacy_cooldowns = data.pop("cooldowns", {})
        data.setdefault("pending", {})
        data.setdefault("overlays", [])
        data.setdefault("local_quota_cooldowns", {})
        data.setdefault("upstream_quota_cooldowns", {})
        data.setdefault("provider_model_circuits", {})
        data.setdefault("provider_group_circuits", {})
        data.setdefault("notification_throttles", {})
        data.setdefault("route_affinities", {})
        data.setdefault("route_affinity_clear_watermarks", {})
        if not isinstance(data["pending"], dict):
            data["pending"] = {}
        if not isinstance(data["overlays"], list):
            data["overlays"] = []
        if not isinstance(data["local_quota_cooldowns"], dict):
            data["local_quota_cooldowns"] = {}
        if not isinstance(data["upstream_quota_cooldowns"], dict):
            data["upstream_quota_cooldowns"] = {}
        if isinstance(legacy_cooldowns, dict):
            for quota_key, item in legacy_cooldowns.items():
                if not isinstance(item, dict):
                    continue
                reason = str(item.get("reason") or "")
                bucket = (
                    data["upstream_quota_cooldowns"]
                    if reason.startswith("upstream_quota")
                    else data["local_quota_cooldowns"]
                )
                bucket.setdefault(quota_key, item)
        now = time.time()
        for quota_key, item in list(
            data["upstream_quota_cooldowns"].items()
        ):
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized.update(
                {
                    "quota_key": str(
                        normalized.get("quota_key") or quota_key
                    ),
                    "kind": "upstream_quota",
                    "reason": "upstream_quota_exhausted_unknown_reset",
                    "expires_at": None,
                    "next_probe_at": float(
                        normalized.get("next_probe_at") or now + 3600
                    ),
                    "probe_interval_seconds": max(
                        60,
                        int(
                            normalized.get("probe_interval_seconds") or 3600
                        ),
                    ),
                    "probe_lease_until": normalized.get(
                        "probe_lease_until"
                    ),
                }
            )
            data["upstream_quota_cooldowns"][quota_key] = normalized
        if not isinstance(data["provider_model_circuits"], dict):
            data["provider_model_circuits"] = {}
        if not isinstance(data["provider_group_circuits"], dict):
            data["provider_group_circuits"] = {}
        if not isinstance(data["notification_throttles"], dict):
            data["notification_throttles"] = {}
        if not isinstance(data["route_affinities"], dict):
            data["route_affinities"] = {}
        if not isinstance(data["route_affinity_clear_watermarks"], dict):
            data["route_affinity_clear_watermarks"] = {}
        return data

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "pending": {},
            "overlays": [],
            "local_quota_cooldowns": {},
            "upstream_quota_cooldowns": {},
            "provider_model_circuits": {},
            "provider_group_circuits": {},
            "notification_throttles": {},
            "route_affinities": {},
            "route_affinity_clear_watermarks": {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload.pop("cooldowns", None)
        tmp_path = self.state_path.with_name(
            f"{self.state_path.name}.{uuid4().hex}.tmp"
        )
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.state_path)
        self.last_load_error = None

    @staticmethod
    def _prune_state(
        state: dict[str, Any],
        *,
        now: float | None = None,
    ) -> bool:
        changed = False
        now = time.time() if now is None else float(now)
        pending = state.get("pending", {})
        if isinstance(pending, dict):
            cleaned_pending = {
                key: item
                for key, item in pending.items()
                if isinstance(item, dict)
                and float(item.get("expires_at") or 0) > now
            }
            changed = changed or len(cleaned_pending) != len(pending)
            state["pending"] = cleaned_pending
        overlays = state.get("overlays", [])
        if isinstance(overlays, list):
            cleaned_overlays = [
                item
                for item in overlays
                if isinstance(item, dict)
                and float(item.get("expires_at") or 0) > now
            ]
            changed = changed or len(cleaned_overlays) != len(overlays)
            state["overlays"] = cleaned_overlays
        for bucket_name in (
            "local_quota_cooldowns",
            "upstream_quota_cooldowns",
        ):
            cooldowns = state.get(bucket_name, {})
            if isinstance(cooldowns, dict):
                cleaned_cooldowns = {
                    key: item
                    for key, item in cooldowns.items()
                    if isinstance(item, dict)
                    and item.get("quota_key")
                    and item.get("window_id")
                }
                changed = changed or len(cleaned_cooldowns) != len(cooldowns)
                state[bucket_name] = cleaned_cooldowns
        model_circuits = state.get("provider_model_circuits", {})
        if isinstance(model_circuits, dict):
            cleaned_model_circuits = {
                str(key): item
                for key, item in model_circuits.items()
                if isinstance(item, dict)
                and item.get("provider_id")
                and float(item.get("retry_at") or 0) > now
            }
            changed = changed or len(cleaned_model_circuits) != len(model_circuits)
            state["provider_model_circuits"] = cleaned_model_circuits
        circuits = state.get("provider_group_circuits", {})
        if isinstance(circuits, dict):
            cleaned: dict[str, dict[str, Any]] = {}
            for key, item in circuits.items():
                if not isinstance(item, dict) or not item.get("group_id"):
                    continue
                if (
                    item.get("status") == "probing"
                    and float(item.get("probe_lease_until") or 0) <= now
                ):
                    item = dict(item)
                    item.update(
                        {
                            "status": "open",
                            "probe_provider_id": "",
                            "probe_started_at": None,
                            "probe_lease_until": None,
                        }
                    )
                cleaned[str(key)] = item
            changed = changed or cleaned != circuits
            state["provider_group_circuits"] = cleaned
        throttles = state.get("notification_throttles", {})
        if isinstance(throttles, dict):
            cleaned_throttles = {
                str(key): item
                for key, item in throttles.items()
                if isinstance(item, dict)
                and now - float(item.get("claimed_at") or 0) < 7 * 86_400
            }
            changed = changed or len(cleaned_throttles) != len(throttles)
            state["notification_throttles"] = cleaned_throttles
        affinities = state.get("route_affinities", {})
        if isinstance(affinities, dict):
            cleaned_affinities: dict[str, dict[str, Any]] = {}
            for conversation_key, bucket in affinities.items():
                if not isinstance(bucket, dict):
                    continue
                cleaned_bucket: dict[str, Any] = {}
                for modality, value in bucket.items():
                    affinity = RouteAffinity.from_mapping(
                        conversation_key=str(conversation_key),
                        modality=str(modality),
                        value=value,
                    )
                    if affinity is None or affinity.expires_at <= now:
                        continue
                    cleaned_bucket[str(modality)] = affinity.to_mapping()
                if cleaned_bucket:
                    cleaned_affinities[str(conversation_key)] = cleaned_bucket
            changed = changed or cleaned_affinities != affinities
            state["route_affinities"] = cleaned_affinities
        clear_watermarks = state.get("route_affinity_clear_watermarks", {})
        if isinstance(clear_watermarks, dict):
            cleaned_watermarks: dict[str, float] = {}
            for conversation_key, value in clear_watermarks.items():
                try:
                    cleared_at = float(value)
                except (TypeError, ValueError):
                    continue
                if now - cleared_at < AFFINITY_CLEAR_WATERMARK_TTL_SECONDS:
                    cleaned_watermarks[str(conversation_key)] = cleared_at
            changed = changed or cleaned_watermarks != clear_watermarks
            state["route_affinity_clear_watermarks"] = cleaned_watermarks
        return changed

    def _backup_corrupt_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            stat = self.state_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return
        if signature == self._last_corrupt_signature:
            return
        backup = self.state_path.with_name(
            f"quota_state.corrupt.{int(time.time())}.{uuid4().hex[:8]}.json"
        )
        try:
            backup.write_bytes(self.state_path.read_bytes())
        except OSError:
            return
        self.last_corrupt_backup = str(backup)
        self._last_corrupt_signature = signature

    @staticmethod
    def _snapshot_view(state: dict[str, Any]) -> dict[str, Any]:
        payload = dict(state)
        payload["cooldowns"] = {
            **(state.get("local_quota_cooldowns", {}) or {}),
            **(state.get("upstream_quota_cooldowns", {}) or {}),
        }
        return payload
