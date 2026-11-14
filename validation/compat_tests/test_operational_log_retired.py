# SPDX-License-Identifier: AGPL-3.0-only






from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import tenshi_config
from kb_apply import extract_delta_sections, has_meaningful_summary_payload
from kb_storage import empty_kb_core_template
import log_update

_OP = {
    "commitments": ["每晚十点向天子报告"],
    "unresolved_threads": ["天子问的问题还没回答"],
    "future_hooks": ["明天追问考试结果"],
}


class TestOperationalLogRetired(unittest.TestCase):
    def test_extract_ignores_operational_log(self):
        _up, _rd, op, _nm = extract_delta_sections({"operational_log": dict(_OP)})
        self.assertEqual(op, {})

    def test_op_only_summary_is_not_meaningful(self):
        self.assertFalse(has_meaningful_summary_payload({"operational_log": dict(_OP)}))

    def test_merge_does_not_store_new_operational_log(self):
        base = {"user_profile": {}, "relationship_dynamics": {}}
        patch = {
            "operational_log": dict(_OP),
            "narrative_memory": {
                "significant_events": [{"event_id": "2026-10-25-test", "description": "d"}]
            },
        }
        out = json.loads(log_update.apply_intelligent_merge(json.dumps(base), json.dumps(patch)))
        self.assertNotIn("operational_log", out)
        self.assertEqual(
            [e["event_id"] for e in out["narrative_memory"]["significant_events"]],
            ["2026-10-25-test"],
        )

    def test_merge_preserves_legacy_operational_log_untouched(self):
        """磁盘上已有的 operational_log 原样保留，不被新 summary 改写。"""
        legacy = {"commitments": ["旧承诺"], "unresolved_threads": [], "future_hooks": []}
        base = {"user_profile": {}, "operational_log": legacy}
        patch = {"operational_log": dict(_OP)}
        out = json.loads(log_update.apply_intelligent_merge(json.dumps(base), json.dumps(patch)))
        self.assertEqual(out["operational_log"], legacy)

    def test_stray_user_profile_slots_dropped(self):
        obj = {"user_profile": {"commitments": ["x"], "future_hooks": ["y"], "identity_facts": ["z"]}}
        with mock.patch.object(log_update, "logger"):
            log_update.coalesce_light_fields_misplaced(obj)
        self.assertNotIn("commitments", obj["user_profile"])
        self.assertNotIn("future_hooks", obj["user_profile"])
        self.assertNotIn("operational_log", obj)
        self.assertEqual(obj["user_profile"]["identity_facts"], ["z"])

    def test_parse_failure_patch_has_no_operational_log(self):

        with mock.patch.object(log_update, "_append_summary_trace") as trace, mock.patch.object(
            log_update, "logger"
        ) as lg:
            patch = json.loads(log_update._build_parse_failure_patch("not json {", "json_parse_failed"))
        self.assertNotIn("operational_log", patch)

        self.assertEqual(trace.call_args[0][0]["raw_output"], "not json {")
        self.assertIn("not json {", lg.error.call_args[0][0])
        self.assertEqual(patch["narrative_memory"]["significant_events"], [])

    def test_empty_template_has_no_operational_log(self):
        self.assertNotIn("operational_log", empty_kb_core_template())

    def test_prompts_no_longer_request_operational_log(self):
        self.assertNotIn("operational_log", tenshi_config.SUMMARY_PROMPT_TEMPLATE)
        self.assertFalse(hasattr(tenshi_config, "MERGE_PROMPT_TEMPLATE"))
        cc = tenshi_config.CONTEXT_COMPRESSION_PROMPT_TEMPLATE
        for gone in ("未完成：", "当前有效约束", "待回答的问题", "有没有遗漏当前未解决的问题"):
            self.assertNotIn(gone, cc)


if __name__ == "__main__":
    unittest.main()
