# SPDX-License-Identifier: AGPL-3.0-only
import os
import sys
import json
import time
import logging
import random
import re
import argparse
from datetime import datetime, timezone, timedelta
import subprocess
import asyncio
from telegram import Bot


sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import tenshi_config
import weather_utils
from tenshi_utils import (
    FileLock,
    setup_logger,
    ensure_tenshi_log_newest_first,
    prune_empty_fields,
    dedupe_back_to_back_long_repeat,
    append_llm_trace,
    select_unreplied_run,
    PERCEPTION_TRACE_BEGIN_SOURCE,
    PERCEPTION_TRACE_END_SOURCE,
)
from tenshi_reply import (
    build_messages_payload_from_chat_history,
    CLAUDE_TOOLS,
    format_private_notes_for_system_prompt,
    note_patch,
    invoke_dialog_model_chain,
    run_typing_stage,
    _build_dialog_model_chain,
    build_choice_violation_text,
    rewrite_choice_text,
)
import reply_two_stage
import bond_state
import stickers
import reactions



from config.persona import (
    PERCEPTION_EVENT_FORCE_DESCRIPTION,
    PERCEPTION_EVENT_FORCE_MANUAL_NOTE,
)
from config.prompts import (
    PERCEPTION_EVENT_AUTO_DESCRIPTION,
    PERCEPTION_EVENT_SCHEDULED_DESCRIPTION,
    PERCEPTION_EVENT_SCHEDULED_MANUAL_NOTE_TEMPLATE,
    PERCEPTION_PROMPT_TEMPLATE,
    PERCEPTION_SOURCE_QUERY_PREFIX_TEMPLATE,
    PERCEPTION_TIME_ANCHOR_DAY_TEMPLATE,
    PERCEPTION_TIME_ANCHOR_FALLBACK_TEMPLATE,
    PERCEPTION_TIME_ANCHOR_NIGHT_OWL_NOTE_TEMPLATE,
    PERCEPTION_TIME_ANCHOR_NIGHT_OWL_TEMPLATE,
    PERCEPTION_TIME_OF_DAY_AFTERNOON,
    PERCEPTION_TIME_OF_DAY_EARLY,
    PERCEPTION_TIME_OF_DAY_EVENING,
    PERCEPTION_TIME_OF_DAY_LATE_NIGHT,
    PERCEPTION_TIME_OF_DAY_MORNING,
    PERCEPTION_TIME_OF_DAY_NIGHT,
    PERCEPTION_TIME_OF_DAY_NOON,
    TIMESTAMP_PREFIX_SYSTEM_NOTE,
)
import reply_budget
import deferred_intent
from llm_tool_text import has_tool_call_markup, sanitize_model_text
from message_split import (
    split_reply,
    describe_split,
    MAX_MESSAGE_CHARS,
    normalize_br_markup,
    unescape_model_text,
)
from rag_modules import TenshiRAG
from perception_schedule import (
    complete_scheduled_trigger,
    is_reply_retry_note,
    load_pending_schedule_entry,
)
from perception_state import get_last_silence_at, write_last_silence_at

logger = setup_logger("TenshiPerception", tenshi_config.DEBUG_LOG_PATH, level=logging.DEBUG)

PERCEPTION_SKIP_EXIT_CODE = 2
POST_SILENCE_COOLDOWN_SEC = 2 * 3600
POST_SILENCE_RAMP_END_SEC = 3 * 3600
COMMITS_LOOKBACK_HOURS = 24

MODELS_UNAVAILABLE_MARKER = "[scheduled_force: all models failed]"





PERCEPTION_TRACE_MARKER_SOURCE = PERCEPTION_TRACE_BEGIN_SOURCE

_TENSHI_NOTE_FILENAME = "Tenshi.json"


_BOT_COMMIT_KEYWORDS = tuple(
    f"{tenshi_config.ROLE_NAME_SHORT}{suffix}"
    for suffix in (
        "已阅",
        "例行巡狩",
        "感知层",
        "周度",
        "亲自",
        "应急",
        "例行整顿",
    )
)

from perception_policy import (
    _extract_silence_from_final_text,
    _is_real_user_message,
    _line_declares_silence,
    _normalize_for_silence_compare,
    _perception_bot_answer_is_silence_only,
    _strip_leading_log_timestamp_prefix,
)
from error_text import format_exception_inline


def get_recent_commits() -> str | None:
    """Return human-readable commits from the last 24h, or None if none worth showing.

    提交时间统一渲染为北京时间（UTC+8），不跟随 runner 的系统 TZ。
    """
    try:
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"], check=False)
        result = subprocess.run(
            [
                "git", "log",
                f"--since={COMMITS_LOOKBACK_HOURS} hours ago",
                "--pretty=format:%h%x1f%an%x1f%at%x1f%s",
                "--no-merges",
            ],
            cwd=os.getenv("RP_CODE_REPO_ROOT") or tenshi_config.PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        if not result.stdout.strip():
            return None

        commit_tz = timezone(timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))
        filtered = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            short, author, ts_raw, subject = parts
            try:
                when = datetime.fromtimestamp(float(ts_raw), commit_tz).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                when = ts_raw
            rendered = f"{short} - {author} ({when}): {subject}"
            is_bot = (
                any(kw in rendered for kw in _BOT_COMMIT_KEYWORDS)
                or "skip ci" in rendered.lower()
            )
            if not is_bot:
                filtered.append(rendered)
        return "\n".join(filtered) if filtered else None
    except subprocess.CalledProcessError as e:
        logger.error(f"Git 命令执行失败：{e.stderr}")
        return None
    except Exception as e:
        logger.error(f"获取 Git 提交记录失败：{format_exception_inline(e)}")
        return None

def get_last_interaction():
    log_path = tenshi_config.LOG_FILE_PATH
    if not os.path.exists(log_path):
        return None, 0, 0
    try:
        with FileLock(tenshi_config.LOCK_FILE_PATH, timeout=10):
            with open(log_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            data, _ = ensure_tenshi_log_newest_first(data)
            if not data:
                return None, 0, 0

            target_conv = data[0]
            history = target_conv.get('chat_history', [])

            last_user_interaction_time = 0
            last_proactive_time = 0






            for msg in history:
                if not isinstance(msg, dict):
                    continue

                ts = msg.get('time', 0)
                if not isinstance(ts, (int, float)):
                    continue

                if msg.get('perception_event'):
                    ba = msg.get('bot_answer')
                    if last_proactive_time == 0 and ba and not _perception_bot_answer_is_silence_only(ba):

                        last_proactive_time = int(ts)
                    continue




                if last_user_interaction_time == 0 and _is_real_user_message(msg):
                    last_user_interaction_time = int(ts)


            return target_conv.get('user_id'), last_user_interaction_time, last_proactive_time
    except Exception as e:
        logger.error(f"读取日志失败：{format_exception_inline(e)}")
        return None, 0, 0


def get_last_perception_silence_time_from_log() -> int:
    """（兼容回退）从 tenshi_log.json 里最后一条 SILENCE 记录取时间戳。

    新口径下「纯沉默」不再写进对话日志，所以正常路径不该依赖这里；
    它只用于部署前的历史沉默记录，以及 perception_state 缺失时的兜底。
    """
    log_path = tenshi_config.LOG_FILE_PATH
    if not os.path.exists(log_path):
        return 0
    try:
        with FileLock(tenshi_config.LOCK_FILE_PATH, timeout=10):
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        data, _ = ensure_tenshi_log_newest_first(data)
        if not data:
            return 0
        history = data[0].get("chat_history", [])
        for msg in history:
            if not isinstance(msg, dict) or not msg.get("perception_event"):
                continue
            if str(msg.get("bot_decision", "")).upper() != "SILENCE":
                continue
            ts = msg.get("time", 0)
            if isinstance(ts, (int, float)) and ts > 0:
                return int(ts)
        return 0
    except Exception as e:
        logger.error(f"读取感知层 SILENCE 记录失败：{format_exception_inline(e)}")
        return 0


def get_last_perception_silence_time() -> int:
    """上次感知层选择 SILENCE 的时间戳（冷却/触发间隔计算的输入）。

    数据源已从对话日志挪到 data/perception_state.json（纯沉默不再存档），
    但返回值的语义与口径保持不变；状态文件缺失时回退扫历史日志。
    """
    return get_last_silence_at()


def _is_pure_silence_record(*, thinking_content: str, clipped_body: str, tool_mutated: bool) -> bool:
    """本次 SILENCE 是否「纯沉默」——没有任何额外信息可存档。

    纯沉默 = 判定 SILENCE 且：
      * 没有 <thinking> 内心独白；
      * 没有「正文 + SILENCE」组合里被截出来的正文（否则正文会进私密笔记）；
      * 本轮没有产生副作用的工具调用（note_patch 改 Tenshi.json / schedule_perception 改定时队列）。

    纯沉默不写 data/tenshi_log.json（省掉对上下文窗口的纯噪声），但沉默时刻照旧写
    data/perception_state.json —— 该文件必然变动，工作流照旧 commit + push。
    """
    if (thinking_content or "").strip():
        return False
    if (clipped_body or "").strip():
        return False
    if tool_mutated:
        return False
    return True


def _snapshot_mutation_targets() -> dict | None:
    """在调用模型之前，记录「工具副作用落地文件」的指纹；失败返回 None（视为无副作用）。

    覆盖的三处工具落地：
      * Tenshi.json          —— note_patch
      * data/perception_schedule.json —— schedule_perception
      * data/tenshi_log.json —— note_patch 等工具可能触发的日志写入
        （注意：纯沉默自身不写日志，所以这里不会被本次存档行为误判）
    """
    try:
        targets = [
            os.path.join(tenshi_config.PROJECT_ROOT, _TENSHI_NOTE_FILENAME),
            os.path.join(tenshi_config.PROJECT_ROOT, "data", "perception_schedule.json"),
            tenshi_config.LOG_FILE_PATH,
        ]
        shot = {}
        for path in targets:
            try:
                st = os.stat(path)
                shot[path] = (st.st_mtime_ns, st.st_size)
            except OSError:
                shot[path] = None
        return shot
    except Exception as e:
        logger.debug(f"工具副作用快照失败（按无副作用处理）：{format_exception_inline(e)}")
        return None


def _tool_produced_side_effects(before: dict | None) -> bool:
    """对比快照：工具是否真的改了文件（只读型工具调用不算额外信息）。"""
    if not before:
        return False
    after = _snapshot_mutation_targets()
    if not after:
        return False
    try:
        for path, old in before.items():
            if after.get(path) != old:
                return True
    except Exception as e:
        logger.debug(f"工具副作用比对失败（按无副作用处理）：{format_exception_inline(e)}")
    return False


_SILENCE_BAND_SHORT_SEC = 1 * 3600
_SILENCE_BAND_MID_SEC = 4 * 3600
_SILENCE_BAND_LONG_SEC = 12 * 3600
_PROB_AT_MID_SEC = 0.30
_PROB_AT_LONG_SEC = 0.75


def _compute_silence_pass_probability(
    silence_duration_sec: float,
    current_time_ts: float,
    last_proactive_time: int,
) -> float:
    """按「距上次真实对话的静默时长」给出随机触发放行概率。

    分档（边界连续，无跳变）：
        <1h        → 0.10
        1h ~ 4h    → 0.30
        4h ~ 12h   → 0.30 线性升到 0.75
        >=12h      → 0.75

    另有一条独立上限：距上次「感知层开口」不足 1h 时，概率封顶 0.25（min 语义）。

    曾用 `> 12h → 0.75` 且爬坡终点写 0.40 增量，导致 12h 整点从 0.70 跳到 0.75。
    现在爬坡终点直接取 0.75，12h 处两侧同为 0.75，连续。
    """
    if silence_duration_sec < _SILENCE_BAND_SHORT_SEC:
        prob = 0.10
    elif silence_duration_sec < _SILENCE_BAND_MID_SEC:
        prob = _PROB_AT_MID_SEC
    elif silence_duration_sec < _SILENCE_BAND_LONG_SEC:
        progress = (silence_duration_sec - _SILENCE_BAND_MID_SEC) / (
            _SILENCE_BAND_LONG_SEC - _SILENCE_BAND_MID_SEC
        )
        prob = _PROB_AT_MID_SEC + progress * (_PROB_AT_LONG_SEC - _PROB_AT_MID_SEC)
    else:
        prob = _PROB_AT_LONG_SEC





    proactive_recent_sec = current_time_ts - last_proactive_time if last_proactive_time > 0 else None
    if proactive_recent_sec is not None and proactive_recent_sec < 1 * 3600:
        prob = min(prob, 0.25)
    return prob



_cooldown_unavailable_warned = False


def _warn_silence_state_unavailable_once() -> bool:
    """冷却因取不到沉默时刻而失效时，首次告警；返回本次是否发出了告警。

    这条路径曾经完全静默：清理掉 tenshi_log 里的 SILENCE 记录后，「回退扫日志」
    已无数据源，于是 data/perception_state.json 一旦丢失，冷却会无声消失、日志里
    不留痕迹。现在首次遇到就 WARNING，让状态丢失在 Actions 日志里可见。
    """
    global _cooldown_unavailable_warned
    if _cooldown_unavailable_warned:
        return False
    _cooldown_unavailable_warned = True
    logger.warning(
        "沉默后冷却不可用：既读不到 %s，tenshi_log 里也没有 SILENCE 记录可回退"
        "（纯沉默已不再写日志）。本次起不施加冷却；若非预期，请检查该状态文件"
        "是否丢失或未提交。",
        getattr(tenshi_config, "PERCEPTION_STATE_FILE_PATH", "perception_state.json"),
    )
    return True


def _apply_post_silence_cooldown(prob: float, current_time_ts: float) -> tuple[float, bool]:
    """
    After a successful perception run that ended in SILENCE:
    - <2h: hard block (prob forced to 0)
    - 2-3h: linear ramp back to the base probability
    """
    last_silence = get_last_perception_silence_time()
    if last_silence <= 0:
        _warn_silence_state_unavailable_once()
        return prob, False

    since = current_time_ts - last_silence
    if since < POST_SILENCE_COOLDOWN_SEC:
        return 0.0, True
    if since < POST_SILENCE_RAMP_END_SEC:
        ramp = (since - POST_SILENCE_COOLDOWN_SEC) / (POST_SILENCE_RAMP_END_SEC - POST_SILENCE_COOLDOWN_SEC)
        return prob * ramp, False
    return prob, False


def _should_skip_perception_probability(force: bool, silence_duration_sec: float, current_time_ts: float, last_proactive_time: int) -> bool:
    if force:
        return False

    prob = _compute_silence_pass_probability(silence_duration_sec, current_time_ts, last_proactive_time)
    prob, hard_skip = _apply_post_silence_cooldown(prob, current_time_ts)

    if hard_skip:
        since_h = (current_time_ts - get_last_perception_silence_time()) / 3600
        print(
            f"Perception trigger skipped: post-SILENCE cooldown "
            f"({since_h:.1f}h since last SILENCE, block <2h)",
            flush=True,
        )
        return True

    if random.random() > prob:
        print(
            f"Perception trigger skipped due to silence jitter check "
            f"(silence: {silence_duration_sec/3600:.1f}h, pass_prob: {prob:.2f})",
            flush=True,
        )
        return True

    return False


def format_duration(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"

def save_perception_log(user_id, prompt, response_text, is_silence=False, thinking_content="",
                        pure_silence=False):
    """把感知层的**主动起念**写进 data/tenshi_log.json。

    2026-09-18 收紧：**SILENCE 一律不写对话日志**（此前只拦「纯沉默」，凡有内心独白
    或工具副作用 —— 例如本轮调了 note_patch / schedule_perception —— 仍会存档）。
    实测日志里积了 23 条这样的沉默记录，它们没有任何面向对话的产出，却有两个害处：

      * 污染注入给模型的上下文窗口（每条还带着 1KB+ 的感知层 prompt）；
      * ``time`` 取的是「落盘时刻」，一次用户消息还在 Actions 队列里排队时，它可能比
        那条消息**更晚**，于是排到 chat_history 最前面，把回复引擎的边界扫描挡住，
        用户消息被静默丢掉（2026-09-16 ca141f1）。

    沉默时刻照旧写 data/perception_state.json（冷却与触发间隔的输入；该文件必然变动，
    工作流照旧 commit + push，下一次全新 checkout 才读得到）；「正文 + SILENCE」组合里
    被截出来的正文仍走 note_patch 进私密笔记（调用方负责）。

    ``pure_silence`` 只用于日志措辞（区分纯沉默与带额外信息的沉默），不再决定是否落库。
    """
    if is_silence:
        thinking_len = len((thinking_content or "").strip())
        logger.info(
            "SILENCE 不写对话日志"
            f"（{'纯沉默' if pure_silence else '含额外信息'}，内心独白 {thinking_len} 字）；"
            "沉默时刻只记 data/perception_state.json。"
        )
        return

    log_path = tenshi_config.LOG_FILE_PATH
    try:
        with FileLock(tenshi_config.LOCK_FILE_PATH, timeout=30):
            with open(log_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            data, _ = ensure_tenshi_log_newest_first(data)

            target_conv = None
            for conv in data:
                if str(conv.get('user_id')) == str(user_id):
                    target_conv = conv
                    break

            if not target_conv:
                logger.error(f"保存感知层事件时未找到用户 {user_id} 的会话记录。")
                return

            timestamp = int(time.time())
            ts_str = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET))).strftime("%Y-%m-%d %H:%M:%S")


            bot_thinking_entry = {
                "intent": "proactive",
                "timestamp": ts_str
            }
            if thinking_content:
                bot_thinking_entry["model_thinking"] = thinking_content
            new_msg = {
                "msg_id": f"proactive_{timestamp}",
                "time": timestamp,
                "user_query": PERCEPTION_SOURCE_QUERY_PREFIX_TEMPLATE.format(
                    role_name=tenshi_config.ROLE_NAME_SHORT
                ),
                "bot_answer": _strip_leading_log_timestamp_prefix(response_text),
                "replied_flag": True,
                "perception_event": True,
                "bot_thinking": bot_thinking_entry
            }

            target_conv['chat_history'].insert(0, prune_empty_fields(new_msg))

            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.info("感知层事件已保存到日志。")
    except Exception as e:
        logger.error(f"保存感知层日志失败：{format_exception_inline(e)}")

def get_recent_dialogue_context_messages(user_id: str | int):
    """
    从 tenshi_log.json 读取最近的（非感知层）对话，
    并转换为按时间顺序排列的 messages payload，供主模型使用。
    """
    log_path = tenshi_config.LOG_FILE_PATH
    if not os.path.exists(log_path):
        return []

    try:
        with FileLock(tenshi_config.LOCK_FILE_PATH, timeout=30):
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        data, _ = ensure_tenshi_log_newest_first(data)
        target_conv = None
        for conv in data:
            if str(conv.get("user_id")) == str(user_id):
                target_conv = conv
                break
        if not target_conv:
            return []

        history_newest_first = target_conv.get("chat_history", [])
        return build_messages_payload_from_chat_history(
            history_newest_first,


            max_messages=max(30, getattr(tenshi_config, "PERCEPTION_CONTEXT_MAX_MESSAGES", 12)),
            truncate_chars=getattr(tenshi_config, "PERCEPTION_CONTEXT_TRUNCATE_CHARS", 800),

            skip_perception_events=False,
        )
    except Exception as e:
        logger.error(f"加载最近对话上下文失败：{format_exception_inline(e)}")
        return []

def _called_from_perception_workflow() -> bool:
    """
    Only activate this "unreplied-message fast-reply" when the script is launched
    by the GitHub Actions perception triggers.
    """
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return False
    wf = os.getenv("GITHUB_WORKFLOW", "") or ""




    return ("Perception Layer Trigger" in wf) or ("Elaina Perception Trigger" in wf)


def _log_has_pending_user_reply(log_path: str) -> bool:
    """某份 tenshi_log 里是否有「**回复引擎会处理**」的未回用户消息。

    判定必须与回复引擎逐字一致（``tenshi_utils.select_unreplied_run``，感知层记录不算
    边界）+ 只认真实用户消息（``_is_real_user_message``）。旧实现是「扫全表找任何
    没有 bot_answer 的记录」，会把「夹在已答边界之后的旧消息」「缺 perception_event
    的旧格式感知层记录」也算成待回复，于是感知层白白补跑一次回复引擎（引擎进去发现
    没有可回复的消息，直接 No unreplied 退出），那一拍定时起念就被吃掉了。
    """
    if not log_path or not os.path.exists(log_path):
        return False
    try:


        with FileLock(tenshi_config.LOCK_FILE_PATH, timeout=10):
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, list):
            return False

        try:
            data, _ = ensure_tenshi_log_newest_first(data)
        except Exception:
            pass
        for conv in data:
            if not isinstance(conv, dict):
                continue
            for msg in select_unreplied_run(conv.get("chat_history")):
                if _is_real_user_message(msg):
                    return True
    except Exception as e:
        logger.warning(f"Failed to check unreplied messages in {log_path}: {format_exception_inline(e)}")
    return False


def _latest_user_message_id() -> str:
    """取「他最后一条真实用户消息」的 Telegram message id（取不到返回空串）。

    为什么感知层需要它：表情回应（〔反应：👍〕）要按在**他那条消息**上，没有 id 就
    按不了。日志里 ``chat_history`` 是新->旧（``insert(0, ...)``），所以第一个
    ``_is_real_user_message`` 的记录就是最新的那条。

    边界：只读日志、不加任何副作用；任何异常一律当「没有」（软失败 —— 这一轮不给她
    表情选项，也不影响正文外发），绝不能让取 id 失败连累整轮感知。
    """
    log_path = getattr(tenshi_config, "LOG_FILE_PATH", "")
    if not log_path or not os.path.exists(log_path):
        return ""
    try:
        with FileLock(tenshi_config.LOCK_FILE_PATH, timeout=10):
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, list):
            return ""
        try:
            data, _ = ensure_tenshi_log_newest_first(data)
        except Exception:
            pass
        for conv in data:
            if not isinstance(conv, dict):
                continue
            for msg in conv.get("chat_history") or []:
                if not isinstance(msg, dict) or not _is_real_user_message(msg):
                    continue
                mid = str(msg.get("msg_id") or "").strip()
                if mid:
                    return mid
    except Exception as e:
        logger.warning(
            f"[reaction] 取他最后那条消息 id 失败（这一轮不给表情选项）：{format_exception_inline(e)}"
        )
    return ""


def _iter_repo_roots(workspace_root: str, current_repo_root: str, repo_name: str):
    """列出「可能是某个仓库根目录」的候选路径（去重，保持顺序）。

    为什么不能直接拼 ``workspace_root/repo_name``：Calling 代跑 Tenshi / Elaina 的工作流
    把仓库 checkout 到 **小写** 的 ``tenshi/``、``elaina/``（见 Calling 的
    ``.github/workflows/*.yml`` 的 ``path:``），而这里的常量是 ``Tenshi`` / ``Elaina``。
    Linux 大小写敏感，直接拼出来的路径根本不存在 —— 于是「感知层补跑回复引擎」和
    ``[reply-retry]`` 近程重试在 Calling 布局下恒为空转（只留一行 warning）。
    这里改成：当前 checkout 若是目标仓库就直接用它；否则对 workspace 下一层做
    大小写不敏感匹配（用磁盘上的真实目录名）。
    """
    seen: set[str] = set()

    def _emit(path: str):
        real = os.path.abspath(path)



        key = os.path.normcase(os.path.realpath(real))
        if key in seen:
            return None
        seen.add(key)
        return real

    candidate = None

    if os.path.basename(os.path.normpath(current_repo_root)).lower() == repo_name.lower():
        candidate = _emit(current_repo_root)
        if candidate:
            yield candidate



    try:
        for entry in sorted(os.listdir(workspace_root)):
            if entry.lower() != repo_name.lower():
                continue
            candidate = _emit(os.path.join(workspace_root, entry))
            if candidate:
                yield candidate
    except OSError:
        return


def _maybe_reply_if_unreplied_before_probability() -> bool:
    """感知层工作流里发现「用户消息还没被回复」时，补跑回复引擎。

    这是感知层与 webhook 的分工：用户刚说了话就不再走「主动起念」，而是把这一轮交回
    回复引擎（否则用户消息会被晾着，感知层那句主动起念还会顶替它的位置）。
    """
    if not (os.getenv("RP_ACTIVE_TASK", "").endswith("perception") or _called_from_perception_workflow()):
        return False

    current_repo_root = tenshi_config.PROJECT_ROOT
    workspace_root = os.path.abspath(os.path.join(current_repo_root, ".."))

    targets = []
    for repo_name in (os.path.basename(current_repo_root),):
        for root in _iter_repo_roots(workspace_root, current_repo_root, repo_name):
            log_path = os.path.join(root, "data", "tenshi_log.json")
            reply_path = os.path.join(root, "src", "tenshi_reply.py")
            if os.path.exists(log_path) and os.path.exists(reply_path):
                targets.append((repo_name, log_path, reply_path))

    pending = [
        (repo_name, reply_path)
        for repo_name, log_path, reply_path in targets
        if _log_has_pending_user_reply(log_path)
    ]
    if not pending:
        return False

    logger.info(
        "Unreplied messages detected (%s). "
        "Triggering reply engines before perception probability...",
        ", ".join(f"{name}={reply}" for name, reply in pending),
    )


    for _repo_name, reply_path in pending:
        try:
            subprocess.run(
                [sys.executable, reply_path],
                cwd=os.path.dirname(reply_path),
                check=False,
            )
        except Exception as e:
            logger.error(f"Failed to invoke reply script {reply_path}: {format_exception_inline(e)}")

    return True


def _has_pending_user_reply() -> bool:
    """本仓库是否有一条「轮到回复引擎处理」的未回用户消息？

    与 :func:`_log_has_pending_user_reply` 同一口径，只是固定读本仓库的对话日志。
    """
    return _log_has_pending_user_reply(tenshi_config.LOG_FILE_PATH)


def _resolve_reply_retry_trigger(schedule_note: str) -> dict | None:
    """本次 scheduled_force 触发是否为「主回复失败重试」条目。

    tenshi_reply 全链失败时会用 schedule_perception 登记一条带 REPLY_RETRY_NOTE_PREFIX 的
    条目；这种条目存在的意义就是补跑回复引擎，因此要把它和普通「定时起念」区分开。
    附言优先取 Cloudflare 载荷，其次回读本队列的队首。
    """
    if is_reply_retry_note(schedule_note):
        entry = load_pending_schedule_entry()
        if entry and is_reply_retry_note(entry.get("note")):
            return entry
        return {"note": str(schedule_note or "").strip(), "trigger_at_ms": 0}
    if (schedule_note or "").strip():

        return None
    entry = load_pending_schedule_entry()
    if entry and is_reply_retry_note(entry.get("note")):
        return entry
    return None


def _scheduled_force_from_env() -> tuple[bool, str, int]:
    """从 GitHub Actions / 环境变量读取 Cloudflare 强制定时触发载荷。"""
    flag = (os.getenv("PERCEPTION_SCHEDULED_FORCE") or "").strip().lower() in ("1", "true", "yes")
    note = (os.getenv("PERCEPTION_SCHEDULE_NOTE") or "").strip()
    raw_ms = (os.getenv("PERCEPTION_TRIGGER_AT_MS") or "").strip()
    try:
        trigger_ms = int(float(raw_ms)) if raw_ms else 0
    except (TypeError, ValueError):
        trigger_ms = 0
    return flag, note, trigger_ms






DEFERRED_INTENT_PROMPT = """
---
【憋着没说的那句（上一轮你自己记的账，只此一次）】
方向：{what}
当时为什么还想说：{why}
如果现在还想说，就冒出**一条新念头**：不补全上回的话、不复述、不催他交答案、不列清单。
不想说了就 SILENCE，这条账本轮结束就作废。
"""


def _deferred_store_exists() -> bool:
    try:
        return os.path.exists(str(getattr(tenshi_config, "DEFERRED_INTENT_STORE", "") or ""))
    except Exception:
        return False


class _DeferredRound:
    """一次感知轮的挂账生命周期；全部软失败。"""

    def __init__(self) -> None:
        self.turn_id: str | None = None
        self.pending: dict | None = None
        self.fired = False

    def begin(self, turn_id: str) -> dict | None:
        self.turn_id = turn_id
        if not getattr(tenshi_config, "DEFERRED_INTENT_ENABLED", True) or not _deferred_store_exists():
            return None
        try:
            items = deferred_intent.expire_and_collect(turn_id)
            self.pending = items[0] if items else None
        except Exception as e:
            logger.warning(f"[choice] 领取挂账意图失败（按无挂账处理）：{format_exception_inline(e)}")
            self.pending = None
        return self.pending

    def finish(self) -> None:
        if self.turn_id is None or not _deferred_store_exists():
            return
        try:
            if self.pending and self.fired:
                deferred_intent.mark_fired(str(self.pending.get("id")))
            deferred_intent.mark_expired_all(self.turn_id)
        except Exception as e:
            logger.warning(f"[choice] 挂账收尾失败（忽略）：{format_exception_inline(e)}")


def _style_guard_perception_text(text: str, *, call_rewrite) -> str:
    """感知层输出的文体守卫：命中→同风格重写一次→复检→仍违则放行最后一版。"""
    try:
        if not getattr(tenshi_config, "STYLE_GUARD_ENABLED", True):
            return text
        violations = reply_budget.detect_style_violations(text)
        if not violations:
            return text
        current = text
        for _ in range(max(0, int(getattr(tenshi_config, "STYLE_GUARD_MAX_REWRITES", 1)))):
            new_text = rewrite_choice_text(
                call_rewrite=call_rewrite,
                violations_text=build_choice_violation_text([], violations),
                original_text=current,
            )
            if not new_text:
                break
            current = new_text
            violations = reply_budget.detect_style_violations(current)
            if not violations:
                break
        if violations:
            logger.warning(f"[choice] 感知层文体重写后仍违规，放行最后一版：{violations}")
        return current
    except Exception as e:
        logger.warning(f"[choice] 感知层文体守卫异常，放行原文：{format_exception_inline(e)}")
        return text


async def run_perception(
    force: bool = False,
    scheduled_force: bool = False,
    schedule_note: str = "",
    schedule_trigger_at_ms: int = 0,
) -> int:
    round_ctx = _DeferredRound()
    try:
        return await _run_perception_impl(
            force=force,
            scheduled_force=scheduled_force,
            schedule_note=schedule_note,
            schedule_trigger_at_ms=schedule_trigger_at_ms,
            round_ctx=round_ctx,
        )
    finally:
        round_ctx.finish()


async def _run_perception_impl(
    force: bool = False,
    scheduled_force: bool = False,
    schedule_note: str = "",
    schedule_trigger_at_ms: int = 0,
    round_ctx: "_DeferredRound | None" = None,
) -> int:
    round_ctx = round_ctx or _DeferredRound()
    user_id, last_time, last_proactive_time = get_last_interaction()
    if not user_id:
        user_id = tenshi_config.ADMIN_ID

    current_time_ts = time.time()
    silence_duration_sec = current_time_ts - last_time if last_time > 0 else 0



    retry_entry = _resolve_reply_retry_trigger(schedule_note) if scheduled_force else None
    if retry_entry is not None:
        retry_trigger_ms = int(schedule_trigger_at_ms or retry_entry.get("trigger_at_ms") or 0)
        logger.warning("[reply-retry] 定时重试触发：补跑回复引擎（不再走定时起念）。")


        complete_scheduled_trigger(retry_trigger_ms or None)
        handled = _maybe_reply_if_unreplied_before_probability()
        if not handled:
            logger.warning(
                "[reply-retry] 未发现未回复消息或当前不在感知工作流环境，本次重试无实际动作。"
            )
        return 0



    if not scheduled_force and _maybe_reply_if_unreplied_before_probability():
        logger.info("Perception run aborted: handled pending unreplied messages via reply engine.")
        return 0

    bypass_probability = force or scheduled_force
    if _should_skip_perception_probability(bypass_probability, silence_duration_sec, current_time_ts, last_proactive_time):
        return PERCEPTION_SKIP_EXIT_CODE

    logger.info("正在启动感知层...")


    deferred_note = ""
    pending_intent = round_ctx.begin(f"perception-{int(current_time_ts)}")
    if pending_intent:
        try:
            deferred_note = DEFERRED_INTENT_PROMPT.format(
                what=str(pending_intent.get("what") or "").strip() or "（未写）",
                why=str(pending_intent.get("why") or "").strip() or "（未写）",
            )
            logger.info("[choice] 感知轮进入补发模式（注入 1 条挂账意图）。")
        except Exception as e:
            logger.warning(f"[choice] 构造补发提示失败，按普通起念：{format_exception_inline(e)}")
            deferred_note = ""

    schedule_entry = load_pending_schedule_entry() if scheduled_force else None
    if scheduled_force and not schedule_note and schedule_entry:
        schedule_note = (schedule_entry.get("note") or "").strip()
    if scheduled_force and not schedule_trigger_at_ms and schedule_entry:
        try:
            schedule_trigger_at_ms = int(schedule_entry.get("trigger_at_ms") or 0)
        except (TypeError, ValueError):
            schedule_trigger_at_ms = 0


    if scheduled_force:
        planned_bj = ""
        if schedule_trigger_at_ms > 0:
            planned_bj = datetime.fromtimestamp(
                schedule_trigger_at_ms / 1000.0,
                tz=timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET)),
            ).strftime("%Y-%m-%d %H:%M:%S")
        description = PERCEPTION_EVENT_SCHEDULED_DESCRIPTION
        manual_note = PERCEPTION_EVENT_SCHEDULED_MANUAL_NOTE_TEMPLATE.format(
            schedule_note=schedule_note or "（无附言）",
            planned_bj=planned_bj or "未知",
        )
        logger.info("Scheduled force trigger: bypassing probability with injected note.")
    elif not force:
        description = PERCEPTION_EVENT_AUTO_DESCRIPTION
        manual_note = ""
        prob = _compute_silence_pass_probability(silence_duration_sec, current_time_ts, last_proactive_time)
        prob, _ = _apply_post_silence_cooldown(prob, current_time_ts)
        proactive_recent_sec = current_time_ts - last_proactive_time if last_proactive_time > 0 else None
        if proactive_recent_sec is not None and proactive_recent_sec < 1 * 3600:
            logger.info(
                f"检测到近期主动消息（{proactive_recent_sec/60:.1f} 分钟前），"
                f"放行概率为 {prob:.2f}"
            )
        logger.info(
            f"Perception trigger passed silence check! "
            f"(silence: {silence_duration_sec/3600:.1f}h, pass_prob: {prob:.2f})"
        )
    else:
        description = PERCEPTION_EVENT_FORCE_DESCRIPTION
        manual_note = PERCEPTION_EVENT_FORCE_MANUAL_NOTE
        logger.info("Force trigger enabled, skipping silence check.")

    silence_str = format_duration(silence_duration_sec)

    tz = timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
    current_time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    lat, lon, place_label, location = weather_utils.resolve_location_for_perception(user_id)
    weather_report = weather_utils.get_weather_report(lat=lat, lon=lon, place_label=place_label)
    commits = get_recent_commits()

    situation_lines = [
        f"- 当前时间：{current_time_str}",
        f"- 距离上次对话：{silence_str}",
        f"- 天气状况：{weather_report}",
        f"- 他的行踪：{location}",
    ]
    if commits:
        situation_lines.append(f"- 近期代码提交（近{COMMITS_LOOKBACK_HOURS}小时）：\n{commits}")
    situation_block = "\n".join(situation_lines)




    perception_prompt = PERCEPTION_PROMPT_TEMPLATE.format(
        description=description,
        situation_block=situation_block,
        manual_note=manual_note,
    ) + deferred_note

    logger.debug(f"Perception Prompt:\n{perception_prompt}")


    messages_payload = get_recent_dialogue_context_messages(user_id)



    try:
        _dt = datetime.strptime(current_time_str, "%Y-%m-%d %H:%M:%S")
        _h = _dt.hour
        if _h < 4:
            _subjective_date = _dt - timedelta(days=1)
            _night_owl_note = PERCEPTION_TIME_ANCHOR_NIGHT_OWL_NOTE_TEMPLATE.format(
                yesterday_md=_subjective_date.strftime("%m月%d日")
            )
            _anchor = PERCEPTION_TIME_ANCHOR_NIGHT_OWL_TEMPLATE.format(
                night_owl_note=_night_owl_note,
                year=_dt.year,
                month=_dt.month,
                day=_dt.day,
                hhmm=_dt.strftime("%H:%M"),
                tz_label=tenshi_config.TIMEZONE_LABEL,
                sub_month=_subjective_date.month,
                sub_day=_subjective_date.day,
            )
        else:
            _tod = (
                PERCEPTION_TIME_OF_DAY_EARLY if _h < 7 else
                PERCEPTION_TIME_OF_DAY_MORNING if _h < 11 else
                PERCEPTION_TIME_OF_DAY_NOON if _h < 13 else
                PERCEPTION_TIME_OF_DAY_AFTERNOON if _h < 17 else
                PERCEPTION_TIME_OF_DAY_EVENING if _h < 19 else
                PERCEPTION_TIME_OF_DAY_NIGHT if _h < 22 else
                PERCEPTION_TIME_OF_DAY_LATE_NIGHT
            )
            _anchor = PERCEPTION_TIME_ANCHOR_DAY_TEMPLATE.format(
                year=_dt.year,
                month=_dt.month,
                day=_dt.day,
                tod=_tod,
                hhmm=_dt.strftime("%H:%M"),
                tz_label=tenshi_config.TIMEZONE_LABEL,
            )
    except Exception:
        _anchor = PERCEPTION_TIME_ANCHOR_FALLBACK_TEMPLATE.format(
            tz_label=tenshi_config.TIMEZONE_LABEL,
            current_time_str=current_time_str,
        )
    messages_payload.insert(0, {"role": "user", "content": _anchor})
    try:
        if not messages_payload:
            logger.warning("Perception context injection is empty (messages_payload=[])")
        else:
            tail_preview = []
            for m in messages_payload[-5:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content")
                if isinstance(content, str):
                    content_preview = content.replace("\n", " ")[:160]
                else:
                    content_preview = str(content)[:160]
                tail_preview.append({"role": role, "content": content_preview})
            logger.info(
                f"Injected perception context messages: {len(messages_payload)}; tail_preview={json.dumps(tail_preview, ensure_ascii=False)}"
            )
    except Exception as e:
        logger.debug(f"Failed to preview injected context: {format_exception_inline(e)}")
    messages_payload.append({"role": "user", "content": perception_prompt})

    rag_engine = TenshiRAG()
    kb_data = rag_engine.kb_data

    user_profile = json.dumps(kb_data.get('user_profile', {}), ensure_ascii=False, indent=2)

    bond_inner = bond_state.inner_block(kb_data)
    bond_typing = bond_state.typing_block()

    _prompt_fields = dict(
        bond_state=bond_inner,
        private_notes_injected=format_private_notes_for_system_prompt(
            int(getattr(tenshi_config, "PRIVATE_NOTES_INJECT_COUNT", 0) or 0)
        ),
        spatio_temporal_context=f"- **当前时刻**: {current_time_str}\n- **天候状况**: {weather_report}",
        user_profile=user_profile,
        compressed_context="",
        long_term_memory="",
        shared_experience="",
        world_history="",
        image_context=""
    )
    system_prompt = tenshi_config.SYSTEM_PROMPT_TEMPLATE.format(**_prompt_fields)

    _dyn_tpl = getattr(tenshi_config, "SYSTEM_PROMPT_DYNAMIC_TEMPLATE", "") or ""
    if getattr(tenshi_config, "REPLY_TYPING_WITH_BACKGROUND", True) and _dyn_tpl.strip():
        typing_background, typing_bond = _dyn_tpl.format(**_prompt_fields), ""
    else:
        typing_background, typing_bond = "", bond_typing

    system_prompt = reply_two_stage.append_to_system(
        system_prompt, reply_two_stage.inner_protocol(perception=True)
    )


    system_prompt += TIMESTAMP_PREFIX_SYSTEM_NOTE

    response_text = None


    _mutation_snapshot = _snapshot_mutation_targets()





    append_llm_trace(
        {
            "source": PERCEPTION_TRACE_MARKER_SOURCE,
            "scheduled_force": bool(scheduled_force),
            "force": bool(force),
        }
    )
    try:
        chain_result = invoke_dialog_model_chain(
            grade_tier="medium",
            system_prompt=system_prompt,
            messages_payload=messages_payload,
            filtered_claude_tools=CLAUDE_TOOLS,
        )
    finally:
        append_llm_trace({"source": PERCEPTION_TRACE_END_SOURCE})
    if chain_result:
        response_text, _, _ = chain_result

    if not response_text:
        logger.critical("All models failed to generate perception thought.")
        if scheduled_force:
            save_perception_log(
                user_id,
                perception_prompt,
                "SILENCE",
                is_silence=True,
                thinking_content=MODELS_UNAVAILABLE_MARKER,
            )


            write_last_silence_at(int(current_time_ts))
            complete_scheduled_trigger(schedule_trigger_at_ms or None)
            logger.warning(
                "Scheduled force: models unavailable; queue cleared, exiting 0 for Actions."
            )
            return 0
        return 1

    response_text = response_text.strip()
    logger.info(f"Model raw output: {response_text}")


    thinking_content = ""
    _thinking_match = re.search(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", response_text, re.IGNORECASE)
    if _thinking_match:
        thinking_content = re.sub(r"<think(?:ing)?>", "", _thinking_match.group(0), flags=re.IGNORECASE)
        thinking_content = re.sub(r"</think(?:ing)?>", "", thinking_content, flags=re.IGNORECASE).strip()
        response_text = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", response_text, flags=re.IGNORECASE).strip()
        logger.debug(f"Stripped thinking block ({len(thinking_content)} chars). Remaining: {repr(response_text[:80])}")





    if has_tool_call_markup(response_text):
        logger.warning(
            "感知层输出含工具调用标记，已剥离后再判定沉默/外发（工具本轮未能执行）。"
        )
        response_text = sanitize_model_text(response_text)


    inner_text = reply_two_stage.clean_inner(response_text)
    logger.info(f"[inner] len={len(inner_text)}：{inner_text[:300]!r}")


    reaction_target_id = _latest_user_message_id() if reactions.enabled() else ""
    if not inner_text or _perception_bot_answer_is_silence_only(inner_text):

        typed_text, typed_no_send, typing_model = "", True, ""
    else:
      typed_text, typed_no_send, typing_model = run_typing_stage(
        model_chain=_build_dialog_model_chain("medium"),
        typing_system=reply_two_stage.build_typing_system(
            tenshi_config.PERSONA_CARD,
            bond_block=typing_bond,
            background=typing_background,
            reaction_block=(reactions.typing_prompt_block() if reaction_target_id else ""),
        ),
        typing_user=reply_two_stage.build_typing_user(
            history_messages=messages_payload[:-1],
            latest=None,
            inner=inner_text,
            perception_situation=situation_block + (("\n" + manual_note.strip()) if manual_note.strip() else ""),
            history_limit=int(getattr(tenshi_config, "REPLY_TYPING_HISTORY_MESSAGES", 10) or 10),
        ),
    )
    if not typed_text and not typed_no_send:
        logger.error("感知层打字那一步全部失败：本次按沉默处理，心里的内容不外发。")
        typed_no_send = True
    thinking_content = reply_two_stage.clean_inner(
        (thinking_content + "\n\n" if thinking_content else "") + "【心里】\n" + inner_text
    ) if inner_text else thinking_content
    clipped_body = ""
    is_silence_decision = bool(typed_no_send)
    response_body_cleaned = "" if is_silence_decision else typed_text

    if is_silence_decision:
        logger.info("Tenshi chose to remain silent.")




        write_last_silence_at(int(current_time_ts))
        _pure_silence = _is_pure_silence_record(
            thinking_content=thinking_content,
            clipped_body=clipped_body,
            tool_mutated=_tool_produced_side_effects(_mutation_snapshot),
        )
        save_perception_log(user_id, perception_prompt, "SILENCE", is_silence=True,
                            thinking_content=thinking_content, pure_silence=_pure_silence)
        if scheduled_force:
            complete_scheduled_trigger(schedule_trigger_at_ms or None)
        return 0

    response_body_cleaned, _dup_removed = dedupe_back_to_back_long_repeat(response_body_cleaned)
    if _dup_removed:
        logger.warning(
            "Removed back-to-back duplicate chunk from perception final response "
            f"(len_after={len(response_body_cleaned)})."
        )


    def _perception_rewrite_call(prompt: str, _base=response_body_cleaned) -> str:
        payload = list(messages_payload) + [
            {"role": "assistant", "content": _base},
            {"role": "user", "content": prompt},
        ]
        out = invoke_dialog_model_chain(
            grade_tier="medium",
            system_prompt=system_prompt,
            messages_payload=payload,
            filtered_claude_tools=CLAUDE_TOOLS,
        )
        return out[0] if out else ""

    response_body_cleaned = _style_guard_perception_text(
        response_body_cleaned, call_rewrite=_perception_rewrite_call
    )





    if _has_pending_user_reply():
        logger.warning(
            "发送前复检：存在尚未回复的用户消息 —— 本次起念让位给回复引擎，不发送、不落库。"
        )
        if scheduled_force:
            complete_scheduled_trigger(schedule_trigger_at_ms or None)
        return 0

    logger.info("Tenshi decided to speak! Sending message...")
    if pending_intent:
        round_ctx.fired = True

    try:
        telegram_sent = False
        bot = Bot(token=tenshi_config.TELEGRAM_TOKEN)



        body_for_send = reactions.strip_markers(response_body_cleaned)
        reaction_emoji = reactions.reaction_of_text(response_body_cleaned)
        if reaction_emoji and reaction_target_id:
            try:
                await reactions.send_async(
                    bot, chat_id=user_id, message_id=reaction_target_id, emoji=reaction_emoji
                )
            except Exception as e:
                logger.warning(f"[reaction] 感知层按表情异常（忽略，不影响正文）：{format_exception_inline(e)}")
        elif reaction_emoji:
            logger.warning(
                f"[reaction] 感知层有表情标记 {reaction_emoji!r} 但找不到他那条消息，跳过不按。"
            )





        segments = stickers.split_for_send(body_for_send, chunk_long=False)
        if not segments and not stickers.has_marker(body_for_send) and body_for_send.strip():
            segments = [body_for_send]
        if not segments:
            logger.info("[reaction] 感知层这一轮只有表情回应，不发送任何文字消息。")
        if len(segments) > 1:
            logger.info(f"感知层分条发送：{describe_split(segments)}")
        elif segments and len(segments[0]) > MAX_MESSAGE_CHARS:
            logger.warning(
                f"感知层单条 {len(segments[0])} 字超过单条上限 {MAX_MESSAGE_CHARS}"
                "且无 <br> 标记，按口径整条发送（可能被平台拒绝）。"
            )

        from telegram import constants

        async def _send_one(text: str) -> None:

            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
            except Exception as e:


                err_str = str(e).lower()
                if "can't parse entities" in err_str or "bad request" in err_str:
                    logger.warning("Markdown parse failed, sending as plain text.")
                    await bot.send_message(chat_id=user_id, text=text, parse_mode=None)
                else:
                    raise

        async def _send_sticker_one(name: str, first: bool) -> bool:

            st = stickers.by_name(name)
            if not st:
                return False
            try:
                await bot.send_chat_action(chat_id=user_id, action=constants.ChatAction.CHOOSE_STICKER)
            except Exception:
                pass
            if not first:
                await asyncio.sleep(stickers.send_delay_sec())
            for _attempt in range(3):
                try:
                    await bot.send_sticker(chat_id=user_id, sticker=st["file_id"])
                    return True
                except Exception as e:
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after is not None and _attempt < 2:
                        try:
                            wait = float(getattr(retry_after, "total_seconds", lambda: retry_after)())
                        except Exception:
                            wait = 1.0
                        await asyncio.sleep(min(max(wait, 0.5), 60.0))
                        continue
                    logger.warning(f"[sticker] 感知层贴纸 {name!r} 发送失败，跳过：{format_exception_inline(e)}")
                    return False
            return False

        from reply_streaming import _followup_delay_sec

        for i, seg in enumerate(segments):
            sticker_name = stickers.sticker_name_of_segment(seg)
            if sticker_name:
                if await _send_sticker_one(sticker_name, first=not telegram_sent):
                    telegram_sent = True
                continue
            if i > 0:

                await asyncio.sleep(_followup_delay_sec(seg))
            await _send_one(seg)
            telegram_sent = True

        if telegram_sent:
            logger.info("Telegram message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {format_exception_inline(e)}")







    persisted_body = normalize_br_markup(unescape_model_text(response_body_cleaned))
    if persisted_body != response_body_cleaned:
        logger.info("Normalized <br> split markers before perception persistence (kept, not stripped).")
    save_perception_log(user_id, perception_prompt, persisted_body, is_silence=False,
                        thinking_content=thinking_content)
    if scheduled_force:
        complete_scheduled_trigger(schedule_trigger_at_ms or None)
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Tenshi Perception Layer')
    parser.add_argument('--force', action='store_true', help='Force trigger without jitter')
    parser.add_argument(
        '--scheduled-force',
        action='store_true',
        help='Scheduled perception trigger from Cloudflare (bypass probability, inject note)',
    )
    parser.add_argument('--schedule-note', default='', help='Note injected for scheduled trigger')
    parser.add_argument('--schedule-trigger-at-ms', type=int, default=0, help='Planned trigger time (ms)')
    args = parser.parse_args()

    env_scheduled, env_note, env_trigger_ms = _scheduled_force_from_env()
    scheduled_force = args.scheduled_force or env_scheduled
    schedule_note = (args.schedule_note or env_note or "").strip()
    schedule_trigger_at_ms = args.schedule_trigger_at_ms or env_trigger_ms or 0
    force = args.force or scheduled_force

    exit_code = asyncio.run(
        run_perception(
            force=force,
            scheduled_force=scheduled_force,
            schedule_note=schedule_note,
            schedule_trigger_at_ms=schedule_trigger_at_ms,
        )
    )
    sys.exit(exit_code if exit_code is not None else 0)
