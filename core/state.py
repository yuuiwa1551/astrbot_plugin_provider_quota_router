from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


STATE_VERSION = 6


class QuotaStateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.state_path = data_dir / "quota_state.json"
        self.decisions_path = data_dir / "route_decisions.jsonl"
        self._lock = asyncio.Lock()
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

    async def release(
        self,
        *,
        request_id: str,
        actual_tokens: int | None,
        overlay_ttl_seconds: int,
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
                        "quota_key": pending["quota_key"],
                        "provider_id": pending["provider_id"],
                        "provider_model": pending.get("provider_model", ""),
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
            self._prune_state(state)
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
            self._save_state(state)
            return pending_tokens, completed_tokens

    async def get_cooldown(self, *, quota_key: str) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["cooldowns"].get(quota_key)
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
            existing = state["cooldowns"].get(quota_key)
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
            }
            state["cooldowns"][quota_key] = item
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
            state["cooldowns"][quota_key] = item
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
            for quota_key, item in list(state["cooldowns"].items()):
                if not isinstance(item, dict):
                    continue
                provider_id = str(item.get("provider_id") or "")
                if not provider_id.casefold().startswith(normalized_prefixes):
                    continue
                if str(item.get("reason") or "") in preserve_reasons:
                    continue
                state["cooldowns"].pop(quota_key, None)
                changed += 1
            self._save_state(state)
            return changed

    async def clear_cooldown(self, *, quota_key: str) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            state["cooldowns"].pop(quota_key, None)
            self._save_state(state)

    async def get_provider_model_circuit(
        self, *, provider_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            item = state["provider_model_circuits"].get(provider_id)
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
            self._prune_state(state)
            item = state["provider_group_circuits"].get(group_id)
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

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            self._save_state(state)
            return state

    async def reset_cache(self) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            self._save_state(
                {
                    "version": STATE_VERSION,
                    "pending": {},
                    "overlays": [],
                    "cooldowns": state.get("cooldowns", {}),
                    "provider_model_circuits": state.get(
                        "provider_model_circuits", {}
                    ),
                    "provider_group_circuits": state.get(
                        "provider_group_circuits", {}
                    ),
                    "notification_throttles": state.get(
                        "notification_throttles", {}
                    ),
                }
            )

    async def record_decision(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.decisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(data, dict):
            return self._empty_state()
        data["version"] = STATE_VERSION
        data.setdefault("pending", {})
        data.setdefault("overlays", [])
        data.setdefault("cooldowns", {})
        data.setdefault("provider_model_circuits", {})
        data.setdefault("provider_group_circuits", {})
        data.setdefault("notification_throttles", {})
        if not isinstance(data["pending"], dict):
            data["pending"] = {}
        if not isinstance(data["overlays"], list):
            data["overlays"] = []
        if not isinstance(data["cooldowns"], dict):
            data["cooldowns"] = {}
        if not isinstance(data["provider_model_circuits"], dict):
            data["provider_model_circuits"] = {}
        if not isinstance(data["provider_group_circuits"], dict):
            data["provider_group_circuits"] = {}
        if not isinstance(data["notification_throttles"], dict):
            data["notification_throttles"] = {}
        return data

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "pending": {},
            "overlays": [],
            "cooldowns": {},
            "provider_model_circuits": {},
            "provider_group_circuits": {},
            "notification_throttles": {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.state_path)

    @staticmethod
    def _prune_state(state: dict[str, Any]) -> None:
        now = time.time()
        pending = state.get("pending", {})
        if isinstance(pending, dict):
            state["pending"] = {
                key: item
                for key, item in pending.items()
                if float(item.get("expires_at") or 0) > now
            }
        overlays = state.get("overlays", [])
        if isinstance(overlays, list):
            state["overlays"] = [
                item for item in overlays if float(item.get("expires_at") or 0) > now
            ]
        cooldowns = state.get("cooldowns", {})
        if isinstance(cooldowns, dict):
            state["cooldowns"] = {
                key: item
                for key, item in cooldowns.items()
                if isinstance(item, dict)
                and item.get("quota_key")
                and item.get("window_id")
            }
        model_circuits = state.get("provider_model_circuits", {})
        if isinstance(model_circuits, dict):
            state["provider_model_circuits"] = {
                str(key): item
                for key, item in model_circuits.items()
                if isinstance(item, dict)
                and item.get("provider_id")
                and float(item.get("retry_at") or 0) > now
            }
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
            state["provider_group_circuits"] = cleaned
        throttles = state.get("notification_throttles", {})
        if isinstance(throttles, dict):
            state["notification_throttles"] = {
                str(key): item
                for key, item in throttles.items()
                if isinstance(item, dict)
                and now - float(item.get("claimed_at") or 0) < 7 * 86_400
            }
