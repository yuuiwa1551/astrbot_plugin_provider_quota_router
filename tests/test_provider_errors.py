from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.provider_errors import is_http_403_response


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


if __name__ == "__main__":
    unittest.main()
