# SPDX-License-Identifier: AGPL-3.0-only




from __future__ import annotations

import json
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import log_update


def _dummy_response(payload: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))


def _memory(facts: list, nicknames: dict | None = None) -> dict:
    mem = {"user_profile": {"identity_facts": list(facts)}}
    if nicknames is not None:
        mem["relationship_dynamics"] = {"special_nicknames": dict(nicknames)}
    return mem


def _facts(n: int) -> list:
    return [f"事实{i}：这是第{i}条测试锚点" for i in range(n)]


class TestIdentityFold(unittest.TestCase):
    def test_below_threshold_no_llm_call(self):
        mem = _memory(_facts(3), {"用户称天子": ["老婆"]})
        with mock.patch.object(log_update, "_deepseek_generate_json_with_fallback") as m:
            log_update.fold_identity_and_nicknames_with_lite(mem)
        m.assert_not_called()
        self.assertEqual(mem["user_profile"]["identity_facts"], _facts(3))

    def test_valid_fold_applied(self):
        facts = _facts(16)
        folded = _facts(6)
        mem = _memory(facts)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"identity_facts": folded, "notes": ""}), "deepseek-lite"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(mem["user_profile"]["identity_facts"], folded)

    def test_growth_rejected(self):
        facts = _facts(16)
        mem = _memory(facts)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"identity_facts": _facts(17)}), "deepseek-lite"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(mem["user_profile"]["identity_facts"], facts)

    def test_empty_output_rejected(self):
        facts = _facts(16)
        mem = _memory(facts)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"identity_facts": []}), "deepseek-lite"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(mem["user_profile"]["identity_facts"], facts)

    def test_too_aggressive_drop_rejected(self):
        facts = _facts(16)
        mem = _memory(facts)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"identity_facts": _facts(2)}), "deepseek-lite"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(mem["user_profile"]["identity_facts"], facts)

    def test_llm_failure_keeps_original(self):
        facts = _facts(16)
        mem = _memory(facts)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            side_effect=RuntimeError("llm down"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(mem["user_profile"]["identity_facts"], facts)

    def test_nickname_keys_fold_when_triggered(self):
        facts = _facts(2)
        nicknames = {f"键{i}": [f"称呼{i}"] for i in range(10)}
        mem = _memory(facts, nicknames)
        merged = {
            "用户称天子": ["老婆", "宝"],
            "天子称用户": ["示例用户"],
            "用户自称": "你老公",
        }
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"special_nicknames": merged}), "deepseek-lite"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(
            mem["relationship_dynamics"]["special_nicknames"],
            merged,
        )

        self.assertEqual(mem["user_profile"]["identity_facts"], facts)

    def test_nickname_empty_value_rejected(self):
        facts = _facts(2)
        nicknames = {f"键{i}": [f"称呼{i}"] for i in range(10)}
        mem = _memory(facts, nicknames)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"special_nicknames": {"用户称天子": []}}), "deepseek-lite"),
        ):
            log_update.fold_identity_and_nicknames_with_lite(mem)
        self.assertEqual(mem["relationship_dynamics"]["special_nicknames"], nicknames)

    def test_apply_intelligent_merge_invokes_fold(self):
        facts = _facts(16)
        folded = _facts(6)
        base = json.dumps(_memory(facts), ensure_ascii=False)
        with mock.patch.object(
            log_update,
            "_deepseek_generate_json_with_fallback",
            return_value=(_dummy_response({"identity_facts": folded}), "deepseek-lite"),
        ):
            out = json.loads(log_update.apply_intelligent_merge(base, "{}"))
        self.assertEqual(out["user_profile"]["identity_facts"], folded)


if __name__ == "__main__":
    unittest.main()
