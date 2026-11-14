# SPDX-License-Identifier: AGPL-3.0-only







from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    from config import tunable
except Exception:
    tunable = None

_LOG = logging.getLogger(__name__)
_EVENTS = {
    "ledger_round",
    "intent_added",
    "intent_dropped_overflow",
    "fired",
    "expired_ttl",
    "overlap_check",
}


def _setting(name: str, default: Any) -> Any:
    try:
        return getattr(tunable, name)
    except Exception:
        return default


def _store_path() -> str:
    return str(_setting("DEFERRED_INTENT_STORE", os.path.join("data", "deferred_intents.json")))


def _stats_path() -> str:
    return str(_setting("DEFERRED_PILOT_STATS", os.path.join("data", "choice_layer_pilot_stats.json")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _locked(path: str) -> Iterator[Any]:
    """以旁路 lock 文件锁住一个 JSON 文件，避免并发读改写丢失。

    无 fcntl 的平台（Windows 开发环境）按无锁继续：锁只是并发正确性的
    尽力而为，不值得为之让整条挂账链路失败。
    """
    lock_path = path + ".lock"
    directory = os.path.dirname(os.path.abspath(lock_path))
    os.makedirs(directory, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        if fcntl is None:
            yield lock_file
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_file
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        _LOG.warning("选择层挂账文件损坏，已重置为空: %s (%s)", path, exc)
        try:

            _write_json(path, default)
        except Exception:
            _LOG.exception("重置损坏的选择层文件失败: %s", path)
        return default


def _write_json(path: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".choice-layer-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def add_intents(items: list[dict], turn_id: str) -> int:
    """写入本轮可挂账意图，超过 ``DEFERRED_INTENT_MAX_CARRY`` 的丢弃。"""
    try:
        if not isinstance(items, list):
            return 0
        limit = max(0, int(_setting("DEFERRED_INTENT_MAX_CARRY", 1)))
        path = _store_path()
        with _locked(path):
            data = _read_json(path, [])
            if not isinstance(data, list):
                _LOG.warning("选择层挂账文件顶层不是数组，已重置为空: %s", path)
                data = []
            accepted = 0
            for item in items:
                if accepted >= limit:
                    pilot_log("intent_dropped_overflow", {"turn_id": turn_id})
                    continue
                if not isinstance(item, dict):
                    continue
                data.append({
                    "id": uuid.uuid4().hex,
                    "what": str(item.get("what", "")),
                    "why": str(item.get("why", "")),
                    "turn_id": str(turn_id),
                    "status": "pending",
                })
                accepted += 1
            _write_json(path, data)
        if accepted:
            pilot_log("intent_added", {"turn_id": turn_id, "count": accepted})
        return accepted
    except Exception:
        _LOG.exception("写入挂账意图失败，继续放行")
        return 0


def expire_and_collect(turn_id: str) -> list[dict]:
    """感知轮领取 FIFO pending 候选，最多一条；不在此步骤改变状态。"""
    del turn_id
    try:
        path = _store_path()
        with _locked(path):
            data = _read_json(path, [])
            if not isinstance(data, list):
                return []
            return [item for item in data if isinstance(item, dict) and item.get("status") == "pending"][:1]
    except Exception:
        _LOG.exception("收集挂账意图失败，继续放行")
        return []


def mark_fired(intent_id: str) -> None:
    """把已补发的意图标记为 fired。"""
    try:
        path = _store_path()
        with _locked(path):
            data = _read_json(path, [])
            if not isinstance(data, list):
                return
            for item in data:
                if isinstance(item, dict) and item.get("id") == intent_id:
                    item["status"] = "fired"
                    item["fired_at"] = _now()
                    break
            _write_json(path, data)
        pilot_log("fired", {"intent_id": intent_id})
    except Exception:
        _LOG.exception("标记挂账意图 fired 失败，继续放行")


def mark_expired_all(turn_id: str) -> None:
    """感知轮结束时将全部 pending 作废，保证 TTL 不跨轮累积。"""
    expired = 0
    try:
        path = _store_path()
        with _locked(path):
            data = _read_json(path, [])
            if not isinstance(data, list):
                return
            for item in data:
                if isinstance(item, dict) and item.get("status") == "pending":
                    item["status"] = "expired"
                    item["expired_at"] = _now()
                    expired += 1
            _write_json(path, data)
        if expired:
            pilot_log("expired_ttl", {"turn_id": turn_id, "count": expired})
    except Exception:
        _LOG.exception("作废挂账意图失败，继续放行")


def pilot_log(event: str, payload: dict) -> None:
    """追加试点事件；未知事件静默忽略，避免污染统计文件。"""
    if event not in _EVENTS:
        return
    try:
        path = _stats_path()
        with _locked(path):
            data = _read_json(path, {"events": []})
            if not isinstance(data, dict):
                data = {"events": []}
            events = data.get("events")
            if not isinstance(events, list):
                events = []
                data["events"] = events
            events.append({"ts": _now(), "event": event, "payload": payload if isinstance(payload, dict) else {}})
            _write_json(path, data)
    except Exception:
        _LOG.exception("写入选择层试点日志失败，继续放行")
