# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.append(SRC_DIR)

import tenshi_config
from tenshi_utils import FileLock, ensure_tenshi_log_newest_first
















WEEK_SECONDS = int(timedelta(weeks=1).total_seconds())


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _msg_dedupe_key(msg: Dict[str, Any]) -> str:

    mid = msg.get("msg_id")
    t = msg.get("time")
    if mid is not None and mid != "":
        return f"mid={mid}|t={t}"
    return f"t={t}"


def _week_start_utc_ts(ts: int) -> int:
    """
    week slice 定义：
    - 本地时区固定为 UTC+TIMEZONE_OFFSET
    - 周一 00:00:00 ~ 周日 23:59:59
    返回该周的“周一 00:00:00（本地）对应的 UTC 时间戳（秒）”
    """
    tz_local = timezone(timedelta(hours=int(getattr(tenshi_config, "TIMEZONE_OFFSET", 0))))
    dt_local = datetime.fromtimestamp(float(ts), tz_local)

    ws_local = dt_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=dt_local.weekday())
    ws_utc = ws_local.astimezone(timezone.utc)
    return int(ws_utc.timestamp())


def _slice_date_local(week_start_utc_ts: int) -> str:
    """
    输入为“周一 00:00:00（本地）对应的 UTC 时间戳”。
    输出该周一在本地时区下的 YYYY-MM-DD。
    """
    tz_local = timezone(timedelta(hours=int(getattr(tenshi_config, "TIMEZONE_OFFSET", 0))))
    dt_local = datetime.fromtimestamp(float(week_start_utc_ts), timezone.utc).astimezone(tz_local)
    return dt_local.strftime("%Y-%m-%d")


def _load_json_text(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_json_gz(path: str) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)

def _slice_base_name(slice_start_week_utc_ts: int, slice_weeks: int) -> str:
    date_str = _slice_date_local(slice_start_week_utc_ts)
    return f"tenshi_log-{date_str}+{int(slice_weeks)}Week(s)"


def _archive_paths_for_slice(base_name: str) -> Tuple[str, str]:
    archive_json = os.path.join(tenshi_config.CHAT_LOG_ARCHIVE_DIR_PATH, f"{base_name}.json")
    return archive_json, archive_json + ".gz"


def _load_archive_slice(base_name: str) -> Tuple[Any, bool, bool]:
    """
    返回 (content, json_exists, gz_exists)
    content 为 list[conversation]
    """
    archive_json, archive_gz = _archive_paths_for_slice(base_name)
    json_exists = os.path.exists(archive_json)
    gz_exists = os.path.exists(archive_gz)
    if gz_exists:
        return _load_json_gz(archive_gz), json_exists, gz_exists
    if json_exists:
        return _load_json_text(archive_json), json_exists, gz_exists
    return [], json_exists, gz_exists


def _write_archive_slice_atomic(base_name: str, content: Any, *, tmp_suffix: str) -> Tuple[str, str]:
    """
    原子写：先写 JSON temp + gz temp，最终由调用方 os.replace。
    返回 (tmp_json_path, tmp_gz_path)
    """
    os.makedirs(tenshi_config.CHAT_LOG_ARCHIVE_DIR_PATH, exist_ok=True)
    archive_json, archive_gz = _archive_paths_for_slice(base_name)

    tmp_json = archive_json + tmp_suffix
    tmp_gz = archive_gz + tmp_suffix

    with open(tmp_json, "w", encoding="utf-8") as f:
        f.write(json.dumps(content, ensure_ascii=False, indent=2) + "\n")

    with open(tmp_json, "rb") as fr, gzip.open(tmp_gz, "wb") as fgz:
        shutil.copyfileobj(fr, fgz)

    return tmp_json, tmp_gz


def _write_tenshi_log_atomic(data: Any, *, tmp_path: str) -> str:
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return tmp_path


def _merge_archive_content(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    incoming: [{conversation_id, user_id, chat_history:[msg...]}, ...]
    existing: 同结构
    """
    conv_key = lambda c: str(c.get("conversation_id") or c.get("user_id") or "")
    existing_map: Dict[str, Dict[str, Any]] = {}
    for conv in existing:
        k = conv_key(conv)
        if not k:
            continue
        existing_map[k] = conv

    for inc_conv in incoming:
        k = conv_key(inc_conv)
        if not k:
            continue
        if k not in existing_map:
            existing_map[k] = {"conversation_id": inc_conv.get("conversation_id"), "user_id": inc_conv.get("user_id"), "chat_history": []}

        target = existing_map[k]
        target_hist = target.get("chat_history") or []
        target_keys = {_msg_dedupe_key(m) for m in target_hist if isinstance(m, dict)}

        for m in inc_conv.get("chat_history") or []:
            if not isinstance(m, dict):
                continue
            mk = _msg_dedupe_key(m)
            if mk in target_keys:
                continue
            target_hist.append(m)
            target_keys.add(mk)


        try:
            target["chat_history"] = sorted(target_hist, key=lambda m: _safe_int((m or {}).get("time"), 0), reverse=True)
        except Exception:
            target["chat_history"] = target_hist

        target["conversation_id"] = target.get("conversation_id") or inc_conv.get("conversation_id")
        target["user_id"] = target.get("user_id") or inc_conv.get("user_id")


    out = list(existing_map.values())
    out.sort(key=lambda c: str(c.get("conversation_id") or c.get("user_id") or ""))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="按周切片归档 tenshi_log.json，避免无限增长。")
    parser.add_argument("--dry-run", action="store_true", help="仅打印计划，不写文件。")
    args = parser.parse_args()

    log_path = tenshi_config.LOG_FILE_PATH
    if not os.path.exists(log_path):
        print(f"[weekly_archive] 未找到日志文件：{log_path}（无需处理）")
        return 0

    keep_weeks = int(getattr(tenshi_config, "CHAT_LOG_ARCHIVE_KEEP_WEEKS", 2))
    min_slice = int(getattr(tenshi_config, "CHAT_LOG_ARCHIVE_MIN_MESSAGES_PER_SLICE", 35))
    min_retain = int(getattr(tenshi_config, "CHAT_LOG_ARCHIVE_MIN_RETAIN_MESSAGES", min_slice))

    if keep_weeks < 2:
        print(f"[weekly_archive] CHAT_LOG_ARCHIVE_KEEP_WEEKS={keep_weeks} 不合法（需要 >= 2）")
        return 1

    tz_offset = int(getattr(tenshi_config, "TIMEZONE_OFFSET", 0))
    tz_local = timezone(timedelta(hours=tz_offset))
    now_local = datetime.now(tz_local)
    current_week_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now_local.weekday())
    current_week_start_utc_ts = int(current_week_start_local.astimezone(timezone.utc).timestamp())

    week_seconds = WEEK_SECONDS
    kept_start_utc_ts = current_week_start_utc_ts - int((keep_weeks - 1) * week_seconds)
    archive_last_week_start_utc_ts = kept_start_utc_ts - week_seconds

    if archive_last_week_start_utc_ts < 0:
        print("[weekly_archive] 计算出的归档范围不合法（跳过）")
        return 0

    os.makedirs(tenshi_config.CHAT_LOG_ARCHIVE_DIR_PATH, exist_ok=True)

    lock_base = tenshi_config.LOCK_FILE_PATH
    with FileLock(lock_base, timeout=120):
        with open(log_path, "r", encoding="utf-8") as f:
            current_data = json.load(f)

        current_data, _ = ensure_tenshi_log_newest_first(current_data)

        if not isinstance(current_data, list):
            print("[weekly_archive] tenshi_log.json 顶层不是 list（跳过）")
            return 1

        counts_by_week: Dict[int, int] = {}
        earliest_old_week_start: Optional[int] = None
        kept_count = 0


        for conv in current_data:
            if not isinstance(conv, dict):
                continue
            hist = conv.get("chat_history") or []
            if not isinstance(hist, list):
                continue
            for msg in hist:
                if not isinstance(msg, dict):
                    continue
                ts = _safe_int(msg.get("time"), -1)
                if ts <= 0:
                    continue
                w = _week_start_utc_ts(ts)
                counts_by_week[w] = counts_by_week.get(w, 0) + 1
                if w >= kept_start_utc_ts:
                    kept_count += 1
                else:
                    if earliest_old_week_start is None or w < earliest_old_week_start:
                        earliest_old_week_start = w


        if kept_count < min_retain:
            print(f"[weekly_archive] kept_count={kept_count} < min_retain={min_retain}（跳过）")
            return 0

        if earliest_old_week_start is None:
            print("[weekly_archive] 没有可移除的旧消息（跳过）")
            return 0

        if earliest_old_week_start > archive_last_week_start_utc_ts:
            print("[weekly_archive] 可移除范围为空（跳过）")
            return 0


        week_keys_for_planning: List[int] = []
        w = earliest_old_week_start
        while w <= kept_start_utc_ts:
            week_keys_for_planning.append(w)
            w += week_seconds


        slices: List[Tuple[int, int]] = []
        cursor = earliest_old_week_start


        def _count_in_week(week_start_utc_ts: int) -> int:
            return counts_by_week.get(week_start_utc_ts, 0)


        while cursor <= archive_last_week_start_utc_ts:
            total = 0
            end_w = cursor
            w = cursor
            while w <= archive_last_week_start_utc_ts:

                if w >= kept_start_utc_ts:
                    break
                total += _count_in_week(w)
                end_w = w
                if total >= min_slice:
                    break
                w += week_seconds

            if total > 0 and end_w < kept_start_utc_ts:
                slices.append((cursor, end_w))
            cursor = end_w + week_seconds



        expected_kept_after = kept_count
        if expected_kept_after < min_slice:
            print(f"[weekly_archive] expected_kept_after={expected_kept_after} < min_slice={min_slice} (skip)")
            return 0


        archive_week_starts: set[int] = set()
        for start_w, end_w in slices:
            ww = start_w
            while ww <= end_w:
                archive_week_starts.add(ww)
                ww += week_seconds

        if not archive_week_starts:
            print("[weekly_archive] computed archive_week_starts empty (skip)")
            return 0

        print(f"[weekly_archive] planning slices={slices} (min_slice={min_slice}) kept_count={kept_count}")

        if args.dry_run:
            return 0


        week_to_slice_base: Dict[int, str] = {}
        slice_bases_in_order: List[str] = []
        for start_w, end_w in slices:
            slice_weeks = int((end_w - start_w) // week_seconds) + 1
            base = _slice_base_name(start_w, slice_weeks)
            slice_bases_in_order.append(base)
            ww = start_w
            while ww <= end_w:
                week_to_slice_base[ww] = base
                ww += week_seconds



        archive_slice_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
        updated_data: List[Dict[str, Any]] = []

        def conv_key(conv_obj: Dict[str, Any]) -> str:
            return str(conv_obj.get("conversation_id") or conv_obj.get("user_id") or "")

        for conv in current_data:
            if not isinstance(conv, dict):
                continue
            hist = conv.get("chat_history") or []
            if not isinstance(hist, list):
                continue

            new_hist: List[Dict[str, Any]] = []
            removed_hist_by_slice: Dict[str, List[Dict[str, Any]]] = {}

            for msg in hist:
                if not isinstance(msg, dict):
                    continue
                ts = _safe_int(msg.get("time"), -1)
                if ts <= 0:

                    new_hist.append(msg)
                    continue

                w = _week_start_utc_ts(ts)
                if w in archive_week_starts:
                    base = week_to_slice_base.get(w)
                    if not base:

                        new_hist.append(msg)
                        continue
                    removed_hist_by_slice.setdefault(base, []).append(msg)
                else:
                    new_hist.append(msg)

            if new_hist:

                new_conv = dict(conv)
                new_conv["chat_history"] = new_hist
                updated_data.append(new_conv)


            if removed_hist_by_slice:
                ck = conv_key(conv)
                for base, removed_hist in removed_hist_by_slice.items():
                    bucket = archive_slice_map.setdefault(base, {})
                    bucket.setdefault(
                        ck,
                        {
                            "conversation_id": conv.get("conversation_id"),
                            "user_id": conv.get("user_id"),
                            "chat_history": [],
                        },
                    )
                    bucket[ck]["chat_history"].extend(removed_hist)

        updated_data, _ = ensure_tenshi_log_newest_first(updated_data)


        slices_to_update = [b for b in slice_bases_in_order if b in archive_slice_map]
        existing_slice_payloads: Dict[str, List[Dict[str, Any]]] = {}
        existing_slice_states: Dict[str, Tuple[bool, bool]] = {}

        for base in slices_to_update:
            existing_payload, json_existed, gz_existed = _load_archive_slice(base)
            if not isinstance(existing_payload, list):
                existing_payload = []
            existing_slice_payloads[base] = existing_payload
            existing_slice_states[base] = (json_existed, gz_existed)

        merged_slice_payloads: Dict[str, List[Dict[str, Any]]] = {}
        for base in slices_to_update:
            incoming_map = archive_slice_map[base]
            incoming_list = list(incoming_map.values())

            for inc_conv in incoming_list:
                try:
                    inc_conv["chat_history"] = sorted(
                        inc_conv.get("chat_history") or [],
                        key=lambda m: _safe_int((m or {}).get("time"), 0),
                        reverse=True,
                    )
                except Exception:
                    pass

            merged = _merge_archive_content(existing_slice_payloads.get(base, []), incoming_list)
            merged_slice_payloads[base] = merged


        temp_suffix = ".tmp.weekly_archive"
        tmp_log_path = log_path + temp_suffix

        tmp_archive_paths: List[Tuple[str, str]] = []
        try:
            _write_tenshi_log_atomic(updated_data, tmp_path=tmp_log_path)

            for base in slices_to_update:
                tmp_json, tmp_gz = _write_archive_slice_atomic(base, merged_slice_payloads[base], tmp_suffix=temp_suffix)
                tmp_archive_paths.append((tmp_json, tmp_gz))


            os.replace(tmp_log_path, log_path)
            for tmp_json, tmp_gz in tmp_archive_paths:
                archive_json = tmp_json.replace(temp_suffix, "")
                archive_gz = tmp_gz.replace(temp_suffix, "")
                os.replace(tmp_json, archive_json)
                os.replace(tmp_gz, archive_gz)

        except Exception as e:

            print(f"[weekly_archive] FAILED: {e}. Attempting rollback...", file=sys.stderr)
            try:
                _write_tenshi_log_atomic(current_data, tmp_path=tmp_log_path + ".rollback")
                os.replace(tmp_log_path + ".rollback", log_path)
            except Exception:
                pass

            for base in slices_to_update:
                try:
                    original_payload = existing_slice_payloads.get(base, [])
                    tmp_json, tmp_gz = _write_archive_slice_atomic(base, original_payload, tmp_suffix=temp_suffix + ".rollback")
                    archive_json, archive_gz = _archive_paths_for_slice(base)
                    os.replace(tmp_json, archive_json)
                    os.replace(tmp_gz, archive_gz)
                except Exception:
                    pass


                try:
                    json_existed, gz_existed = existing_slice_states.get(base, (False, False))
                    if not json_existed:
                        try:
                            os.remove(_archive_paths_for_slice(base)[0])
                        except Exception:
                            pass
                    if not gz_existed:
                        try:
                            os.remove(_archive_paths_for_slice(base)[1])
                        except Exception:
                            pass
                except Exception:
                    pass
            return 1


        for tmp_json, tmp_gz in tmp_archive_paths:
            try:
                if os.path.exists(tmp_json):
                    os.remove(tmp_json)
            except Exception:
                pass
            try:
                if os.path.exists(tmp_gz):
                    os.remove(tmp_gz)
            except Exception:
                pass
        try:
            if os.path.exists(tmp_log_path):
                os.remove(tmp_log_path)
        except Exception:
            pass

        print(f"[weekly_archive] success: archived weeks={slices} into slices={slices_to_update}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
