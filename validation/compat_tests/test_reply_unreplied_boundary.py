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


os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")
os.environ.pop("PERCEPTION_SCHEDULE_SYNC_URL", None)
os.environ.pop("PERCEPTION_SCHEDULE_SYNC_SECRET", None)

import tenshi_utils
from tenshi_utils import select_unreplied_messages, select_unreplied_run

PERCEPTION_IMPORT_ERROR: Exception | None = None
try:
    import perception_layer
except Exception as e:
    PERCEPTION_IMPORT_ERROR = e


def _conv(*history):
    return {"conversation_id": "c1", "user_id": "u1", "chat_history": list(history)}


def _user(msg_id, ts, answered=False, replied_flag=None):
    msg = {"msg_id": msg_id, "time": ts, "user_query": f"消息 {msg_id}"}
    if answered:
        msg["bot_answer"] = f"回复 {msg_id}"
        msg["replied_flag"] = True if replied_flag is None else replied_flag
    elif replied_flag is not None:
        msg["replied_flag"] = replied_flag
    return msg


def _proactive(ts, text="主动起念"):
    return {
        "msg_id": f"proactive_{ts}",
        "time": ts,
        "user_query": "[系统级感知层触发 - 天子主动起念]",
        "bot_answer": text,
        "replied_flag": True,
        "perception_event": True,
    }


def _silence(ts):
    return {
        "msg_id": f"perception_{ts}",
        "time": ts,
        "user_query": "[感知层上报世界状态，天子选择了沉默不语]",
        "perception_event": True,
        "perception_prompt": "…",
        "bot_decision": "SILENCE",
        "replied_flag": True,
    }


class UnrepliedBoundarySelectionTest(unittest.TestCase):
    """ca141f1 / 9f163353 的两种形状：感知层记录不得挡住用户消息。"""

    def test_proactive_record_ahead_of_user_message_no_longer_masks_it(self):
        """ca141f1：proactive(…726) 排在 7945(…720) 前面，7945 仍要被选中。"""
        history = [
            _proactive(1789599726, "08:00了。第二遍问——几点开口？"),
            _user("7945", 1789599720),
            _user("7943", 1789597926, answered=True),
        ]
        run = select_unreplied_run(history)
        self.assertEqual([m["msg_id"] for m in run], ["7945"])

    def test_bare_silence_record_ahead_of_user_message_no_longer_masks_it(self):
        """9f163353：沉默记录（replied_flag=True、无 bot_answer）同样不是边界。"""
        history = [
            _silence(1789520039),
            _user("7912", 1789520016),
            _user("7909", 1789518237, answered=True),
        ]
        run = select_unreplied_run(history)
        self.assertEqual([m["msg_id"] for m in run], ["7912"])

    def test_contiguous_unreplied_run_keeps_chronological_order(self):
        history = [
            _user("m3", 300),
            _user("m2", 200),
            _user("m1", 100, answered=True),
        ]
        run = select_unreplied_run(history)
        self.assertEqual([m["msg_id"] for m in run], ["m2", "m3"])

    def test_perception_records_inside_the_run_are_not_collected(self):
        """夹在未回消息之间的感知层记录（时间倒序）不能被当成待回复消息。"""
        history = [
            _user("m3", 300),
            _proactive(250),
            _user("m2", 200),
            _user("m1", 100, answered=True),
        ]
        run = select_unreplied_run(history)
        self.assertEqual([m["msg_id"] for m in run], ["m2", "m3"])

    def test_stops_at_first_answered_user_message(self):
        history = [
            _user("m3", 300, answered=True),
            _user("m2", 200),
            _user("m1", 100),
        ]
        self.assertEqual(select_unreplied_run(history), [])

    def test_replied_flag_alone_ends_the_run(self):
        """回复失败但已标记 replied_flag 的消息（旧口径）仍然终结这一串。"""
        history = [
            _user("m2", 200),
            _user("m1", 100, replied_flag=True),
        ]
        self.assertEqual([m["msg_id"] for m in select_unreplied_run(history)], ["m2"])

    def test_empty_and_garbage_history(self):
        self.assertEqual(select_unreplied_run(None), [])
        self.assertEqual(select_unreplied_run([]), [])
        self.assertEqual(select_unreplied_run(["not-a-dict", 42]), [])

    def test_select_messages_picks_first_conversation_with_pending(self):
        doc = [
            _conv(_user("a1", 300, answered=True)),
            _conv(_silence(250), _user("b1", 200)),
        ]
        conv, run = select_unreplied_messages(doc)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["conversation_id"], "c1")
        self.assertEqual([m["msg_id"] for m in run], ["b1"])

    def test_select_messages_returns_empty_when_all_answered(self):
        conv, run = select_unreplied_messages([_conv(_user("a1", 300, answered=True))])
        self.assertIsNone(conv)
        self.assertEqual(run, [])

    def test_log_has_pending_user_reply_agrees_with_engine(self):
        """感知层补跑用的判定也不能被「只有感知层记录」误触发。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tenshi_log.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([_conv(_proactive(100), _user("m1", 90, answered=True))], f)
            if PERCEPTION_IMPORT_ERROR is None:
                self.assertFalse(perception_layer._log_has_pending_user_reply(path))

            with open(path, "w", encoding="utf-8") as f:
                json.dump([_conv(_proactive(100), _user("m2", 90))], f)
            if PERCEPTION_IMPORT_ERROR is None:
                self.assertTrue(perception_layer._log_has_pending_user_reply(path))

            with open(path, "w", encoding="utf-8") as f:
                json.dump([_conv(_user("m2", 90, replied_flag=True), _user("m1", 80))], f)
            if PERCEPTION_IMPORT_ERROR is None:

                self.assertFalse(perception_layer._log_has_pending_user_reply(path))


@unittest.skipIf(
    PERCEPTION_IMPORT_ERROR is not None,
    f"依赖不可用：{PERCEPTION_IMPORT_ERROR}",
)
class PerceptionRepoSiblingResolutionTest(unittest.TestCase):
    """Calling 代跑布局（小写 tenshi/、elaina/）下，感知层必须能找到兄弟仓库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.join(self._tmp.name, "Calling", "Calling")
        os.makedirs(self.workspace, exist_ok=True)
        self._orig_file = perception_layer.__file__
        self._orig_root = perception_layer.tenshi_config.PROJECT_ROOT

    def tearDown(self):
        perception_layer.__file__ = self._orig_file
        perception_layer.tenshi_config.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def _make_repo(self, dirname: str, *, pending: bool) -> str:
        root = os.path.join(self.workspace, dirname)
        os.makedirs(os.path.join(root, "data"), exist_ok=True)
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        history = [_user("m2", 200)] if pending else [_user("m1", 100, answered=True)]
        with open(os.path.join(root, "data", "tenshi_log.json"), "w", encoding="utf-8") as f:
            json.dump([_conv(*history)], f)
        with open(os.path.join(root, "src", "tenshi_reply.py"), "w", encoding="utf-8") as f:
            f.write("# stub\n")
        return root

    def _host_in(self, dirname: str):
        """把 perception_layer 伪装成住在 workspace/<dirname> 里。"""
        root = os.path.join(self.workspace, dirname)
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        perception_layer.__file__ = os.path.join(root, "src", "perception_layer.py")
        perception_layer.tenshi_config.PROJECT_ROOT = root
        return root

    def _assert_single_call_for(self, calls, repo_root):
        self.assertEqual(len(calls), 1, f"应只补跑一次，实际 {calls}")
        expected = os.path.normcase(os.path.realpath(os.path.join(repo_root, "src", "tenshi_reply.py")))
        got = os.path.normcase(os.path.realpath(calls[0][0][1]))
        self.assertEqual(got, expected)

    def test_lowercase_sibling_is_isolated(self):
        self._host_in("Calling")
        tenshi_root = self._make_repo("tenshi", pending=True)

        calls = []
        orig_run = perception_layer.subprocess.run
        orig_called = perception_layer._called_from_perception_workflow
        perception_layer._called_from_perception_workflow = lambda: True
        perception_layer.subprocess.run = lambda cmd, **kw: calls.append((cmd, kw))
        try:
            handled = perception_layer._maybe_reply_if_unreplied_before_probability()
        finally:
            perception_layer.subprocess.run = orig_run
            perception_layer._called_from_perception_workflow = orig_called

        self.assertFalse(handled, "公开引擎不得读取其他角色的私密仓")
        self.assertEqual(calls, [])

    def test_exact_case_sibling_is_isolated(self):
        self._host_in("Calling")
        tenshi_root = self._make_repo("Tenshi", pending=True)

        calls = []
        orig_run = perception_layer.subprocess.run
        orig_called = perception_layer._called_from_perception_workflow
        perception_layer._called_from_perception_workflow = lambda: True
        perception_layer.subprocess.run = lambda cmd, **kw: calls.append((cmd, kw))
        try:
            handled = perception_layer._maybe_reply_if_unreplied_before_probability()
        finally:
            perception_layer.subprocess.run = orig_run
            perception_layer._called_from_perception_workflow = orig_called

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_own_repo_host_is_used_first(self):
        """本仓库就是 tenshi/ 时（旧布局 / 逐仓库运行），直接用自己的日志、只跑一次。"""
        tenshi_root = self._host_in("tenshi")
        os.makedirs(os.path.join(tenshi_root, "data"), exist_ok=True)
        with open(os.path.join(tenshi_root, "data", "tenshi_log.json"), "w", encoding="utf-8") as f:
            json.dump([_conv(_user("m2", 200))], f)
        with open(os.path.join(tenshi_root, "src", "tenshi_reply.py"), "w", encoding="utf-8") as f:
            f.write("# stub\n")

        calls = []
        orig_run = perception_layer.subprocess.run
        orig_called = perception_layer._called_from_perception_workflow
        perception_layer._called_from_perception_workflow = lambda: True
        perception_layer.subprocess.run = lambda cmd, **kw: calls.append((cmd, kw))
        try:
            handled = perception_layer._maybe_reply_if_unreplied_before_probability()
        finally:
            perception_layer.subprocess.run = orig_run
            perception_layer._called_from_perception_workflow = orig_called

        self.assertTrue(handled)
        self._assert_single_call_for(calls, tenshi_root)

    def test_no_pending_message_does_not_invoke_anything(self):
        self._host_in("Calling")
        self._make_repo("tenshi", pending=False)

        calls = []
        orig_run = perception_layer.subprocess.run
        orig_called = perception_layer._called_from_perception_workflow
        perception_layer._called_from_perception_workflow = lambda: True
        perception_layer.subprocess.run = lambda cmd, **kw: calls.append((cmd, kw))
        try:
            handled = perception_layer._maybe_reply_if_unreplied_before_probability()
        finally:
            perception_layer.subprocess.run = orig_run
            perception_layer._called_from_perception_workflow = orig_called

        self.assertFalse(handled)
        self.assertEqual(calls, [])


@unittest.skipIf(
    PERCEPTION_IMPORT_ERROR is not None,
    f"依赖不可用：{PERCEPTION_IMPORT_ERROR}",
)
class PendingUserReplyRecheckTest(unittest.TestCase):
    """发送前复检：有未回用户消息时，感知层这次起念必须让位。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_log = perception_layer.tenshi_config.LOG_FILE_PATH
        self._orig_lock = perception_layer.tenshi_config.LOCK_FILE_PATH
        self.log_path = os.path.join(self._tmp.name, "tenshi_log.json")
        perception_layer.tenshi_config.LOG_FILE_PATH = self.log_path
        perception_layer.tenshi_config.LOCK_FILE_PATH = self.log_path

    def tearDown(self):
        perception_layer.tenshi_config.LOG_FILE_PATH = self._orig_log
        perception_layer.tenshi_config.LOCK_FILE_PATH = self._orig_lock
        self._tmp.cleanup()

    def _write(self, history):
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump([_conv(*history)], f)

    def test_detects_pending_user_message_behind_perception_record(self):
        self._write([_proactive(200), _user("m1", 190), _user("m0", 100, answered=True)])
        self.assertTrue(perception_layer._has_pending_user_reply())

    def test_no_pending_when_only_perception_records(self):
        self._write([_proactive(200), _silence(150), _user("m0", 100, answered=True)])
        self.assertFalse(perception_layer._has_pending_user_reply())

    def test_missing_log_is_not_pending(self):
        self.assertFalse(perception_layer._has_pending_user_reply())


if __name__ == "__main__":
    unittest.main()
