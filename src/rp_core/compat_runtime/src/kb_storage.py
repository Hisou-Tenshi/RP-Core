# SPDX-License-Identifier: AGPL-3.0-only










from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import tenshi_config


from kb_apply import SHORT_TERM_STATE_KEY

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_EVENT_ID_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})-")


def _data_dir() -> str:
    return os.path.join(tenshi_config.PROJECT_ROOT, "data")


def events_dir() -> str:
    return os.path.join(_data_dir(), "knowledge_base_events")


def shared_experience_dir() -> str:
    return os.path.join(_data_dir(), "knowledge_base_shared_experience")


def _tz() -> timezone:
    return timezone(timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))


def events_month_path(month: str) -> str:
    return os.path.join(events_dir(), f"{_parse_month(month) or month}.json")


def shared_experience_month_path(month: str) -> str:
    return os.path.join(shared_experience_dir(), f"{_parse_month(month) or month}.json")


def _parse_month(month: str) -> Optional[str]:
    if not isinstance(month, str):
        return None
    m = month.strip()
    if _MONTH_RE.match(m):
        return m
    return None


def month_from_event_id(event_id: str) -> Optional[str]:
    if not isinstance(event_id, str):
        return None
    m = _EVENT_ID_MONTH_RE.match(event_id.strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def month_from_source_event_id(source_event_id: str, fallback_month: str) -> str:
    m = month_from_event_id(source_event_id or "")
    return m or fallback_month


def current_workflow_month() -> str:
    return datetime.now(_tz()).strftime("%Y-%m")


def _list_month_stems(directory: str, require_month: bool = True) -> List[str]:
    if not os.path.isdir(directory):
        return []
    out: List[str] = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        if require_month and not _parse_month(stem):
            continue
        out.append(stem)
    out.sort()
    return out


def list_month_files(_prefix_unused: str = "") -> List[str]:
    """兼容旧接口：返回 YYYY-MM 列表（事件目录）。"""
    return _list_month_stems(events_dir())


def _migrate_flat_monthly_files_to_subdirs() -> None:
    """将 data 根目录下旧版 knowledge_base_*-YYYY-MM.json 移入子目录。"""
    root = _data_dir()
    if not os.path.isdir(root):
        return
    os.makedirs(events_dir(), exist_ok=True)
    os.makedirs(shared_experience_dir(), exist_ok=True)
    for name in list(os.listdir(root)):
        src = os.path.join(root, name)
        if not os.path.isfile(src) or not name.endswith(".json"):
            continue
        if name.startswith("knowledge_base_events-") and name != "knowledge_base_events.json":
            month = name[len("knowledge_base_events-") : -len(".json")]
            if _parse_month(month):
                dst = events_month_path(month)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
        elif name.startswith("knowledge_base_shared_experience-") and name != "knowledge_base_shared_experience.json":
            month = name[len("knowledge_base_shared_experience-") : -len(".json")]
            if _parse_month(month):
                dst = shared_experience_month_path(month)
                if not os.path.exists(dst):
                    shutil.move(src, dst)


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: str, data: Any) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def load_events_for_month(month: str) -> List[Dict[str, Any]]:
    month = _parse_month(month) or month
    payload = _read_json(events_month_path(month), {})
    evs = payload.get("significant_events") if isinstance(payload, dict) else payload
    return [e for e in (evs or []) if isinstance(e, dict)]


def load_all_significant_events() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for month in _list_month_stems(events_dir()):
        events.extend(load_events_for_month(month))
    return events


def load_shared_experience_for_month(month: str) -> List[Dict[str, Any]]:
    month = _parse_month(month) or month
    payload = _read_json(shared_experience_month_path(month), {})
    items = payload.get("shared_experience_index") if isinstance(payload, dict) else payload
    return [x for x in (items or []) if isinstance(x, dict)]


def load_all_shared_experience_index() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for month in _list_month_stems(shared_experience_dir()):
        items.extend(load_shared_experience_for_month(month))
    return items


def filter_events_for_summary_day(
    events: List[Dict[str, Any]] | None,
    summary_day: str,
) -> List[Dict[str, Any]]:
    """Keep only events whose event_id starts with YYYY-MM-DD- for the summary day."""
    day = (summary_day or "").strip()
    if not day:
        return [e for e in (events or []) if isinstance(e, dict)]
    prefix = f"{day}-"
    return [
        e
        for e in (events or [])
        if isinstance(e, dict) and str(e.get("event_id") or "").startswith(prefix)
    ]


def merge_events_into_month_file(month: str, new_events: List[Dict[str, Any]]) -> None:
    """Append new events to the month file; existing event_id rows are left unchanged."""
    if not new_events:
        return
    month = _parse_month(month) or month
    existing = load_events_for_month(month)
    known_ids = {str(e.get("event_id")) for e in existing if e.get("event_id")}
    appended = 0
    for ne in new_events:
        if not isinstance(ne, dict):
            continue
        eid = ne.get("event_id")
        if not eid:
            continue
        eid_s = str(eid)
        if eid_s in known_ids:
            continue
        known_ids.add(eid_s)
        existing.append(ne)
        appended += 1
    if appended == 0:
        return
    _write_json_atomic(
        events_month_path(month),
        {"month": month, "significant_events": existing},
    )


def merge_shared_experience_into_month_file(month: str, new_items: List[Dict[str, Any]]) -> None:
    if not new_items:
        return
    month = _parse_month(month) or month
    existing = load_shared_experience_for_month(month)
    by_tag: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in existing + new_items:
        if not isinstance(item, dict):
            continue
        tag = (item.get("tag") or "").strip()
        if not tag:
            continue
        if tag not in by_tag:
            order.append(tag)
        by_tag[tag] = item
    merged = [by_tag[t] for t in order if t in by_tag]
    _write_json_atomic(
        shared_experience_month_path(month),
        {"month": month, "shared_experience_index": merged},
    )


def flush_narrative_from_memory_obj(
    memory_obj: Dict[str, Any],
    *,
    workflow_month: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Move narrative_memory.significant_events / shared_experience_index into monthly files;
    strip them from the returned core object (for knowledge_base.json).
    """
    wf_month = _parse_month(workflow_month or "") or current_workflow_month()
    nm = memory_obj.get("narrative_memory")
    if not isinstance(nm, dict):
        nm = {}
    events = [e for e in (nm.get("significant_events") or []) if isinstance(e, dict)]
    shared = [x for x in (nm.get("shared_experience_index") or []) if isinstance(x, dict)]

    events_by_month: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        eid = ev.get("event_id")
        month = month_from_event_id(str(eid or "")) or wf_month
        events_by_month.setdefault(month, []).append(ev)
    for month, evs in events_by_month.items():
        merge_events_into_month_file(month, evs)

    shared_by_month: Dict[str, List[Dict[str, Any]]] = {}
    for item in shared:
        src = item.get("source_event_id")
        month = month_from_source_event_id(str(src or ""), wf_month)
        shared_by_month.setdefault(month, []).append(item)
    for month, items in shared_by_month.items():
        merge_shared_experience_into_month_file(month, items)

    memory_obj["narrative_memory"] = {}
    return memory_obj


def load_kb_core() -> Dict[str, Any]:
    _migrate_flat_monthly_files_to_subdirs()
    path = tenshi_config.KB_FILE_PATH
    if not os.path.exists(path):
        return empty_kb_core_template()
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return empty_kb_core_template()
    migrate_monolithic_kb_if_needed(data)
    return data


def load_kb_composite() -> Dict[str, Any]:
    """In-memory view: core + all monthly narrative (for merge / DB reconcile)."""
    core = load_kb_core()
    composite = copy_core_dict(core)
    composite["narrative_memory"] = {
        "significant_events": load_all_significant_events(),
        "shared_experience_index": load_all_shared_experience_index(),
    }
    return composite


def copy_core_dict(core: Dict[str, Any]) -> Dict[str, Any]:
    import copy

    out = copy.deepcopy(core)
    nm = out.get("narrative_memory")
    if not isinstance(nm, dict):
        out["narrative_memory"] = {}
    return out


def empty_kb_core_template() -> Dict[str, Any]:
    return {
        "user_profile": {
            "identity_facts": [],
            SHORT_TERM_STATE_KEY: [],
            "preference_and_taboos": {"likes": [], "dislikes": [], "habits": []},
        },
        "relationship_dynamics": {
            "special_nicknames": {},
        },
        "narrative_memory": {},

        "user_location": None,
    }


def migrate_monolithic_kb_if_needed(kb_data: Dict[str, Any]) -> bool:
    """If legacy KB still holds events/shared index, flush to monthly files and rewrite core."""
    nm = kb_data.get("narrative_memory")
    if not isinstance(nm, dict):
        return False
    evs = nm.get("significant_events") or []
    shared = nm.get("shared_experience_index") or []
    if not evs and not shared:
        return False
    flush_narrative_from_memory_obj(kb_data)
    _write_json_atomic(tenshi_config.KB_FILE_PATH, kb_data)
    return True


def strip_narrative_from_kb_file() -> bool:
    """One-shot: migrate monolithic KB on disk if needed."""
    if not os.path.exists(tenshi_config.KB_FILE_PATH):
        return False
    data = _read_json(tenshi_config.KB_FILE_PATH, {})
    if not isinstance(data, dict):
        return False
    return migrate_monolithic_kb_if_needed(data)


def latest_events_hint_json(limit: int = 2) -> str:
    events = load_all_significant_events()
    if not events:
        return ""
    tail = events[-limit:]
    return json.dumps(tail, ensure_ascii=False)


def collect_all_event_ids() -> set:
    ids = set()
    for ev in load_all_significant_events():
        eid = (ev.get("event_id") or "").strip()
        if eid:
            ids.add(eid)
    return ids
