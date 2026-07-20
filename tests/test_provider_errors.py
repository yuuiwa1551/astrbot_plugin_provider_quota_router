from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.provider_errors import (
    is_http_403_error_text,
    is_http_403_response,
    is_provider_error_text,
    is_upstream_free_quota_exhausted_text,
)


class ProviderErrorTests(unittest.TestCase):
    def test_detects_fire_account_overdue_403(self) -> None:
        response = SimpleNamespace(
            role="err",
            completion_text=(
                "All chat models failed: PermissionDeniedError: "
                "Error code: 403 - AccountOverdueError"
            ),
        )
        self.assertTrue(is_http_403_response(response))

    def test_does_not_trip_on_success_or_non_403_error(self) -> None:
        self.assertFalse(
            is_http_403_response(
                SimpleNamespace(role="assistant", completion_text="403")
            )
        )
        self.assertFalse(
            is_http_403_response(
                SimpleNamespace(role="err", completion_text="Error code: 429")
            )
        )

    def test_detects_decorated_agent_provider_error_text(self) -> None:
        text = (
            "LLM 响应错误: All chat models failed: ProviderAPIError: "
            "Error code: 403 AccountOverdueError"
        )

        self.assertTrue(is_provider_error_text(text))
        self.assertTrue(is_http_403_error_text(text))

    def test_does_not_treat_normal_model_text_as_provider_error(self) -> None:
        self.assertFalse(
            is_provider_error_text("Error code: 403 表示服务器拒绝了请求。")
        )

    def test_detects_opencode_free_usage_limit_error(self) -> None:
        text = (
            "Error code: 429 - {'type': 'error', 'error': "
            "{'type': 'FreeUsageLimitError', "
            "'message': 'Rate limit exceeded. Please try again later.'}}"
        )

        self.assertTrue(is_provider_error_text(text))
        self.assertTrue(is_upstream_free_quota_exhausted_text(text))

    def test_generic_429_is_not_a_daily_quota_signal(self) -> None:
        self.assertFalse(
            is_upstream_free_quota_exhausted_text(
                "Error code: 429 - too many requests per minute"
            )
        )


if __name__ == "__main__":
    unittest.main()
