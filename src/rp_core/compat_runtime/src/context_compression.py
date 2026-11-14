# SPDX-License-Identifier: AGPL-3.0-only












from __future__ import annotations

import re
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import tenshi_config
from deepseek_modules import call_deepseek
from tenshi_utils import FileLock, setup_logger
from error_text import format_exception_inline



logger = setup_logger("ContextCompression", tenshi_config.DEBUG_LOG_PATH)


def _raw_turns() -> int:
    return max(1, int(getattr(tenshi_config, "CONTEXT_COMPRESSION_RAW_TURNS", 15) or 15))


def _window_turns() -> int:
    return max(1, int(getattr(tenshi_config, "CONTEXT_COMPRESSION_WINDOW_TURNS", 10) or 10))


def _anchor_trigger_newer_min() -> int:
    """最新包结束时间之后至少有多少条更新对话才触发（默认 14）。"""
    v = getattr(tenshi_config, "CONTEXT_COMPRESSION_ANCHOR_TRIGGER_NEWER", None)
    if v is not None:
        return max(1, int(v))
    trigger = int(getattr(tenshi_config, "CONTEXT_COMPRESSION_TRIGGER_TURNS", 15) or 15)
    return max(1, trigger - 1)


def _tz() -> timezone:
    return timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET))


def format_mmdd_hhmm(ts: float | int | str | None) -> str:
    if isinstance(ts, str) and ts.strip():
        s = ts.strip()
        if len(s) >= 11 and s[2] == "-" and s[5] == " ":
            return s[:11]
        try:
            ts = float(s)
        except Exception:
            return s[:11] if len(s) >= 11 else s
    try:
        val = float(ts or 0)
    except Exception:
        return "??-?? ??:??"
    if val <= 0:
        return "??-?? ??:??"
    return datetime.fromtimestamp(val, _tz()).strftime("%m-%d %H:%M")


def _turn_unix_ts(turn: dict[str, Any]) -> float:
    try:
        return float(turn.get("time", 0) or 0)
    except Exception:
        return 0.0


def _load_summary_cursor_timestamp() -> float:
    path = getattr(tenshi_config, "SUMMARY_CURSOR_FILE", "")
    if not path or not os.path.exists(path):
        return 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f) or {}
        ts = obj.get("last_summarized_time") if isinstance(obj, dict) else None
        return float(ts or 0.0)
    except Exception:
        return 0.0


def _load_store() -> dict[str, Any]:
    path = tenshi_config.CONTEXT_COMPRESSION_FILE_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"读取 context_compressions 失败，改为空：{format_exception_inline(e)}")
        return {}


def _save_store(data: dict[str, Any]) -> None:
    path = tenshi_config.CONTEXT_COMPRESSION_FILE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _get_conversation_key(target_conv: dict[str, Any]) -> str:
    conv_id = str(target_conv.get("conversation_id") or "").strip()
    user_id = str(target_conv.get("user_id") or "").strip()
    return conv_id or user_id or "unknown"


def _effective_turns_newest_first(chat_history_newest_first: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history = []
    for msg in chat_history_newest_first or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("perception_event"):
            continue




        if msg.get("context_masked"):
            continue
        uq = msg.get("user_query")
        ba = msg.get("bot_answer")
        if (isinstance(uq, str) and uq.strip()) or (isinstance(ba, str) and ba.strip()):
            history.append(msg)
    return history


def _format_turn_block_for_compression(turn: dict[str, Any], *, rel_pos: int) -> str:
    ts_label = format_mmdd_hhmm(_turn_unix_ts(turn))
    parts = [f"### [{ts_label}] 相对位置{rel_pos}（越靠前越新）"]
    uq = (turn.get("user_query") or "").strip()
    ba = (turn.get("bot_answer") or "").strip()
    if uq:
        parts.append(f"User: {uq}")
    if ba:
        parts.append(f"Tenshi: {ba}")
    return "\n".join(parts)


def _package_time_span_label(item: dict[str, Any]) -> str:
    start_label = str(item.get("start_time_label") or "").strip() or format_mmdd_hhmm(item.get("start_time"))
    end_label = str(item.get("end_time_label") or "").strip() or format_mmdd_hhmm(item.get("end_time"))
    return f"{start_label}～{end_label}"


def _conversation_store_entry(store: dict[str, Any], conversation_key: str) -> dict[str, Any]:
    raw = store.get(conversation_key)
    if not isinstance(raw, dict):
        raw = {}
    summaries = raw.get("summaries")
    if not isinstance(summaries, list):
        summaries = []
    pending = raw.get("pending")
    if not isinstance(pending, list):
        pending = []
    return {
        "conversation_id": conversation_key,
        "updated_at": raw.get("updated_at"),
        "summaries": summaries,
        "pending": pending,
    }


def _normalize_summaries(entry: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in entry.get("summaries") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("summary") or "").strip()
        if not text:
            continue
        start_ts = float(item.get("start_time") or 0.0)
        end_ts = float(item.get("end_time") or 0.0)
        if end_ts <= 0 and start_ts <= 0:
            continue
        if start_ts > end_ts > 0:
            start_ts, end_ts = end_ts, start_ts
        start_label = str(item.get("start_time_label") or "").strip() or format_mmdd_hhmm(start_ts)
        end_label = str(item.get("end_time_label") or "").strip() or format_mmdd_hhmm(end_ts)
        out.append(
            {
                "package_id": str(item.get("package_id") or f"{start_ts}-{end_ts}"),
                "summary": text,
                "created_at": float(item.get("created_at") or 0.0),
                "start_time": start_ts,
                "end_time": end_ts,
                "start_time_label": start_label,
                "end_time_label": end_label,
                "model": str(item.get("model") or ""),
            }
        )

    out.sort(key=lambda x: (x["start_time"], x["end_time"], x["created_at"]), reverse=True)
    return out


def _normalize_pending(entry: dict[str, Any]) -> list[dict[str, float]]:
    out = []
    for item in entry.get("pending") or []:
        if not isinstance(item, dict):
            continue
        try:
            start_ts = float(item.get("start_time") or 0.0)
            end_ts = float(item.get("end_time") or 0.0)
        except Exception:
            continue
        if end_ts <= 0:
            continue
        if start_ts > end_ts:
            start_ts, end_ts = end_ts, start_ts
        out.append({"start_time": start_ts, "end_time": end_ts})
    seen: set[tuple[float, float]] = set()
    uniq = []
    for p in sorted(out, key=lambda x: (x["start_time"], x["end_time"])):
        key = (round(p["start_time"], 3), round(p["end_time"], 3))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _latest_package_end_time(summaries: list[dict[str, Any]]) -> float:
    if not summaries:
        return 0.0
    return max(float(s.get("end_time") or 0.0) for s in summaries)


def _turns_newer_than(newest_first: list[dict[str, Any]], after_ts: float) -> list[dict[str, Any]]:
    if after_ts <= 0:
        return list(newest_first)
    return [t for t in newest_first if _turn_unix_ts(t) > after_ts]


def _slice_time_span(slice_newest_first: list[dict[str, Any]]) -> tuple[float, float]:
    """返回 (最旧端 time, 最新端 time)。"""
    oldest = _turn_unix_ts(slice_newest_first[-1])
    newest = _turn_unix_ts(slice_newest_first[0])
    return oldest, newest


def _package_exists(summaries: list[dict[str, Any]], oldest_ts: float, newest_ts: float) -> bool:
    for s in summaries:
        if abs(float(s.get("start_time") or 0) - oldest_ts) < 0.5 and abs(float(s.get("end_time") or 0) - newest_ts) < 0.5:
            return True
    return False


def _rel_range_for_newest_slice(start_idx: int, end_idx: int) -> tuple[int, int]:
    """newest_first[start_idx:end_idx] → 时间正序相对位置 (rel_start, rel_end)。"""
    return start_idx + 1, end_idx


def _bootstrap_due_slices(
    newest_first: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    """
    无压缩包：从新往旧取相对 16～25、6～15（先压更旧的 16～25）。
    返回 newest_first 上的 [start, end) 半开区间列表。
    """
    n = len(newest_first)
    windows: list[tuple[int, int]] = []
    if n >= 25:
        windows.append((15, 25))
    if n >= 15:
        windows.append((5, 15))
    due = []
    for start, end in windows:
        sl = newest_first[start:end]
        if len(sl) != end - start:
            continue
        oldest, newest = _slice_time_span(sl)
        if not _package_exists(summaries, oldest, newest):
            due.append((start, end))
    return due


def _anchor_due_slices(
    newest_first: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    """
    有压缩包：从最新包 end_time 往更新方向，每满 10 轮一包；≥14 条更新对话才触发。

    newer 是 newest_first 里时间严格新于锚点的部分，保持顺序。
    先在 newer 上做切片（下标 0..n），再把每个切片的边界映射回 newest_first 的真实下标，
    避免 newer 不是 newest_first 严格前缀时（如中间有被过滤的 perception 轮）下标偏移。
    """
    anchor = _latest_package_end_time(summaries)
    newer = _turns_newer_than(newest_first, anchor)
    if len(newer) < _anchor_trigger_newer_min():
        return []

    window = _window_turns()
    n = len(newer)
    full_chunks = n // window
    if full_chunks <= 0:
        return []


    newer_to_full: list[int] = []
    full_idx = 0
    for nw_turn in newer:
        while full_idx < len(newest_first):
            if newest_first[full_idx] is nw_turn:
                newer_to_full.append(full_idx)
                full_idx += 1
                break
            full_idx += 1

    if len(newer_to_full) != n:

        newer_to_full = list(range(n))

    due: list[tuple[int, int]] = []
    for k in range(full_chunks):

        sub_start = n - (k + 1) * window
        sub_end   = n - k * window
        sl = newer[sub_start:sub_end]
        if len(sl) != window:
            continue
        oldest, newest = _slice_time_span(sl)
        if _package_exists(summaries, oldest, newest):
            continue
        full_start = newer_to_full[sub_start]
        full_end   = newer_to_full[sub_end - 1] + 1
        due.append((full_start, full_end))

    due.sort(key=lambda x: x[0], reverse=True)
    return due


def _pending_due_slices(
    newest_first: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    pending: list[dict[str, float]],
) -> list[tuple[int, int]]:
    due: list[tuple[int, int]] = []
    for p in pending:
        ps, pe = float(p["start_time"]), float(p["end_time"])
        if _package_exists(summaries, ps, pe):
            continue
        hit = [t for t in newest_first if ps - 0.5 <= _turn_unix_ts(t) <= pe + 0.5]
        if len(hit) < _window_turns():
            continue
        sl = hit[-_window_turns() :] if len(hit) > _window_turns() else hit
        oldest, newest = _slice_time_span(sl)
        if _package_exists(summaries, oldest, newest):
            continue

        idxs = []
        for turn in sl:
            for i, t in enumerate(newest_first):
                if _turn_unix_ts(t) == _turn_unix_ts(turn):
                    idxs.append(i)
                    break
        if not idxs:
            continue
        due.append((min(idxs), max(idxs) + 1))
    return due


def _all_due_slices(
    newest_first: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    pending: list[dict[str, float]] | None = None,
) -> list[tuple[int, int]]:
    pending_due = _pending_due_slices(newest_first, summaries, pending or [])
    if pending_due:
        return pending_due
    if not summaries:
        return _bootstrap_due_slices(newest_first, summaries)
    return _anchor_due_slices(newest_first, summaries)


def _select_due_slice(
    newest_first: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    pending: list[dict[str, float]] | None = None,
) -> Optional[list[dict[str, Any]]]:
    due = _all_due_slices(newest_first, summaries, pending)
    if not due:
        return None
    start, end = due[0]
    sl = newest_first[start:end]
    return sl if len(sl) == end - start else None


def build_compressed_context_block(target_conv: dict[str, Any]) -> str:
    """
    主模型注入块。
    - 存档文件按新→旧排列；取头部 keep_count 个即最新的那些包。
    - keep_count = max(5, 上次总结时间之后的包数)，上限 20。
    - 注入时反转为时间正序（旧→新），排在 15 轮原文之前。
    """
    conversation_key = _get_conversation_key(target_conv)
    with FileLock(tenshi_config.CONTEXT_COMPRESSION_LOCK_FILE_PATH, timeout=10):
        store = _load_store()
    entry = _conversation_store_entry(store, conversation_key)
    summaries = _normalize_summaries(entry)
    if not summaries:
        return ""

    last_cursor_ts = _load_summary_cursor_timestamp()

    since_last_summary = sum(1 for s in summaries if float(s.get("end_time") or 0.0) > last_cursor_ts)
    keep_count = max(
        int(getattr(tenshi_config, "CONTEXT_COMPRESSION_KEEP_MIN_PACKAGES", 5) or 5),
        int(since_last_summary),
    )
    keep_count = min(keep_count, int(getattr(tenshi_config, "CONTEXT_COMPRESSION_MAX_PACKAGES", 20) or 20))


    newest_kept = summaries[:keep_count] if keep_count > 0 else []
    if not newest_kept:
        return ""


    to_inject = list(reversed(newest_kept))

    lines = ["# 更早聊过的事（压缩概要，时间正序；位于近期原文之前）"]
    for item in to_inject:
        lines.append(f"## {_package_time_span_label(item)}")
        lines.append(strip_retired_summary_lines(item["summary"]))
    return "\n".join(lines).strip()





_RETIRED_SUMMARY_LINE_RE = re.compile(r"^\s*[*\-•]?\s*(?:未完成|当前有效约束)\s*[：:]")


def strip_retired_summary_lines(summary: str) -> str:
    """去掉压缩概要里以「未完成：」「当前有效约束：」开头的行。"""
    if not isinstance(summary, str) or not summary:
        return summary or ""
    kept = [ln for ln in summary.splitlines() if not _RETIRED_SUMMARY_LINE_RE.match(ln)]
    return "\n".join(kept).strip()


def needs_context_compression(target_conv: dict[str, Any]) -> bool:
    newest_first = _effective_turns_newest_first(target_conv.get("chat_history", []))
    conversation_key = _get_conversation_key(target_conv)
    with FileLock(tenshi_config.CONTEXT_COMPRESSION_LOCK_FILE_PATH, timeout=10):
        store = _load_store()
    entry = _conversation_store_entry(store, conversation_key)
    summaries = _normalize_summaries(entry)
    pending = _normalize_pending(entry)
    return _select_due_slice(newest_first, summaries, pending) is not None


def compute_context_compression_entry(target_conv: dict[str, Any]) -> Optional[dict[str, Any]]:
    conversation_key = _get_conversation_key(target_conv)
    newest_first = _effective_turns_newest_first(target_conv.get("chat_history", []))

    with FileLock(tenshi_config.CONTEXT_COMPRESSION_LOCK_FILE_PATH, timeout=10):
        store = _load_store()
    entry = _conversation_store_entry(store, conversation_key)
    summaries = _normalize_summaries(entry)
    pending = _normalize_pending(entry)

    due_windows = _all_due_slices(newest_first, summaries, pending)
    if not due_windows:
        return None
    start_idx, end_idx = due_windows[0]
    due_slice = newest_first[start_idx:end_idx]
    window = _window_turns()
    if len(due_slice) != window:
        return None

    oldest_ts, newest_ts = _slice_time_span(due_slice)
    if _package_exists(summaries, oldest_ts, newest_ts):
        return None

    prev_keep = int(getattr(tenshi_config, "CONTEXT_COMPRESSION_CONTINUITY_PACKAGES", 3) or 3)

    previous = list(reversed(summaries[:prev_keep])) if prev_keep > 0 else []
    previous_block = "\n\n".join(
        f"[压缩包 {_package_time_span_label(s)}]\n{s['summary']}" for s in previous
    ).strip() or "（暂无）"

    rel_start, rel_end = _rel_range_for_newest_slice(start_idx, end_idx)
    chronological = list(reversed(due_slice))
    dialogue_parts = []
    for i, turn in enumerate(chronological):
        dialogue_parts.append(_format_turn_block_for_compression(turn, rel_pos=rel_start + i))
    dialogue_block = "\n\n".join(dialogue_parts).strip()
    if not dialogue_block:
        return None

    span_start = format_mmdd_hhmm(oldest_ts)
    span_end = format_mmdd_hhmm(newest_ts)

    prompt = tenshi_config.CONTEXT_COMPRESSION_PROMPT_TEMPLATE.format(
        rel_start=rel_start,
        rel_end=rel_end,
        time_span=f"{span_start}～{span_end}",
        previous_packages=previous_block,
        dialogue_block=dialogue_block,
        max_summary_chars=tenshi_config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS,
    )
    model_name = tenshi_config.deepseek_context_compression_primary()
    compression_system = "你只负责压缩上下文，不做聊天回复，不要脱离给定材料编造。摘要中须保留简明的时间变化。"
    try:
        summary_text, _ = call_deepseek(
            model_name=model_name,
            system_prompt=compression_system,
            messages=[{"role": "user", "content": prompt}],
            agent_complex=False,
        )
    except Exception as e:
        logger.warning(f"DeepSeek 压缩调用失败，尝试 OpenRouter 同款兜底：{format_exception_inline(e)}")
        summary_text = ""
        try:
            from llm_routing import try_openrouter_fallback_path

            fb = try_openrouter_fallback_path(
                model_name,
                lambda slug, path: call_deepseek(
                    slug,
                    compression_system,
                    [{"role": "user", "content": prompt}],
                    llm_path=path,
                    agent_complex=False,
                ),
                log=logger,
            )
            if fb:
                summary_text = fb[0] or ""
        except Exception as fb_exc:
            logger.warning(f"OpenRouter 压缩兜底失败：{format_exception_inline(fb_exc)}")
        if not summary_text:
            _mark_pending(conversation_key, target_conv, oldest_ts, newest_ts)
            return None

    summary_text = (summary_text or "").strip()
    if not summary_text:
        _mark_pending(conversation_key, target_conv, oldest_ts, newest_ts)
        return None

    return {
        "package_id": f"{conversation_key}:{span_start}_{span_end}",
        "summary": summary_text,
        "created_at": time.time(),
        "start_time": oldest_ts,
        "end_time": newest_ts,
        "start_time_label": span_start,
        "end_time_label": span_end,
        "model": model_name,
    }


def _mark_pending(
    conversation_key: str,
    target_conv: dict[str, Any],
    start_ts: float,
    end_ts: float,
) -> None:
    with FileLock(tenshi_config.CONTEXT_COMPRESSION_LOCK_FILE_PATH, timeout=30):
        store = _load_store()
        conv_entry = _conversation_store_entry(store, conversation_key)
        pending = _normalize_pending(conv_entry)
        key = (round(start_ts, 3), round(end_ts, 3))
        if key not in {(round(p["start_time"], 3), round(p["end_time"], 3)) for p in pending}:
            pending.append({"start_time": start_ts, "end_time": end_ts})
        store[conversation_key] = {
            "conversation_id": conversation_key,
            "user_id": str(target_conv.get("user_id") or ""),
            "updated_at": time.time(),
            "summaries": _normalize_summaries(conv_entry),
            "pending": pending,
        }
        _save_store(store)


def save_context_compression_entry(target_conv: dict[str, Any], entry: Optional[dict[str, Any]]) -> bool:
    if not isinstance(entry, dict):
        return False
    text = str(entry.get("summary") or "").strip()
    if not text:
        return False

    conversation_key = _get_conversation_key(target_conv)
    start_ts = float(entry.get("start_time") or 0.0)
    end_ts = float(entry.get("end_time") or 0.0)
    if not str(entry.get("start_time_label") or "").strip():
        entry["start_time_label"] = format_mmdd_hhmm(start_ts)
    if not str(entry.get("end_time_label") or "").strip():
        entry["end_time_label"] = format_mmdd_hhmm(end_ts)

    with FileLock(tenshi_config.CONTEXT_COMPRESSION_LOCK_FILE_PATH, timeout=30):
        store = _load_store()
        conv_entry = _conversation_store_entry(store, conversation_key)
        summaries = _normalize_summaries(conv_entry)
        summaries = [
            s
            for s in summaries
            if not (
                abs(float(s.get("start_time") or 0) - start_ts) < 0.5
                and abs(float(s.get("end_time") or 0) - end_ts) < 0.5
            )
        ]
        summaries.append(entry)

        summaries.sort(
            key=lambda x: (float(x.get("end_time") or 0), float(x.get("start_time") or 0)),
            reverse=True,
        )
        max_keep = int(getattr(tenshi_config, "CONTEXT_COMPRESSION_MAX_PACKAGES", 20) or 20)
        if max_keep > 0 and len(summaries) > max_keep:
            summaries = summaries[:max_keep]

        pending = [
            p
            for p in _normalize_pending(conv_entry)
            if not (abs(p["start_time"] - start_ts) < 0.5 and abs(p["end_time"] - end_ts) < 0.5)
        ]

        store[conversation_key] = {
            "conversation_id": conversation_key,
            "user_id": str(target_conv.get("user_id") or ""),
            "updated_at": time.time(),
            "summaries": summaries,
            "pending": pending,
        }
        _save_store(store)
    return True


def ensure_context_compression_after_persist(target_conv: dict[str, Any]) -> Optional[dict[str, Any]]:
    """主对话落盘后：循环压缩直到没有待压窗口或失败。"""
    last: Optional[dict[str, Any]] = None
    max_rounds = int(getattr(tenshi_config, "CONTEXT_COMPRESSION_MAX_PACKAGES_PER_REPLY", 5) or 5)
    for _ in range(max(1, max_rounds)):
        if not needs_context_compression(target_conv):
            break
        entry = compute_context_compression_entry(target_conv)
        if not entry or not save_context_compression_entry(target_conv, entry):
            break
        last = entry
    return last


def ensure_context_compression_file_initialized() -> None:
    path = tenshi_config.CONTEXT_COMPRESSION_FILE_PATH
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(tenshi_config.CONTEXT_COMPRESSION_LOCK_FILE_PATH, timeout=10):
        if not os.path.exists(path):
            _save_store({})
