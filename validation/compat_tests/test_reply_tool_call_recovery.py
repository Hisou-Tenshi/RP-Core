# SPDX-License-Identifier: AGPL-3.0-only














from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")

IMPORT_ERROR: Exception | None = None
try:
    import tenshi_reply
except Exception as e:
    IMPORT_ERROR = e



LEAK_XML = (
    '<function_calls>\n'
    '<invoke name="note_patch">\n'
    '<parameter name="operations">[{"op": "add", "text": "09-13 08:31 他醒了。"}]</parameter>\n'
    '<parameter name="read_n">5</parameter>\n'
    '</invoke>\n'
    '<invoke name="schedule_perception">\n'
    '<parameter name="entries" string="false">[{"trigger_at": "2026-09-13 09:15", '
    '"note": "雨该落地了。"}]</parameter>\n'
    '</invoke>\n'
    '</function_calls>\n'
    '\n'
    '早。\n'
)

FINAL_REPLY = "早。\n\n*帽檐往上推了推。*"


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class OpenAiCompatTextToolCallRecoveryTest(unittest.TestCase):
    """模型把工具调用写成正文时，必须就地抢救成真实执行，而不是当成回复。"""

    def setUp(self):
        self.executed: list[tuple[str, dict]] = []
        self._orig_dispatch = tenshi_reply._dispatch_tenshi_tool
        self._orig_trace = tenshi_reply.append_llm_trace

        def _fake_dispatch(name, args, state):
            self.executed.append((name, args))
            return f"[TOOL RESULT: {name}] ok"

        tenshi_reply._dispatch_tenshi_tool = _fake_dispatch
        tenshi_reply.append_llm_trace = lambda *a, **k: None

    def tearDown(self):
        tenshi_reply._dispatch_tenshi_tool = self._orig_dispatch
        tenshi_reply.append_llm_trace = self._orig_trace

    def _run(self, call_with_tools):
        return tenshi_reply._run_openai_compat_tools_loop(
            provider="deepseek",
            call_with_tools=call_with_tools,
            model_name="deepseek-flash",
            system_prompt="system",
            messages_payload=[{"role": "user", "content": "早～"}],
            tools=[
                {"type": "function", "function": {"name": name}}
                for name in ("note_patch", "schedule_perception", "read_note")
            ],
        )

    def test_recovers_executes_and_strips(self):
        rounds: list[dict] = []

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:

                return LEAK_XML, [], {"role": "assistant", "content": LEAK_XML}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, had_tools = self._run(call_with_tools)


        self.assertEqual([name for name, _ in self.executed], ["note_patch", "schedule_perception"])
        note_args = self.executed[0][1]
        self.assertEqual(note_args["read_n"], 5)
        self.assertEqual(note_args["operations"][0]["op"], "add")
        sched_args = self.executed[1][1]
        self.assertEqual(sched_args["entries"][0]["trigger_at"], "2026-09-13 09:15")


        self.assertEqual(text, FINAL_REPLY)
        self.assertNotIn("<function_calls>", text)
        self.assertIn("<function_calls>", LEAK_XML)
        self.assertTrue(had_tools)

    def test_history_is_clean_and_tool_ids_match(self):
        rounds: list[dict] = []

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return LEAK_XML, [], {"role": "assistant", "content": LEAK_XML}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        self._run(call_with_tools)
        self.assertEqual(len(rounds), 2)

        second_messages = rounds[1]["messages"]

        self.assertNotIn("<function_calls>", json.dumps(second_messages, ensure_ascii=False))

        assistant_msgs = [m for m in second_messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in second_messages if m.get("role") == "tool"]
        self.assertTrue(assistant_msgs)
        recovered = assistant_msgs[-1].get("tool_calls")
        self.assertTrue(recovered, "抢救出来的调用必须以结构化 tool_calls 回灌")
        self.assertEqual(len(tool_msgs), len(recovered))


        self.assertEqual(
            [m["tool_call_id"] for m in tool_msgs],
            [tc["id"] for tc in recovered],
        )

    def test_native_tool_calls_still_work_unchanged(self):
        """对照组：结构化 tool_calls 路径不能因为本次修复而改变。"""
        rounds: list[dict] = []
        native = [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "read_note", "arguments": '{"n": 3}'},
            }
        ]

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return "", native, {"role": "assistant", "content": "", "tool_calls": native}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, had_tools = self._run(call_with_tools)
        self.assertEqual([name for name, _ in self.executed], ["read_note"])
        self.assertEqual(self.executed[0][1], {"n": 3})
        self.assertEqual(text, FINAL_REPLY)
        self.assertTrue(had_tools)

    def test_native_call_without_id_is_not_dropped(self):
        """回归：旧 normalize_tool_calls 会把缺 id 的调用整条丢掉，工具凭空消失。"""
        rounds: list[dict] = []
        native = [{"function": {"name": "read_note", "arguments": '{"n": 4}'}}]

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return "", native, {"role": "assistant", "content": "", "tool_calls": native}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, had_tools = self._run(call_with_tools)
        self.assertEqual([name for name, _ in self.executed], ["read_note"])
        self.assertEqual(self.executed[0][1], {"n": 4})
        self.assertEqual(text, FINAL_REPLY)
        self.assertTrue(had_tools)

    def test_native_call_with_broken_args_is_not_executed(self):
        """回归：旧代码在参数解析失败时会用空参数把工具跑一遍。"""
        rounds: list[dict] = []
        native = [
            {"id": "call_x", "function": {"name": "schedule_perception", "arguments": "{oops"}}
        ]

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return "", native, {"role": "assistant", "content": "", "tool_calls": native}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, _had = self._run(call_with_tools)
        self.assertEqual(self.executed, [], "参数坏掉时绝不能执行工具")
        self.assertEqual(text, FINAL_REPLY)

        tool_msgs = [m for m in rounds[1]["messages"] if m.get("role") == "tool"]
        self.assertTrue(tool_msgs)
        self.assertIn("无法解析", tool_msgs[0]["content"])

    def test_alternate_text_format_is_recovered(self):
        """不局限于 legacy XML：<tool_call>{json}</tool_call> 同样要抢救执行。"""
        rounds: list[dict] = []
        payload = '<tool_call>{"name": "read_note", "arguments": {"n": 8}}</tool_call>\n早。'

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return payload, [], {"role": "assistant", "content": payload}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, had_tools = self._run(call_with_tools)
        self.assertEqual([name for name, _ in self.executed], ["read_note"])
        self.assertEqual(self.executed[0][1], {"n": 8})
        self.assertEqual(text, FINAL_REPLY)
        self.assertTrue(had_tools)
        self.assertNotIn("<tool_call>", json.dumps(rounds[1]["messages"], ensure_ascii=False))

    def test_prose_json_is_not_executed(self):
        """正文里只是「提到」一段 JSON，绝不能被执行。"""
        prose = '我看了下结构，大概是 {"name": "read_note", "arguments": {"n": 1}} 这种。'
        rounds: list[dict] = []

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            return prose, [], {"role": "assistant", "content": prose}

        text, _thinking, had_tools = self._run(call_with_tools)
        self.assertEqual(self.executed, [])
        self.assertEqual(text, prose)
        self.assertFalse(had_tools)

    def test_native_plus_markup_never_leaks(self):
        """既给结构化调用、又在正文贴 markup 时，正文必须被剥干净且消息要重建。"""
        rounds: list[dict] = []
        native = [
            {"id": "call_1", "function": {"name": "read_note", "arguments": '{"n": 1}'}}
        ]
        content = '早。\n<invoke name="read_note"><parameter name="n">1</parameter></invoke>'

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return content, native, {"role": "assistant", "content": content, "tool_calls": native}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, _had = self._run(call_with_tools)
        self.assertEqual(text, FINAL_REPLY)
        for message in rounds[1]["messages"]:
            self.assertNotIn("<invoke", json.dumps(message, ensure_ascii=False))

    def test_plain_reply_without_markup_is_untouched(self):
        def call_with_tools(**kwargs):
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, had_tools = self._run(call_with_tools)
        self.assertEqual(text, FINAL_REPLY)
        self.assertEqual(self.executed, [])
        self.assertFalse(had_tools)

    def test_recovered_block_keeps_visible_reply_in_history(self):
        """剥离 markup 后剩下的正文必须保留（不能连正文一起丢掉）。"""
        with_reply = (
            '<function_calls>\n'
            '<invoke name="read_note"><parameter name="n">3</parameter></invoke>\n'
            '</function_calls>\n'
            '\n'
            '早。'
        )
        rounds: list[dict] = []

        def call_with_tools(**kwargs):
            rounds.append(kwargs)
            if len(rounds) == 1:
                return with_reply, [], {"role": "assistant", "content": with_reply}
            return FINAL_REPLY, [], {"role": "assistant", "content": FINAL_REPLY}

        text, _thinking, _had = self._run(call_with_tools)
        self.assertEqual(text, FINAL_REPLY)
        assistant_msgs = [m for m in rounds[1]["messages"] if m.get("role") == "assistant"]
        self.assertEqual(assistant_msgs[-1]["content"], "早。")

    def test_stray_markup_is_never_emitted(self):
        """只有残缺标记、正文永远修不好时，也绝不允许把标签发给用户。"""
        broken = "<function_calls>\n<invoke name=\"note_patch\">"

        def call_with_tools(**kwargs):
            return broken, [], {"role": "assistant", "content": broken}

        text, _thinking, _had = self._run(call_with_tools)
        self.assertNotIn("<function_calls>", text)
        self.assertNotIn("<invoke", text)
        self.assertEqual(text.strip(), "")


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class ClaudeTextToolCallRecoveryTest(unittest.TestCase):
    """代理把 Claude 的 tool_use 退化成正文时，同样要抢救执行而不是外发 markup。

    Claude 循环只在 `stop_reason == "tool_use"` 时进工具分支；退化后 `stop_reason`
    是 end_turn，旧代码会把整段 XML 当最终回复返回。
    """

    def setUp(self):
        self.executed: list[tuple[str, dict]] = []
        self._orig_dispatch = tenshi_reply._dispatch_tenshi_tool
        self._orig_trace = tenshi_reply.append_llm_trace
        self._orig_proxies = tenshi_reply.build_claude_proxy_configs_for_model
        self._orig_key = tenshi_reply.CLAUDE_API_KEY
        self._orig_client = tenshi_reply.anthropic_client


        self._orig_or_key = tenshi_reply.OPENROUTER_API_KEY
        tenshi_reply.OPENROUTER_API_KEY = None


        import provider_health as _ph

        self._ph = _ph
        self._orig_filter_paths = _ph.filter_paths
        self._orig_path_state = _ph.path_state
        self._orig_model_state = getattr(_ph, "model_path_state", None)
        _ph.filter_paths = lambda category, paths, **k: list(paths)
        _ph.path_state = lambda *a, **k: None
        _ph.model_path_state = lambda *a, **k: None

        def _fake_dispatch(name, args, state):
            self.executed.append((name, args))
            return f"[TOOL RESULT: {name}] ok"

        tenshi_reply._dispatch_tenshi_tool = _fake_dispatch
        tenshi_reply.append_llm_trace = lambda *a, **k: None
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: []

    def tearDown(self):
        tenshi_reply._dispatch_tenshi_tool = self._orig_dispatch
        tenshi_reply.append_llm_trace = self._orig_trace
        tenshi_reply.build_claude_proxy_configs_for_model = self._orig_proxies
        tenshi_reply.CLAUDE_API_KEY = self._orig_key
        tenshi_reply.anthropic_client = self._orig_client
        tenshi_reply.OPENROUTER_API_KEY = self._orig_or_key
        self._ph.filter_paths = self._orig_filter_paths
        self._ph.path_state = self._orig_path_state
        if self._orig_model_state is not None:
            self._ph.model_path_state = self._orig_model_state



    class _TextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

        def model_dump(self):
            return {"type": "text", "text": self.text}

    class _Response:
        def __init__(self, text):
            self.stop_reason = "end_turn"
            self.content = [ClaudeTextToolCallRecoveryTest._TextBlock(text)]

    class _Stream:
        def __init__(self, response):
            self._response = response
            self.text_stream = [b.text for b in response.content]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return self._response

    class _FakeClient:
        def __init__(self, script):
            self.script = list(script)
            self.calls: list[dict] = []
            self.messages = self

        def _next(self):
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return ClaudeTextToolCallRecoveryTest._Response(self._next())

        def stream(self, **kwargs):
            self.calls.append(kwargs)
            return ClaudeTextToolCallRecoveryTest._Stream(
                ClaudeTextToolCallRecoveryTest._Response(self._next())
            )

    def _call(self, script):
        client = self._FakeClient(script)
        tenshi_reply.CLAUDE_API_KEY = "dummy-for-tests"
        tenshi_reply.anthropic_client = client
        result = tenshi_reply.call_claude(
            "claude-sonnet-5",
            "system",
            [{"role": "user", "content": "早～"}],
            tools=[{"name": "read_note", "input_schema": {"type": "object", "properties": {}}}],
        )
        return result, client

    def test_recovers_executes_and_rewrites_reply(self):
        leaked = (
            '<function_calls><invoke name="read_note">'
            '<parameter name="n">5</parameter></invoke></function_calls>\n\n早。'
        )
        (text, _thinking, had_tools), client = self._call([leaked, FINAL_REPLY])

        self.assertEqual([name for name, _ in self.executed], ["read_note"])
        self.assertEqual(self.executed[0][1], {"n": 5})
        self.assertEqual(text, FINAL_REPLY)
        self.assertNotIn("<function_calls>", text)
        self.assertTrue(had_tools)

        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("tools", client.calls[1])
        self.assertIn("read_note", json.dumps(client.calls[1]["messages"], ensure_ascii=False))

    def test_native_tool_use_path_untouched(self):
        """对照组：正常 end_turn 且无 markup 时行为不变。"""
        (text, _thinking, had_tools), client = self._call([FINAL_REPLY])
        self.assertEqual(text, FINAL_REPLY)
        self.assertEqual(self.executed, [])
        self.assertFalse(had_tools)
        self.assertEqual(len(client.calls), 1)

    def test_unrecoverable_markup_is_never_forwarded(self):
        broken = "<function_calls>早。"
        (text, _thinking, _had), _client = self._call([broken])
        self.assertEqual(text, "早。")
        self.assertEqual(self.executed, [])


if __name__ == "__main__":
    unittest.main()
