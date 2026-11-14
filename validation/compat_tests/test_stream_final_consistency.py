# SPDX-License-Identifier: AGPL-3.0-only

















from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import tenshi_utils



DRAFT = "*帽檐一挑。*\n\n哈？椅子还没坐热，第一件事是给本小姐上保险？！\n" * 6
TAIL = "……对了，水杯先倒满，放桌上。等会儿一个字一个字念，喉咙容易干。"
V1 = "六点半——谁给你定的钟？下午溜出来那些回本小姐都没算你的账，今天倒卡上点了。\n" * 4
V2 = "六点半——谁给你定的钟？下午你自己溜出来那些本小姐都没算，这会儿倒讲上纪律了。\n" * 4


class StreamFinalConsistencyTest(unittest.TestCase):
    """`evaluate_stream_final_consistency` 的判定口径。"""

    def _eval(self, streamed, final):
        return tenshi_utils.evaluate_stream_final_consistency(streamed, final)

    def test_identical_texts_are_consistent(self):
        res = self._eval("本小姐记下了。", "本小姐记下了。")
        self.assertEqual(res, {
            "streamed_superset": False,
            "missing_stream_content": False,
            "shorter_and_incomplete": False,
        })

    def test_streamed_prefix_of_final_is_consistent(self):
        """正常流式：最终响应把流式内容整段包含（可能还多收尾）。"""
        res = self._eval("前半句", "前半句，后半句。")
        self.assertFalse(res["streamed_superset"])
        self.assertFalse(res["missing_stream_content"])
        self.assertFalse(res["shorter_and_incomplete"])

    def test_whitespace_only_inputs_are_consistent(self):
        for streamed, final in (("", "有内容"), ("有内容", ""), ("  \n ", "有内容"), ("", "")):
            res = self._eval(streamed, final)
            self.assertEqual(res, {
                "streamed_superset": False,
                "missing_stream_content": False,
                "shorter_and_incomplete": False,
            }, msg=f"streamed={streamed!r} final={final!r}")

    def test_final_is_tail_of_streamed_is_not_incomplete(self):
        """7827 形状：流式 = 草稿 + 续写；最终 = 续写那一句。内容没丢，不该算未完整。"""
        res = self._eval(DRAFT + "\n\n" + TAIL, TAIL)
        self.assertTrue(res["streamed_superset"])
        self.assertFalse(res["missing_stream_content"])
        self.assertFalse(res["shorter_and_incomplete"])

    def test_model_restatement_is_not_incomplete(self):
        """7859 形状：流式 = 同一句话写了两遍；最终 = 第二遍。"""
        res = self._eval(V1 + V2, V2)
        self.assertTrue(res["streamed_superset"])
        self.assertFalse(res["missing_stream_content"])
        self.assertFalse(res["shorter_and_incomplete"])

    def test_superset_with_surrounding_whitespace(self):
        res = self._eval(f"\n\n{V1}{V2}\n\n", f"  {V2}  ")
        self.assertTrue(res["streamed_superset"])
        self.assertFalse(res["missing_stream_content"])

    def test_genuine_divergence_shorter_final_is_incomplete(self):
        """两边互不包含且最终明显更短 —— 保持保守：按未完整处理。"""
        res = self._eval("甲" * 400, "乙" * 100)
        self.assertFalse(res["streamed_superset"])
        self.assertTrue(res["missing_stream_content"])
        self.assertTrue(res["shorter_and_incomplete"])

    def test_genuine_divergence_long_final_is_missing_but_not_shorter(self):
        res = self._eval("甲" * 400, "乙" * 320)
        self.assertFalse(res["streamed_superset"])
        self.assertTrue(res["missing_stream_content"])
        self.assertFalse(res["shorter_and_incomplete"])


class RecoverReplyTextProvenanceTest(unittest.TestCase):
    """`recover_reply_text_from_llm_trace` 必须区分「半截」与「已收尾的完整响应」。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.trace_path = os.path.join(self._tmp.name, "llm_trace.jsonl")
        self._orig_trace_path = getattr(tenshi_utils.tenshi_config, "LLM_TRACE_PATH", None)
        tenshi_utils.tenshi_config.LLM_TRACE_PATH = self.trace_path

    def tearDown(self):
        if self._orig_trace_path is None:
            try:
                delattr(tenshi_utils.tenshi_config, "LLM_TRACE_PATH")
            except Exception:
                pass
        else:
            tenshi_utils.tenshi_config.LLM_TRACE_PATH = self._orig_trace_path
        self._tmp.cleanup()

    def _write(self, records):
        with open(self.trace_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_complete_final_response_is_flagged_complete(self):
        """7851 形状：只有一条已收尾的 final 响应 → 内容完整，不该盖尾标。"""
        self._write([
            {"source": "tenshi_reply.deepseek.final", "ts": 100.0, "response_text": "人不吭声，git倒是勤快。"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace()
        self.assertEqual(text, "人不吭声，git倒是勤快。")
        self.assertTrue(complete)

    def test_partial_stream_tokens_are_flagged_incomplete(self):
        self._write([
            {"source": "tenshi_reply.deepseek.stream", "ts": 100.0, "token": "前"},
            {"source": "tenshi_reply.deepseek.stream", "ts": 101.0, "token": "半截"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace()
        self.assertEqual(text, "前半截")
        self.assertFalse(complete)

    def test_partial_stream_tokens_win_over_older_complete_final(self):
        """既有口径：逐字流式内容最新，优先拿它（哪怕更早有一次已收尾的响应）。"""
        self._write([
            {"source": "tenshi_reply.deepseek.final", "ts": 100.0, "response_text": "旧的一整条回复"},
            {"source": "tenshi_reply.deepseek.stream", "ts": 200.0, "token": "新"},
            {"source": "tenshi_reply.deepseek.stream", "ts": 201.0, "token": "续写"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace()
        self.assertEqual(text, "新续写")
        self.assertFalse(complete)

    def test_bare_silence_is_never_recovered_as_reply(self):
        """7830 形状：SILENCE 是感知层控制符，不能被抢救成一句话。"""
        self._write([
            {"source": "tenshi_reply.deepseek.final", "ts": 100.0, "response_text": "SILENCE"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace()
        self.assertEqual(text, "")
        self.assertFalse(complete)

    def test_wrapped_silence_is_never_recovered_as_reply(self):
        for wrapped in ("`SILENCE`", "**SILENCE**", "silence"):
            self._write([
                {"source": "tenshi_reply.deepseek.final", "ts": 100.0, "response_text": wrapped},
            ])
            text, _complete = tenshi_utils.recover_reply_text_from_llm_trace()
            self.assertEqual(text, "", msg=f"wrapped={wrapped!r}")

    def test_final_with_only_streamed_tokens_is_incomplete(self):
        """claude 侧只有 streamed_tokens 留档、没有 response_text 时，保守当半截。"""
        self._write([
            {"source": "tenshi_reply.claude.final", "ts": 100.0, "streamed_tokens": ["甲", "乙"]},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace()
        self.assertEqual(text, "甲乙")
        self.assertFalse(complete)

    def test_since_ts_skips_older_records(self):
        self._write([
            {"source": "tenshi_reply.deepseek.final", "ts": 50.0, "response_text": "上一轮的回复"},
            {"source": "tenshi_reply.deepseek.final", "ts": 150.0, "response_text": "这一轮的回复"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=100.0)
        self.assertEqual(text, "这一轮的回复")
        self.assertTrue(complete)

    def test_missing_trace_file_returns_empty(self):
        os.remove(self.trace_path) if os.path.exists(self.trace_path) else None
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace()
        self.assertEqual(text, "")
        self.assertFalse(complete)

    def test_legacy_wrapper_returns_text_only(self):
        self._write([
            {"source": "tenshi_reply.deepseek.final", "ts": 100.0, "response_text": "完整回复"},
        ])
        self.assertEqual(tenshi_utils.recover_stream_text_from_llm_trace(), "完整回复")


    def test_perception_turn_is_never_recovered_as_reply(self):
        """感知层每轮写 begin/end 标记；区间内的 final 不是回复引擎的产出。"""
        self._write([
            {"source": "perception_layer.begin", "ts": 100.0, "scheduled_force": True},
            {
                "source": "tenshi_reply.deepseek.final",
                "ts": 105.0,
                "response_text": "08:00了。第二遍问——几点开口？",
            },
            {"source": "perception_layer.end", "ts": 106.0},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=99.0)
        self.assertEqual(text, "")
        self.assertFalse(complete)

    def test_perception_stream_tokens_are_dropped_too(self):
        self._write([
            {"source": "perception_layer.begin", "ts": 100.0},
            {"source": "tenshi_reply.deepseek.stream", "ts": 101.0, "token": "感知层"},
            {"source": "tenshi_reply.deepseek.stream", "ts": 102.0, "token": "的正文"},
            {"source": "perception_layer.end", "ts": 103.0},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=99.0)
        self.assertEqual(text, "")
        self.assertFalse(complete)

    def test_reply_final_older_than_perception_turn_is_still_recovered(self):
        """窗口内既有真回复、又有感知层轮次时，取区间之外的那条。"""
        self._write([
            {"source": "tenshi_reply.deepseek.final", "ts": 90.0, "response_text": "这一轮真的回复"},
            {"source": "perception_layer.begin", "ts": 100.0},
            {"source": "tenshi_reply.deepseek.final", "ts": 105.0, "response_text": "感知层那句话"},
            {"source": "perception_layer.end", "ts": 106.0},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=80.0)
        self.assertEqual(text, "这一轮真的回复")
        self.assertTrue(complete)

    def test_reply_final_after_perception_turn_is_still_recovered(self):
        """回归：感知层收工之后回复引擎自己写下的 trace 必须照旧能抢救回来。

        只用 begin 做「之后全部作废」的粗口径会把这种情况一起判死 —— 真是回复脚本
        被杀/超时时，连自己的半截回复都救不回来。
        """
        self._write([
            {"source": "perception_layer.begin", "ts": 100.0},
            {"source": "tenshi_reply.deepseek.final", "ts": 105.0, "response_text": "感知层那句话"},
            {"source": "perception_layer.end", "ts": 106.0},
            {"source": "tenshi_reply.deepseek.final", "ts": 120.0, "response_text": "回复引擎的回复"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=99.0)
        self.assertEqual(text, "回复引擎的回复")
        self.assertTrue(complete)

    def test_closed_perception_turn_outside_the_window_is_ignored(self):
        """起止都早于 since_ts 的感知层轮次不参与本轮隔离。"""
        self._write([
            {"source": "perception_layer.begin", "ts": 10.0},
            {"source": "perception_layer.end", "ts": 20.0},
            {"source": "tenshi_reply.deepseek.final", "ts": 105.0, "response_text": "正常回复"},
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=100.0)
        self.assertEqual(text, "正常回复")
        self.assertTrue(complete)

    def test_unclosed_perception_turn_is_capped_not_open_ended(self):
        """只有 begin（进程被杀）时按最长一轮时长封顶，不会判死之后的所有候选。"""
        begin = 100.0
        self._write([
            {"source": "perception_layer.begin", "ts": begin},
            {"source": "tenshi_reply.deepseek.final", "ts": begin + 5, "response_text": "感知层那句话"},
            {
                "source": "tenshi_reply.deepseek.final",
                "ts": begin + tenshi_utils.PERCEPTION_TRACE_MAX_SPAN_SEC + 1,
                "response_text": "很久之后的回复",
            },
        ])
        text, complete = tenshi_utils.recover_reply_text_from_llm_trace(since_ts=90.0)
        self.assertEqual(text, "很久之后的回复")
        self.assertTrue(complete)


if __name__ == "__main__":
    unittest.main()
