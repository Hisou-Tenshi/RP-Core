# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
import importlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import tenshi_config
from config import providers
import gemini_dual
import llm_compat
import llm_routing
from _rp_responses import ResponsesChatClient, chunks, completion, request_options


class FirstPriorityRoutesTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CODEYU_BASE_URL": "https://old-one.example",
                "CLAUDE_CODEYU_BASE_API_KEY": "fixture-base",
                "CLAUDE_CODEYU_PRIME_API_KEY": "fixture-prime",
                "CLAUDE_CODEYU_MAX_API_KEY": "fixture-max",
                "CLAUDE_CODERPLAN_API_KEY": "fixture-claude-two",
                "CLAUDE_TOTOKENS_API_KEY": "fixture-claude-three",
                "GEMINI_CODEYU_API_KEY": "fixture-gemini-one",
                "GEMINI_CODERPLAN_API_KEY": "fixture-gemini-two",
                "GEMINI_TOTOKENS_API_KEY": "fixture-gemini-three",
                "GEMINI_API_KEY": "fixture-gemini-official",
                "GPT_CODEYU_API_KEY": "fixture-gpt-one",
                "GPT_CODERPLAN_API_KEY": "fixture-gpt-two",
                "GPT_TOTOKENS_API_KEY": "fixture-gpt-three",
                "GPT_API_KEY": "fixture-gpt-official",
                "GROK_CODEYU_API_KEY": "fixture-grok-codeyu",
                "GROK_CODERPLAN_API_KEY": "fixture-grok-one",
                "GROK_TOTOKENS_API_KEY": "fixture-grok-two",
                "GROK_API_KEY": "fixture-grok-official",
                "OPENROUTER_API_KEY": "fixture-openrouter",
            },
        )
        self.environment.start()
        mod = importlib.reload(providers)
        self.config = mock.patch.multiple(
            tenshi_config, create=True, **{name: getattr(mod, name) for name in mod.__all__}
        )
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.environment.stop()
        importlib.reload(providers)

    def test_claude_totokens_precedes_all_codeyu_tiers_including_fable(self):
        self.assertEqual(
            [
                p["name"]
                for p in llm_routing.build_claude_proxy_configs_for_model("claude-opus-5-5")
            ],
            ["To-Tokens", "Codeyu Base", "Codeyu Prime", "Codeyu Max", "CoderPlan"],
        )
        self.assertEqual(
            [p["name"] for p in llm_routing.build_claude_proxy_configs_for_model("claude-fable-5")],
            ["To-Tokens", "Codeyu Prime", "Codeyu Max", "CoderPlan"],
        )

    def test_gpt_gemini_grok_priorities_use_site_names(self):
        self.assertEqual(
            [
                p["name"]
                for p in llm_routing.build_gpt_paths("official", "https://official.example/v1")
            ][:3],
            ["To-Tokens", "Codeyu", "CoderPlan"],
        )
        self.assertEqual(
            [p["name"] for p in llm_routing.build_gemini_paths("official")][:3],
            ["To-Tokens", "Codeyu", "CoderPlan"],
        )
        self.assertEqual(
            [
                p["name"]
                for p in llm_routing.build_grok_paths("official", "https://official.example/v1")
            ][:3],
            ["To-Tokens", "Codeyu", "CoderPlan"],
        )

    def test_gemini_dual_auxiliary_client_also_uses_totokens_first(self):
        with mock.patch.object(gemini_dual.genai, "Client", side_effect=lambda **kwargs: kwargs):
            client = gemini_dual.build_gemini_dual_client()
        self.assertEqual(client.path_names[:3], ["To-Tokens", "Codeyu", "CoderPlan"])

    def test_probe_uses_the_same_first_priority_paths(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "new_proxy_probe", root / "scripts/probe_provider_paths.py"
        )
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        targets = probe.collect_targets(["claude", "gpt", "gemini", "grok"])
        for provider, name in [
            ("claude", "To-Tokens"),
            ("gpt", "To-Tokens"),
            ("gemini", "To-Tokens"),
            ("grok", "To-Tokens"),
        ]:
            self.assertEqual(next(t["name"] for t in targets if t["provider"] == provider), name)

    def test_new_gpt_grok_clients_use_responses_but_old_clients_remain_chat(self):
        raw = SimpleNamespace()
        with (
            mock.patch.object(llm_compat, "OpenAI", return_value=raw),
            mock.patch.object(llm_compat, "build_sdk_timeout", return_value=10),
        ):
            for p in (tenshi_config.GPT_TOTOKENS, tenshi_config.GROK_TOTOKENS):
                client = llm_compat.build_openai_compat_client(p["api_key"], p["base_url"])
                self.assertIsInstance(client, ResponsesChatClient)
            self.assertIs(
                llm_compat.build_openai_compat_client(
                    "fixture-gpt-one", "https://old-one.example/v1"
                ),
                raw,
            )

    def test_missing_new_keys_keep_old_routes_and_credentials(self):
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_TOTOKENS_API_KEY": "",
                "GEMINI_TOTOKENS_API_KEY": "",
                "GPT_TOTOKENS_API_KEY": "",
                "GROK_TOTOKENS_API_KEY": "",
            },
        ):
            mod = importlib.reload(providers)
            self.assertEqual(mod.GPT_PROXIES[0]["name"], "Codeyu")
            self.assertEqual(mod.GEMINI_PROXIES[0]["name"], "Codeyu")
            self.assertEqual(mod.GROK_PROXIES[0]["name"], "Codeyu")
            self.assertIsNone(mod.CLAUDE_TOTOKENS)


def response(**extra):
    return {
        "id": "response-fixture",
        "created_at": 1,
        "status": "completed",
        "model": "fixture-model",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "reply"}]}],
        **extra,
    }


class ResponsesCompatibilityTest(unittest.TestCase):
    def test_history_tools_options_and_input_images_are_preserved(self):
        kwargs = {
            "model": "fixture-model",
            "messages": [
                {"role": "system", "content": "unchanged instructions"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-original",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-original",
                    "content": "private fixture tool result",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
            "reasoning_effort": "high",
            "max_tokens": 123,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            },
        }
        original = copy.deepcopy(kwargs)
        got = request_options(kwargs)
        self.assertEqual(kwargs, original)
        self.assertIs(got["store"], False)
        self.assertEqual(got["input"][0]["content"], "unchanged instructions")
        self.assertEqual(got["input"][1]["content"][1]["type"], "input_image")
        self.assertEqual(got["input"][2]["call_id"], got["input"][3]["call_id"])
        self.assertEqual(got["input"][3]["output"], "private fixture tool result")
        self.assertEqual(got["reasoning"], {"effort": "high"})
        self.assertEqual(got["max_output_tokens"], 123)
        self.assertEqual(got["tool_choice"], {"type": "function", "name": "lookup"})
        self.assertEqual(got["text"]["format"]["name"], "answer")

    def test_completion_tools_usage_and_length_reason(self):
        data = response(
            output=[
                {
                    "type": "function_call",
                    "call_id": "call-original",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ],
            usage={
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
                "input_tokens_details": {"cached_tokens": 7},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        )
        got = completion(data, "fixture-model")
        self.assertEqual(got.choices[0].finish_reason, "tool_calls")
        self.assertEqual(got.choices[0].message.tool_calls[0].id, "call-original")
        self.assertEqual(got.usage.prompt_tokens_details.cached_tokens, 7)
        self.assertEqual(got.usage.completion_tokens_details.reasoning_tokens, 2)
        self.assertEqual(
            completion(
                response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
                "fixture",
            )
            .choices[0]
            .finish_reason,
            "length",
        )
        with self.assertRaises(RuntimeError):
            completion(response(status="failed"), "fixture")

    def test_stream_text_tools_finish_and_close(self):
        class Stream:
            closed = False

            def __iter__(self):
                return iter(
                    [
                        {"type": "response.output_text.delta", "delta": "hello"},
                        {
                            "type": "response.output_item.added",
                            "output_index": 2,
                            "item": {
                                "type": "function_call",
                                "call_id": "original-id",
                                "name": "lookup",
                                "arguments": "",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": 2,
                            "delta": "{}",
                        },
                        {"type": "response.completed", "response": response()},
                    ]
                )

            def close(self):
                self.closed = True

        stream = Stream()
        got = list(chunks(stream, "fixture-model"))
        self.assertEqual(got[0].choices[0].delta.content, "hello")
        self.assertEqual(got[1].choices[0].delta.tool_calls[0].id, "original-id")
        self.assertEqual(got[2].choices[0].delta.tool_calls[0].function.arguments, "{}")
        self.assertEqual(got[-1].choices[0].finish_reason, "tool_calls")
        self.assertTrue(stream.closed)

    def test_truncated_stream_errors_and_closes(self):
        stream = mock.Mock()
        stream.__iter__ = lambda self: iter(
            [{"type": "response.output_text.delta", "delta": "partial"}]
        )
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            list(chunks(stream, "fixture-model"))
        stream.close.assert_called_once()

    def test_actual_client_boundary_calls_responses_instead_of_chat(self):
        sdk = SimpleNamespace(
            responses=SimpleNamespace(create=mock.Mock(return_value=response())), close=mock.Mock()
        )
        client = ResponsesChatClient(sdk)
        got = client.chat.completions.create(
            model="fixture-model", messages=[{"role": "user", "content": "fixture"}], stream=False
        )
        self.assertEqual(got.choices[0].message.content, "reply")
        self.assertFalse(sdk.responses.create.call_args.kwargs["store"])
        client.close()
        sdk.close.assert_called_once()

    def test_unsupported_options_are_rejected_before_request(self):
        with self.assertRaises(ValueError):
            request_options({"model": "fixture", "messages": [], "unsupported_flag": True})
