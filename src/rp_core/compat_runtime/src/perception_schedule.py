# SPDX-License-Identifier: AGPL-3.0-only









from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

import tenshi_config
from tenshi_utils import FileLock

logger = logging.getLogger(__name__)

SCHEDULE_FILENAME = "perception_schedule.json"
KV_KEY = "perception_schedule"
POLL_INTERVAL_MS = 1 * 60 * 1000
SCHEDULE_EARLY_WINDOW_MS = POLL_INTERVAL_MS
SCHEDULE_LATE_WINDOW_MS = 15 * 60 * 1000

MAX_STALE_AFTER_MS = 24 * 60 * 60 * 1000
MAX_ENTRIES = 32







REPLY_RETRY_NOTE_PREFIX = "[reply-retry"
_REPLY_RETRY_NOTE_RE = re.compile(r"^\[reply-retry(?:\s+(\d+)\s*/\s*(\d+))?\]", re.IGNORECASE)


def is_reply_retry_note(note: str | None) -> bool:
    """是否为「主回复彻底失败」登记的重试条目。"""
    return _REPLY_RETRY_NOTE_RE.match((note or "").strip()) is not None


def parse_reply_retry_note(note: str | None) -> tuple[int, int] | None:
    """解析重试附言，返回 (第几次, 上限)；不是重试条目返回 None。

    旧格式 "[reply-retry]"（没有次数）视为 (1, 1)。
    """
    m = _REPLY_RETRY_NOTE_RE.match((note or "").strip())
    if not m:
        return None
    if not m.group(1):
        return 1, 1
    return int(m.group(1)), int(m.group(2))


def build_reply_retry_note(reason: str, attempt: int = 1, max_attempts: int = 1) -> str:
    """构造重试附言（次数上限为 1 时不写次数，保持旧格式可读）。"""
    body = (
        f"上一轮主回复全链失败（{reason or '所有模型/通路都失败'}），请尽快补跑一次回复："
        "读未回复消息重新生成并发出，不要当成普通定时起念。"
    )
    if int(max_attempts) <= 1:
        return f"{REPLY_RETRY_NOTE_PREFIX}] {body}"
    return f"{REPLY_RETRY_NOTE_PREFIX} {int(attempt)}/{int(max_attempts)}] {body}"

_TRIGGER_AT_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\s*$"
)


def schedule_file_path() -> str:
    return os.path.join(tenshi_config.PROJECT_ROOT, "data", SCHEDULE_FILENAME)


def _local_tz() -> timezone:
    """项目统一时区（北京时间 UTC+8，由 TIMEZONE_OFFSET 决定）。"""
    return timezone(timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))


def _format_local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=_local_tz()).strftime("%Y-%m-%d %H:%M:%S")


def _now_ms() -> int:
    return int(datetime.now(tz=_local_tz()).timestamp() * 1000)


def _new_entry_id(existing: set[str]) -> str:
    for _ in range(16):
        nid = f"sched_{secrets.token_hex(4)}"
        if nid not in existing:
            return nid
    return f"sched_{_now_ms()}"


def _parse_trigger_at(trigger_at: str) -> int:
    raw = (trigger_at or "").strip()
    m = _TRIGGER_AT_RE.match(raw)
    if not m:
        raise ValueError(
            "trigger_at 格式应为 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS（按北京时间 UTC+8 理解）"
        )
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mm = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    dt = datetime(y, mo, d, hh, mm, ss, tzinfo=_local_tz())
    return int(dt.timestamp() * 1000)


def _compute_trigger_ms(
    *,
    trigger_at: str | None,
    interval_minutes: int | None,
    interval_hours: int | None,
) -> tuple[int, int | None]:
    now_ms = _now_ms()
    interval_sec = None
    if trigger_at:
        target = _parse_trigger_at(trigger_at)
        if target <= now_ms:
            raise ValueError("trigger_at 必须晚于当前时间")
        return target, None
    minutes = int(interval_minutes or 0)
    hours = int(interval_hours or 0)
    if minutes <= 0 and hours <= 0:
        raise ValueError("请提供 trigger_at，或 interval_minutes / interval_hours 之一")
    interval_sec = minutes * 60 + hours * 3600
    return now_ms + interval_sec * 1000, interval_sec


def _normalize_entry(raw: Any, now_ms: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    note = (raw.get("note") or "").strip()
    try:
        trigger_ms = int(raw.get("trigger_at_ms") or 0)
    except (TypeError, ValueError):
        return None
    if not note or trigger_ms <= 0:
        return None
    late_by = now_ms - trigger_ms
    if late_by > MAX_STALE_AFTER_MS:
        return None
    if late_by > SCHEDULE_LATE_WINDOW_MS:
        return None
    eid = (raw.get("id") or "").strip() or _new_entry_id(set())
    interval_sec = raw.get("interval_sec")
    set_at_ms = int(raw.get("set_at_ms") or now_ms)
    return {
        "id": eid,
        "trigger_at_ms": trigger_ms,
        "trigger_at_bj": _format_local(trigger_ms),
        "note": note,
        "interval_sec": interval_sec,
        "set_at_ms": set_at_ms,
        "set_at_bj": _format_local(set_at_ms),
    }


def _sanitize_entries(entries: list[Any], now_ms: int | None = None) -> list[dict[str, Any]]:
    now_ms = now_ms if now_ms is not None else _now_ms()
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in entries or []:
        ent = _normalize_entry(raw, now_ms)
        if not ent or ent["id"] in seen_ids:
            continue
        seen_ids.add(ent["id"])
        out.append(ent)
    out.sort(key=lambda e: e["trigger_at_ms"])
    return out[:MAX_ENTRIES]


def _extract_raw_entries(data: Any) -> list[dict[str, Any]]:
    """从磁盘 JSON 取出条目列表，不做「过期 15 分钟」过滤（完成触发时需要能删到迟到的队首）。"""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("entries"), list):
            return [e for e in data["entries"] if isinstance(e, dict)]
        legacy: list[dict[str, Any]] = []
        for key in ("tenshi", "elaina"):
            if isinstance(data.get(key), dict):
                legacy.append(data[key])
        if data.get("trigger_at_ms"):
            legacy.append(data)
        return legacy
    return []


def _load_raw_entries_from_disk() -> list[dict[str, Any]]:
    path = schedule_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return _extract_raw_entries(data)


def _load_doc() -> dict[str, Any]:
    path = schedule_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return {"entries": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"entries": []}
    return {"entries": _sanitize_entries(_extract_raw_entries(data))}


def _save_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    entries = _sanitize_entries(doc.get("entries") or [])
    path = schedule_file_path()
    lock_path = path + ".lock"
    with FileLock(lock_path, timeout=15):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
    return entries


def _kv_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    slim = [
        {
            "id": e["id"],
            "trigger_at_ms": e["trigger_at_ms"],
            "note": e["note"],
        }
        for e in entries
    ]
    return {"entries": slim}


def _validate_http_sync_config(url: str, secret: str) -> str | None:
    """
    urllib/http.client 要求请求头为 latin-1；非 ASCII 会在 Bearer 处报
    'latin-1' codec can't encode character '\\uff1f' in position 7（与附言中文无关）。
    """
    for label, val in (
        ("PERCEPTION_SCHEDULE_SYNC_URL", url),
        ("PERCEPTION_SCHEDULE_SYNC_SECRET", secret),
    ):
        try:
            val.encode("ascii")
        except UnicodeEncodeError as e:
            bad = e.object[e.start : e.end] if e.object is not None else "?"
            return (
                f"KV 同步失败: GitHub Secret `{label}` 含非 ASCII 字符（如 {bad!r} U+{ord(bad):04X}）。"
                f"请改为纯英文/数字 URL 与密钥；附言里的中日韩写在 JSON 正文，不受此限制。"
            )
    return None


def _strip_secret(value: str) -> str:
    return (value or "").strip().strip("\r\n")


def _format_kv_http_error(status_code: int, body: str, url: str) -> str:
    preview = (body or "").strip()[:300]
    if status_code == 401:
        hint = preview
        try:
            data = json.loads(body)
            hint = data.get("hint") or data.get("detail") or preview
            wl = data.get("worker_has_secret")
            lg = data.get("token_length_got")
            le = data.get("token_length_expected")
            if wl is not None:
                hint += (
                    f"\nWorker 已配置密钥: {wl}；"
                    f"收到 token 长度 {lg}，期望 {le}（长度不同即 Secret 不一致或未 Deploy）。"
                )
        except (json.JSONDecodeError, TypeError):
            pass
        return (
            f"KV 同步失败 HTTP 401（Worker 拒绝授权）。{hint}\n"
            f"请逐项核对：\n"
            f"1) Cloudflare Worker 环境变量名 SCHEDULE_SYNC_SECRET（不是 PERCEPTION_ 前缀）；\n"
            f"2) 与 GitHub Secret PERCEPTION_SCHEDULE_SYNC_SECRET 逐字相同并已 Deploy Worker；\n"
            f"3) 伊蕾娜走 Calling 时，Secret 设在 Calling 仓库，可另设 ELAINA_PERCEPTION_SCHEDULE_SYNC_*；\n"
            f"4) URL 为 Worker 公网根地址（如 https://elaina….workers.dev），非 Dashboard 链接。\n"
            f"请求: {url}"
        )
    return f"KV 同步异常 HTTP {status_code}: {preview}"


def _sync_to_worker_kv(entries: list[dict[str, Any]]) -> str:
    url = _strip_secret(os.getenv("PERCEPTION_SCHEDULE_SYNC_URL") or "")
    secret = _strip_secret(os.getenv("PERCEPTION_SCHEDULE_SYNC_SECRET") or "")
    if not url or not secret:
        return "KV 同步已跳过（未配置 PERCEPTION_SCHEDULE_SYNC_URL / PERCEPTION_SCHEDULE_SYNC_SECRET）"

    cfg_err = _validate_http_sync_config(url, secret)
    if cfg_err:
        return cfg_err

    endpoint = url.rstrip("/") + "/internal/perception-schedule"
    headers = {
        "Authorization": f"Bearer {secret}",
        "X-Schedule-Sync-Secret": secret,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "perception-schedule-sync/1",
    }

    try:
        if not entries:
            resp = requests.delete(endpoint, headers=headers, timeout=15)
            if resp.status_code == 405:
                resp = requests.post(
                    endpoint,
                    headers=headers,
                    json={"entries": []},
                    timeout=15,
                )
            if resp.ok:
                return "KV 已清空（无待触发任务）"
            return _format_kv_http_error(resp.status_code, resp.text, endpoint)

        resp = requests.post(
            endpoint,
            headers=headers,
            json=_kv_payload(entries),
            timeout=15,
        )
        if resp.ok:
            return f"已同步 {len(entries)} 条到 Cloudflare KV"
        return _format_kv_http_error(resp.status_code, resp.text, endpoint)
    except UnicodeEncodeError as e:
        return (
            f"KV 同步失败: 请求头编码错误 ({e})。"
            f"请检查 PERCEPTION_SCHEDULE_SYNC_SECRET 是否为纯 ASCII；与附言中文无关。"
        )
    except requests.RequestException as e:
        return f"KV 同步失败: {e}"
    except Exception as e:
        return f"KV 同步失败: {e}"


def _format_queue_summary(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "（队列已空）"
    lines = []
    now_ms = _now_ms()
    for i, e in enumerate(entries[:8], 1):
        lead = max(0, (int(e["trigger_at_ms"]) - now_ms) // 60000)
        lines.append(
            f"  {i}. [{e['trigger_at_bj']}] 约{lead}分钟后 — {e['note'][:80]}"
        )
    if len(entries) > 8:
        lines.append(f"  … 另有 {len(entries) - 8} 条")
    return "\n".join(lines)


def _build_entry(
    *,
    note: str,
    trigger_at: str | None,
    interval_minutes: int | None,
    interval_hours: int | None,
) -> dict[str, Any]:
    note_text = (note or "").strip()
    if not note_text:
        raise ValueError("note（附言）不能为空")
    trigger_ms, interval_sec = _compute_trigger_ms(
        trigger_at=trigger_at,
        interval_minutes=interval_minutes,
        interval_hours=interval_hours,
    )
    now_ms = _now_ms()
    existing_ids = {e.get("id") for e in _load_doc().get("entries") or [] if isinstance(e, dict)}
    return {
        "id": _new_entry_id({i for i in existing_ids if isinstance(i, str)}),
        "trigger_at_ms": trigger_ms,
        "trigger_at_bj": _format_local(trigger_ms),
        "note": note_text,
        "interval_sec": interval_sec,
        "set_at_ms": now_ms,
        "set_at_bj": _format_local(now_ms),
    }


def schedule_perception(
    note: str = "",
    trigger_at: str | None = None,
    interval_minutes: int | None = None,
    interval_hours: int | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> str:
    """
    登记一条或多条定时感知。多条会按时间排序进入同一队列。
    """
    doc = _load_doc()
    queue = list(doc.get("entries") or [])
    new_items: list[dict[str, Any]] = []

    if entries and isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            new_items.append(
                _build_entry(
                    note=item.get("note") or "",
                    trigger_at=item.get("trigger_at"),
                    interval_minutes=item.get("interval_minutes"),
                    interval_hours=item.get("interval_hours"),
                )
            )
    else:
        new_items.append(
            _build_entry(
                note=note,
                trigger_at=trigger_at,
                interval_minutes=interval_minutes,
                interval_hours=interval_hours,
            )
        )

    if not new_items:
        raise ValueError("未添加任何有效定时条目")

    queue.extend(new_items)
    saved = _save_doc({"entries": queue})
    kv_msg = _sync_to_worker_kv(saved)

    return (
        f"[TOOL RESULT: schedule_perception]\n"
        f"本次新增 {len(new_items)} 条，队列共 {len(saved)} 条（按时间排序）。\n"
        f"数据文件: {schedule_file_path()}\n"
        f"{kv_msg}\n"
        f"待触发队列:\n{_format_queue_summary(saved)}\n"
        f"说明: Cloudflare 每 1 分钟检查；队首落在「预订时刻前 1 分钟～过期后 15 分钟」内将 scheduled_force 推送；"
        f"触发后该条从文件与 KV 移除（一般不会重复触发）。"
    )


def load_pending_schedule_entry() -> dict[str, Any] | None:
    entries = _load_doc().get("entries") or []
    return entries[0] if entries else None


def list_pending_schedule_entries() -> list[dict[str, Any]]:
    """当前待触发队列（副本，按时间升序）。"""
    return [dict(e) for e in (_load_doc().get("entries") or [])]


def has_pending_reply_retry() -> bool:
    """队列里是否已有待触发的重试条目（避免同一次故障把重试无限叠加）。"""
    return any(is_reply_retry_note(e.get("note")) for e in list_pending_schedule_entries())


def complete_scheduled_trigger(trigger_at_ms: int | None = None) -> None:
    """感知层在定时强制触发完成后，移除已触发条目并刷新 KV。"""
    entries = _load_raw_entries_from_disk()
    if not entries:
        saved = _save_doc({"entries": []})
        _sync_to_worker_kv(saved)
        return

    if trigger_at_ms and trigger_at_ms > 0:
        remaining = [e for e in entries if int(e.get("trigger_at_ms") or 0) != int(trigger_at_ms)]
        if len(remaining) == len(entries):
            remaining = entries[1:]
    else:
        remaining = entries[1:]

    saved = _save_doc({"entries": remaining})
    _sync_to_worker_kv(saved)



def clear_character_schedule(char: str | None = None) -> None:
    complete_scheduled_trigger()


def load_character_schedule(char: str | None = None) -> dict | None:
    return load_pending_schedule_entry()
