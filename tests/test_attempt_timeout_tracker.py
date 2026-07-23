from __future__ import annotations

import unittest

from core.attempt_timeout_tracker import AttemptTimeoutTracker


class AttemptTimeoutTrackerTests(unittest.TestCase):
    def test_first_timeout_is_deferred_and_second_triggers(self) -> None:
        tracker = AttemptTimeoutTracker()

        first = tracker.record_timeout(
            provider_id="provider/model",
            threshold=2,
            window_seconds=300,
            now=100.0,
        )
        second = tracker.record_timeout(
            provider_id="provider/model",
            threshold=2,
            window_seconds=300,
            now=200.0,
        )

        self.assertFalse(first.should_cooldown)
        self.assertEqual(first.count, 1)
        self.assertTrue(second.should_cooldown)
        self.assertEqual(tracker.pending_count(provider_id="provider/model"), 0)

    def test_success_resets_timeout_streak(self) -> None:
        tracker = AttemptTimeoutTracker()
        tracker.record_timeout(
            provider_id="provider/model",
            threshold=2,
            window_seconds=300,
            now=100.0,
        )

        tracker.record_success(provider_id="provider/model")
        after_success = tracker.record_timeout(
            provider_id="provider/model",
            threshold=2,
            window_seconds=300,
            now=200.0,
        )

        self.assertFalse(after_success.should_cooldown)
        self.assertEqual(after_success.count, 1)

    def test_timeout_outside_window_starts_new_streak(self) -> None:
        tracker = AttemptTimeoutTracker()
        tracker.record_timeout(
            provider_id="provider/model",
            threshold=2,
            window_seconds=300,
            now=100.0,
        )

        observation = tracker.record_timeout(
            provider_id="provider/model",
            threshold=2,
            window_seconds=300,
            now=401.0,
        )

        self.assertFalse(observation.should_cooldown)
        self.assertEqual(observation.count, 1)


if __name__ == "__main__":
    unittest.main()
