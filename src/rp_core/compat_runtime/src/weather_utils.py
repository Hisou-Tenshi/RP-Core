# SPDX-License-Identifier: AGPL-3.0-only
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import tenshi_config
from error_text import format_exception_inline



OWM_API_KEY = tenshi_config.OWM_API_KEY
AMAP_API_KEY = getattr(tenshi_config, "AMAP_API_KEY", None) or ""
TZ_OFFSET = tenshi_config.TIMEZONE_OFFSET
DEFAULT_LAT = getattr(tenshi_config, "WEATHER_DEFAULT_LAT", 0.0)
DEFAULT_LON = getattr(tenshi_config, "WEATHER_DEFAULT_LON", 0.0)
DEFAULT_PLACE_LABEL = getattr(tenshi_config, "WEATHER_DEFAULT_PLACE_LABEL", "未设置默认位置")

logger = logging.getLogger("TenshiWeather")

_LAT_LON_LINE_RE = re.compile(
    r"Lat:\s*([+-]?\d+\.?\d*)\s*,\s*Lon:\s*([+-]?\d+\.?\d*)",
    re.IGNORECASE,
)
_PAIR_RE = re.compile(
    r"([+-]?\d+\.?\d*)\s*[,，]\s*([+-]?\d+\.?\d*)",
)


def parse_lat_lon_from_text(text: str | None) -> tuple[float | None, float | None]:
    """
    从 Telegram 日志里常见的 location 字符串解析经纬度。
    支持：「Lat: x, Lon: y」、逗号分隔的两数字、以及含 latitude/longitude 的片段。
    """
    if not isinstance(text, str) or not text.strip():
        return None, None
    s = text.strip()
    if "（未提供）" in s or s == "（未提供）":
        return None, None

    m = _LAT_LON_LINE_RE.search(s)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass

    for m in _PAIR_RE.finditer(s):
        try:
            a, b = float(m.group(1)), float(m.group(2))
        except ValueError:
            continue
        if 3 <= abs(a) <= 54 and 70 <= abs(b) <= 180:
            return a, b
        if 3 <= abs(b) <= 54 and 70 <= abs(a) <= 180:
            return b, a

    return None, None


def _is_mainland_china(lat: float, lon: float) -> bool:
    """粗略判断是否为「中国大陆」适用高德逆地理的范围（排除台港澳）。"""
    if lat < 17.5 or lat > 54.5 or lon < 72.5 or lon > 135.5:
        return False
    if 21.7 <= lat <= 25.6 and 119.2 <= lon <= 122.1:
        return False
    if 22.0 <= lat <= 22.65 and 113.7 <= lon <= 114.55:
        return False
    if 22.0 <= lat <= 22.35 and 113.45 <= lon <= 113.68:
        return False
    return True


def _maybe_override_timezone_from_latlon(lat: float, lon: float) -> None:
    """
    项目内所有时间统一为北京时间（UTC+8），不再根据定位漂移。

    历史上这里会「中国大陆 → +8，其他地区保持原配置（日本 +9）」；现在无论
    Telegram 定位落在哪个国家，都固定写回 8，保证运行时/落库时间口径唯一。
    函数名与签名保留，避免改动调用方。
    """
    try:
        new_offset = 8
        if not isinstance(new_offset, int):
            new_offset = int(new_offset)
    except Exception:
        return

    try:
        if hasattr(tenshi_config, "TIMEZONE_OFFSET") and tenshi_config.TIMEZONE_OFFSET != new_offset:
            tenshi_config.TIMEZONE_OFFSET = new_offset

        if hasattr(tenshi_config, "_default_tz_label"):
            label = os.getenv("TIMEZONE_LABEL") or tenshi_config._default_tz_label(new_offset)
            setattr(tenshi_config, "TIMEZONE_LABEL", label)
    except Exception:

        return


def reverse_geocode_amap(lon: float, lat: float) -> str | None:
    """高德逆地理；location 参数为「经度,纬度」。"""
    key = (AMAP_API_KEY or "").strip()
    if not key:
        return None
    try:
        url = "https://restapi.amap.com/v3/geocode/regeo"
        resp = requests.get(
            url,
            params={
                "key": key,
                "location": f"{lon:.6f},{lat:.6f}",
                "extensions": "base",
            },
            timeout=6,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if str(data.get("status")) != "1":
            return None
        regeo = data.get("regeocode") or {}
        addr = (regeo.get("formatted_address") or "").strip()
        return addr or None
    except Exception as e:
        logger.debug(f"Amap regeo failed: {format_exception_inline(e)}")
        return None


def reverse_geocode_geopy(lat: float, lon: float) -> str | None:
    try:
        from geopy.geocoders import Nominatim

        geolocator = Nominatim(user_agent="tenshi_perception_weather/1.0")
        loc = geolocator.reverse(f"{lat}, {lon}", language="zh", timeout=8)
        if not loc:
            return None
        return (loc.address or "").strip() or None
    except Exception as e:
        logger.debug(f"Geopy reverse failed: {format_exception_inline(e)}")
        return None


def reverse_geocode_label(lat: float, lon: float) -> str | None:
    """中国大陆优先高德，否则 Nominatim。"""
    if _is_mainland_china(lat, lon):
        label = reverse_geocode_amap(lon, lat)
        if label:
            return label
    return reverse_geocode_geopy(lat, lon)


def resolve_location_for_perception(user_id: str | int | None = None):
    """
    从 knowledge_base.json 的 user_location 解析观测点与行踪文案。
    返回 (lat, lon, place_label, location_line)。
    user_id 保留以兼容旧调用方，已不再使用。
    """
    from user_location import resolve_for_dialogue, resolve_for_weather

    lat, lon, place_label = resolve_for_weather()
    location_line = resolve_for_dialogue()
    return lat, lon, place_label, location_line


def _local_tz():
    return timezone(timedelta(hours=TZ_OFFSET))


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, _local_tz()).strftime("%H:%M")


def _precipitation_hints(minutely: list | None, hourly: list | None, current: dict) -> str:
    """基于 minutely 与 hourly 生成降水开始/停止推测（若不可得则省略）。"""
    lines: list[str] = []
    thr = 0.08
    now = int(datetime.now(_local_tz()).timestamp())

    cur_w = (current.get("weather") or [{}])[0].get("main") or ""
    cur_rain = float((current.get("rain") or {}).get("1h") or 0)
    cur_snow = float((current.get("snow") or {}).get("1h") or 0)
    now_raining = cur_w in ("Rain", "Drizzle", "Thunderstorm") or cur_rain > 0.05 or cur_snow > 0.05

    if minutely and isinstance(minutely, list) and len(minutely) > 0:
        rates = [float(m.get("precipitation") or 0) for m in minutely]
        times = [int(m.get("dt") or 0) for m in minutely]
        if rates and times:
            if now_raining:
                stop_idx = None
                for i, r in enumerate(rates):
                    if r <= thr:
                        stop_idx = i
                        break
                if stop_idx is not None and times[stop_idx] >= now:
                    lines.append(
                        f"预计降水减弱或停止约 {_fmt_ts(times[stop_idx])} 前后（分钟级预报，约未来 1 小时内）。"
                    )
            else:
                for i, r in enumerate(rates):
                    if r > thr and times[i] >= now:
                        lines.append(
                            f"预计降水开始约 {_fmt_ts(times[i])} 前后（分钟级预报，约未来 1 小时内）。"
                        )
                        break

    if hourly and isinstance(hourly, list) and len(hourly) >= 1:
        if not lines:
            for h in hourly[:18]:
                ts = int(h.get("dt") or 0)
                if ts < now:
                    continue
                pop = float(h.get("pop") or 0)
                wmain = (h.get("weather") or [{}])[0].get("main") or ""
                rain_h = float((h.get("rain") or {}).get("1h") or 0)
                if pop >= 0.4 or wmain in ("Rain", "Drizzle", "Thunderstorm") or rain_h > 0.05:
                    lines.append(
                        f"小时级预报：约 {_fmt_ts(ts)} 起可能有降水（概率约 {pop:.0%}）。"
                    )
                    break
        need_stop = now_raining and not any(
            "停止" in x or "减弱" in x for x in lines
        )
        if need_stop and hourly:
            started = False
            for h in hourly[:24]:
                ts = int(h.get("dt") or 0)
                if ts < now:
                    continue
                pop = float(h.get("pop") or 0)
                wmain = (h.get("weather") or [{}])[0].get("main") or ""
                rain_h = float((h.get("rain") or {}).get("1h") or 0)
                if wmain in ("Rain", "Drizzle", "Thunderstorm") or rain_h > 0.05 or pop >= 0.35:
                    started = True
                    continue
                if started and pop < 0.25 and wmain not in ("Rain", "Drizzle", "Thunderstorm") and rain_h < 0.05:
                    lines.append(f"预计降水停止或明显减弱约 {_fmt_ts(ts)} 前后（小时级预报）。")
                    break

    if not lines:
        return ""
    return "\n" + "\n".join(lines[:4])


def _onecall_url(lat: float, lon: float) -> str:

    return (
        f"https://api.openweathermap.org/data/2.5/onecall?lat={lat}&lon={lon}"
        f"&appid={OWM_API_KEY}&units=metric&lang=zh_cn"
    )


def _onecall_url_v3(lat: float, lon: float) -> str:
    return (
        f"https://api.openweathermap.org/data/3.0/onecall?lat={lat}&lon={lon}"
        f"&appid={OWM_API_KEY}&units=metric&lang=zh_cn"
    )


def get_weather_report(lat: float | None = None, lon: float | None = None, place_label: str | None = None):
    """
    从 OpenWeatherMap 获取天气：区分「当前」与「今日」日汇总，并附降水开始/停止推测（若接口提供）。
    """
    use_lat = float(lat) if lat is not None else float(DEFAULT_LAT)
    use_lon = float(lon) if lon is not None else float(DEFAULT_LON)
    label = (place_label or "").strip() or DEFAULT_PLACE_LABEL

    try:
        if not OWM_API_KEY or not str(OWM_API_KEY).strip():
            return "（天气数据获取失败：未配置 OWM_API_KEY）"

        url = _onecall_url(use_lat, use_lon)
        try:
            resp = requests.get(url, timeout=8)
        except Exception as e:
            logger.error(f"天气请求失败：{format_exception_inline(e)}")
            return "（天气数据获取失败：网络错误）"

        data = None
        if resp.status_code == 200:
            data = resp.json()
        else:
            logger.warning(f"OneCall 2.5 失败（{resp.status_code}），尝试 3.0...")
            resp_3 = requests.get(_onecall_url_v3(use_lat, use_lon), timeout=8)
            if resp_3.status_code == 200:
                data = resp_3.json()
            else:
                logger.error(f"OneCall 3.0 失败（{resp_3.status_code}）。尝试旧版接口...")
                return _get_legacy_weather(use_lat, use_lon, label)

        if not data:
            return "（天气数据获取失败：API响应异常）"

        current = data.get("current", {})
        daily = data.get("daily", [])
        alerts = data.get("alerts", [])
        minutely = data.get("minutely")
        hourly = data.get("hourly")

        temp = current.get("temp", "N/A")
        feels = current.get("feels_like", "N/A")
        humidity = current.get("humidity", "N/A")
        wind = current.get("wind_speed", "N/A")
        uvi = current.get("uvi", "N/A")
        w0 = (current.get("weather") or [{}])[0]
        cur_desc = w0.get("description", "未知")
        cur_main = w0.get("main", "")

        report = f"观测点：{label}（经纬度 {use_lat:.4f}, {use_lon:.4f}）\n"
        report += (
            f"【当前天气】（实时）{cur_desc}"
            f"，气温 {temp}°C（体感 {feels}°C），湿度 {humidity}%，"
            f"风速 {wind} m/s，紫外线指数 {uvi}。"
        )
        if cur_main in ("Rain", "Drizzle", "Thunderstorm"):
            report += "（当前天气现象含降水）"

        day0 = daily[0] if daily else {}
        if day0:
            d0t = day0.get("temp", {})
            dmin = d0t.get("min", "N/A")
            dmax = d0t.get("max", "N/A")
            d_desc = (day0.get("weather") or [{}])[0].get("description", "")
            pop = float(day0.get("pop") or 0)
            dt0 = datetime.fromtimestamp(day0.get("dt", 0), _local_tz()).strftime("%m-%d")
            report += (
                f"\n【今日天气】（日汇总）{dt0}：{d_desc}，"
                f"气温 {dmin}~{dmax}°C，当日降水概率约 {pop:.0%}。"
            )
        else:
            report += "\n【今日天气】（日汇总）暂无日级数据。"

        precip_extra = _precipitation_hints(minutely, hourly, current)
        if precip_extra:
            report += f"\n【降水推测】{precip_extra.strip()}"

        if daily and len(daily) > 1:
            report += "\n【近期日预报】"
            for day in daily[1:3]:
                dt = datetime.fromtimestamp(day["dt"], _local_tz())
                date_str = dt.strftime("%m-%d")
                d_temp = day.get("temp", {})
                d_min = d_temp.get("min", "N/A")
                d_max = d_temp.get("max", "N/A")
                d_desc = (day.get("weather") or [{}])[0].get("description", "")
                report += f" [{date_str}] {d_desc} ({d_min}~{d_max}°C)；"

        if alerts:
            report += "\n⚠️ 预警信息："
            for alert in alerts:
                event = alert.get("event", "未知预警")
                description = (alert.get("description") or "").replace("\n", " ")
                report += f"【{event}】{description[:50]}...;"

        return report

    except Exception as e:
        logger.error(f"天气获取异常：{format_exception_inline(e)}")
        return f"（天气数据获取异常：{format_exception_inline(e)}）"


def _get_legacy_weather(lat: float, lon: float, place_label: str):
    """
    回退：/weather + /forecast；无 minutely，仅区分当前与预报片段。
    """
    try:
        url_cur = (
            f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}"
            f"&appid={OWM_API_KEY}&units=metric&lang=zh_cn"
        )
        resp_cur = requests.get(url_cur, timeout=8)
        if resp_cur.status_code != 200:
            return "（天气数据完全获取失败）"

        cur = resp_cur.json()
        main = cur.get("main", {})
        weather = cur.get("weather", [{}])[0]
        cur_desc = weather.get("description", "未知")

        report = (
            f"观测点：{place_label}\n"
            f"【当前天气】（实时）{cur_desc}，气温 {main.get('temp')}°C，"
            f"湿度 {main.get('humidity')}%。"
        )

        url_fore = (
            f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}"
            f"&appid={OWM_API_KEY}&units=metric&lang=zh_cn"
        )
        resp_fore = requests.get(url_fore, timeout=8)
        if resp_fore.status_code == 200:
            fore = resp_fore.json()
            report += "\n【今日/后续】（3 小时粒度）"
            for i, item in enumerate(fore.get("list", [])[:8]):
                dt = datetime.fromtimestamp(item["dt"], _local_tz())
                d_desc = (item.get("weather") or [{}])[0].get("description", "")
                d_temp = item.get("main", {}).get("temp", "N/A")
                report += f" [{dt.strftime('%m-%d %H:%M')}] {d_desc} ({d_temp}°C)；"

        return report

    except Exception as e:
        logger.error(f"旧版接口天气获取失败：{format_exception_inline(e)}")
        return "（天气数据获取失败）"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(get_weather_report())
