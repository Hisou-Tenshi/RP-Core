# SPDX-License-Identifier: AGPL-3.0-only



from __future__ import annotations

import copy
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import tenshi_config
from kb_storage import _write_json_atomic, load_kb_core
from weather_utils import _maybe_override_timezone_from_latlon, reverse_geocode_label

logger = logging.getLogger("TenshiUserLocation")


KB_TOP_LEVEL_KEY = "user_location"

WEATHER_VALID_DAYS = int(tenshi_config.LOCATION_WEATHER_VALID_DAYS)
DIALOGUE_CUTOFF_HOUR = int(tenshi_config.DAILY_SUMMARY_CUTOFF_HOUR)
DEFAULT_LAT = float(getattr(tenshi_config, "WEATHER_DEFAULT_LAT", 0.0))
DEFAULT_LON = float(getattr(tenshi_config, "WEATHER_DEFAULT_LON", 0.0))
DEFAULT_PLACE_LABEL = getattr(tenshi_config, "WEATHER_DEFAULT_PLACE_LABEL", "未设置默认位置")


def _local_tz():
    return timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET))


def _now_local() -> datetime:
    return datetime.now(_local_tz())


def strip_user_location_from_kb(kb_data: dict[str, Any] | None) -> dict[str, Any]:
    """供 LLM 总结/合并使用的 KB 视图：去掉顶层 user_location，避免模型改写定位。"""
    if not isinstance(kb_data, dict):
        return {}
    out = copy.deepcopy(kb_data)
    out.pop(KB_TOP_LEVEL_KEY, None)
    return out


def load_user_location() -> dict[str, Any] | None:
    """从 knowledge_base.json 读取 user_location 对象。"""
    kb = load_kb_core()
    loc = kb.get(KB_TOP_LEVEL_KEY)
    if not isinstance(loc, dict):
        return None
    try:
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
        recorded_at = int(loc["recorded_at"])
    except (KeyError, TypeError, ValueError):
        return None
    place_name = loc.get("place_name")
    if not isinstance(place_name, str) or not place_name.strip():
        place_name = f"{lat:.4f},{lon:.4f}"
    return {
        "latitude": lat,
        "longitude": lon,
        "place_name": place_name.strip(),
        "recorded_at": recorded_at,
    }


def parse_telegram_location_payload(location_data) -> tuple[float, float] | None:
    """解析 Telegram / webhook 传入的 location JSON，返回 (lat, lon)。"""
    if not location_data:
        return None
    try:
        if isinstance(location_data, str):
            ld = json.loads(location_data)
        else:
            ld = location_data
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(ld, dict):
        return None
    lat = ld.get("latitude")
    lon = ld.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def save_user_location_from_coords(
    latitude: float,
    longitude: float,
    timestamp: int | None = None,
) -> dict[str, Any] | None:
    """逆地理后写入 knowledge_base.json 的 user_location。"""
    ts = int(timestamp or _now_local().timestamp())
    place_name = reverse_geocode_label(latitude, longitude) or f"{latitude:.4f},{longitude:.4f}"
    _maybe_override_timezone_from_latlon(latitude, longitude)

    record = {
        "latitude": latitude,
        "longitude": longitude,
        "place_name": place_name,
        "recorded_at": ts,
    }

    kb = load_kb_core()
    kb[KB_TOP_LEVEL_KEY] = record
    if not _write_json_atomic(tenshi_config.KB_FILE_PATH, kb):
        logger.error("写入 knowledge_base.json user_location 失败。")
        return None
    logger.info("已更新 KB %s：%s", KB_TOP_LEVEL_KEY, place_name)
    return record


def _recorded_local_date(recorded_at: int) -> date:
    return datetime.fromtimestamp(recorded_at, _local_tz()).date()


def is_weather_location_active(loc: dict[str, Any], now: datetime | None = None) -> bool:
    """定位日起连续 LOCATION_WEATHER_VALID_DAYS 个自然日有效；第 8 日 0 点起失效。"""
    now = now or _now_local()
    recorded_at = int(loc["recorded_at"])
    return (now.date() - _recorded_local_date(recorded_at)).days < WEATHER_VALID_DAYS


def is_dialogue_location_active(loc: dict[str, Any], now: datetime | None = None) -> bool:
    """
    定位当日（自然日）在对话/感知层展示地名；
    次日凌晨 DAILY_SUMMARY_CUTOFF_HOUR（默认 4:00）起不再展示。
    """
    now = now or _now_local()
    recorded_at = int(loc["recorded_at"])
    recorded_date = _recorded_local_date(recorded_at)
    expire = datetime.combine(
        recorded_date + timedelta(days=1),
        time(hour=DIALOGUE_CUTOFF_HOUR, minute=0, second=0),
        tzinfo=_local_tz(),
    )
    return now < expire


def resolve_for_weather() -> tuple[float, float, str]:
    """天气观测点：有效期内用 KB 坐标，否则默认箕面。"""
    loc = load_user_location()
    if loc and is_weather_location_active(loc):
        label = loc.get("place_name") or DEFAULT_PLACE_LABEL
        return float(loc["latitude"]), float(loc["longitude"]), label
    return float(DEFAULT_LAT), float(DEFAULT_LON), DEFAULT_PLACE_LABEL


def resolve_for_dialogue() -> str:
    """对话/感知层行踪文案：有效期内返回地名，否则（未提供）。"""
    loc = load_user_location()
    if loc and is_dialogue_location_active(loc):
        return loc.get("place_name") or "（未提供）"
    return "（未提供）"


def strip_user_location_in_place(kb_data: dict[str, Any] | None) -> None:
    """从内存中的 KB 对象移除顶层 user_location（LLM 产出清洗）。"""
    if isinstance(kb_data, dict):
        kb_data.pop(KB_TOP_LEVEL_KEY, None)


def preserve_user_location_in_kb(kb_data: dict[str, Any]) -> dict[str, Any]:
    """log_update 写 KB 前：去掉 LLM 可能写入的 user_location，再恢复磁盘上的真实定位。"""
    strip_user_location_in_place(kb_data)
    prev = load_kb_core().get(KB_TOP_LEVEL_KEY)
    if isinstance(prev, dict) and prev.get("recorded_at") is not None:
        kb_data[KB_TOP_LEVEL_KEY] = copy.deepcopy(prev)
    else:
        kb_data.pop(KB_TOP_LEVEL_KEY, None)
    return kb_data
