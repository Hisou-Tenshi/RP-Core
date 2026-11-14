# SPDX-License-Identifier: AGPL-3.0-only






from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from context_compression import (
    _all_due_slices,
    _anchor_due_slices,
    _anchor_trigger_newer_min,
    _bootstrap_due_slices,
    _format_turn_block_for_compression,
    _normalize_summaries,
    _package_exists,
    _raw_turns,
    _select_due_slice,
    _slice_time_span,
    build_compressed_context_block,
    format_mmdd_hhmm,
    save_context_compression_entry,
)


def _mk_turns(n: int, *, t0: float = 1_000_000.0, step: float = 60.0) -> list[dict]:
    return [
        {"time": t0 - i * step, "user_query": f"u{i}", "bot_answer": f"b{i}"}
        for i in range(n)
    ]


def _pkg(slice_nf: list[dict], *, created_at: float = 1.0) -> dict:
    oldest, newest = _slice_time_span(slice_nf)
    return {
        "summary": "x",
        "created_at": created_at,
        "start_time": oldest,
        "end_time": newest,
        "start_time_label": format_mmdd_hhmm(oldest),
        "end_time_label": format_mmdd_hhmm(newest),
    }


class ContextCompressionPolicyTest(unittest.TestCase):
    def test_bootstrap_no_package(self):
        turns = _mk_turns(25)
        due = _bootstrap_due_slices(turns, [])
        self.assertEqual(due, [(15, 25), (5, 15)])

        turns15 = _mk_turns(15)
        self.assertEqual(_bootstrap_due_slices(turns15, []), [(5, 15)])
        self.assertEqual(_bootstrap_due_slices(_mk_turns(14), []), [])

    def test_anchor_trigger_threshold(self):
        self.assertEqual(_anchor_trigger_newer_min(), 14)
        turns30 = _mk_turns(30)
        anchor30 = _pkg(turns30[15:25])
        newer_count = sum(1 for t in turns30 if t["time"] > anchor30["end_time"])
        self.assertEqual(newer_count, 15)
        due = _anchor_due_slices(turns30, [anchor30])
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0], (5, 15))

    def test_anchor_multi_packages(self):
        turns44 = _mk_turns(44)
        anchor44 = _pkg(turns44[25:35])
        due2 = _anchor_due_slices(turns44, [anchor44])
        self.assertEqual(len(due2), 2)
        self.assertEqual(due2[0], (15, 25))
        self.assertEqual(due2[1], (5, 15))

    def test_select_first_due(self):
        turns = _mk_turns(25)
        sl = _select_due_slice(turns, [], None)
        self.assertTrue(sl and sl[0]["user_query"] == "u15")
        summaries = [_pkg(turns[15:25])]
        sl2 = _select_due_slice(turns, summaries, None)
        self.assertTrue(sl2 and sl2[0]["user_query"] == "u5")

    def test_store_newest_first_order(self):
        """存档 summaries 应按新→旧排列(end_time 大的在前)。"""
        entry = {
            "summaries": [
                {"summary": "old", "start_time": 1000.0, "end_time": 2000.0, "created_at": 1.0},
                {"summary": "new", "start_time": 3000.0, "end_time": 4000.0, "created_at": 2.0},
                {"summary": "mid", "start_time": 2000.0, "end_time": 3000.0, "created_at": 1.5},
            ],
            "pending": [],
        }
        normalized = _normalize_summaries(entry)
        end_times = [s["end_time"] for s in normalized]
        self.assertEqual(end_times, sorted(end_times, reverse=True), f"not newest-first: {end_times}")

    def test_gap_less_than_15_between_latest_pkg_and_newest_turn(self):
        """有包且新于锚点的对话不足阈值 → 不触发(避免无限压缩)。"""
        turns = _mk_turns(25)
        pkg = _pkg(turns[5:15])
        newer_count = sum(1 for t in turns if t["time"] > pkg["end_time"])
        self.assertEqual(newer_count, 5)
        due = _anchor_due_slices(turns, [pkg])
        self.assertEqual(due, [], f"should not trigger with only {newer_count} newer turns")

    def test_injection_chronological_oldest_first(self):
        """注入压缩块:旧→新顺序,最旧的包排第一行。"""
        entry = {
            "summaries": [
                {"summary": "older pkg", "start_time": 1000.0, "end_time": 2000.0, "created_at": 1.0},
                {"summary": "newer pkg", "start_time": 3000.0, "end_time": 4000.0, "created_at": 2.0},
            ],
            "pending": [],
        }
        normalized = _normalize_summaries(entry)
        to_inject = list(reversed(normalized))
        self.assertLess(to_inject[0]["end_time"], to_inject[1]["end_time"], "injection should be oldest-first")

    def test_time_label_format(self):
        label = format_mmdd_hhmm(1787543820)
        self.assertEqual(len(label), 11)
        self.assertEqual(label[2], "-")
        self.assertEqual(label[5], " ", label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
