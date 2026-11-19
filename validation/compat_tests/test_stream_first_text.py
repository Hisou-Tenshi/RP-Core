# SPDX-License-Identifier: AGPL-3.0-only
"""Regressions for a visible first token, typing failover, and safe queue retries."""
from contextlib import ExitStack
import asyncio
import json
import threading
import time
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from _rp_cancellation import mark_text_progress
from llm_compat import LlmDeadlineExceeded, call_with_deadline, chat_completion
import telegram_tenshi_logger as ingress
import tenshi_reply as reply


def test_first_text_releases_nested_timers():
    def consume():
        mark_text_progress("首")
        time.sleep(0.2)
        return "首字之后继续生成"

    def nested():
        return call_with_deadline(consume, timeout_sec=0.06, first_text=True)

    assert call_with_deadline(
        nested, timeout_sec=0.1, first_text=True, hard_timeout_sec=2
    ) == "首字之后继续生成"


def test_empty_chunks_keep_first_text_deadline():
    stale_callback = mock.Mock()
    late = threading.Event()

    def consume():
        mark_text_progress(" \n")
        time.sleep(0.2)
        try:
            mark_text_progress("迟到的正文")
            stale_callback()
        finally:
            late.set()

    with pytest.raises(LlmDeadlineExceeded):
        call_with_deadline(consume, timeout_sec=0.06, first_text=True)
    assert late.wait(1)
    stale_callback.assert_not_called()


def test_first_text_keeps_hard_deadline():
    def consume():
        mark_text_progress("首")
        time.sleep(0.3)

    with pytest.raises(LlmDeadlineExceeded):
        call_with_deadline(consume, timeout_sec=0.12, first_text=True, hard_timeout_sec=0.18)


def test_completion_assembles_indexed_tool_arguments():
    def chunk(tool_calls=(), finish=None):
        return NS(choices=[NS(delta=NS(tool_calls=tool_calls), finish_reason=finish)])

    stream = iter([
        chunk([NS(index=0, id="call_1", function=NS(name="lookup", arguments='{"id":'))]),
        NS(choices=[]),
        chunk([NS(index=0, id=None, function=NS(name=None, arguments="1}"))]),
        chunk(finish="tool_calls"),
    ])
    create = mock.Mock(return_value=stream)
    result = chat_completion(create, model="synthetic")
    create.assert_called_once_with(model="synthetic", stream=True)
    assert result.choices[0].message.tool_calls == [
        {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"id":1}'}}
    ]


def test_explicit_stream_unsupported_falls_back_before_output():
    class Unsupported(Exception):
        status_code = 400

    create = mock.Mock(side_effect=[Unsupported("Streaming is not supported"), NS(choices=[])])
    assert chat_completion(create, model="synthetic").choices == []
    assert [c.kwargs["stream"] for c in create.call_args_list] == [True, False]


def test_gemini_stream_excludes_thought_and_keeps_tool_parts():
    thought = NS(thought=True, text="private thought", function_call=None)
    answer = NS(thought=False, text="正文", function_call=None)
    tool = NS(text=None, function_call=NS(name="lookup"))
    chunk = NS(candidates=[NS(content=NS(parts=[thought, answer, tool]))])
    chat = NS(send_message_stream=mock.Mock(return_value=iter([chunk])), send_message=mock.Mock())
    callback = mock.Mock()
    with mock.patch.object(reply, "append_llm_trace"):
        assert reply._gemini_chat_reply(chat, [], label="gemini", stream_callback=callback) == ("正文", [tool])
    callback.assert_called_once_with("正文")
    chat.send_message.assert_not_called()


def test_typing_budget_reaches_other_provider_after_three_failed_gemini_paths():
    model_providers = {"flash": "gemini", "pro": "gemini", "backup": "deepseek"}
    clock = NS(now=0.0)
    attempts = []

    def run(fn, *, timeout_sec, first_text, **kwargs):
        assert first_text
        return fn(timeout_sec)

    def failure(timeout):
        attempts.append(timeout)
        clock.now += timeout
        raise LlmDeadlineExceeded("synthetic timeout")

    def _plain(provider, model, system, messages, max_sec=None):
        if provider == "gemini":
            return original(provider, model, system, messages, max_sec)
        assert max_sec > 0
        return "回复"

    original = reply._plain_text_call
    paths = [{"name": name} for name in ("Dragon", "CoderPlan", "Official")]
    with ExitStack() as patches:
        for target, name, kwargs in [
            (reply, "time", {"new": NS(monotonic=lambda: clock.now)}),
            (reply, "_llm_deadline", {"return_value": 150}),
            (reply, "reply_deadline_remaining", {"return_value": None}),
            (reply, "reply_deadline_exceeded", {"return_value": False}),
            (reply, "_provider_is_dead_this_round", {"return_value": False}),
            (reply, "_provider_from_model_id", {"side_effect": model_providers.__getitem__}),
            (reply, "_plain_text_call", {"side_effect": _plain}),
            (reply, "call_with_deadline", {"side_effect": lambda fn, **kw: run(lambda sec: failure(sec), **kw)}),
            (reply.provider_health, "filter_paths", {"return_value": paths}),
            (reply.provider_call_stats, "record", {}),
            (reply.tenshi_config, "REPLY_TYPING_TOTAL_TIMEOUT_SEC", {"new": 300}),
            (reply.tenshi_config, "REPLY_TYPING_MAX_MODELS", {"new": 2}),
        ]:
            patches.enter_context(mock.patch.object(target, name, **kwargs))
        assert reply.run_typing_stage(
            model_chain=["flash", "pro", "backup"], typing_system="sys", typing_user="user"
        ) == ("回复", False, "backup")
    assert attempts == [50, 50, 50]


def test_predelivery_exit_retries_without_duplicate_message(tmp_path):
    log = tmp_path / "chat.json"
    process = NS(
        stdout=None, stderr=None, returncode=ingress.REPLY_PREDELIVERY_FAILURE_EXIT,
        wait=mock.AsyncMock(return_value=ingress.REPLY_PREDELIVERY_FAILURE_EXIT),
    )

    async def replay():
        for _ in range(2):
            assert await ingress.process_single_message(
                "synthetic", chat_id=42, user_id=42, msg_id=123, timestamp=100
            ) == "retryable_failure"

    with ExitStack() as patches:
        for name, kwargs in [
            ("Bot", {}), ("LOG_FILE", {"new": str(log)}),
            ("LOCK_FILE", {"new": str(tmp_path / "chat.lock")}),
            ("_notify_reply_failure", {"new_callable": mock.AsyncMock}),
            ("_typing_indicator_loop", {"new_callable": mock.AsyncMock}),
            ("_stream_subprocess_output", {"new_callable": mock.AsyncMock}),
        ]:
            patches.enter_context(mock.patch.object(ingress, name, **kwargs))
        patches.enter_context(mock.patch.object(ingress.tenshi_config, "ADMIN_ID", 42))
        patches.enter_context(mock.patch.object(ingress.asyncio, "create_subprocess_exec", return_value=process))
        asyncio.run(replay())
        ingress._notify_reply_failure.assert_not_called()
    history = json.loads(log.read_text())[0]["chat_history"]
    assert [entry["msg_id"] for entry in history] == ["123"]
    assert not history[0].get("rp_delivery_quarantined")
