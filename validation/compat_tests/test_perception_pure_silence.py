# SPDX-License-Identifier: AGPL-3.0-only













from __future__ import annotations

import json
import re
import os
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import tenshi_config
import perception_layer as pl_module
from perception_layer import (
    _apply_post_silence_cooldown,
    _compute_silence_pass_probability,
    _is_pure_silence_record,
    _tool_produced_side_effects,
    get_last_interaction,
    get_last_perception_silence_time,
    save_perception_log,
)
from perception_state import (
    get_last_silence_at,
    state_file_path,
    write_last_silence_at,
)


class PureSilencePredicateTest(unittest.TestCase):
    """无额外信息 = 无 thinking + 无正文 + 无工具副作用。"""

    def test_bare_silence_is_pure(self):
        self.assertTrue(
            _is_pure_silence_record(thinking_content="", clipped_body="", tool_mutated=False)
        )

    def test_whitespace_only_is_pure(self):
        self.assertTrue(
            _is_pure_silence_record(
                thinking_content="  \n ", clipped_body="\t", tool_mutated=False
            )
        )

    def test_thinking_counts_as_extra_info(self):
        self.assertFalse(
            _is_pure_silence_record(
                thinking_content="他在实验室待到很晚，不打扰。",
                clipped_body="",
                tool_mutated=False,
            )
        )

    def test_clipped_body_counts_as_extra_info(self):
        self.assertFalse(
            _is_pure_silence_record(
                thinking_content="", clipped_body="记一笔：明天提醒他喝水", tool_mutated=False
            )
        )

    def test_tool_side_effect_counts_as_extra_info(self):
        """note_patch / schedule_perception 落盘了，就不是纯沉默。"""
        self.assertFalse(
            _is_pure_silence_record(thinking_content="", clipped_body="", tool_mutated=True)
        )


class ToolSideEffectDetectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_project_root = tenshi_config.PROJECT_ROOT
        self._orig_log_path = tenshi_config.LOG_FILE_PATH
        os.makedirs(os.path.join(self._tmp.name, "data"), exist_ok=True)
        tenshi_config.PROJECT_ROOT = self._tmp.name
        tenshi_config.LOG_FILE_PATH = os.path.join(self._tmp.name, "data", "tenshi_log.json")
        with open(tenshi_config.LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("[]")

    def tearDown(self):
        tenshi_config.PROJECT_ROOT = self._orig_project_root
        tenshi_config.LOG_FILE_PATH = self._orig_log_path
        self._tmp.cleanup()

    def _snapshot(self):
        from perception_layer import _snapshot_mutation_targets

        return _snapshot_mutation_targets()

    def test_no_side_effect_when_nothing_changes(self):
        before = self._snapshot()
        self.assertFalse(_tool_produced_side_effects(before))

    def test_schedule_write_is_detected(self):
        before = self._snapshot()
        schedule = os.path.join(self._tmp.name, "data", "perception_schedule.json")
        with open(schedule, "w", encoding="utf-8") as f:
            json.dump({"entries": []}, f)
        self.assertTrue(_tool_produced_side_effects(before))

    def test_missing_snapshot_means_no_side_effect(self):
        self.assertFalse(_tool_produced_side_effects(None))


class PerceptionStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = getattr(tenshi_config, "PERCEPTION_STATE_FILE_PATH", None)
        self._orig_log_path = tenshi_config.LOG_FILE_PATH
        tenshi_config.PERCEPTION_STATE_FILE_PATH = os.path.join(
            self._tmp.name, "data", "perception_state.json"
        )

        tenshi_config.LOG_FILE_PATH = os.path.join(self._tmp.name, "data", "tenshi_log.json")

    def tearDown(self):
        if self._orig_state is None:
            try:
                del tenshi_config.PERCEPTION_STATE_FILE_PATH
            except AttributeError:
                pass
        else:
            tenshi_config.PERCEPTION_STATE_FILE_PATH = self._orig_state
        tenshi_config.LOG_FILE_PATH = self._orig_log_path
        self._tmp.cleanup()

    def test_missing_state_returns_zero(self):
        self.assertEqual(get_last_silence_at(), 0)

    def test_round_trip(self):
        ts = 1789449777
        self.assertEqual(write_last_silence_at(ts), ts)
        self.assertEqual(get_last_silence_at(), ts)
        with open(state_file_path(), "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["last_silence_at"], ts)
        self.assertTrue(payload["last_silence_bj"])
        self.assertTrue(payload["updated_at"])

    def test_corrupt_state_returns_zero(self):
        os.makedirs(os.path.dirname(state_file_path()), exist_ok=True)
        with open(state_file_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(get_last_silence_at(), 0)

    def test_state_without_timestamp_returns_zero(self):
        os.makedirs(os.path.dirname(state_file_path()), exist_ok=True)
        with open(state_file_path(), "w", encoding="utf-8") as f:
            json.dump({"last_silence_jst": "2026-09-15 14:00:00"}, f)
        self.assertEqual(get_last_silence_at(), 0)


class PureSilenceArchiveSkipTest(unittest.TestCase):
    """SILENCE 一律不写对话日志；只有主动起念才存档。两种沉默都写状态文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_log_path = tenshi_config.LOG_FILE_PATH
        self._orig_lock_path = tenshi_config.LOCK_FILE_PATH
        self._orig_state_path = getattr(tenshi_config, "PERCEPTION_STATE_FILE_PATH", None)
        self._log_path = os.path.join(self._tmp.name, "tenshi_log.json")
        tenshi_config.LOG_FILE_PATH = self._log_path
        tenshi_config.LOCK_FILE_PATH = self._log_path
        tenshi_config.PERCEPTION_STATE_FILE_PATH = os.path.join(
            self._tmp.name, "perception_state.json"
        )
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "user_id": "u1",
                        "chat_history": [
                            {
                                "msg_id": "m1",
                                "time": 1789000000,
                                "user_query": "早",
                                "replied_flag": True,
                            }
                        ],
                    }
                ],
                f,
            )

    def tearDown(self):
        tenshi_config.LOG_FILE_PATH = self._orig_log_path
        tenshi_config.LOCK_FILE_PATH = self._orig_lock_path
        if self._orig_state_path is None:
            try:
                del tenshi_config.PERCEPTION_STATE_FILE_PATH
            except AttributeError:
                pass
        else:
            tenshi_config.PERCEPTION_STATE_FILE_PATH = self._orig_state_path
        self._tmp.cleanup()

    def _history(self):
        with open(self._log_path, "r", encoding="utf-8") as f:
            return json.load(f)[0]["chat_history"]

    def test_pure_silence_writes_nothing_to_log(self):
        save_perception_log(
            "u1", "prompt", "SILENCE", is_silence=True, thinking_content="", pure_silence=True
        )
        self.assertEqual(len(self._history()), 1)

    def test_silence_with_thinking_is_not_archived(self):
        """带内心独白的沉默同样不落对话日志（ca141f1 的 9f163353 形状）。"""
        save_perception_log(
            "u1", "prompt", "SILENCE", is_silence=True,
            thinking_content="想了一下，不打扰他。", pure_silence=False,
        )
        self.assertEqual(len(self._history()), 1)

    def test_silence_flag_defaults_to_no_archiving(self):
        """未显式标记时也不落库：沉默没有面向对话的产出。"""
        save_perception_log("u1", "prompt", "SILENCE", is_silence=True)
        self.assertEqual(len(self._history()), 1)

    def test_non_silence_ignores_pure_silence_flag(self):
        save_perception_log(
            "u1", "prompt", "说点什么", is_silence=False, pure_silence=True
        )
        history = self._history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["bot_answer"], "说点什么")


class TriggerIntervalUnchangedTest(unittest.TestCase):
    """对话日志不再承载沉默记录，但「沉默后冷却/爬坡」的触发间隔口径必须逐字不变。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = getattr(tenshi_config, "PERCEPTION_STATE_FILE_PATH", None)
        self._orig_log_path = tenshi_config.LOG_FILE_PATH
        tenshi_config.PERCEPTION_STATE_FILE_PATH = os.path.join(
            self._tmp.name, "data", "perception_state.json"
        )
        tenshi_config.LOG_FILE_PATH = os.path.join(self._tmp.name, "data", "tenshi_log.json")
        self.silence_ts = 1_789_400_000
        write_last_silence_at(self.silence_ts)

    def tearDown(self):
        if self._orig_state is None:
            try:
                del tenshi_config.PERCEPTION_STATE_FILE_PATH
            except AttributeError:
                pass
        else:
            tenshi_config.PERCEPTION_STATE_FILE_PATH = self._orig_state
        tenshi_config.LOG_FILE_PATH = self._orig_log_path
        self._tmp.cleanup()

    def test_silence_time_is_read_from_state(self):
        self.assertEqual(get_last_perception_silence_time(), self.silence_ts)

    def test_post_silence_cooldown_blocks_under_two_hours(self):
        prob, hard_skip = _apply_post_silence_cooldown(0.5, self.silence_ts + 3600)
        self.assertTrue(hard_skip)
        self.assertEqual(prob, 0.0)

    def test_post_silence_ramp_between_two_and_three_hours(self):

        prob, hard_skip = _apply_post_silence_cooldown(0.4, self.silence_ts + 9000)
        self.assertFalse(hard_skip)
        self.assertAlmostEqual(prob, 0.2)

    def test_no_cooldown_after_ramp_ends(self):
        prob, hard_skip = _apply_post_silence_cooldown(0.4, self.silence_ts + 4 * 3600)
        self.assertFalse(hard_skip)
        self.assertAlmostEqual(prob, 0.4)

    def test_silence_pass_probability_bands_unchanged(self):
        """分档与爬坡斜率。

        注：8h 处由 0.50 改为 0.525 是**刻意**的——爬坡终点从 0.70 抬到 0.75，
        以消除 12h 整点 0.70→0.75 的跳变（见 SilenceBandContinuityTest）。
        端点 0.10 / 0.30 / 0.75 未变；「近 1h 主动发言」是封顶 0.25（min 语义）。
        """
        now = 1_789_500_000.0
        self.assertAlmostEqual(_compute_silence_pass_probability(1800, now, 0), 0.10)
        self.assertAlmostEqual(_compute_silence_pass_probability(2 * 3600, now, 0), 0.30)
        self.assertAlmostEqual(_compute_silence_pass_probability(8 * 3600, now, 0), 0.525)
        self.assertAlmostEqual(_compute_silence_pass_probability(13 * 3600, now, 0), 0.75)

        self.assertAlmostEqual(
            _compute_silence_pass_probability(2 * 3600, now, int(now) - 600), 0.25
        )

    def test_recent_proactive_caps_but_never_raises(self):
        """封顶语义：只下调、绝不抬高。

        早期实现在该分支直接 `prob = 0.25`，会把 <1h 档的 0.10 抬到 0.25——
        刚主动说完话本该更克制，反而比基线更激进。改成 min 后：
          * 基线高于 0.25（1–4h 档、爬坡档）→ 下调到 0.25
          * 基线低于 0.25（<1h 档 0.10）→ 保持 0.10，不被抬高
        期望值直接取真实基线（不经手算），避免把斜率的算术抄错。
        """
        now = 1_789_500_000.0
        recent = int(now) - 600

        for hours in (0.3, 0.9, 2.0, 6.0, 13.0):
            baseline = _compute_silence_pass_probability(hours * 3600, now, 0)
            got = _compute_silence_pass_probability(hours * 3600, now, recent)
            self.assertAlmostEqual(got, min(baseline, 0.25), places=9, msg=f"{hours}h")
            self.assertLessEqual(got, 0.25, f"{hours}h：封顶后不得超过 0.25")
            self.assertLessEqual(got, baseline + 1e-9, f"{hours}h：不得高于自身基线")

    def test_recent_proactive_expires_after_one_hour(self):
        """正好 1h 起不再封顶（判定是严格小于）。"""
        now = 1_789_500_000.0
        baseline = _compute_silence_pass_probability(6 * 3600, now, 0)
        self.assertGreater(baseline, 0.25, "该用例需要基线高于 0.25 才有意义")
        self.assertAlmostEqual(
            _compute_silence_pass_probability(6 * 3600, now, int(now) - 3600), baseline, places=9
        )
        self.assertAlmostEqual(
            _compute_silence_pass_probability(6 * 3600, now, int(now) - 3599), 0.25, places=9
        )


class RunPerceptionPureSilenceTest(unittest.TestCase):
    """端到端：run_perception 判 SILENCE 后不落日志、只落状态；开口才落日志。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = {
            "LOG_FILE_PATH": tenshi_config.LOG_FILE_PATH,
            "LOCK_FILE_PATH": tenshi_config.LOCK_FILE_PATH,
            "PERCEPTION_STATE_FILE_PATH": getattr(
                tenshi_config, "PERCEPTION_STATE_FILE_PATH", None
            ),
        }
        self._log_path = os.path.join(self._tmp.name, "data", "tenshi_log.json")
        os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
        tenshi_config.LOG_FILE_PATH = self._log_path
        tenshi_config.LOCK_FILE_PATH = self._log_path
        tenshi_config.PERCEPTION_STATE_FILE_PATH = os.path.join(
            self._tmp.name, "data", "perception_state.json"
        )
        self._seed_log()
        self._patches = []


        self.trace_records = []
        self._trace_sink = lambda rec, **kwargs: (self.trace_records.append(rec), True)[1]

    def tearDown(self):
        for target, attr, original in reversed(self._patches):
            setattr(target, attr, original)
        tenshi_config.LOG_FILE_PATH = self._orig["LOG_FILE_PATH"]
        tenshi_config.LOCK_FILE_PATH = self._orig["LOCK_FILE_PATH"]
        if self._orig["PERCEPTION_STATE_FILE_PATH"] is None:
            try:
                del tenshi_config.PERCEPTION_STATE_FILE_PATH
            except AttributeError:
                pass
        else:
            tenshi_config.PERCEPTION_STATE_FILE_PATH = self._orig["PERCEPTION_STATE_FILE_PATH"]
        self._tmp.cleanup()

    def _patch(self, target, attr, value):
        self._patches.append((target, attr, getattr(target, attr)))
        setattr(target, attr, value)

    def _seed_log(self):
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "user_id": "u1",
                        "chat_history": [
                            {
                                "msg_id": "m1",
                                "time": 1789000000,
                                "user_query": "早",
                                "bot_answer": "早。",
                                "replied_flag": True,
                            }
                        ],
                    }
                ],
                f,
            )

    def _history(self):
        with open(self._log_path, "r", encoding="utf-8") as f:
            return json.load(f)[0]["chat_history"]

    def _read_state_bytes(self):
        with open(state_file_path(), "rb") as f:
            return f.read()

    def _run(self, model_output, now_ts=None):
        import asyncio

        import perception_layer as pl

        self._patch(pl, "invoke_dialog_model_chain",
                    lambda **kwargs: (model_output, "", False))

        def _fake_typing(**kwargs):
            body = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", model_output).strip()
            if not body or "SILENCE" in body:
                return "", True, "fake"
            return body, False, "fake"
        self._patch(pl, "run_typing_stage", _fake_typing)


        self._patch(pl, "append_llm_trace", self._trace_sink)
        self._patch(pl, "get_recent_commits", lambda: None)
        self._patch(pl.weather_utils, "resolve_location_for_perception",
                    lambda *a, **k: (34.8, 135.5, "大阪箕面市", "（未提供）"))
        self._patch(pl.weather_utils, "get_weather_report",
                    lambda *a, **k: "大阪箕面市天气：晴。")
        self._patch(pl, "TenshiRAG", lambda *a, **k: types.SimpleNamespace(kb_data={}))

        self._run_now = float(now_ts if now_ts is not None else time.time())
        self._patch(pl.time, "time", lambda: self._run_now)
        return asyncio.run(pl.run_perception(force=True))

    def test_pure_silence_leaves_no_archive_and_records_state(self):
        code = self._run("SILENCE")
        self.assertEqual(code, 0)

        self.assertEqual(len(self._history()), 1)

        self.assertGreater(get_last_silence_at(), 0)
        self.assertTrue(os.path.exists(state_file_path()), "状态文件必须被创建/更新")

    def test_pure_silence_state_change_is_committable(self):
        """纯沉默轮次里，perception_state.json 必须真的变了（否则工作流无东西可提交）。"""
        self._run("SILENCE", now_ts=1_789_500_000)
        first = self._read_state_bytes()
        self.assertGreater(len(first), 0)

        self._run("SILENCE", now_ts=1_789_500_600)
        self.assertNotEqual(first, self._read_state_bytes())

    def test_same_silence_time_produces_identical_state(self):
        """同一时刻重复判沉默不制造无意义改动（工作流不会为它空跑一次提交）。"""
        self._run("SILENCE", now_ts=1_789_500_000)
        first = self._read_state_bytes()
        self._run("SILENCE", now_ts=1_789_500_000)
        self.assertEqual(first, self._read_state_bytes())

    def test_silence_with_thinking_is_not_archived(self):
        """带 <thinking> 的沉默也不落对话日志（只有主动起念才写日志）。"""
        code = self._run("<thinking>他在实验室，不打扰。</thinking>\nSILENCE")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._history()), 1)
        self.assertGreater(get_last_silence_at(), 0)

    def test_silence_with_extra_info_also_records_state(self):
        """带额外信息的沉默同样写状态文件 → 两种沉默都照旧 commit+push。"""
        self._run("<thinking>记一下。</thinking>SILENCE")
        self.assertGreater(get_last_silence_at(), 0)
        self.assertTrue(os.path.exists(state_file_path()))

    def test_perception_turn_writes_paired_trace_markers(self):
        """每一轮感知层都必须成对写 begin/end 标记（兜底抢救的轮次隔离依据）。

        只有 begin 的话，感知层收工之后回复引擎自己写下的 trace 也会被跟着排除，
        真是回复脚本被杀时连自己的半截回复都救不回来。
        """
        code = self._run("SILENCE")
        self.assertEqual(code, 0)
        self.assertEqual(
            [r.get("source") for r in self.trace_records],
            ["perception_layer.begin", "perception_layer.end"],
        )

    def test_speak_is_deferred_when_a_user_message_is_pending(self):
        """用户刚发了话、还没被回复：这次起念必须让位给回复引擎（不发送、不落库）。

        2026-09-16 ca141f1：感知层 08:02 开口，用户 08:02 发的消息被当成「已答边界」，
        最后感知层那句话被写成了那条用户消息的回复。
        """
        import perception_layer as pl

        sends = []

        class _FakeBot:
            def __init__(self, *a, **k):
                pass

            async def send_message(self, **kwargs):
                sends.append(kwargs)
                return types.SimpleNamespace(message_id=1)

        self._patch(pl, "Bot", _FakeBot)

        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "user_id": "u1",
                        "chat_history": [
                            {"msg_id": "m2", "time": 1789000100, "user_query": "还在吗"},
                            {
                                "msg_id": "m1",
                                "time": 1789000000,
                                "user_query": "早",
                                "bot_answer": "早。",
                                "replied_flag": True,
                            },
                        ],
                    }
                ],
                f,
            )

        code = self._run("08:00了。第二遍问——几点开口？")
        self.assertEqual(code, 0)
        self.assertEqual(sends, [], "有未回用户消息时不该发主动起念")
        self.assertEqual(len(self._history()), 2, "也不该写进对话日志")


class BootstrapSeedBehaviorTest(unittest.TestCase):
    """初始化 perception_state 的占位值必须让冷启动可用。

    真实种子取「日志最新记录往前 24h」：若改填成最近一次对话时刻，部署后头 2 小时
    感知层会被 _apply_post_silence_cooldown 硬阻 —— 与「防止从 0 启动导致感知层
    无法工作」的初始化本意相反。这条测试把这个约束钉住。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = getattr(tenshi_config, "PERCEPTION_STATE_FILE_PATH", None)
        self._orig_log = tenshi_config.LOG_FILE_PATH
        tenshi_config.PERCEPTION_STATE_FILE_PATH = os.path.join(
            self._tmp.name, "perception_state.json"
        )
        tenshi_config.LOG_FILE_PATH = os.path.join(self._tmp.name, "tenshi_log.json")
        self.base_prob = 0.30

    def tearDown(self):
        if self._orig_state is None:
            try:
                del tenshi_config.PERCEPTION_STATE_FILE_PATH
            except AttributeError:
                pass
        else:
            tenshi_config.PERCEPTION_STATE_FILE_PATH = self._orig_state
        tenshi_config.LOG_FILE_PATH = self._orig_log
        self._tmp.cleanup()

    def _hard_skip_at(self, silence_ts: int, current_ts: float) -> bool:
        write_last_silence_at(silence_ts)
        _prob, hard = _apply_post_silence_cooldown(self.base_prob, current_ts)
        return hard

    def test_past_seed_does_not_block_at_deploy_time(self):
        """种子取过去时刻 → 在真实「现在」触发时冷却已过期，感知层照常工作。

        这正是初始化要保证的：文件存在(非 0)且不会把冷启动阻塞掉。
        """
        now = time.time()
        seed = int(now) - 24 * 3600
        self.assertFalse(self._hard_skip_at(seed, now))

        self.assertFalse(self._hard_skip_at(seed, now + 3600))
        self.assertFalse(self._hard_skip_at(seed, now + 2 * 3600))

    def test_recent_conversation_as_seed_would_block(self):
        """反例：填最近一次对话时刻 → 部署后头 2 小时感知层被硬阻（故不采用）。"""
        now = time.time()
        recent = int(now) - 600
        self.assertTrue(self._hard_skip_at(recent, now))

    def test_shipped_state_is_a_valid_past_moment(self):
        """入库的 perception_state 必须是「已发生且自洽的时刻」。

        2026-09-15 修正：这条测试原来断言 `git HEAD` 里的 last_silence_at 比现在
        早 3 小时以上，理由是「HEAD 里永远是初始化那份 24h 前的种子」。但感知层
        工作流每判定一次 SILENCE 就把**实时**状态文件提交回仓库（见
        data/perception_state.json 的提交历史），于是 HEAD 里早就是「几分钟前的
        真实沉默」——旧断言必然失败，而数据本身完全正确（本地 3h、线上 2h 的
        冷却/爬坡本来就是为「刚沉默过」设计的）。所以改成钉住真正会出事的坏值：

        * 缺字段 / 0 —— 说明初始化没生效，文件形同不存在；
        * **未来时刻** —— `since = now - seed` 恒为负，冷却永远判定「刚沉默过」，
          比「最近一次真实沉默」严重得多（后者只是正常的 2h 硬阻）；
        * epoch 与 `last_silence_bj`（旧数据为 `last_silence_jst`）不自洽 —— 写盘时把时区算错会在这里露出来
          （这个字段只给人看，正因为不参与计算，才需要单独校验）。

        「种子取过去 24h 才不硬阻冷启动」这条策略由上面两个行为测试钉住
        （`test_past_seed_does_not_block_at_deploy_time` /
        `test_recent_conversation_as_seed_would_block`），与这份运行时文件无关。
        """
        live = os.path.join(ROOT, "data", "perception_state.json")
        self.assertTrue(os.path.exists(live), "初始化状态文件必须入库")

        with open(live, "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertIsInstance(doc, dict)

        seed = int(doc.get("last_silence_at") or 0)
        self.assertGreater(seed, 0, "不能是 0：文件存在但读不出时刻，等于没初始化")
        self.assertLessEqual(
            seed,
            time.time() + 60,
            "状态时刻不能落在未来：since = now - seed 恒为负，冷却会永久硬阻",
        )

        time_key = "last_silence_bj" if doc.get("last_silence_bj") else "last_silence_jst"
        time_str = doc.get(time_key) or ""
        if time_str:
            from datetime import datetime, timedelta, timezone




            if time_key == "last_silence_bj":
                offsets = {int(getattr(tenshi_config, "TIMEZONE_OFFSET", 8) or 8)}
            else:
                offsets = {8, 9, int(getattr(tenshi_config, "TIMEZONE_OFFSET", 8) or 8)}
            rendered = {
                datetime.fromtimestamp(seed, timezone(timedelta(hours=off))).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                for off in offsets
            }
            self.assertIn(time_str, rendered, f"epoch 与 {time_key} 必须自洽")


class SilenceBandContinuityTest(unittest.TestCase):
    """静默时长分档必须在边界连续——旧实现 12h 整点会从 0.70 跳到 0.75。"""

    NOW = 1_789_454_000.0

    def _p(self, hours: float) -> float:
        return _compute_silence_pass_probability(hours * 3600, self.NOW, 0)

    def test_bands(self):
        self.assertAlmostEqual(self._p(0.5), 0.10)
        self.assertAlmostEqual(self._p(3.99), 0.30)
        self.assertAlmostEqual(self._p(4.0), 0.30)
        self.assertAlmostEqual(self._p(24.0), 0.75)

    def test_continuous_at_twelve_hours(self):
        """12h 处不得跳变：左右极限同值。"""
        left = self._p(11.999)
        right = self._p(12.001)
        self.assertLess(abs(right - left), 0.001)
        self.assertAlmostEqual(self._p(12.0), 0.75)

    def test_continuous_at_four_hours(self):
        """4h 是「0.30 平台」与「线性爬坡」的接缝，两侧必须同为 0.30。

        注意别用 assertAlmostEqual(places=4) 比较跨接缝的两点：places 走的是
        round(diff, 4)==0，而 3.999h 与 4.001h 的差正好是 5.6e-05（round 到 0.0001），
        会假失败。这里改用对称取点 + 明确容差。
        """
        self.assertAlmostEqual(self._p(4.0), 0.30, places=9)
        self.assertLess(abs(self._p(4.001) - self._p(4.0)), 1e-4)
        self.assertAlmostEqual(self._p(3.999), 0.30, places=9)

    def test_monotonic_non_decreasing(self):
        values = [self._p(h / 10) for h in range(0, 481)]
        for a, b in zip(values, values[1:]):
            self.assertLessEqual(a, b + 1e-9)

    def test_recent_proactive_override_still_applies(self):
        self.assertAlmostEqual(
            _compute_silence_pass_probability(6 * 3600, self.NOW, int(self.NOW) - 600), 0.25
        )


class CooldownStateLossVisibilityTest(unittest.TestCase):
    """沉默状态缺失时冷却会失效——必须留下可见告警，且只告警一次。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = getattr(tenshi_config, "PERCEPTION_STATE_FILE_PATH", None)
        self._orig_log = tenshi_config.LOG_FILE_PATH
        self._orig_warned = pl_module._cooldown_unavailable_warned

        tenshi_config.PERCEPTION_STATE_FILE_PATH = os.path.join(
            self._tmp.name, "perception_state.json"
        )
        tenshi_config.LOG_FILE_PATH = os.path.join(self._tmp.name, "tenshi_log.json")
        pl_module._cooldown_unavailable_warned = False

    def tearDown(self):
        pl_module._cooldown_unavailable_warned = self._orig_warned
        if self._orig_state is None:
            try:
                del tenshi_config.PERCEPTION_STATE_FILE_PATH
            except AttributeError:
                pass
        else:
            tenshi_config.PERCEPTION_STATE_FILE_PATH = self._orig_state
        tenshi_config.LOG_FILE_PATH = self._orig_log
        self._tmp.cleanup()

    def test_cooldown_disabled_when_state_missing(self):
        prob, hard = _apply_post_silence_cooldown(0.4, time.time())
        self.assertFalse(hard)
        self.assertAlmostEqual(prob, 0.4, msg="状态缺失时不施加冷却（行为未变）")

    def test_warning_fires_exactly_once(self):
        with self.assertLogs("TenshiPerception", level="WARNING") as cm:
            _apply_post_silence_cooldown(0.4, time.time())
        self.assertTrue(any("冷却不可用" in m for m in cm.output))

        with self.assertRaises(AssertionError):
            with self.assertLogs("TenshiPerception", level="WARNING"):
                _apply_post_silence_cooldown(0.4, time.time())

    def test_helper_reports_first_only(self):
        self.assertTrue(pl_module._warn_silence_state_unavailable_once())
        self.assertFalse(pl_module._warn_silence_state_unavailable_once())

    def test_no_warning_when_state_present(self):
        write_last_silence_at(int(time.time()) - 4 * 3600)
        pl_module._cooldown_unavailable_warned = False
        with self.assertRaises(AssertionError):
            with self.assertLogs("TenshiPerception", level="WARNING"):
                _apply_post_silence_cooldown(0.4, time.time())


class GetLastInteractionScanTest(unittest.TestCase):
    """last_time 与 last_proactive 是两个独立量，取值不得依赖记录顺序。

    真实缺陷：原实现在命中真实用户消息时 `break`，于是「最近一条是用户消息」时
    last_proactive 恒为 0，_compute_silence_pass_probability 里
    「距上次主动发言不足 1h → 概率封顶 0.25」这条规则在用户回话后即失效。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_log = tenshi_config.LOG_FILE_PATH
        self._orig_lock = tenshi_config.LOCK_FILE_PATH
        p = os.path.join(self._tmp.name, "tenshi_log.json")
        tenshi_config.LOG_FILE_PATH = p
        tenshi_config.LOCK_FILE_PATH = p
        self._path = p

    def tearDown(self):
        tenshi_config.LOG_FILE_PATH = self._orig_log
        tenshi_config.LOCK_FILE_PATH = self._orig_lock
        self._tmp.cleanup()

    def _write(self, history_newest_first):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump([{"user_id": "u1", "chat_history": history_newest_first}], f)

    @staticmethod
    def _user(ts, text="在呢"):
        return {"msg_id": f"m{ts}", "time": ts, "user_query": text,
                "bot_answer": "嗯", "replied_flag": True}

    @staticmethod
    def _proactive(ts, text="主动说一句"):
        return {
            "msg_id": f"proactive_{ts}", "time": ts,
            "user_query": "[系统级感知层触发 - 天子主动起念]",
            "bot_answer": text, "replied_flag": True, "perception_event": True,
        }

    @staticmethod
    def _silence(ts):
        return {
            "msg_id": f"perception_{ts}", "time": ts,
            "user_query": "[感知层上报世界状态，天子选择了沉默不语]",
            "perception_event": True, "bot_decision": "SILENCE", "replied_flag": True,
        }

    def test_user_message_newest_still_finds_proactive(self):
        """★ 缺陷场景：用户消息在最前，感知开口紧随其后。"""
        self._write([self._user(1000), self._proactive(900), self._user(800)])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual(lt, 1000)
        self.assertEqual(lp, 900, "用户消息在前时也必须取到感知开口")

    def test_proactive_newest_then_user(self):
        self._write([self._proactive(1000), self._user(900), self._user(800)])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual(lt, 900)
        self.assertEqual(lp, 1000)

    def test_picks_most_recent_of_each(self):
        self._write([
            self._user(1000), self._proactive(950), self._proactive(900),
            self._user(850), self._proactive(800),
        ])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual(lt, 1000)
        self.assertEqual(lp, 950, "取最近一次开口，而不是最早一次")

    def test_silence_record_is_not_proactive(self):
        self._write([self._user(1000), self._silence(950)])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual(lt, 1000)
        self.assertEqual(lp, 0, "沉默记录不算主动发言")

    def test_synthetic_queries_never_count_as_user(self):
        self._write([self._proactive(1000), self._user(900)])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual(lt, 900, "感知开口的合成 user_query 不能算用户互动")

    def test_empty_history(self):
        self._write([])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual((lt, lp), (0, 0))

    def test_proactive_only(self):
        self._write([self._proactive(1000)])
        _uid, lt, lp = get_last_interaction()
        self.assertEqual(lt, 0)
        self.assertEqual(lp, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
