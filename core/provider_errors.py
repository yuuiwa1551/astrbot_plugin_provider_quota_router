from __future__ import annotations

from typing import Any


def response_error_text(response: Any) -> str:
    return str(getattr(response, "completion_text", "") or "")


def is_http_403_response(response: Any) -> bool:
    if str(getattr(response, "role", "") or "").casefold() != "err":
        return False
    normalized = response_error_text(response).casefold()
    return any(
        marker in normalized
        for marker in (
            "error code: 403",
            "status code: 403",
            "status_code=403",
            "accountoverdueerror",
        )
    )
