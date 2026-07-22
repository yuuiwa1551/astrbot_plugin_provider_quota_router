from __future__ import annotations

import csv
import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from .time_window import UsageWindow


def build_alerts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    chain_status: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        chain_status.setdefault(str(row.get("chain") or ""), []).append(row)
        if not bool(row.get("quota_managed", True)):
            continue
        limit = int(row.get("limit") or 0)
        used = int(row.get("effective_tokens") or 0)
        if limit <= 0:
            alerts.append(
                {
                    "level": "critical",
                    "type": "invalid_limit",
                    "provider_id": row.get("provider_id"),
                    "quota_key": row.get("quota_key"),
                    "message": "额度上限为 0，当前 provider 不会被视为可用。",
                }
            )
            continue
        ratio = used / limit
        if ratio >= 1:
            alerts.append(
                {
                    "level": "critical",
                    "type": "quota_exhausted",
                    "provider_id": row.get("provider_id"),
                    "quota_key": row.get("quota_key"),
                    "message": f"{row.get('provider_id')} 已超过配置额度。",
                }
            )
        elif ratio >= 0.95:
            alerts.append(_usage_alert(row, ratio, "critical"))
        elif ratio >= 0.90:
            alerts.append(_usage_alert(row, ratio, "warning"))
        elif ratio >= 0.80:
            alerts.append(_usage_alert(row, ratio, "notice"))

    for chain, chain_rows in chain_status.items():
        if chain_rows and all(
            row.get("status")
            in {
                "exhausted",
                "cooldown",
                "provider_group_cooldown",
                "provider_group_probe",
                "provider_error_cooldown",
                "upstream_quota_cooldown",
            }
            for row in chain_rows
        ):
            alerts.append(
                {
                    "level": "critical",
                    "type": "chain_exhausted",
                    "chain": chain,
                    "message": f"路由链 {chain} 已全部耗尽。",
                }
            )
    return alerts


def _usage_alert(row: dict[str, Any], ratio: float, level: str) -> dict[str, Any]:
    percent = round(ratio * 100, 1)
    return {
        "level": level,
        "type": "quota_near_limit",
        "provider_id": row.get("provider_id"),
        "quota_key": row.get("quota_key"),
        "ratio": ratio,
        "message": f"{row.get('provider_id')} 已使用 {percent}% 额度。",
    }


def build_summary(rows: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> dict[str, Any]:
    total_limit = sum(int(row.get("limit") or 0) for row in rows)
    total_used = sum(int(row.get("effective_tokens") or 0) for row in rows)
    exhausted_count = sum(
        1
        for row in rows
        if row.get("status")
        in {
            "exhausted",
            "cooldown",
            "provider_group_cooldown",
            "provider_group_probe",
            "provider_error_cooldown",
            "upstream_quota_cooldown",
        }
    )
    return {
        "provider_count": len(rows),
        "exhausted_count": exhausted_count,
        "available_count": max(0, len(rows) - exhausted_count),
        "total_limit": total_limit,
        "total_used": total_used,
        "alert_count": len(alerts),
        "critical_alert_count": sum(1 for alert in alerts if alert.get("level") == "critical"),
    }


def read_recent_decisions(path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    if not path.exists():
        return []
    try:
        lines = _read_tail_lines(path, limit)
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return list(reversed(items))


def _read_tail_lines(path: Path, limit: int) -> list[str]:
    """Read only enough bytes from the end of a JSONL file for the UI."""
    chunk_size = 64 * 1024
    with path.open("rb") as fh:
        fh.seek(0, 2)
        position = fh.tell()
        chunks: list[bytes] = []
        line_count = 0
        while position > 0 and line_count <= limit:
            size = min(chunk_size, position)
            position -= size
            fh.seek(position)
            chunk = fh.read(size)
            chunks.append(chunk)
            line_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def write_snapshot(data_dir: Path, window: UsageWindow, payload: dict[str, Any]) -> Path:
    snapshot_dir = data_dir / "daily_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{window.window_id}.json"
    serializable = {
        "saved_at": datetime.now().astimezone().isoformat(),
        "window": payload.get("window"),
        "summary": payload.get("summary"),
        "rows": payload.get("rows"),
        "alerts": payload.get("alerts"),
    }
    path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def export_usage_csv(rows: list[dict[str, Any]], window: UsageWindow) -> str:
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "window_start",
            "window_end",
            "chain",
            "provider_id",
            "provider_model",
            "quota_key",
            "status",
            "db_tokens",
            "pending_tokens",
            "overlay_tokens",
            "effective_tokens",
            "limit",
            "safety_buffer",
            "quota_managed",
            "cooldown_started_at",
            "cooldown_until",
            "next_probe_at",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "window_start": window.start_local.isoformat(),
                "window_end": window.end_local.isoformat(),
                "chain": row.get("chain", ""),
                "provider_id": row.get("provider_id", ""),
                "provider_model": row.get("provider_model", ""),
                "quota_key": row.get("quota_key", ""),
                "status": row.get("status", ""),
                "db_tokens": row.get("db_tokens", 0),
                "pending_tokens": row.get("pending_tokens", 0),
                "overlay_tokens": row.get("overlay_tokens", 0),
                "effective_tokens": row.get("effective_tokens", 0),
                "limit": row.get("limit", 0),
                "safety_buffer": row.get("safety_buffer", 0),
                "quota_managed": row.get("quota_managed", True),
                "cooldown_started_at": row.get("cooldown_started_at", ""),
                "cooldown_until": row.get("cooldown_until", ""),
                "next_probe_at": row.get("next_probe_at", ""),
            }
        )
    return output.getvalue()
