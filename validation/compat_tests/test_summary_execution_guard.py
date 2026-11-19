# SPDX-License-Identifier: AGPL-3.0-only









from __future__ import annotations

import datetime
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import log_update
import tenshi_config
from log_update import (
    evaluate_summary_execution_guard,
    is_in_summary_execution_window,
    latest_effective_message_time,
    parse_summary_execution_windows,
    summary_cut_observation_complete,
)


def _local(year, month, day, hour, minute=0):
    tz = datetime.timezone(datetime.timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))
    return datetime.datetime(year, month, day, hour, minute, tzinfo=tz)


class SummaryExecutionGuardTest(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "guard": tenshi_config.SUMMARY_EXECUTION_GUARD_ENABLED,
            "idle": tenshi_config.SUMMARY_ACTIVITY_IDLE_SECONDS,
            "windows": tenshi_config.SUMMARY_EXECUTION_WINDOWS,
        }
        tenshi_config.SUMMARY_EXECUTION_GUARD_ENABLED = True
        tenshi_config.SUMMARY_ACTIVITY_IDLE_SECONDS = 2700
        tenshi_config.SUMMARY_EXECUTION_WINDOWS = "22:00-08:00"

    def tearDown(self):
        tenshi_config.SUMMARY_EXECUTION_GUARD_ENABLED = self._orig["guard"]
        tenshi_config.SUMMARY_ACTIVITY_IDLE_SECONDS = self._orig["idle"]
        tenshi_config.SUMMARY_EXECUTION_WINDOWS = self._orig["windows"]

    def test_parse_overnight_window(self):
        windows = parse_summary_execution_windows(tenshi_config.SUMMARY_EXECUTION_WINDOWS)
        self.assertEqual(windows, [(22 * 60, 8 * 60)], str(windows))

    def test_window_boundaries(self):
        windows = parse_summary_execution_windows(tenshi_config.SUMMARY_EXECUTION_WINDOWS)
        self.assertTrue(is_in_summary_execution_window(_local(2026, 8, 24, 22, 0), windows))
        self.assertTrue(is_in_summary_execution_window(_local(2026, 8, 24, 2, 11), windows))
        self.assertTrue(is_in_summary_execution_window(_local(2026, 8, 24, 7, 59), windows))
        self.assertFalse(is_in_summary_execution_window(_local(2026, 8, 24, 8, 0), windows))
        self.assertFalse(is_in_summary_execution_window(_local(2026, 8, 24, 15, 0), windows))

    def test_window_out_skips(self):
        now_out = _local(2026, 8, 24, 15, 0)
        skip, reason = evaluate_summary_execution_guard(now_out, None)
        self.assertTrue(skip)
        self.assertIn("不在允许窗口", reason)

    def test_in_window_but_active_skips(self):
        now_in = _local(2026, 8, 24, 2, 11)
        recent_ts = now_in.timestamp() - 600
        skip, reason = evaluate_summary_execution_guard(now_in, recent_ts)
        self.assertTrue(skip)
        self.assertIn("还需", reason)

    def test_in_window_idle_continues(self):
        now_in = _local(2026, 8, 24, 2, 11)
        idle_ts = now_in.timestamp() - 3600
        skip, reason = evaluate_summary_execution_guard(now_in, idle_ts)
        self.assertFalse(skip)
        self.assertIn("继续总结", reason)

    def test_ignore_silence_for_activity(self):
        idle_ts = _local(2026, 8, 24, 1, 11).timestamp()
        log_data = [
            {
                "chat_history": [
                    {"time": idle_ts, "user_query": "hi", "bot_answer": "yo"},
                    {"time": idle_ts + 120, "bot_decision": "SILENCE"},
                ]
            }
        ]
        latest = latest_effective_message_time(log_data)
        self.assertEqual(latest, idle_ts, str(latest))

    def test_pre_cutoff_window_is_yesterday_today(self):
        today = datetime.date(2026, 8, 24)
        period_end_date = today
        period_start_date = today - datetime.timedelta(days=1)
        self.assertEqual(period_start_date, datetime.date(2026, 8, 23))
        self.assertEqual(period_end_date, today)

    def test_summary_cut_observation(self):
        day = datetime.date(2026, 8, 24)
        self.assertFalse(summary_cut_observation_complete(_local(2026, 8, 24, 4, 11), day, 6))
        self.assertFalse(summary_cut_observation_complete(_local(2026, 8, 24, 5, 59), day, 6))
        self.assertTrue(summary_cut_observation_complete(_local(2026, 8, 24, 6, 0), day, 6))


    def test_budget_pause_returns_only_completed_chunk_and_resumes_after_cursor(self):
        now = _local(2026, 8, 24, 7)
        messages = [
            {"time": _local(2026, 8, day, 21, minute).timestamp(), "user_query": text}
            for day, minute, text in [(21, 0, "first"), (21, 1, "first-two"),
                                      (22, 0, "second"), (22, 1, "second-two")]
        ]

        class FrozenDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        def summarize(text, *, existing_summary, summary_day):
            event = {"event_id": f"{summary_day}-1", "description": text}
            return json.dumps({"narrative_memory": {"significant_events": [event]}})

        clock = mock.Mock()
        clock.monotonic.side_effect = [10, 800, 0]
        with (
            mock.patch.object(log_update.datetime, "datetime", FrozenDatetime),
            mock.patch.object(log_update, "time", clock),
            mock.patch.object(log_update, "load_latest_memory_from_storage", return_value="{}"),
            mock.patch.object(log_update, "get_summary_with_fallback", side_effect=summarize) as run,
            mock.patch.object(log_update, "apply_intelligent_merge", side_effect=lambda base, patch: patch),
            mock.patch.object(log_update, "ensure_mem_mgr", return_value=None),
            mock.patch.object(log_update, "ensure_rag", return_value=None),
            mock.patch("bond_reflect.collect_day_evidence"),
        ):
            memory, cursor, status = log_update.global_segment_and_summarize(
                messages, start_monotonic=0, soft_timeout_seconds=720,
            )
            self.assertEqual(status, "deferred")
            self.assertEqual(cursor, messages[1]["time"])
            self.assertIn("first-two", memory)
            self.assertNotIn("second-two", memory)
            self.assertEqual(run.call_count, 1)

            _, cursor, status = log_update.global_segment_and_summarize(
                [message for message in messages if message["time"] > cursor],
                start_monotonic=0, soft_timeout_seconds=720,
            )
            self.assertEqual(status, "completed")
            self.assertEqual(cursor, messages[3]["time"])
            self.assertEqual(run.call_count, 2)

    def test_deferred_summary_saves_cursor_before_exiting_and_fails_if_save_fails(self):
        with (
            mock.patch.object(log_update, "load_local_data", return_value=[{}]),
            mock.patch.object(log_update, "evaluate_summary_execution_guard", return_value=(False, "")),
            mock.patch.object(log_update, "load_summary_cursor_timestamp", return_value=10),
            mock.patch.object(log_update, "process_summaries", return_value=(True, 20, "deferred")),
            mock.patch.object(log_update, "push_knowledge_base_and_check_success", return_value=True),
            mock.patch.object(log_update, "FileLock"),
            mock.patch.object(log_update, "save_summary_cursor_timestamp", side_effect=[True, False]) as save,
            mock.patch.object(log_update.tenshi_config, "GITHUB_OUTPUT", None),
            mock.patch.object(log_update, "ensure_mem_mgr") as manager,
        ):
            self.assertEqual(log_update.main(), log_update.SUMMARY_DEFERRED_EXIT)
            save.assert_called_once_with(20)
            self.assertEqual(log_update.main(), 1)
            manager.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
