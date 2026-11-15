# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic regression cases for the post-migration review findings."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from _rp_cancellation import CallCancelled, ensure_active
from llm_compat import LlmDeadlineExceeded, call_with_deadline
from rag_modules import TenshiRAG
import reply_streaming
import telegram_tenshi_logger as ttl
import tenshi_reply as tr
from tenshi_tool_exec import ToolLoopState, execute_tenshi_tool


class ReviewRegressions(unittest.TestCase):
    def test_logger_lock_failure_is_retryable_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(ttl, 'Bot'), \
                 mock.patch.object(ttl.tenshi_config, 'ADMIN_ID', 1), \
                 mock.patch.object(ttl, 'LOG_FILE', str(Path(temporary) / 'log.json')), \
                 mock.patch.object(ttl, 'FileLock', side_effect=TimeoutError('synthetic lock timeout')):
                result = asyncio.run(ttl.process_single_message('synthetic', 1, 1, 1))
        self.assertEqual(result, 'retryable_failure')

    def test_quarantined_turn_is_not_implicitly_retried(self):
        from tenshi_utils import select_unreplied_run
        history = [
            {"msg_id": "new", "user_query": "synthetic new turn"},
            {"msg_id": "unknown", "rp_delivery_quarantined": True},
            {"msg_id": "old", "user_query": "synthetic old turn"},
        ]
        self.assertEqual([item["msg_id"] for item in select_unreplied_run(history)], ["new"])

    def test_runtime_result_is_atomic_private_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / 'outcome.json'
            with mock.patch.dict(os.environ, {'RP_RESULT_PATH': str(result)}):
                ttl._write_runtime_result('no_send')
            self.assertEqual(json.loads(result.read_text()), {'status': 'no_send'})
            self.assertFalse(result.with_suffix('.json.tmp').exists())

    def test_evaluation_never_enters_tool_chain(self):
        for provider in ('claude', 'gpt', 'gemini', 'grok', 'glm', 'deepseek'):
            with self.subTest(provider=provider), \
                 mock.patch.object(tr, '_plain_text_call', return_value='synthetic') as plain, \
                 mock.patch.object(tr, '_run_path_first_provider') as tool_path, \
                 mock.patch.object(tr, '_run_claude_model_chain') as claude:
                result = tr.call_dialog_model_once(
                    provider=provider, model_id='synthetic', system_prompt='sys',
                    flat_system_prompt='sys', messages_payload=[],
                    filtered_claude_tools=[{'name': 'note_patch'}],
                )
                self.assertEqual(result, 'synthetic')
                plain.assert_called_once()
                tool_path.assert_not_called()
                claude.assert_not_called()

    def test_evaluation_executor_rejects_writes_even_if_adapter_is_faulty(self):
        write = mock.Mock()
        def incorrect_adapter(*args, **kwargs):
            return execute_tenshi_tool(
                'note_patch', {}, ToolLoopState(), search_memory_fn=mock.Mock(),
                search_web_fn=mock.Mock(), read_config_file_fn=mock.Mock(),
                read_note_fn=mock.Mock(), note_patch_fn=write,
                schedule_perception_fn=mock.Mock(),
            )
        with mock.patch.object(tr, '_plain_text_call', side_effect=incorrect_adapter):
            result = tr.call_dialog_model_once(
                provider='claude', model_id='synthetic', system_prompt='sys',
                flat_system_prompt='sys', messages_payload=[],
            )
        self.assertEqual(result, '')
        write.assert_not_called()

    def test_no_tool_graded_rewrite_uses_plain_adapter(self):
        with mock.patch.object(tr, '_build_dialog_model_chain', return_value=['synthetic']), \
             mock.patch.object(tr, '_provider_from_model_id', return_value='gpt'), \
             mock.patch.object(tr, '_plain_text_call', return_value='rewritten') as plain, \
             mock.patch.object(tr, '_run_path_first_provider') as tool_path:
            result = tr.invoke_dialog_model_chain(
                system_prompt='sys', messages_payload=[], tools_enabled=False,
            )
        self.assertEqual(result, ('rewritten', '', False))
        plain.assert_called_once()
        tool_path.assert_not_called()

    def test_completed_wave_results_survive_expired_budget(self):
        futures = []
        values = ['', {'nsfw': False}, {'intent': 'memory'}]
        for value in values:
            future = Future()
            future.set_result(value)
            futures.append(future)
        # Clock expires just before collection; all completed results remain usable.
        clock = iter([0, 1, 2, 3, 4, 5, 6, 7])
        with mock.patch.object(tr, '_submit_bounded', side_effect=futures), \
             mock.patch.object(tr.time, 'monotonic', side_effect=lambda: next(clock, 100)), \
             mock.patch.object(tr.tenshi_config, 'REPLY_WAVE1_TIMEOUT_SEC', 0.1), \
             mock.patch.object(tr, '_degrade_future', side_effect=lambda n, f, d, t: f.result()):
            result = tr._pre_reply_wave1([], 'synthetic', '')
        self.assertEqual(result['speculative_intent'], {'intent': 'memory'})
        self.assertTrue(result['intent_attempted'])
        self.assertIsNone(result['speculative_embedding'])

    def test_proxy_only_grok_enters_actual_available_tiers(self):
        tiers = [{'provider': 'grok', 'models': ['synthetic'], 'paths': [{'api_key': 'synthetic'}]}]
        with mock.patch.object(tr, 'MAGMAROUTER_API_KEY', ''), \
             mock.patch.object(tr, 'OPENROUTER_API_KEY', ''), \
             mock.patch.object(tr, 'GROK_API_KEY', ''), \
             mock.patch.object(tr, 'build_grok_slot_tiers', return_value=tiers), \
             mock.patch.object(tr, 'run_slot_path_first', return_value='synthetic') as run:
            result = tr._run_path_first_provider('grok', ['grok-synthetic'], 'sys', [], None, [], set())
        self.assertEqual(result, 'synthetic')
        self.assertEqual(run.call_args.args[0], tiers)

    def test_actual_fallback_model_is_preserved_for_evaluation(self):
        path = {"name": "Synthetic", "api_key": "synthetic", "base_url": "https://example.invalid"}
        def route(models, paths, invoke, **kwargs):
            return invoke(models[1], paths[0])
        with mock.patch.object(tr, 'build_gpt_paths', return_value=[path]), \
             mock.patch.object(tr.provider_health, 'filter_paths', side_effect=lambda p, paths, **k: paths), \
             mock.patch.object(tr.provider_health, 'model_path_state', return_value=None), \
             mock.patch.object(tr, 'run_path_first', side_effect=route), \
             mock.patch.object(tr, '_bounded_call', return_value=('synthetic', '', False)):
            result = tr._run_path_first_provider('gpt', ['failed', 'winner'], 'sys', [], None, [], set())
        self.assertEqual(result.model_id, 'winner')
        self.assertEqual(result.provider, 'gpt')
        self.assertEqual(result, ('synthetic', '', False))

    def test_timed_out_worker_cannot_publish_or_write_after_returning(self):
        release, ended = threading.Event(), threading.Event()
        published = []
        def worker():
            try:
                release.wait(1)
                execute_tenshi_tool(
                    'note_patch', {}, ToolLoopState(), search_memory_fn=mock.Mock(),
                    search_web_fn=mock.Mock(), read_config_file_fn=mock.Mock(),
                    read_note_fn=mock.Mock(),
                    note_patch_fn=lambda **kwargs: published.append('late write'),
                    schedule_perception_fn=mock.Mock(),
                )
            finally:
                ended.set()
        with self.assertRaises(LlmDeadlineExceeded):
            call_with_deadline(worker, timeout_sec=0.02)
        release.set()
        self.assertTrue(ended.wait(1))
        self.assertEqual(published, [])

    def test_nested_deadline_keeps_parent_cancelled(self):
        ended = threading.Event()
        published = []
        def inner():
            time.sleep(0.06)
            try:
                ensure_active()
                published.append('wrong')
            finally:
                ended.set()
        with self.assertRaises(LlmDeadlineExceeded):
            call_with_deadline(
                lambda: call_with_deadline(inner, timeout_sec=1), timeout_sec=0.02,
            )
        self.assertTrue(ended.wait(1))
        self.assertEqual(published, [])

    def rag(self, client):
        rag = TenshiRAG.__new__(TenshiRAG)
        rag.client = client
        rag.embedding_dim = 2
        rag.embedding_model = 'synthetic'
        rag.embedding_cache = {}
        rag.memory_manager = None
        rag.cache_modified = False
        return rag

    def test_embedding_singleflight_shares_four_concurrent_requests(self):
        started, release = threading.Event(), threading.Event()
        def embed(*args, **kwargs):
            started.set()
            release.wait(1)
            return [1, 2]
        client = mock.Mock(embed=mock.Mock(side_effect=embed))
        rag = self.rag(client)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(rag.get_embedding, 'same', 'query') for _ in range(4)]
            self.assertTrue(started.wait(1))
            time.sleep(0.03)
            release.set()
            values = [future.result() for future in futures]
        self.assertEqual(client.embed.call_count, 1)
        for value in values:
            np.testing.assert_array_equal(value, [1, 2])

    def test_embedding_batches_deduplicate_preserve_order_and_purpose(self):
        client = mock.Mock()
        client.embed_batch.return_value = [[1, 2], [3, 4]]
        client.embed.return_value = [9, 8]
        rag = self.rag(client)
        values = rag.get_embeddings(['a', 'a', '', 'b'])
        client.embed_batch.assert_called_once_with(['a', 'b'], purpose='document')
        np.testing.assert_array_equal(values, [[1, 2], [1, 2], [0, 0], [3, 4]])
        np.testing.assert_array_equal(rag.get_embedding('a', 'query'), [9, 8])
        client.embed.assert_called_once()

    def test_failed_embedding_not_cached_and_waiter_unblocked(self):
        client = mock.Mock(embed=mock.Mock(side_effect=ValueError('synthetic')))
        rag = self.rag(client)
        np.testing.assert_array_equal(rag.get_embedding('a'), [0, 0])
        np.testing.assert_array_equal(rag.get_embedding('a'), [0, 0])
        self.assertEqual(client.embed.call_count, 2)
        self.assertEqual(rag.embedding_cache, {})
        self.assertEqual(rag._embedding_inflight, {})

    def test_summary_shares_one_memory_manager(self):
        import log_update
        manager = object()
        with mock.patch.object(log_update, '_rag', None), \
             mock.patch.object(log_update, '_mem_mgr', manager), \
             mock.patch.object(log_update, 'TenshiRAG') as constructor:
            log_update.ensure_rag()
        constructor.assert_called_once_with(memory_manager=manager)

    def test_final_delivery_failure_does_not_report_sent(self):
        streamer = reply_streaming.TelegramStreamer('synthetic', 1)
        streamer.is_active = True
        with mock.patch.object(streamer, '_send_edit'), \
             mock.patch.object(streamer, '_send_followup', return_value=None):
            streamer.finalize_with_text('synthetic')
        self.assertEqual(streamer.delivery_status, 'delivery_unknown')

    def test_partial_bubble_failure_does_not_report_sent(self):
        streamer = reply_streaming.TelegramStreamer('synthetic', 1)
        streamer.is_active = True
        streamer.message_id = 1
        streamer.last_sent_text = 'first'
        with mock.patch.object(streamer, '_send_edit'), \
             mock.patch.object(streamer, '_send_followup', return_value=None), \
             mock.patch.object(reply_streaming, '_followup_delay_sec', return_value=0):
            streamer.finalize_with_text('first<br>second')
        self.assertEqual(streamer.delivery_status, 'delivery_unknown')


if __name__ == '__main__':
    unittest.main()
