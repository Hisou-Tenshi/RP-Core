# SPDX-License-Identifier: AGPL-3.0-only














from __future__ import annotations

import io
import logging
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import provider_health
import tenshi_utils

LONG_MESSAGE = "ERROR-PAYLOAD-" + ("x" * 900)


class ConsoleRedactionIsolationTest(unittest.TestCase):
    """文件日志始终全量；控制台才脱敏。"""

    def setUp(self):
        self._orig_enabled = tenshi_utils.actions_console_redaction_enabled
        tenshi_utils.actions_console_redaction_enabled = lambda: True
        self._orig_stdout = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = pathlib.Path(self._tmp.name) / "debug.log"

    def tearDown(self):
        sys.stdout = self._orig_stdout
        tenshi_utils.actions_console_redaction_enabled = self._orig_enabled
        self._tmp.cleanup()

    def test_file_keeps_full_text_while_console_is_redacted(self):
        logger = tenshi_utils.setup_logger("RedactionIsolationTest", str(self.log_path), level=logging.INFO)
        try:
            logger.info(LONG_MESSAGE)
            for handler in logger.handlers:
                handler.flush()
        finally:
            for handler in list(logger.handlers):
                handler.close()
            logger.handlers.clear()

        file_text = self.log_path.read_text(encoding="utf-8")
        console_text = self._buf.getvalue()

        self.assertIn(LONG_MESSAGE, file_text, "文件日志必须是完整原文")
        self.assertNotIn(LONG_MESSAGE, console_text, "控制台应当被脱敏/截断")
        self.assertIn("已脱敏", console_text, console_text[:200])

    def test_short_message_untouched_in_both(self):
        logger = tenshi_utils.setup_logger("RedactionIsolationShort", str(self.log_path), level=logging.INFO)
        try:
            logger.info("短消息：一切都好")
            for handler in logger.handlers:
                handler.flush()
        finally:
            for handler in list(logger.handlers):
                handler.close()
            logger.handlers.clear()

        self.assertIn("短消息：一切都好", self.log_path.read_text(encoding="utf-8"))
        self.assertIn("短消息：一切都好", self._buf.getvalue())


class ProviderHealthLoggingTest(unittest.TestCase):
    """provider_health 的告警要走调用方装进来的 logger（否则静默丢失）。"""

    def setUp(self):
        self.records: list[str] = []
        self._orig_logger = provider_health._configured_logger
        records = self.records

        class _Collector:
            def warning(self, message, *args, **kwargs):
                records.append(str(message))

        provider_health.set_logger(_Collector())
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "provider_health.json"
        self._orig_path = provider_health.HEALTH_PATH
        provider_health.HEALTH_PATH = self.path
        provider_health.reset_cache()

    def tearDown(self):
        provider_health._configured_logger = self._orig_logger
        provider_health.HEALTH_PATH = self._orig_path
        provider_health.reset_cache()
        self._tmp.cleanup()

    def _write_health(self, payload: dict) -> None:
        import datetime as dt
        import json

        stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self.path.write_text(
            json.dumps({"schema": 2, "generated_at_utc": stamp, "providers": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        provider_health.reset_cache()

    def test_ban_decision_is_logged(self):
        self._write_health(
            {
                "claude": {
                    "enabled": True,
                    "paths": {
                        "Codeyu Base": {"ok": True, "verdict": "ok"},
                        "CoderPlan": {"ok": False, "verdict": "dns_fail"},
                    },
                }
            }
        )
        kept = provider_health.filter_paths(
            "claude", [{"name": "Codeyu Base"}, {"name": "CoderPlan"}]
        )
        self.assertEqual([p["name"] for p in kept], ["Codeyu Base"])
        self.assertTrue(any("CoderPlan" in r and "ban" in r for r in self.records), self.records)

    def test_blank_key_decision_is_logged(self):
        self._write_health(
            {"grok": {"enabled": False, "paths": {"Official": {"ok": False, "verdict": "dns_fail"}}}}
        )
        self.assertEqual(provider_health.key_or_blank("grok", "Official", "gk"), "")
        self.assertTrue(any("传空 key" in r for r in self.records), self.records)

    def test_missing_file_is_logged_once(self):
        provider_health.reset_cache()
        provider_health.filter_paths("claude", [{"name": "Official"}])
        provider_health.filter_paths("claude", [{"name": "Official"}])
        warn_count = sum(1 for r in self.records if "没有探活结果" in r)
        self.assertEqual(warn_count, 1, "fail-open 的提示只该出现一次，别刷屏")


if __name__ == "__main__":
    unittest.main()
