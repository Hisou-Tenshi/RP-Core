# SPDX-License-Identifier: AGPL-3.0-only


import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import bond_state as bs
import reply_two_stage as rts

STATE = {
    "standing": "我跟他在一起了。",
    "settled": [
        {"id": "a", "text": "我说过爱你。", "since": "2026-04-16", "src": ["x"]},
        {"id": "b", "text": "  ", "since": "2026-04-16"},
    ],
    "recent": [
        {"text": "还有点气他喝酒。", "since": "2026-10-20", "until": "2026-10-22"},
        {"text": "惦记他考研。", "since": "2026-10-26"},
    ],
}


class RenderTest(unittest.TestCase):
    def test_empty_state_renders_nothing(self):
        self.assertEqual(bs.format_bond_for_inner({}), "")
        self.assertEqual(bs.format_bond_for_typing({}), "")
        self.assertEqual(bs.format_bond_for_inner(None, None), "")

    def test_inner_has_heading_settled_and_skips_blank(self):
        out = bs.format_bond_for_inner(STATE, today="2026-10-21")
        self.assertTrue(out.startswith(bs.INNER_HEADING))
        self.assertIn("我跟他在一起了。", out)
        self.assertIn("- 我说过爱你。", out)
        self.assertNotIn("-   ", out)

    def test_recent_expiry(self):
        self.assertIn("还有点气他喝酒。", bs.format_bond_for_inner(STATE, today="2026-10-22"))
        out = bs.format_bond_for_inner(STATE, today="2026-10-23")
        self.assertNotIn("还有点气他喝酒。", out)
        self.assertIn("惦记他考研。", out)

    def test_nicknames_whitelist_and_labels(self):
        nick = {
            "天子称用户": ["示例用户", "宝"],
            "用户称天子": ["老婆"],
            "你的天人": "天子口误后被用户锁定的归属表述",
            "天子称系统": "下人",
        }
        out = bs.format_bond_for_inner({}, nick)
        self.assertIn("- 我叫他：示例用户、宝", out)
        self.assertIn("- 他叫我：老婆", out)
        self.assertNotIn("口误", out)
        self.assertNotIn("下人", out)

    def test_typing_is_compact(self):
        out = bs.format_bond_for_typing(STATE, today="2026-10-27")
        self.assertTrue(out.startswith(bs.TYPING_HEADING))
        self.assertIn("- 我说过爱你。", out)
        self.assertNotIn("我叫他", out)

    def test_load_missing_or_broken_file(self):
        self.assertEqual(bs.load_bond_state("/nonexistent/bond.json"), {})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        try:
            self.assertEqual(bs.load_bond_state(f.name), {})
            self.assertEqual(bs.typing_block(path=f.name), "")
        finally:
            os.unlink(f.name)

    def test_inner_block_reads_file_and_kb(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        try:
            kb = {"relationship_dynamics": {"special_nicknames": {"天子称用户": "示例用户"}}}
            out = bs.inner_block(kb, path=f.name, today="2026-10-27")
            self.assertIn("我说过爱你。", out)
            self.assertIn("- 我叫他：示例用户", out)
        finally:
            os.unlink(f.name)


class RealFileTest(unittest.TestCase):
    """仓库里的 data/bond_state.json 要能被读、每条 settled 有 id/text/since/src。"""

    def test_real_file_shape(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bond_state.json")
        if not os.path.exists(path):
            self.skipTest("no bond_state.json")
        data = bs.load_bond_state(path)
        self.assertTrue(data.get("settled"))
        ids = set()
        for it in data["settled"]:
            for k in ("id", "text", "since", "src"):
                self.assertIn(k, it)
            self.assertNotIn(it["id"], ids)
            ids.add(it["id"])

            for bad in ("下次", "记得", "应该", "要记住"):
                self.assertNotIn(bad, it["text"])


class TypingSystemTest(unittest.TestCase):
    def test_no_bond_is_byte_identical(self):
        self.assertEqual(
            rts.build_typing_system("CARD", sticker_block=""),
            rts.build_typing_system("CARD", sticker_block="", bond_block=""),
        )

    def test_bond_goes_right_after_card(self):
        s = rts.build_typing_system("CARD", sticker_block="", bond_block="【我和他】\n- x")
        self.assertTrue(s.startswith("CARD\n\n【我和他】\n- x\n\n# 这一步：打字"))


    def test_background_after_bond_before_protocol(self):
        s = rts.build_typing_system("CARD", sticker_block="", background="# 此刻\n- 我叫他：宝宝")
        self.assertTrue(s.startswith(f"CARD\n\n{rts.TYPING_BACKGROUND_LEAD}\n\n# 此刻\n- 我叫他：宝宝\n\n# 这一步：打字"))
        self.assertEqual(
            rts.build_typing_system("CARD", sticker_block=""),
            rts.build_typing_system("CARD", sticker_block="", background="  "),
        )

    def test_typing_background_has_no_tools(self):
        import tenshi_config

        tpl = tenshi_config.SYSTEM_PROMPT_DYNAMIC_TEMPLATE
        self.assertTrue(tpl.startswith("# 此刻"))
        self.assertNotIn("note_patch", tpl)


class TemplateTest(unittest.TestCase):
    def test_dynamic_template_has_bond_slot_first(self):
        import tenshi_config

        tpl = tenshi_config.SYSTEM_PROMPT_DYNAMIC_TEMPLATE
        self.assertIn("{bond_state}", tpl)
        self.assertLess(tpl.index("{bond_state}"), tpl.index("{private_notes_injected}"))

        self.assertNotIn("{bond_state}", tenshi_config.SYSTEM_PROMPT_STATIC_CORE)


if __name__ == "__main__":
    unittest.main()
