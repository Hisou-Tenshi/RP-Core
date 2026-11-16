# SPDX-License-Identifier: AGPL-3.0-only
"""审查拒绝（UnsafeResponseError）必须按「决定不说话 / 不发」正常收尾。

2026-11-16 的事故：感知层最终外发前的安全复审抛 UnsafeResponseError，
异常一路冒到进程入口变成 exit 1；host 把它当基础设施失败记 delivery_unknown，
于是每 15 分钟的 scheduled run 永久 quarantine。审查说「不发」是业务结论，
不是故障，正确行为是：不发送、不重试同一段内容、清掉定时触发、退出码 0。

这里直接跑真实的 run_perception / tenshi_reply.main 路径，只把网络、模型、
数据库等外部依赖打桩，让 review_outgoing_reply 真的抛 UnsafeResponseError。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPAT_SRC = REPO_ROOT / "src" / "rp_core" / "compat_runtime" / "src"

UNSAFE_REASON = "用户消息包含工具清单及调用说明，属于工具摘要类内容。"


class _CapturingLogger:
    """替换模块 logger：不落盘、可断言，只收集渲染后的日志行。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _log(self, level: str, msg, *args, **kwargs):
        try:
            rendered = msg % args if args else str(msg)
        except Exception:
            rendered = str(msg)
        self.lines.append(f"{level}: {rendered}")

    def debug(self, msg, *args, **kwargs):
        self._log("debug", msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log("info", msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log("warning", msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log("error", msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._log("critical", msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._log("exception", msg, *args, **kwargs)


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _prepare_sandbox(root: Path) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "character.json",
        {"id": "tenshi", "name": "比那名居天子", "instructions": "合成测试角色，不得外发。"},
    )
    _write_json(root / "Tenshi.json", [])
    _write_json(root / "data" / "knowledge_base.json", {})
    _write_json(root / "data" / "tenshi_log.json", [])
    _write_json(root / "data" / "stickers.json", [])
    _write_json(root / "data" / "bond_state.json", {"settled": []})
    _write_json(root / "data" / "perception_state.json", {})


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    """在临时沙箱里导入 compat_runtime（config 在 import 时读 RP_RUNTIME_ROOT）。"""
    root = tmp_path_factory.mktemp("unsafe-suppression")
    _prepare_sandbox(root)
    patch = pytest.MonkeyPatch()
    patch.setenv("RP_RUNTIME_ROOT", str(root))
    patch.setenv("RP_CHARACTER_PATH", str(root / "character.json"))
    patch.setenv("RP_EXTERNAL_PERSISTENCE", "1")
    patch.setenv("RP_KB_BASE_SHA", hashlib.sha256(b"{}").hexdigest())
    patch.setenv("RP_SHADOW", "1")
    patch.setenv("RP_UNIT_TESTS", "1")
    patch.setenv("ADMIN_ID", "1")
    sys.path.insert(0, str(COMPAT_SRC))
    try:
        try:
            perception_layer = importlib.import_module("perception_layer")
            tenshi_reply = importlib.import_module("tenshi_reply")
            reply_two_stage = importlib.import_module("reply_two_stage")
            tenshi_config = importlib.import_module("tenshi_config")
            telegram_tenshi_logger = importlib.import_module("telegram_tenshi_logger")
        except ImportError as exc:
            # CI 的 test job 只装 dev extra；只有 full-runtime job 装了 full extra，
            # 这些端到端用例才有 telegram / google-genai 等可选依赖可跑。
            pytest.skip(f"compat runtime optional dependency missing: {exc}")
        modules = SimpleNamespace(
            root=root,
            perception_layer=perception_layer,
            tenshi_reply=tenshi_reply,
            reply_two_stage=reply_two_stage,
            tenshi_config=tenshi_config,
            telegram_tenshi_logger=telegram_tenshi_logger,
        )
        yield modules
    finally:
        try:
            sys.path.remove(str(COMPAT_SRC))
        except ValueError:
            pass
        patch.undo()


@pytest.fixture
def perception(compat, monkeypatch):
    """把感知层真实管道里除审查外的外部依赖全部打桩。"""
    pl = compat.perception_layer
    rec = SimpleNamespace(
        logger=_CapturingLogger(),
        scheduled_completions=[],
        perception_logs=[],
        silence_writes=[],
        bot_constructions=0,
    )
    monkeypatch.setattr(pl, "logger", rec.logger)

    monkeypatch.setattr(pl, "get_last_interaction", lambda: (1, 0, 0))
    monkeypatch.setattr(pl, "_resolve_reply_retry_trigger", lambda note: None)
    monkeypatch.setattr(pl, "load_pending_schedule_entry", lambda: None)
    monkeypatch.setattr(pl, "_should_skip_perception_probability", lambda *a, **k: False)
    monkeypatch.setattr(
        pl.weather_utils, "resolve_location_for_perception", lambda uid: (0.0, 0.0, "某地", "在家")
    )
    monkeypatch.setattr(pl.weather_utils, "get_weather_report", lambda **k: "晴")
    monkeypatch.setattr(pl, "get_recent_commits", lambda: "")
    monkeypatch.setattr(pl, "get_recent_dialogue_context_messages", lambda uid: [])

    class _FakeRAG:
        kb_data = {"user_profile": {}}

    monkeypatch.setattr(pl, "TenshiRAG", _FakeRAG)
    monkeypatch.setattr(
        pl,
        "bond_state",
        SimpleNamespace(inner_block=lambda kb: "关系状态", typing_block=lambda: "打字状态"),
    )
    monkeypatch.setattr(pl, "format_private_notes_for_system_prompt", lambda n: "")
    monkeypatch.setattr(
        pl,
        "invoke_dialog_model_chain",
        lambda **k: ("<inner>他在实验室待到很晚。</inner>", "fake-model", None),
    )
    monkeypatch.setattr(pl, "_snapshot_mutation_targets", lambda: {})
    monkeypatch.setattr(pl, "append_llm_trace", lambda *a, **k: None)
    monkeypatch.setattr(
        pl, "run_typing_stage", lambda **k: ("准备发出去的一句话。", False, "fake-typing")
    )
    monkeypatch.setattr(pl, "_style_guard_perception_text", lambda text, *, call_rewrite: text)
    monkeypatch.setattr(pl, "_has_pending_user_reply", lambda: False)
    monkeypatch.setattr(pl.reactions, "enabled", lambda: False)
    monkeypatch.setattr(pl.reactions, "strip_markers", lambda text: text)
    monkeypatch.setattr(
        pl,
        "complete_scheduled_trigger",
        lambda trigger=None: rec.scheduled_completions.append(trigger),
    )
    monkeypatch.setattr(
        pl, "save_perception_log", lambda *a, **k: rec.perception_logs.append((a, k))
    )
    monkeypatch.setattr(pl, "write_last_silence_at", lambda ts: rec.silence_writes.append(ts))

    def _no_bot(*a, **k):
        rec.bot_constructions += 1
        raise AssertionError("审查拒绝后不应构造 Telegram Bot 或发送任何内容")

    monkeypatch.setattr(pl, "Bot", _no_bot)
    return pl, rec


def _scheduled_call(pl, trigger_ms=1_700_000_000_123):
    return asyncio.run(pl.run_perception(scheduled_force=True, schedule_trigger_at_ms=trigger_ms))


def test_review_rejection_is_suppressed_as_normal_skip(perception, monkeypatch):
    pl, rec = perception
    review_calls = []

    def _reject(**kwargs):
        review_calls.append(kwargs)
        raise pl.reply_two_stage.UnsafeResponseError(UNSAFE_REASON)

    monkeypatch.setattr(pl, "review_outgoing_reply", _reject)

    code = _scheduled_call(pl)

    assert code == 0, "审查拒绝应按正常结束返回 0，而不是抛异常让进程 exit 1"
    assert len(review_calls) == 1
    assert rec.scheduled_completions == [1_700_000_000_123], (
        "定时触发必须清队列，避免永久 quarantine"
    )
    assert rec.perception_logs == [], "被拒内容不得落进感知日志（等同于未发生）"
    assert rec.silence_writes == [], "审查拒绝不是主动沉默，不改静默时间"
    assert rec.bot_constructions == 0, "审查拒绝后绝不发送"
    assert any(
        "perception_suppressed_unsafe: " + UNSAFE_REASON in line for line in rec.logger.lines
    )


def test_second_gemini_flash_attempt_rejection_exits_zero(perception, monkeypatch):
    pl, rec = perception
    calls = []

    async def _always_reject(**kwargs):
        calls.append(kwargs)
        raise pl.reply_two_stage.UnsafeResponseError(UNSAFE_REASON)

    monkeypatch.setattr(pl, "_run_perception_impl", _always_reject)

    code = _scheduled_call(pl, trigger_ms=42)

    assert code == 0, "两次尝试都被拒也不能把异常漏出 run_perception"
    assert len(calls) == 2, "第一次被拒仍应换 Gemini Flash 重试一次"
    assert calls[1]["force_gemini_flash_retry"] is True
    assert rec.scheduled_completions == [42]
    assert any("stage=gemini_flash_retry" in line for line in rec.logger.lines)


@pytest.fixture
def reply_env(compat, monkeypatch):
    ts = compat.tenshi_reply
    rec = SimpleNamespace(logger=_CapturingLogger(), main_impl_calls=[], compression_persists=[])
    monkeypatch.setattr(ts, "logger", rec.logger)
    monkeypatch.setattr(
        ts,
        "_persist_context_compression_future",
        lambda future, conv: rec.compression_persists.append((future, conv)),
    )
    return ts, rec


def _seed_reply_log(compat, *, msg_id="m-2"):
    doc = [
        {
            "conversation_id": "conv-1",
            "user_id": 1,
            "chat_history": [
                {"msg_id": "m-1", "time": 1, "user_query": "早"},
                {"msg_id": msg_id, "time": 2, "user_query": "在吗"},
            ],
        }
    ]
    _write_json(compat.root / "data" / "tenshi_log.json", doc)
    return doc[0]["chat_history"][-1]


def _reply_ctx(*, streamer=None):
    return {
        "unreplied_msgs": [{"msg_id": "m-1"}, {"msg_id": "m-2"}],
        "target_conv": {"conversation_id": "conv-1", "user_id": 1},
        "since_ts": 2,
        "streamer": streamer,
        "compression_future": None,
    }


def test_reply_main_suppresses_final_unsafe_and_marks_no_send(reply_env, compat, monkeypatch):
    ts, rec = reply_env
    _seed_reply_log(compat)
    monkeypatch.setattr(ts, "_partial_save_ctx", _reply_ctx())

    def _reject_twice(**kwargs):
        rec.main_impl_calls.append(kwargs)
        raise compat.reply_two_stage.UnsafeResponseError(UNSAFE_REASON)

    monkeypatch.setattr(ts, "_main_impl", _reject_twice)

    result = ts.main()  # 不得抛异常：兜底后正常返回

    assert result is None
    assert len(rec.main_impl_calls) == 2, "第一次被拒仍换 Gemini Flash 重试一次"
    assert rec.main_impl_calls[1]["force_gemini_flash"] is True
    assert any("reply_suppressed_unsafe: " + UNSAFE_REASON in line for line in rec.logger.lines)

    saved = json.loads((compat.root / "data" / "tenshi_log.json").read_text(encoding="utf-8"))
    last = next(m for m in saved[0]["chat_history"] if m["msg_id"] == "m-2")
    assert last["replied_flag"] is True
    assert last["delivery_status"] == "no_send"
    assert last["no_send"] is True
    assert last["bot_thinking"]["unsafe_suppressed_reason"] == UNSAFE_REASON
    assert last["bot_thinking"]["unsafe_withdraw_failed"] is False
    # 入口读这份记号 → RP_RESULT_PATH 写 no_send，而不是 delivery_unknown。
    assert compat.telegram_tenshi_logger.is_intentional_no_send(last) is True


def test_reply_main_does_not_retry_when_withdrawal_fails(reply_env, compat, monkeypatch):
    ts, rec = reply_env
    _seed_reply_log(compat)
    failed_streamer = SimpleNamespace(withdraw=lambda: False)
    monkeypatch.setattr(ts, "_partial_save_ctx", _reply_ctx(streamer=failed_streamer))

    def _reject_once(**kwargs):
        rec.main_impl_calls.append(kwargs)
        raise compat.reply_two_stage.UnsafeResponseError(UNSAFE_REASON)

    monkeypatch.setattr(ts, "_main_impl", _reject_once)

    assert ts.main() is None

    assert len(rec.main_impl_calls) == 1, "撤回失败时不得重试，避免把新内容叠加上去"
    assert any("撤回失败" in line for line in rec.logger.lines)

    saved = json.loads((compat.root / "data" / "tenshi_log.json").read_text(encoding="utf-8"))
    last = next(m for m in saved[0]["chat_history"] if m["msg_id"] == "m-2")
    assert last["delivery_status"] == "delivery_unknown", "撤回失败必须如实报未知，不能谎称未发送"
    assert "no_send" not in last
    assert last["bot_thinking"]["unsafe_withdraw_failed"] is True
