from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.state import QuotaStateStore


class StateCooldownTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
