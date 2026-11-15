# SPDX-License-Identifier: AGPL-3.0-only


































from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib

HEALTH_PATH = pathlib.Path(
    os.getenv("PROVIDER_HEALTH_PATH")
    or (pathlib.Path(__file__).resolve().parent.parent / "data" / "provider_health.json")
)
MAX_AGE_HOURS = float(os.getenv("PROVIDER_HEALTH_MAX_AGE_HOURS") or 26.0)
ENABLED = (os.getenv("PROVIDER_HEALTH_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")

logger = logging.getLogger("ProviderHealth")




_configured_logger: logging.Logger = logger


def set_logger(configured: logging.Logger | None) -> None:
    """把调用方（带文件 handler）的 logger 装进来，让本模块的告警真正落盘。"""
    global _configured_logger
    if configured is not None:
        _configured_logger = configured


def _warn(message: str) -> None:
    _configured_logger.warning(message)

_cached_payload: dict | None = None
_cached_mtime: float | None = None
_cached_stamp: dt.datetime | None = None
_warned: set[str] = set()


def reset_cache() -> None:
    """测试 / 每轮刷新用。"""
    global _cached_payload, _cached_mtime, _cached_stamp
    _cached_payload = None
    _cached_mtime = None
    _cached_stamp = None
    _warned.clear()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _warn(message)


def load(force: bool = False) -> dict | None:
    """读探活 JSON（按 mtime 缓存）。任何异常都返回 None（= 未知 → fail-open）。"""
    global _cached_payload, _cached_mtime, _cached_stamp
    if not ENABLED:
        return None
    try:
        mtime = HEALTH_PATH.stat().st_mtime
    except OSError:
        _warn_once("missing", f"[provider-health] 没有探活结果（{HEALTH_PATH}），本次全部通路照常调用。")
        _cached_payload = None
        return None
    if not force and _cached_payload is not None and _cached_mtime == mtime:
        return _cached_payload
    try:
        payload = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        expected = os.getenv("RP_HEALTH_CONFIG_SHA256")
        if expected and (
            payload.get("config_sha256") != expected
            or payload.get("credential_source") != "rp-core-actions:" + os.getenv("RP_HEALTH_INSTANCE_ID", "")
        ):
            _cached_payload = None
            return None  # A different configuration/environment must never ban this run's routes.
    except Exception as exc:
        _warn_once("broken", f"[provider-health] 探活结果无法解析（{type(exc).__name__}: {exc}），本次全部通路照常调用。")
        _cached_payload = None
        return None
    _cached_payload = payload
    _cached_mtime = mtime
    _cached_stamp = None
    return payload


def generated_at(payload: dict | None) -> dt.datetime | None:
    if not payload:
        return None
    raw = payload.get("generated_at_utc")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_stale(payload: dict | None) -> bool:
    stamp = generated_at(payload)
    if stamp is None:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 3600.0
    return age > MAX_AGE_HOURS


def snapshot() -> dict[str, dict[str, bool]] | None:
    """{provider: {path_name: ok}}；未知 / 过期返回 None。"""
    payload = load()
    if not payload or is_stale(payload):
        if payload and is_stale(payload):
            stamp = generated_at(payload)
            _warn_once(
                "stale",
                f"[provider-health] 探活结果已过期（{stamp}，超过 {MAX_AGE_HOURS:.0f}h），本次全部通路照常调用。",
            )
        return None
    out: dict[str, dict[str, bool]] = {}
    for provider, entry in (payload.get("providers") or {}).items():
        paths = (entry or {}).get("paths") or {}
        out[provider] = {name: bool(info.get("ok")) for name, info in paths.items()}
    return out


def path_state(provider: str, path_name: str) -> bool | None:
    """True=绿(可用) / False=红(ban) / None=未知(照常调用)。"""
    snap = snapshot()
    if snap is None:
        return None
    return (snap.get(provider) or {}).get(path_name)


def model_path_state(provider: str, path_name: str, model_id: str | None) -> bool | None:
    """「通路 × 模型」级状态（2026-10-29 新增）。

    数据来自探活 JSON 的 `paths.<通路>.models.<模型>`（逐模型非生成校验结论）：
      * `ok: false`（典型 verdict=`model_not_found`，如代理账号组没挂这个模型）
        → **False = 该模型在该通路上被 ban**，运行时必须跳过这个组合；
      * `ok: true` → True；
      * 模型没被探到 / 旧版 JSON 没有逐模型字段 / unknown → None（fail-open，照常调用）。

    注意与 `path_state` 的分层：通路级红（key 被拒 / 网络不通）仍由 `filter_paths`
    在最外层 ban 整条通路；模型级红只影响「这个模型 × 这条通路」这一个组合。
    """
    if not model_id:
        return None
    payload = load()
    if not payload or is_stale(payload):
        return None
    entry = ((payload.get("providers") or {}).get(provider) or {}).get("paths") or {}
    info = ((entry.get(path_name) or {}).get("models") or {}).get(str(model_id))
    if not isinstance(info, dict):
        return None
    ok = info.get("ok")
    if ok is False:
        return False
    if ok is True:
        return True
    return None


def provider_enabled(provider: str) -> bool:
    """类别级：该 provider 至少有一条绿通路 → True；全红 → False；未知 → True（fail-open）。"""
    snap = snapshot()
    if snap is None:
        return True
    paths = snap.get(provider)
    if not paths:
        return True
    return any(paths.values())


def filter_paths(
    provider: str,
    paths: list[dict],
    *,
    name_key: str = "name",
    model: str | None = None,
) -> list[dict]:
    """只保留探活为绿的通路；红的丢掉并记一条日志（调用方原有的「没有可用通路」检查会接住）。

    `model` 传入时（2026-10-29 起）额外做**模型级**过滤：探活判定
    「该模型在该通路上不可用」（models.<model>.ok=false，如代理账号组没挂这个模型）
    的通路同样丢掉 —— 这就是「模型不可用也一样 ban 掉那个模型」，不再报黄照跑。
    模型级结论缺失 / unknown 一律 fail-open（照常调用，靠运行时降级兜底）。
    """
    kept: list[dict] = []
    banned: list[str] = []
    model_banned: list[str] = []
    for path in paths or []:
        name = str(path.get(name_key) or path.get("base_url") or "?")
        state = path_state(provider, name)
        if state is False:
            banned.append(name)
            continue
        if model is not None and model_path_state(provider, name, model) is False:
            model_banned.append(name)
            continue
        kept.append(path)
    if banned:
        _warn(
            f"[provider-health] 按探活结果 ban 掉 {provider} 的 {'、'.join(banned)}（红通路不再调用）。"
        )
    if model_banned:
        _warn(
            f"[provider-health] {provider} 模型级 ban：{model} 在 {'、'.join(model_banned)} "
            "被探活判定不可用（模型×通路红），本次跳过这些组合。"
        )
    return kept


def key_or_blank(provider: str, path_name: str, api_key: str | None) -> str:
    """红的通路把 key 换成空串 —— 既有逻辑会自己抛「没有可用通路」并往下一档走。"""
    if path_state(provider, path_name) is False:
        _warn(f"[provider-health] {provider}/{path_name} 探活为红，本次传空 key（外层 ban）。")
        return ""
    return api_key or ""


def describe() -> str:
    """启动日志用的一句话摘要。"""
    if not ENABLED:
        return "provider-health: 已关闭（PROVIDER_HEALTH_ENABLED=0）"
    payload = load()
    if payload is None:
        return "provider-health: 无探活结果 → 全部通路照常调用"
    if is_stale(payload):
        return f"provider-health: 探活结果已过期（{payload.get('generated_at_utc')}）→ 全部通路照常调用"
    parts: list[str] = []
    for provider, entry in sorted((payload.get("providers") or {}).items()):
        paths = (entry or {}).get("paths") or {}
        good = [name for name, info in paths.items() if info.get("ok")]
        red = [name for name, info in paths.items() if not info.get("ok") and info.get("verdict") != "not_configured"]
        parts.append(f"{provider}:绿{good or '无'}" + (f" 红{red}" if red else ""))
    return f"provider-health（{payload.get('generated_at_bj') or payload.get('generated_at_jst') or payload.get('generated_at_utc')}）" + "；".join(parts)
