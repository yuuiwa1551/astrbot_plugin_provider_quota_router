from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc

ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class Image:
    pass


class Record:
    pass


class Reply:
    def __init__(self, chain=None) -> None:
        self.chain = list(chain or [])


class MainRequiredModalitiesTests(unittest.TestCase):
    def test_detects_media_nested_in_quoted_message(self) -> None:
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                message=[Reply([Image(), Reply([Record()])])]
            )
        )

        self.assertEqual(
            ProviderQuotaRouterPlugin._required_modalities(event),
            {"image", "audio"},
        )

    def test_handles_recursive_reply_chain(self) -> None:
        reply = Reply()
        reply.chain.append(reply)
        event = SimpleNamespace(
            message_obj=SimpleNamespace(message=[reply, Image()])
        )

        self.assertEqual(
            ProviderQuotaRouterPlugin._required_modalities(event),
            {"image"},
        )


if __name__ == "__main__":
    unittest.main()
