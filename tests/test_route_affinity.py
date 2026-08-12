from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core.affinity import affinity_modality, conversation_affinity_key
from core.state import QuotaStateStore


class RouteAffinityStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_lease_does_not_slide_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            created = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/a",
                provider_model="model-a",
                request_started_at=90,
                ttl_seconds=3_600,
                now=100,
            )
            refreshed = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/a",
                provider_model="model-a",
                request_started_at=150,
                ttl_seconds=3_600,
                now=200,
            )

            self.assertEqual(created.status, "created")
            self.assertEqual(refreshed.status, "unchanged")
            self.assertEqual(refreshed.affinity.expires_at, 3_700)
            self.assertEqual(refreshed.affinity.last_request_started_at, 150)
            self.assertEqual(
                (await store.lookup_route_affinity(
                    conversation_key="conversation-a",
                    modality="text",
                    now=3_699,
                )).status,
                "hit",
            )
            self.assertEqual(
                (await store.lookup_route_affinity(
                    conversation_key="conversation-a",
                    modality="text",
                    now=3_700,
                )).status,
                "expired",
            )

    async def test_affinity_is_persistent_and_isolated_by_modality(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = conversation_affinity_key("aiocqhttp:GroupMessage:123")
            store = QuotaStateStore(Path(temp_dir))
            await store.commit_route_affinity(
                conversation_key=key,
                modality="text",
                provider_id="provider/text",
                provider_model="text-model",
                request_started_at=10,
                ttl_seconds=3_600,
                now=100,
            )
            await store.commit_route_affinity(
                conversation_key=key,
                modality="image",
                provider_id="provider/vision",
                provider_model="vision-model",
                request_started_at=20,
                ttl_seconds=3_600,
                now=110,
            )

            reloaded = QuotaStateStore(Path(temp_dir))
            text = await reloaded.lookup_route_affinity(
                conversation_key=key,
                modality="text",
                now=120,
            )
            image = await reloaded.lookup_route_affinity(
                conversation_key=key,
                modality="image",
                now=120,
            )

            self.assertEqual(text.affinity.provider_id, "provider/text")
            self.assertEqual(image.affinity.provider_id, "provider/vision")
            self.assertEqual(affinity_modality(set()), "text")
            self.assertEqual(
                affinity_modality({"audio", "image"}),
                "image+audio",
            )

    async def test_fallback_replaces_affinity_and_late_result_cannot_revert_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            first = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/a",
                provider_model="model-a",
                request_started_at=10,
                ttl_seconds=3_600,
                now=100,
            )
            replaced = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/b",
                provider_model="model-b",
                request_started_at=30,
                ttl_seconds=3_600,
                expected_generation=first.affinity.generation,
                planned_affinity_provider_id="provider/a",
                now=200,
            )
            stale = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/a",
                provider_model="model-a",
                request_started_at=20,
                ttl_seconds=3_600,
                expected_generation=first.affinity.generation,
                planned_affinity_provider_id="provider/a",
                now=300,
            )

            self.assertEqual(replaced.status, "replaced")
            self.assertEqual(stale.status, "stale")
            current = await store.lookup_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                now=301,
            )
            self.assertEqual(current.affinity.provider_id, "provider/b")

    async def test_clear_blocks_inflight_request_from_recreating_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/a",
                provider_model="model-a",
                request_started_at=10,
                ttl_seconds=3_600,
                now=100,
            )

            removed = await store.clear_route_affinities(
                conversation_key="conversation-a",
                now=150,
            )
            stale = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/b",
                provider_model="model-b",
                request_started_at=140,
                ttl_seconds=3_600,
                now=160,
            )
            next_request = await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/b",
                provider_model="model-b",
                request_started_at=160,
                ttl_seconds=3_600,
                now=170,
            )

            self.assertEqual(removed, 1)
            self.assertEqual(stale.status, "cleared")
            self.assertEqual(next_request.status, "created")

    async def test_reset_cache_preserves_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            now = time.time()
            await store.commit_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                provider_id="provider/a",
                provider_model="model-a",
                request_started_at=now - 1,
                ttl_seconds=3_600,
                now=now,
            )

            await store.reset_cache()

            lookup = await store.lookup_route_affinity(
                conversation_key="conversation-a",
                modality="text",
                now=now + 1,
            )
            self.assertEqual(lookup.status, "hit")


if __name__ == "__main__":
    unittest.main()
