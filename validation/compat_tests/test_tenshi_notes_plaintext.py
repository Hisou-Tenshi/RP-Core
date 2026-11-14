# SPDX-License-Identifier: AGPL-3.0-only



















from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
REPLY_PY = os.path.join(SRC, "tenshi_reply.py")
PROMPTS_PY = os.path.join(SRC, "config", "prompts.py")
SCRIPT_PY = os.path.join(ROOT, "scripts", "tenshi_notes_crypto.py")
REAL_NOTE_JSON = os.path.join(ROOT, "Tenshi.json")


if SRC not in sys.path:
    sys.path.insert(0, SRC)


for _k in ("GEMINI_OFFICIAL_KEY", "CLAUDE_PROXY_KEY", "OPENROUTER_API_KEY"):
    os.environ.setdefault(_k, "dummy-test-key")

NOTE_FUNCS = (
    "_tenshi_note_path",
    "_ensure_tenshi_note_exists",
    "_load_tenshi_note_entries",
    "_save_tenshi_note_entries",
    "format_private_notes_for_system_prompt",
    "read_note",
    "note_patch",
)


TOOL_DESC_READ_NOTE = "Read your own diary (Tenshi.json, one entry per day"
TOOL_DESC_NOTE_PATCH = "Only today's page can be written or edited; past days are read-only."
READ_NOTE_RETURN_TEXT = "这是你的日记 `Tenshi.json`，一天一页，只有今天那页还能写"
PURPOSE_SCRIPT_TEXT = "私密笔记加密/解密的开发工具"
PURPOSE_REPLY_TEXT = "私密笔记加密存储/读取"


def _load_tenshi_reply():
    """import tenshi_reply；依赖缺失时返回 None 由调用方 skip。"""
    try:
        import tenshi_reply

        return tenshi_reply
    except Exception:
        return None


def _load_script_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tenshi_notes_crypto_under_test", SCRIPT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NoteStaticTest(unittest.TestCase):
    """静态：笔记链路去加密 + 模型可见文案未变。"""

    @classmethod
    def setUpClass(cls):
        with open(REPLY_PY, encoding="utf-8") as f:
            cls.source = f.read()
        cls.tree = ast.parse(cls.source)
        cls.func_nodes = {
            n.name: n
            for n in ast.walk(cls.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_note_functions_exist(self):
        for name in NOTE_FUNCS:
            self.assertIn(name, self.func_nodes, f"笔记函数缺失：{name}")

    def test_note_functions_have_no_crypto_remnants(self):
        forbidden = ("fernet", "decrypt", "encrypt", "_tenshi_note_key", "hashlib", "cryptography")
        for name in NOTE_FUNCS:
            node = self.func_nodes[name]
            segment = ast.get_source_segment(self.source, node) or ""
            lowered = segment.lower()
            for token in forbidden:
                self.assertNotIn(
                    token, lowered, f"{name}() 仍含加解密残留：{token}"
                )

    def test_module_has_no_note_key_and_no_fernet_import(self):
        self.assertNotIn("_tenshi_note_key", self.source)
        self.assertNotIn("from cryptography.fernet import Fernet", self.source)

    def test_nsfw_crypto_is_kept(self):

        self.assertIn("encrypt_content_wrapped", self.source)
        self.assertIn("decrypt_content_wrapped", self.source)

    def test_model_visible_tool_descriptions_unchanged(self):


        with open(PROMPTS_PY, encoding="utf-8") as f:
            prompts_source = f.read()
        self.assertIn(TOOL_DESC_READ_NOTE, prompts_source)
        self.assertIn(TOOL_DESC_NOTE_PATCH, prompts_source)

    def test_tenshi_reply_reexports_prompt_tool_schemas(self):
        """旧调用方仍应能从 tenshi_reply 拿到 CLAUDE_TOOLS，且文案一致。"""
        tenshi_reply = _load_tenshi_reply()
        if tenshi_reply is None:
            self.skipTest("tenshi_reply 不可用")
        import config.prompts as prompts

        self.assertIs(tenshi_reply.CLAUDE_TOOLS, prompts.CLAUDE_TOOLS)
        dumped = json.dumps(tenshi_reply.CLAUDE_TOOLS, ensure_ascii=False)
        self.assertIn(TOOL_DESC_READ_NOTE, dumped)
        self.assertIn(TOOL_DESC_NOTE_PATCH, dumped)

    def test_read_note_return_text_unchanged(self):
        self.assertIn(READ_NOTE_RETURN_TEXT, self.source)

    def test_purpose_notes_unchanged(self):
        self.assertIn(PURPOSE_SCRIPT_TEXT, self.source)
        self.assertIn(PURPOSE_REPLY_TEXT, self.source)

    def test_script_has_no_crypto_dependency(self):
        with open(SCRIPT_PY, encoding="utf-8") as f:
            script = f.read()
        for token in ("cryptography", "Fernet", "hashlib", "_tenshi_note_key", "gAAAAA"):
            self.assertNotIn(token, script, f"离线脚本仍含加密残留：{token}")
        self.assertNotIn("decrypt_to_md", script)
        self.assertNotIn("encrypt_from_md", script)
        self.assertIn("export_to_md", script)
        self.assertIn("import_from_md", script)


class NoteDynamicTest(unittest.TestCase):
    """动态：临时目录内读写往返（不触碰真实 Tenshi.json / data/）。"""

    @classmethod
    def setUpClass(cls):
        cls.tr = _load_tenshi_reply()
        if cls.tr is None:
            raise unittest.SkipTest("tenshi_reply 无法 import（依赖缺失），跳过动态测试")

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="tenshi_note_test_")
        self.tmp = self._tmpdir.name
        self._orig_root = self.tr.tenshi_config.PROJECT_ROOT
        self._orig_lock = self.tr.tenshi_config.LOCK_FILE_PATH
        self.tr.tenshi_config.PROJECT_ROOT = self.tmp
        self.tr.tenshi_config.LOCK_FILE_PATH = os.path.join(self.tmp, "note.lock")
        self.note_path = os.path.join(self.tmp, "Tenshi.json")

    def tearDown(self):
        self.tr.tenshi_config.PROJECT_ROOT = self._orig_root
        self.tr.tenshi_config.LOCK_FILE_PATH = self._orig_lock
        self._tmpdir.cleanup()

    def test_save_writes_pretty_plaintext_json(self):
        entries = [
            {
                "id": "2026-10-25",
                "text": "第一条\n含换行 & 中文",
            }
        ]
        err = self.tr._save_tenshi_note_entries(entries)
        self.assertIsNone(err)
        with open(self.note_path, encoding="utf-8") as f:
            raw = f.read()

        self.assertEqual(json.loads(raw), entries)
        self.assertFalse(raw.startswith("gAAAAA"))

        self.assertEqual(raw, json.dumps(entries, ensure_ascii=False, indent=2))
        self.assertIn("\n", raw)
        self.assertFalse(raw.endswith("\n"))

    def test_load_roundtrip(self):
        entries = [
            {"id": "2026-10-25", "text": "A"},
            {"id": "2026-10-24", "text": "B"},
        ]
        self.assertIsNone(self.tr._save_tenshi_note_entries(entries))
        loaded, err = self.tr._load_tenshi_note_entries()
        self.assertIsNone(err)
        self.assertEqual(loaded, entries)

    def test_empty_file_returns_empty_list(self):
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write("")
        loaded, err = self.tr._load_tenshi_note_entries()
        self.assertEqual(loaded, [])
        self.assertIsNone(err)

    def test_missing_file_is_created_as_empty_list(self):
        if os.path.exists(self.note_path):
            os.remove(self.note_path)
        loaded, err = self.tr._load_tenshi_note_entries()
        self.assertEqual(loaded, [])
        self.assertIsNone(err)
        self.assertTrue(os.path.exists(self.note_path))
        with open(self.note_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[]")

    def test_invalid_json_returns_error_string(self):
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        loaded, err = self.tr._load_tenshi_note_entries()
        self.assertEqual(loaded, [])
        self.assertIsInstance(err, str)
        self.assertIn("Failed to load Tenshi.json", err)

    def test_non_list_payload_returns_error_string(self):
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write('{"id": "x"}')
        loaded, err = self.tr._load_tenshi_note_entries()
        self.assertEqual(loaded, [])
        self.assertIsInstance(err, str)

    def test_format_private_notes_returns_string(self):
        self.tr._save_tenshi_note_entries(
            [{"id": "2026-10-25", "text": "最新一条"}]
        )
        out = self.tr.format_private_notes_for_system_prompt(5)
        self.assertIsInstance(out, str)
        self.assertIn("最新一条", out)

    def test_note_patch_and_read_note_end_to_end(self):
        self.tr._save_tenshi_note_entries(
            [{"id": "2026-10-20", "text": "以前的一天"}]
        )
        today = self.tr._diary_day()
        res = self.tr.note_patch([{"op": "append", "text": "今天第一段"}])
        self.assertIn("[TOOL RESULT: note_patch]", res)
        with open(self.note_path, encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after[0]["id"], today, "今天那页应排在最前")
        self.assertEqual(after[0]["text"], "今天第一段")
        self.assertEqual(set(after[0].keys()), {"id", "text"}, "日记条目只有日期 id 与正文，不存时间字段")

        self.tr.note_patch([{"op": "append", "text": "今天第二段"}])
        self.tr.note_patch([{"op": "add", "text": "旧调用 add 也接到今天"}])
        with open(self.note_path, encoding="utf-8") as f:
            page = json.load(f)[0]
        self.assertEqual(page["text"], "今天第一段\n\n今天第二段\n\n旧调用 add 也接到今天")

        self.tr.note_patch([{"op": "write", "text": "整页重写"}])
        with open(self.note_path, encoding="utf-8") as f:
            entries = json.load(f)
        self.assertEqual(entries[0]["text"], "整页重写")
        self.assertEqual(len([e for e in entries if e["id"] == today]), 1, "一天只有一页")


        res = self.tr.note_patch([{"op": "update", "id": "2026-10-20", "text": "篡改"},
                                  {"op": "delete", "id": "2026-10-20"}])
        self.assertIn("以前的日子不能改", res)
        self.assertIn("日记不能删", res)
        with open(self.note_path, encoding="utf-8") as f:
            old = [e for e in json.load(f) if e["id"] == "2026-10-20"][0]
        self.assertEqual(old["text"], "以前的一天")

        read_out = self.tr.read_note(5)
        self.assertIn(READ_NOTE_RETURN_TEXT, read_out)
        self.assertIn("整页重写", read_out)

    def test_diary_day_cutover_at_4am(self):
        from datetime import datetime
        tz = self.tr._note_tz()
        self.assertEqual(self.tr._diary_day(datetime(2026, 10, 26, 3, 59, tzinfo=tz)), "2026-10-25")
        self.assertEqual(self.tr._diary_day(datetime(2026, 10, 26, 4, 0, tzinfo=tz)), "2026-10-26")

    def test_injection_uses_day_labels(self):
        today = self.tr._diary_day()
        self.tr._save_tenshi_note_entries([{"id": today, "text": "今天的"}])
        out = self.tr.format_private_notes_for_system_prompt(2)
        self.assertIn("- 今天\n今天的", out)
        self.assertNotIn("已归档", out)


class RealTenshiJsonTest(unittest.TestCase):
    """真实数据：仓库根 Tenshi.json 已是明文 JSON list。"""

    def test_real_file_is_valid_plaintext_list(self):
        self.assertTrue(os.path.exists(REAL_NOTE_JSON), "仓库根缺少 Tenshi.json")
        with open(REAL_NOTE_JSON, encoding="utf-8") as f:
            raw = f.read()
        self.assertFalse(raw.lstrip().startswith("gAAAAA"), "Tenshi.json 仍是 Fernet 密文")
        self.assertFalse(raw.endswith("\n"), "Tenshi.json 不应有末尾换行（运行时格式）")
        obj = json.loads(raw)
        self.assertIsInstance(obj, list)
        self.assertGreater(len(obj), 0)
        for entry in obj:
            self.assertIsInstance(entry, dict)
            self.assertIsInstance(entry.get("id"), str)
            self.assertEqual(set(entry.keys()), {"id", "text"}, "日记条目只有日期 id 与正文")
            self.assertIsInstance(entry.get("text"), str)
        ids = [e["id"] for e in obj]
        for _id in ids:
            self.assertRegex(_id, r"^\d{4}-\d{2}-\d{2}$", "日记 id 应为日记日 YYYY-MM-DD")
        self.assertEqual(len(ids), len(set(ids)), "一天只能有一页")
        self.assertEqual(ids, sorted(ids, reverse=True), "应按日期倒序")


class OfflineScriptTest(unittest.TestCase):
    """离线工具：无加密依赖，export/import 计数往返一致，`---` 行转义。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_script_module()

    def test_escape_helpers(self):
        self.assertEqual(self.mod._escape_md_body_line("---"), "\\---")
        self.assertEqual(self.mod._unescape_md_body_line("\\---"), "---")
        self.assertEqual(self.mod._escape_md_body_line("普通正文"), "普通正文")
        self.assertEqual(self.mod._escape_md_body_line("--- 不是恰好分隔符"), "--- 不是恰好分隔符")

    def test_export_import_count_roundtrip_with_separator_body_line(self):
        with tempfile.TemporaryDirectory(prefix="tenshi_script_test_") as tmp:
            entries = [
                {
                    "id": "2026-10-25",
                    "text": "第一行\n---\n最后一行",
                },
                {"id": "2026-10-24", "text": "普通正文"},
            ]
            with open(os.path.join(tmp, "Tenshi.json"), "w", encoding="utf-8") as f:
                f.write(json.dumps(entries, ensure_ascii=False))

            mod = self.mod
            old_root = mod._project_root
            mod._project_root = lambda: tmp
            try:
                md_path = os.path.join(tmp, "Tenshi_temp.md")
                mod.export_to_md(md_path)
                with open(md_path, encoding="utf-8") as f:
                    md = f.read()
                self.assertIn("\\---", md, "正文中的 --- 行未被转义")
                mod.import_from_md(md_path)
            finally:
                mod._project_root = old_root

            with open(os.path.join(tmp, "Tenshi.json"), encoding="utf-8") as f:
                roundtripped = json.load(f)

            self.assertEqual(len(roundtripped), len(entries))
            self.assertEqual(
                {e["id"]: e["text"] for e in roundtripped},
                {e["id"]: e["text"] for e in entries},
            )


if __name__ == "__main__":
    unittest.main()
