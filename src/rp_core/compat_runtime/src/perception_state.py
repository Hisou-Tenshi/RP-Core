# SPDX-License-Identifier: AGPL-3.0-only
























from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import tenshi_config
from error_text import format_exception_inline
from tenshi_utils import FileLock

logger = logging.getLogger(__name__)


def state_file_path() -> str:
    """感知层状态文件的绝对路径（可在测试里 monkeypatch tenshi_config 覆盖）。"""
    return getattr(
        tenshi_config,
        "PERCEPTION_STATE_FILE_PATH",
        os.path.join(tenshi_config.PROJECT_ROOT, "data", "perception_state.json"),
    )


def _parse_state(raw) -> dict | None:
    """校验状态文档，返回统一结构；无有效 ``last_silence_at`` 时返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        ts = int(raw.get("last_silence_at") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return {
        "last_silence_at": ts,
        "last_silence_bj": raw.get("last_silence_bj") or raw.get("last_silence_jst") or "",
        "updated_at": raw.get("updated_at") or "",
    }


def _read_state_file() -> dict | None:
    try:
        path = state_file_path()
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return _parse_state(json.load(f))
    except Exception as e:
        logger.warning(f"读取感知层状态文件失败：{format_exception_inline(e)}")
        return None


def _scan_log_for_last_silence() -> int:
    """兼容回退：从 tenshi_log.json 扫最后一条 SILENCE 记录的时间戳。"""
    try:
        from perception_layer import get_last_perception_silence_time_from_log

        return int(get_last_perception_silence_time_from_log() or 0)
    except Exception as e:
        logger.debug(f"回退扫描 tenshi_log SILENCE 记录失败：{format_exception_inline(e)}")
        return 0


def get_last_silence_at() -> int:
    """上次感知层沉默的 unix 时间戳；查不到返回 0。

    优先读状态文件（新口径），缺失时回退扫对话日志（部署前的历史沉默记录）。
    """
    entry = _read_state_file()
    if entry is not None:
        return int(entry["last_silence_at"])
    return _scan_log_for_last_silence()


def write_last_silence_at(timestamp: int | None = None) -> int:
    """记录「本次沉默」时刻，返回实际写入的时间戳（写入失败返回 0）。

    调用点：感知层判定 SILENCE 之后（无论这次是否要把沉默存档到对话日志）。
    """
    try:
        ts = int(timestamp if timestamp is not None else datetime.now(tz=timezone.utc).timestamp())
    except (TypeError, ValueError):
        ts = int(datetime.now(tz=timezone.utc).timestamp())
    if ts <= 0:
        return 0

    bj = timezone(timedelta(hours=int(getattr(tenshi_config, "TIMEZONE_OFFSET", 8) or 8)))
    try:
        path = state_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "last_silence_at": ts,
            "last_silence_bj": datetime.fromtimestamp(ts, bj).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with FileLock(path, timeout=15):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        return ts
    except Exception as e:
        logger.error(f"写入感知层状态文件失败：{format_exception_inline(e)}")
        return 0
