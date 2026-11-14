# SPDX-License-Identifier: AGPL-3.0-only



















from __future__ import annotations

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import grok_modules
import tenshi_config

REAL_400 = (
    "Error code: 400 - {'error': {'message': \"Function tools with reasoning_effort are "
    "not supported for gpt-6-luna in /v1/chat/completions. To use function tools, use "
    "/v1/responses or set reasoning_effort to 'none'.\", 'type': 'invalid_request_error', "
    "'param': 'reasoning_effort', 'code': None}}"
)

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get current time",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


class _FakeError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _chat_completion(content: str = "", tool_calls=None):
    msg = types.SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class _FakeCompletions:
    """模拟真实上游：chat/completions 下 tools 与非空 reasoning_effort 不共存。"""

    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        self._owner.chat_calls.append(kwargs)
        if self._owner.chat_error is not None:
            raise self._owner.chat_error
        if kwargs.get("tools") and kwargs.get("reasoning_effort"):
            raise _FakeError(REAL_400, status_code=400)
        return _chat_completion(content="chat-ok")


class _FakeClient:
    def __init__(self, *, chat_error=None):
        self.chat_calls: list[dict] = []
        self.chat_error = chat_error
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(self))


class GptReasoningEffortConflictTest(unittest.TestCase):
    """两条 GPT 调用（主回复 / NSFW 分级）在冲突时的统一行为。"""

    def setUp(self):
        self._orig_client = grok_modules._build_openai_client
        self._orig_cfg = {
            name: getattr(tenshi_config, name, None)
            for name in ("GPT_TOOL_REASONING_EFFORT", "GPT_REASONING_EFFORT")
        }
        self.client = _FakeClient()
        grok_modules._build_openai_client = lambda *a, **k: self.client

    def tearDown(self):
        grok_modules._build_openai_client = self._orig_client
        for name, value in self._orig_cfg.items():
            if value is None:
                if hasattr(tenshi_config, name):
                    delattr(tenshi_config, name)
            else:
                setattr(tenshi_config, name, value)


    def test_tools_call_omits_reasoning_effort_by_default(self):
        tenshi_config.GPT_TOOL_REASONING_EFFORT = ""
        content, tool_calls = grok_modules.call_gpt_with_tools(
            "gpt-6-luna", "sys", [{"role": "user", "content": "hi"}], CHAT_TOOLS
        )

        self.assertEqual(len(self.client.chat_calls), 1, "默认配置不该有第二次请求")
        kw = self.client.chat_calls[0]
        self.assertNotIn("reasoning_effort", kw, "带 tools 的调用默认不带思考强度")
        self.assertEqual(kw["tools"], CHAT_TOOLS)
        self.assertEqual(kw["tool_choice"], "auto")
        self.assertFalse(kw["stream"])
        self.assertEqual(content, "chat-ok")
        self.assertEqual(tool_calls, [])


    def test_tools_call_drops_effort_when_rejected(self):
        tenshi_config.GPT_TOOL_REASONING_EFFORT = "xhigh"
        content, _ = grok_modules.call_gpt_with_tools(
            "gpt-6-luna", "sys", [{"role": "user", "content": "hi"}], CHAT_TOOLS
        )

        self.assertEqual(content, "chat-ok")
        self.assertEqual(
            [kw.get("reasoning_effort") for kw in self.client.chat_calls], ["xhigh", None]
        )
        self.assertNotIn("reasoning_effort", self.client.chat_calls[1])
        self.assertEqual(self.client.chat_calls[1]["tools"], CHAT_TOOLS, "重试仍要带 tools")


    def test_nsfw_routing_keeps_effort_then_drops_on_conflict(self):
        tenshi_config.GPT_REASONING_EFFORT = "xhigh"

        content, extra = grok_modules.call_gpt("gpt-6-luna", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(content, "chat-ok")
        self.assertEqual(extra, "")
        self.assertEqual([kw.get("reasoning_effort") for kw in self.client.chat_calls], ["xhigh"])
        self.assertNotIn("tools", self.client.chat_calls[0])


        self.client.chat_calls.clear()

        class _FailOnceCompletions(_FakeCompletions):
            def create(self, **kwargs):
                self._owner.chat_calls.append(kwargs)
                if kwargs.get("reasoning_effort"):
                    raise _FakeError(REAL_400, status_code=400)
                return _chat_completion(content="routed-ok")

        self.client.chat_calls.clear()
        self.client.chat = types.SimpleNamespace(completions=_FailOnceCompletions(self.client))
        content, _ = grok_modules.call_gpt("gpt-6-luna", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(content, "routed-ok")
        self.assertEqual(
            [kw.get("reasoning_effort") for kw in self.client.chat_calls], ["xhigh", None]
        )


    def test_unrelated_error_is_not_swallowed(self):
        tenshi_config.GPT_TOOL_REASONING_EFFORT = ""
        self.client.chat_error = _FakeError(
            "Error code: 429 - Rate limit reached for gpt-6-luna", status_code=429
        )
        with self.assertRaises(Exception):
            grok_modules.call_gpt_with_tools(
                "gpt-6-luna", "sys", [{"role": "user", "content": "hi"}], CHAT_TOOLS
            )
        self.assertEqual(len(self.client.chat_calls), 1, "非 reasoning_effort 错误不该重试")

    def test_drop_effort_does_not_mask_second_failure(self):
        """去掉参数后仍失败时，异常照常抛出（不能把失败当成功）。"""
        tenshi_config.GPT_TOOL_REASONING_EFFORT = "xhigh"

        class _AlwaysFailCompletions(_FakeCompletions):
            def create(self, **kwargs):
                self._owner.chat_calls.append(kwargs)
                raise _FakeError(REAL_400, status_code=400)

        self.client.chat = types.SimpleNamespace(completions=_AlwaysFailCompletions(self.client))
        with self.assertRaises(Exception):
            grok_modules.call_gpt_with_tools(
                "gpt-6-luna", "sys", [{"role": "user", "content": "hi"}], CHAT_TOOLS
            )
        self.assertEqual(len(self.client.chat_calls), 2, "先带参数一次、再去掉参数一次")


    def test_blank_and_none_values_mean_no_effort(self):
        for raw in ("", "  ", "none", "omit", "OFF"):
            tenshi_config.GPT_TOOL_REASONING_EFFORT = raw
            self.assertIsNone(grok_modules._gpt_tool_reasoning_effort(), raw)
        tenshi_config.GPT_TOOL_REASONING_EFFORT = "low"
        self.assertEqual(grok_modules._gpt_tool_reasoning_effort(), "low")

    def test_tools_are_still_sent_when_list_is_empty(self):
        tenshi_config.GPT_TOOL_REASONING_EFFORT = ""
        grok_modules.call_gpt_with_tools("gpt-6-luna", "sys", [{"role": "user", "content": "hi"}], [])
        kw = self.client.chat_calls[0]
        self.assertNotIn("tools", kw)
        self.assertNotIn("tool_choice", kw)


if __name__ == "__main__":
    unittest.main()
