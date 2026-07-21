from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.state import QuotaStateStore


class StateCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_model_circuit_is_isolated_persistent_and_expires(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            first = await store.open_provider_model_circuit(
                provider_id="relay/model-a",
                provider_model="model-a",
                ttl_seconds=1_800,
                error="HTTP 429",
            )
            duplicate = await store.open_provider_model_circuit(
                provider_id="relay/model-a",
                provider_model="model-a",
                ttl_seconds=3_600,
                error="another failure",
            )
            await store.open_provider_model_circuit(
                provider_id="relay/model-b",
                provider_model="model-b",
                ttl_seconds=1_800,
                error="HTTP 500",
            )

            self.assertEqual(duplicate["retry_at"], first["retry_at"])
            reloaded = QuotaStateStore(Path(temp_dir))
            self.assertIsNotNone(
                await reloaded.get_provider_model_circuit(
                    provider_id="relay/model-a"
                )
            )
            self.assertIsNotNone(
                await reloaded.get_provider_model_circuit(
                    provider_id="relay/model-b"
                )
            )

            await reloaded.reset_cache()
            self.assertIsNotNone(
                await reloaded.get_provider_model_circuit(
                    provider_id="relay/model-a"
                )
            )

            await reloaded.open_provider_model_circuit(
                provider_id="relay/expired",
                provider_model="expired",
                ttl_seconds=0,
                error="failure",
            )
            self.assertIsNone(
                await reloaded.get_provider_model_circuit(
                    provider_id="relay/expired"
                )
            )

    async def test_notification_claim_is_persistent_and_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            first = await store.claim_notification(
                key="provider_error_admin_alert",
                interval_seconds=3_600,
                detail="HTTP 403",
            )
            self.assertIsNotNone(first)

            reloaded = QuotaStateStore(Path(temp_dir))
            duplicate = await reloaded.claim_notification(
                key="provider_error_admin_alert",
                interval_seconds=3_600,
                detail="another error",
            )
            self.assertIsNone(duplicate)

            await reloaded.reset_cache()
            after_reset = await reloaded.claim_notification(
                key="provider_error_admin_alert",
                interval_seconds=3_600,
                detail="after reset",
            )
            self.assertIsNone(after_reset)

    async def test_provider_group_circuit_persists_and_probe_controls_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            opened = await store.open_provider_group_circuit(
                group_id="volcengine",
                trigger_provider_id="openai/doubao",
                ttl_seconds=0,
                error="HTTP 403",
            )
            self.assertEqual(opened["status"], "open")

            reloaded = QuotaStateStore(Path(temp_dir))
            probe = await reloaded.acquire_provider_group_probe(
                group_id="volcengine",
                provider_id="openai/doubao-probe",
                lease_seconds=60,
            )
            self.assertIsNotNone(probe)
            self.assertEqual(probe["status"], "probing")

            duplicate = await reloaded.acquire_provider_group_probe(
                group_id="volcengine",
                provider_id="openai/doubao-other",
                lease_seconds=60,
            )
            self.assertIsNone(duplicate)

            reopened = await reloaded.finish_provider_group_probe(
                group_id="volcengine",
                success=False,
                cooldown_seconds=1_800,
                error="still 403",
            )
            self.assertIsNotNone(reopened)
            self.assertEqual(reopened["status"], "open")
            self.assertGreater(reopened["retry_at"], reopened["started_at"])

            await reloaded.reset_cache()
            persisted = await reloaded.get_provider_group_circuit(
                group_id="volcengine"
            )
            self.assertIsNotNone(persisted)

            await reloaded.finish_provider_group_probe(
                group_id="volcengine",
                success=True,
                cooldown_seconds=1_800,
            )
            cleared = await reloaded.get_provider_group_circuit(
                group_id="volcengine"
            )
            self.assertIsNone(cleared)

    async def test_cooldown_persists_and_reset_cache_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            created = await store.start_cooldown(
                quota_key="doubao-model",
                window_id="window-a",
                provider_id="openai/doubao-model",
                provider_model="doubao-model",
                ttl_seconds=86_400,
            )
            self.assertGreater(created["expires_at"], created["started_at"])

            reloaded = QuotaStateStore(Path(temp_dir))
            persisted = await reloaded.get_cooldown(quota_key="doubao-model")
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted["window_id"], "window-a")

            await reloaded.reset_cache()
            after_reset = await reloaded.get_cooldown(quota_key="doubao-model")
            self.assertIsNotNone(after_reset)

    async def test_upstream_quota_cooldown_uses_absolute_reset_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            reset_at = 1_900_000_000.0
            created = await store.set_cooldown_until(
                quota_key="mimo-v2.5-free",
                window_id="window-a",
                provider_id="opencode-zen/mimo-v2.5-free",
                provider_model="mimo-v2.5-free",
                expires_at=reset_at,
                reason="upstream_quota_exhausted",
            )

            self.assertEqual(created["expires_at"], reset_at)
            self.assertEqual(created["reason"], "upstream_quota_exhausted")

    async def test_clears_old_opencode_token_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            await store.start_cooldown(
                quota_key="mimo-v2.5-free",
                window_id="window-old",
                provider_id="opencode-zen/mimo-v2.5-free",
                provider_model="mimo-v2.5-free",
                ttl_seconds=86_400,
            )

            changed = await store.clear_legacy_cooldowns_for_provider_prefixes(
                provider_prefixes=("opencode-zen/",),
            )
            cooldown = await store.get_cooldown(quota_key="mimo-v2.5-free")

            self.assertEqual(changed, 1)
            self.assertIsNone(cooldown)

    async def test_preserves_real_opencode_upstream_quota_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            await store.set_cooldown_until(
                quota_key="mimo-v2.5-free",
                window_id="window-a",
                provider_id="opencode-zen/mimo-v2.5-free",
                provider_model="mimo-v2.5-free",
                expires_at=1_900_000_000.0,
                reason="upstream_quota_exhausted",
            )

            changed = await store.clear_legacy_cooldowns_for_provider_prefixes(
                provider_prefixes=("opencode-zen/",),
            )
            cooldown = await store.get_cooldown(quota_key="mimo-v2.5-free")

            self.assertEqual(changed, 0)
            self.assertEqual(cooldown["reason"], "upstream_quota_exhausted")


if __name__ == "__main__":
    unittest.main()
