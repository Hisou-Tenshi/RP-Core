# SPDX-License-Identifier: AGPL-3.0-only










from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from llm_tool_schema import (
    ToolDeclarationError,
    assert_tool_params_annotated,
    build_gemini_tool_declarations,
    is_error_placeholder_response,
    json_schema_to_gemini_schema,
    should_skip_provider_on_error,
)


class ErrorPlaceholderTest(unittest.TestCase):
    """事故中真正发给用户的那句话，必须被判为错误占位。"""

    def test_captures_the_actual_incident_string(self):
        self.assertTrue(
            is_error_placeholder_response("Error: No API keys configured.")
        )
        self.assertTrue(
            is_error_placeholder_response("  Error: No API keys configured.  ")
        )

    def test_captures_upstream_gateway_placeholder(self):
        """2026-11-12 事故：网关的中文占位提示被当成正常回复发给了用户。"""
        self.assertTrue(
            is_error_placeholder_response(
                "⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、"
                "或模型对当前输入直接结束。请重试或简化输入后再试。"
            )
        )

        self.assertFalse(
            is_error_placeholder_response("上游的事我管不着，你自己看着办。")
        )

        self.assertFalse(
            is_error_placeholder_response("请重试或简化输入后再试。" + "嗯" * 400)
        )

    def test_captures_generic_error_placeholders(self):
        self.assertTrue(is_error_placeholder_response("Error: something went wrong"))
        self.assertTrue(is_error_placeholder_response("Exception: boom"))
        self.assertTrue(is_error_placeholder_response("[TOOL RESULT: note_patch]\nError: x"))
        self.assertTrue(is_error_placeholder_response("No Claude clients available for claude-opus-5"))
        self.assertTrue(is_error_placeholder_response("No API paths configured for gemini"))

    def test_does_not_swallow_real_replies(self):
        self.assertFalse(is_error_placeholder_response(""))
        self.assertFalse(is_error_placeholder_response(None))
        self.assertFalse(is_error_placeholder_response("   "))
        self.assertFalse(
            is_error_placeholder_response("本小姐今天心情不错，才不是特意回你的。")
        )

        self.assertFalse(
            is_error_placeholder_response(
                "刚才那个 error 我看到了，已经记在抽屉里了。总之别担心，"
                "这种小场面还不至于让本小姐失态。"
            )
        )

    def test_long_error_like_prose_is_not_treated_as_placeholder(self):
        long_prose = "Error: " + ("x" * 400)
        self.assertFalse(is_error_placeholder_response(long_prose))

    def test_non_string_input(self):
        self.assertFalse(is_error_placeholder_response(123))
        self.assertFalse(is_error_placeholder_response([{"a": 1}]))


class AnnotatedParamsTest(unittest.TestCase):
    """A 项修复：漏注解必须被启动期自检抓住。"""

    def test_flagged_when_annotation_missing(self):
        def broken(operations, read_n: int = 20):
            """like the 2026-09-10 note_patch"""

        offenders = assert_tool_params_annotated([broken], strict=False)
        self.assertEqual(offenders, ["broken.operations"])

    def test_raises_in_strict_mode(self):
        def broken(operations, read_n: int = 20):
            """like the 2026-09-10 note_patch"""

        with self.assertRaises(ToolDeclarationError):
            assert_tool_params_annotated([broken])

    def test_clean_signature_passes(self):
        def fine(operations: list, read_n: int = 20):
            """ok"""

        self.assertEqual(assert_tool_params_annotated([fine]), [])

    def test_real_note_patch_signature_is_annotated(self):
        """直接读源码断言 note_patch 的 operations 有注解（防止再次被改回去）。"""
        path = os.path.join(SRC, "tenshi_reply.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertRegex(
            source,
            re.compile(r"def note_patch\(\s*operations\s*:\s*[^,)]+", re.MULTILINE),
            "note_patch 的 operations 必须带类型注解，否则 Gemini 全通路会瘫痪",
        )


class GeminiSchemaTest(unittest.TestCase):
    def test_maps_types_and_constraints(self):
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 200},
                "q": {"type": "string"},
            },
            "required": ["n"],
        }
        out = json_schema_to_gemini_schema(schema)
        self.assertEqual(out["type"], "OBJECT")
        self.assertEqual(out["properties"]["n"]["type"], "INTEGER")
        self.assertEqual(out["properties"]["n"]["minimum"], 1)
        self.assertEqual(out["properties"]["n"]["maximum"], 200)
        self.assertEqual(out["properties"]["q"]["type"], "STRING")
        self.assertEqual(out["required"], ["n"])

    def test_maps_nested_array_of_objects_with_enum(self):
        schema = {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["add", "update", "delete"]},
                            "id": {"type": "string"},
                        },
                        "required": ["op"],
                    },
                }
            },
            "required": ["operations"],
        }
        out = json_schema_to_gemini_schema(schema)
        ops = out["properties"]["operations"]
        self.assertEqual(ops["type"], "ARRAY")
        self.assertEqual(ops["items"]["type"], "OBJECT")
        self.assertEqual(
            ops["items"]["properties"]["op"]["enum"], ["add", "update", "delete"]
        )
        self.assertEqual(ops["items"]["required"], ["op"])

    def test_drops_unknown_keys_and_handles_garbage(self):
        out = json_schema_to_gemini_schema({"type": "string", "$schema": "x", "title": "t"})
        self.assertEqual(out, {"type": "STRING"})
        self.assertEqual(
            json_schema_to_gemini_schema(None), {"type": "OBJECT", "properties": {}}
        )

    def test_declarations_include_every_tool_once(self):
        claude_tools = [
            {"name": "a", "description": "d1", "input_schema": {"type": "object", "properties": {}}},
            {"name": "b", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}},
            {"name": "a", "description": "dup", "input_schema": {"type": "object", "properties": {}}},
        ]
        decls = build_gemini_tool_declarations(claude_tools)["function_declarations"]
        self.assertEqual([d["name"] for d in decls], ["a", "b"])
        self.assertEqual(decls[0]["description"], "d1")

    def test_empty_tool_set_raises(self):
        with self.assertRaises(ToolDeclarationError):
            build_gemini_tool_declarations([])


class GeminiToolsRuntimeTest(unittest.TestCase):
    """B 项修复的端到端断言：真实 CLAUDE_TOOLS 能构建出合法 Gemini Tool。

    这里刻意不 import tenshi_reply（它会拉起重型依赖/网络/环境变量），
    而是从源码取出 CLAUDE_TOOLS 字面量过于脆弱，故改用直接构造等价声明的方式
    验证 SDK 侧接受度：若 google-genai 不可用则跳过。
    """

    def test_sdk_accepts_built_declaration(self):
        try:
            from google.genai import types
        except Exception as e:
            self.skipTest(f"google-genai 不可用：{e}")

        claude_tools = [
            {
                "name": "note_patch",
                "description": "Patch your private note entries.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "operations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": ["add", "update", "delete"]},
                                    "id": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                                "required": ["op"],
                            },
                        },
                        "read_n": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["operations"],
                },
            }
        ]
        tool = types.Tool.model_validate(build_gemini_tool_declarations(claude_tools))
        decl = tool.function_declarations[0]
        self.assertEqual(decl.name, "note_patch")
        self.assertEqual(decl.parameters.type, types.Type.OBJECT)
        self.assertEqual(decl.parameters.required, ["operations"])
        self.assertEqual(decl.parameters.properties["operations"].type, types.Type.ARRAY)


class ProviderBreakerHeuristicTest(unittest.TestCase):
    """provider 级熔断的判定：确定性错误该毙，瞬时错误该留。"""

    def test_incident_error_kills_provider(self):
        """2026-09-09 事故原文：google-genai 工具声明解析失败。"""
        exc = RuntimeError(
            "All paths/models failed: Failed to parse the parameter operations of "
            "function note_patch for automatic function calling.Automatic function "
            "calling works best with simpler function signature schema, consider "
            "manually parsing your function declaration for function note_patch."
        )
        skip, reason = should_skip_provider_on_error(exc)
        self.assertTrue(skip, f"事故错误必须熔断，实际 reason={reason}")

    def test_local_valueerror_kills_provider(self):
        skip, _ = should_skip_provider_on_error(ValueError("bad tool schema"))
        self.assertTrue(skip)
        skip, _ = should_skip_provider_on_error(TypeError("unexpected kwarg"))
        self.assertTrue(skip)

    def test_auth_and_bad_request_kill_provider(self):
        for code in (400, 401, 403, 502, 503):
            exc = RuntimeError(f"{code} INVALID_ARGUMENT. bad request")
            exc.code = code
            skip, reason = should_skip_provider_on_error(exc)
            self.assertTrue(skip, f"HTTP {code} 应熔断（reason={reason}）")

    def test_google_genai_style_code_attribute(self):
        class FakeAPIError(Exception):
            def __init__(self, code, msg):
                self.code = code
                super().__init__(msg)

        skip, reason = should_skip_provider_on_error(FakeAPIError(403, "PERMISSION_DENIED"))
        self.assertTrue(skip)
        self.assertIn("403", reason)

    def test_transient_errors_do_not_kill_provider(self):
        cases = [
            "ReadTimeout: request timed out after 300s",
            "APIConnectionError: Connection error.",
            "429 RESOURCE_EXHAUSTED. rate limit exceeded",
            "500 INTERNAL. server error",
            "504 Deadline Exceeded",
            "524 upstream timeout",
        ]
        for text in cases:
            skip, reason = should_skip_provider_on_error(RuntimeError(text))
            self.assertFalse(skip, f"瞬时错误不该熔断：{text}（reason={reason}）")

    def test_model_level_404_does_not_kill_provider(self):
        """404 是模型级问题，链条会自己换下一个模型，不应熔断整个 provider。"""
        skip, _ = should_skip_provider_on_error(
            RuntimeError("404 NOT_FOUND. models/gemini-9.9 is not found")
        )
        self.assertFalse(skip)

    def test_timeout_wins_over_deterministic_marker(self):
        """同时含'超时'与可疑标记时，宁可当瞬时处理（不误毙）。"""
        exc = RuntimeError("Failed to parse the parameter operations ... read timeout")
        skip, reason = should_skip_provider_on_error(exc)
        self.assertFalse(skip)
        self.assertEqual(reason, "transient")

    def test_none_and_empty(self):
        skip, reason = should_skip_provider_on_error(None)
        self.assertFalse(skip)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
