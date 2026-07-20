from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


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
            }
            state["cooldowns"][quota_key] = item
            self._save_state(state)
            return dict(item)

    async def clear_cooldown(self, *, quota_key: str) -> None:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            state["cooldowns"].pop(quota_key, None)
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
                    "version": 3,
                    "pending": {},
                    "overlays": [],
                    "cooldowns": state.get("cooldowns", {}),
                    "provider_group_circuits": state.get(
                        "provider_group_circuits", {}
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
        data["version"] = 3
        data.setdefault("pending", {})
        data.setdefault("overlays", [])
        data.setdefault("cooldowns", {})
        data.setdefault("provider_group_circuits", {})
        if not isinstance(data["pending"], dict):
            data["pending"] = {}
        if not isinstance(data["overlays"], list):
            data["overlays"] = []
        if not isinstance(data["cooldowns"], dict):
            data["cooldowns"] = {}
        if not isinstance(data["provider_group_circuits"], dict):
            data["provider_group_circuits"] = {}
        return data

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": 3,
            "pending": {},
            "overlays": [],
            "cooldowns": {},
            "provider_group_circuits": {},
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
