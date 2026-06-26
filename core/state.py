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

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state()
            self._prune_state(state)
            self._save_state(state)
            return state

    async def reset_cache(self) -> None:
        async with self._lock:
            self._save_state({"version": 1, "pending": {}, "overlays": []})

    async def record_decision(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.decisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "pending": {}, "overlays": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "pending": {}, "overlays": []}
        if not isinstance(data, dict):
            return {"version": 1, "pending": {}, "overlays": []}
        data.setdefault("version", 1)
        data.setdefault("pending", {})
        data.setdefault("overlays", [])
        if not isinstance(data["pending"], dict):
            data["pending"] = {}
        if not isinstance(data["overlays"], list):
            data["overlays"] = []
        return data

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
