from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.reports import read_recent_decisions
from core.state import QuotaStateStore


class ReportsAndRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_decisions_reads_and_reverses_file_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "route_decisions.jsonl"
            path.write_text(
                "".join(
                    json.dumps({"index": index}) + "\n"
                    for index in range(1000)
                ),
                encoding="utf-8",
            )

            items = read_recent_decisions(path, limit=3)

            self.assertEqual(
                [item["index"] for item in items],
                [999, 998, 997],
            )

    async def test_decision_log_rotates_before_appending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QuotaStateStore(Path(temp_dir))
            store.decisions_path.write_text("old-log-data\n", encoding="utf-8")

            with patch("core.state.DECISION_LOG_MAX_BYTES", 4):
                await store.record_decision({"request_id": "new"})

            rotated = store.decisions_path.with_suffix(".jsonl.1")
            self.assertEqual(
                rotated.read_text(encoding="utf-8"),
                "old-log-data\n",
            )
            self.assertEqual(
                json.loads(store.decisions_path.read_text(encoding="utf-8"))[
                    "request_id"
                ],
                "new",
            )


if __name__ == "__main__":
    unittest.main()
