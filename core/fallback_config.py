from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ChainConfig


@dataclass(frozen=True)
class ConfigFileSignature:
    mtime_ns: int
    size: int


class ConfigChangedDuringRead(RuntimeError):
    """Raised when cmd_config.json changes while it is being parsed."""


def resolve_cmd_config_path(plugin_file: str | Path) -> Path:
    """Resolve data/cmd_config.json from data/plugins/<plugin>/main.py."""
    return Path(plugin_file).resolve().parents[2] / "cmd_config.json"


def file_signature(path: str | Path) -> ConfigFileSignature:
    stat = Path(path).stat()
    return ConfigFileSignature(mtime_ns=stat.st_mtime_ns, size=stat.st_size)


def build_astrbot_fallback_chain(provider_settings: Any) -> ChainConfig | None:
    if not isinstance(provider_settings, dict):
        raise ValueError("provider_settings must be an object")

    default_id = str(provider_settings.get("default_provider_id") or "").strip()
    fallback_ids = provider_settings.get("fallback_chat_models") or []
    if not isinstance(fallback_ids, list):
        raise ValueError("provider_settings.fallback_chat_models must be a list")

    providers: list[str] = []
    for provider_id in [default_id, *fallback_ids]:
        normalized = str(provider_id or "").strip()
        if normalized and normalized not in providers:
            providers.append(normalized)
    if not providers:
        return None
    return ChainConfig(name="astrbot-default", providers=providers)


def load_astrbot_fallback_chain(
    path: str | Path,
) -> tuple[ChainConfig, ConfigFileSignature]:
    config_path = Path(path)
    signature_before = file_signature(config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    signature_after = file_signature(config_path)
    if signature_before != signature_after:
        raise ConfigChangedDuringRead(
            f"{config_path} changed while being read; retry on the next watch cycle"
        )
    if not isinstance(raw, dict):
        raise ValueError("cmd_config.json root must be an object")
    chain = build_astrbot_fallback_chain(raw.get("provider_settings"))
    if chain is None:
        raise ValueError("cmd_config.json does not contain a usable fallback chain")
    return chain, signature_after
