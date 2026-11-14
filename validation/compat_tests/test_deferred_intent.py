# SPDX-License-Identifier: AGPL-3.0-only


import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import deferred_intent


class DeferredIntentTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Path(self.tempdir.name) / "nested" / "intents.json"
        self.stats = Path(self.tempdir.name) / "nested" / "stats.json"
        self.patcher = patch.object(deferred_intent, "_store_path", return_value=str(self.store))
        self.stats_patcher = patch.object(deferred_intent, "_stats_path", return_value=str(self.stats))
        self.patcher.start()
        self.stats_patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.stats_patcher.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_collect_and_expire_are_two_steps(self):
        self.assertEqual(deferred_intent.add_intents([{"what": "汤", "why": "还想说"}], "t1"), 1)
        collected = deferred_intent.expire_and_collect("perception-1")
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["status"], "pending")
        with self.store.open(encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)[0]["status"], "pending")
        deferred_intent.mark_expired_all("perception-1")
        with self.store.open(encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)[0]["status"], "expired")
        self.assertEqual(deferred_intent.expire_and_collect("perception-2"), [])

    def test_mark_fired(self):
        deferred_intent.add_intents([{"what": "花", "why": "想到"}], "t1")
        intent = deferred_intent.expire_and_collect("p")[0]
        deferred_intent.mark_fired(intent["id"])
        self.assertEqual(deferred_intent.expire_and_collect("p2"), [])

    def test_overflow_and_invalid_event(self):
        with patch.object(deferred_intent, "_setting", side_effect=lambda name, default: 1 if name == "DEFERRED_INTENT_MAX_CARRY" else default):
            self.assertEqual(
                deferred_intent.add_intents([{"what": "一"}, {"what": "二"}], "t"),
                1,
            )
        deferred_intent.pilot_log("unknown", {"bad": True})
        with self.stats.open(encoding="utf-8") as stream:
            events = json.load(stream)["events"]
        self.assertTrue(all(event["event"] != "unknown" for event in events))
        self.assertTrue(any(event["event"] == "intent_dropped_overflow" for event in events))

    def test_corrupt_json_resets_with_warning_and_continues(self):
        self.store.parent.mkdir(parents=True)
        self.store.write_text("{not json", encoding="utf-8")
        with self.assertLogs(deferred_intent._LOG, level="WARNING"):
            self.assertEqual(deferred_intent.expire_and_collect("p"), [])
        self.assertEqual(deferred_intent.add_intents([{"what": "新", "why": "理由"}], "t"), 1)
        with self.store.open(encoding="utf-8") as stream:
            self.assertEqual(len(json.load(stream)), 1)

    def test_nested_directory_created_and_lock_file_exists(self):
        self.assertEqual(deferred_intent.add_intents([{"what": "x", "why": "y"}], "t"), 1)
        self.assertTrue(self.store.exists())
        self.assertTrue(Path(str(self.store) + ".lock").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
