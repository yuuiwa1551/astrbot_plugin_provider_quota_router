from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.opencode_quota_guard import (
    DIRECT_ROUTE_NOT_HANDLED,
    OpenCodeQuotaCooldownError,
    ProviderAttemptTimeoutError,
    ProviderFullCallTimeoutError,
    begin_provider_guard_bypass,
    bind_provider_guard_route_plan,
    current_provider_guard_provider_id,
    end_provider_guard_bypass,
    install_opencode_quota_guard,
    is_opencode_quota_guard_installed,
    reset_provider_guard_route_plan,
    uninstall_opencode_quota_guard,
)


class FakeProvider:
    def __init__(self) -> None:
        self.provider_config = {
            "id": "opencode-zen/mimo-v2.5-free",
            "model": "mimo-v2.5-free",
        }
        self.calls = 0
        self.last_kwargs = None
        self.delay = 0.0
        self.response = None
        self.exception = None

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception is not None:
            raise self.exception
        if self.response is not None:
            return self.response
        raise RuntimeError("FreeUsageLimitError: Rate limit exceeded")

    async def text_chat_stream(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        yield "chunk"


class FakeOwner:
    def __init__(self) -> None:
        self.cooldown = None
        self.errors = []
        self.timeout = 0.0
        self.full_timeout = 0.0
        self.request_max_retries = 1
        self.max_output_tokens = None
        self.attempts = []
        self.successes = []
        self.direct_result = DIRECT_ROUTE_NOT_HANDLED
        self.direct_calls = []

    async def opencode_quota_guard_cooldown(self, provider):
        return self.cooldown

    async def opencode_quota_guard_error(self, provider, exc):
        self.errors.append((provider, exc))

    def opencode_quota_guard_request_max_retries(self, provider):
        return self.request_max_retries

    def opencode_quota_guard_timeout_seconds(self, provider):
        return self.timeout

    def opencode_quota_guard_full_call_timeout_seconds(self, provider):
        return self.full_timeout

    def opencode_quota_guard_max_output_tokens(self, provider):
        return self.max_output_tokens

    async def opencode_quota_guard_attempt(self, provider, route_plan):
        self.attempts.append((provider, route_plan))

    async def opencode_quota_guard_success(self, provider):
        self.successes.append(provider)

    async def opencode_quota_guard_direct_text_chat(
        self,
        provider,
        args,
        kwargs,
    ):
        self.direct_calls.append((provider, args, kwargs))
        return self.direct_result


class OpenCodeQuotaGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.owner = FakeOwner()
        self.original_text_chat = FakeProvider.text_chat
        install_opencode_quota_guard(self.owner, FakeProvider)

    async def asyncTearDown(self) -> None:
        uninstall_opencode_quota_guard(self.owner, FakeProvider)

    async def test_reports_provider_error_and_preserves_original_exception(self) -> None:
        provider = FakeProvider()

        with self.assertRaisesRegex(RuntimeError, "FreeUsageLimitError"):
            await provider.text_chat(prompt="test")

        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.last_kwargs["request_max_retries"], 1)
        self.assertEqual(len(self.owner.errors), 1)

    async def test_bound_route_plan_tracks_the_actual_attempt(self) -> None:
        provider = FakeProvider()
        route_plan = object()
        token = bind_provider_guard_route_plan(route_plan)
        try:
            with self.assertRaises(RuntimeError):
                await provider.text_chat(prompt="test")
            self.assertEqual(self.owner.attempts[0], (provider, route_plan))
            self.assertEqual(
                current_provider_guard_provider_id(),
                provider.provider_config["id"],
            )
        finally:
            reset_provider_guard_route_plan(token)

        self.assertEqual(current_provider_guard_provider_id(), "")

    async def test_active_cooldown_blocks_before_external_call(self) -> None:
        provider = FakeProvider()
        self.owner.cooldown = {"expires_at": 1_900_000_000.0}

        with self.assertRaises(OpenCodeQuotaCooldownError):
            await provider.text_chat(prompt="test")

        self.assertEqual(provider.calls, 0)

    async def test_full_call_timeout_is_reported_and_raised(self) -> None:
        provider = FakeProvider()
        provider.delay = 0.05
        self.owner.full_timeout = 0.01

        with self.assertRaisesRegex(
            ProviderFullCallTimeoutError,
            "full call timed out after 0.01 seconds",
        ):
            await provider.text_chat(prompt="test")

        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(self.owner.errors), 1)

    async def test_nonstream_does_not_use_stream_first_response_timeout(
        self,
    ) -> None:
        provider = FakeProvider()
        provider.delay = 0.03
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="ok",
        )
        self.owner.timeout = 0.01

        response = await provider.text_chat(prompt="test")

        self.assertEqual(response.completion_text, "ok")
        self.assertEqual(provider.calls, 1)

    async def test_provider_timeout_exception_is_not_rewrapped_as_local_budget(
        self,
    ) -> None:
        provider = FakeProvider()
        provider.exception = TimeoutError("provider socket timed out")
        self.owner.timeout = 1

        with self.assertRaisesRegex(TimeoutError, "provider socket timed out") as ctx:
            await provider.text_chat(prompt="test")

        self.assertNotIsInstance(ctx.exception, ProviderAttemptTimeoutError)
        self.assertIs(self.owner.errors[0][1], ctx.exception)

    async def test_success_is_reported_to_reset_timeout_streak(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="ok",
        )

        await provider.text_chat(prompt="test")

        self.assertEqual(self.owner.successes, [provider])

    async def test_provider_budget_injects_output_token_cap(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="ok",
        )
        self.owner.request_max_retries = 2
        self.owner.max_output_tokens = 220

        await provider.text_chat(prompt="test")

        self.assertEqual(provider.last_kwargs["request_max_retries"], 2)
        self.assertEqual(provider.last_kwargs["max_tokens"], 220)

    async def test_provider_budget_preserves_smaller_requested_output(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="ok",
        )
        self.owner.max_output_tokens = 220

        await provider.text_chat(prompt="test", max_tokens=15)

        self.assertEqual(provider.last_kwargs["max_tokens"], 15)

    async def test_provider_budget_caps_max_completion_tokens(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="ok",
        )
        self.owner.max_output_tokens = 220

        await provider.text_chat(prompt="test", max_completion_tokens=500)

        self.assertEqual(provider.last_kwargs["max_completion_tokens"], 220)
        self.assertNotIn("max_tokens", provider.last_kwargs)

    async def test_stream_call_receives_provider_output_cap(self) -> None:
        provider = FakeProvider()
        self.owner.max_output_tokens = 220

        chunks = [
            chunk
            async for chunk in provider.text_chat_stream(prompt="test")
        ]

        self.assertEqual(chunks, ["chunk"])
        self.assertEqual(provider.last_kwargs["max_tokens"], 220)

    async def test_response_is_attributed_and_role_error_is_reported(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="err",
            completion_text="Error code: 503 service unavailable",
        )

        response = await provider.text_chat(prompt="test")

        self.assertEqual(
            response._provider_quota_router_provider_id,
            provider.provider_config["id"],
        )
        self.assertEqual(len(self.owner.errors), 1)
        self.assertIn("503", str(self.owner.errors[0][1]))

    async def test_stream_first_chunk_timeout_is_reported_and_raised(self) -> None:
        provider = FakeProvider()
        provider.delay = 0.05
        self.owner.timeout = 0.01

        with self.assertRaises(ProviderAttemptTimeoutError):
            async for _ in provider.text_chat_stream(prompt="test"):
                pass

        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(self.owner.errors), 1)

    async def test_direct_handler_can_take_over_unbound_calls(self) -> None:
        provider = FakeProvider()
        self.owner.direct_result = SimpleNamespace(
            role="assistant",
            completion_text="routed",
        )

        response = await provider.text_chat(prompt="test")

        self.assertEqual(response.completion_text, "routed")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(self.owner.direct_calls), 1)

    async def test_bound_route_plan_bypasses_direct_handler(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="normal",
        )
        self.owner.direct_result = SimpleNamespace(
            role="assistant",
            completion_text="wrong",
        )
        token = bind_provider_guard_route_plan(object())
        try:
            response = await provider.text_chat(prompt="test")
        finally:
            reset_provider_guard_route_plan(token)

        self.assertEqual(response.completion_text, "normal")
        self.assertEqual(provider.calls, 1)

    async def test_plugin_call_inside_normal_route_plan_is_direct_routed(
        self,
    ) -> None:
        provider = FakeProvider()
        self.owner.direct_result = SimpleNamespace(
            role="assistant",
            completion_text="nested-routed",
        )
        token = bind_provider_guard_route_plan(object())
        try:
            with patch(
                "core.opencode_quota_guard.detect_plugin_caller",
                return_value="astrbot_plugin_stealer",
            ):
                response = await provider.text_chat(prompt="test")
        finally:
            reset_provider_guard_route_plan(token)

        self.assertEqual(response.completion_text, "nested-routed")
        self.assertEqual(provider.calls, 0)

    async def test_probe_bypass_scope_skips_direct_handler(self) -> None:
        provider = FakeProvider()
        provider.response = SimpleNamespace(
            role="assistant",
            completion_text="probe",
        )
        self.owner.direct_result = SimpleNamespace(
            role="assistant",
            completion_text="wrong",
        )
        token = begin_provider_guard_bypass("source")
        try:
            response = await provider.text_chat(prompt="probe")
        finally:
            end_provider_guard_bypass(token)

        self.assertEqual(response.completion_text, "probe")
        self.assertEqual(provider.calls, 1)

    async def test_inherited_guards_run_once_for_same_provider(self) -> None:
        class ParentProvider:
            def __init__(self) -> None:
                self.provider_config = {"id": "provider/inherited"}
                self.calls = 0

            async def text_chat(self, *args, **kwargs):
                self.calls += 1
                return SimpleNamespace(
                    role="assistant",
                    completion_text="ok",
                )

            async def text_chat_stream(self, *args, **kwargs):
                self.calls += 1
                yield "chunk"

        class ChildProvider(ParentProvider):
            pass

        install_opencode_quota_guard(self.owner, ParentProvider)
        install_opencode_quota_guard(self.owner, ChildProvider)
        provider = ChildProvider()
        try:
            response = await provider.text_chat(prompt="test")
            chunks = [
                chunk
                async for chunk in provider.text_chat_stream(prompt="test")
            ]
        finally:
            uninstall_opencode_quota_guard(self.owner, ChildProvider)
            uninstall_opencode_quota_guard(self.owner, ParentProvider)

        self.assertEqual(response.completion_text, "ok")
        self.assertEqual(chunks, ["chunk"])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(self.owner.direct_calls), 1)
        self.assertEqual(self.owner.successes, [provider, provider])
        self.assertNotIn("text_chat", ChildProvider.__dict__)
        self.assertNotIn("text_chat_stream", ChildProvider.__dict__)
        self.assertIs(ChildProvider.text_chat, ParentProvider.text_chat)
        self.assertIs(
            ChildProvider.text_chat_stream,
            ParentProvider.text_chat_stream,
        )

    async def test_uninstall_restores_original_methods(self) -> None:
        self.assertTrue(is_opencode_quota_guard_installed(FakeProvider))
        uninstall_opencode_quota_guard(self.owner, FakeProvider)
        self.assertIs(FakeProvider.text_chat, self.original_text_chat)
        self.owner = FakeOwner()


if __name__ == "__main__":
    unittest.main()
