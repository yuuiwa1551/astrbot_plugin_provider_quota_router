from __future__ import annotations

from typing import Any


def response_error_text(response: Any) -> str:
    return str(getattr(response, "completion_text", "") or "")


def is_provider_error_response(response: Any) -> bool:
    return str(getattr(response, "role", "") or "").casefold() == "err"


def is_provider_error_text(error_text: str) -> bool:
    normalized = str(error_text or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "llm 响应错误:",
            "all chat models failed:",
            "providerapierror",
            "accountoverdueerror",
            "all available chat models are unavailable",
            "error occurred during ai execution.",
        )
    )


def is_http_403_error_text(error_text: str) -> bool:
    normalized = str(error_text or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "error code: 403",
            "status code: 403",
            "status_code=403",
            "accountoverdueerror",
        )
    )


def is_http_403_response(response: Any) -> bool:
    if not is_provider_error_response(response):
        return False
    return is_http_403_error_text(response_error_text(response))
