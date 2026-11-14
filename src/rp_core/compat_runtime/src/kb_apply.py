# SPDX-License-Identifier: AGPL-3.0-only










from __future__ import annotations

import calendar
import copy
import datetime as dt
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import tenshi_config

SHORT_TERM_STATE_KEY = "short_term_state"
LEGACY_PHYSICAL_STATE_KEY = "physical_state"

logger = logging.getLogger(__name__)


_DURATION_RE = re.compile(
    r"^\s*(?:(?:短期|中期|长期)\s*)?(\d+)\s*(日|月|年)\s*$",
    re.UNICODE,
)
_YYMMDD_RE = re.compile(r"^\d{6}$")
_DEFAULT_DURATION = "7日"


def _tz() -> dt.timezone:
    return dt.timezone(dt.timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))


def today_local() -> dt.date:
    return dt.datetime.now(_tz()).date()


def parse_yymmdd(s: str) -> Optional[dt.date]:
    if not isinstance(s, str) or not _YYMMDD_RE.match(s.strip()):
        return None
    s = s.strip()
    yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return dt.date(year, mm, dd)
    except ValueError:
        return None


def format_yymmdd(d: dt.date) -> str:
    y = d.year % 100
    return f"{y:02d}{d.month:02d}{d.day:02d}"


def max_retention_days_safe() -> int:
    try:
        return int(
            os.getenv("SHORT_TERM_STATE_MAX_RETENTION_DAYS")
            or os.getenv("PHYSICAL_STATE_MAX_RETENTION_DAYS")
            or "7"
        )
    except Exception:
        return 7


def _load_state_list(user_profile: Dict[str, Any]) -> Optional[List[Any]]:
    if not isinstance(user_profile, dict):
        return None
    st = user_profile.get(SHORT_TERM_STATE_KEY)
    if isinstance(st, list):
        if LEGACY_PHYSICAL_STATE_KEY in user_profile:
            user_profile.pop(LEGACY_PHYSICAL_STATE_KEY, None)
        return st
    legacy = user_profile.get(LEGACY_PHYSICAL_STATE_KEY)
    if isinstance(legacy, list):
        user_profile[SHORT_TERM_STATE_KEY] = legacy
        user_profile.pop(LEGACY_PHYSICAL_STATE_KEY, None)
        return legacy
    return None


def _add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return dt.date(y, m, min(d.day, last))


def _add_years(d: dt.date, years: int) -> dt.date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def canonicalize_duration(duration_str: str) -> Optional[str]:
    """Normalize to 'N日' / 'N月' / 'N年' (no tier prefix)."""
    if not isinstance(duration_str, str):
        return None
    m = _DURATION_RE.match(duration_str.strip())
    if not m:
        return None
    try:
        n = max(1, int(m.group(1)))
    except ValueError:
        return None
    unit = m.group(2)
    return f"{n}{unit}"


def duration_to_end_date(start: dt.date, duration_str: str) -> Optional[dt.date]:
    canon = canonicalize_duration(duration_str)
    if not canon:
        return None
    m = re.match(r"^(\d+)(日|月|年)$", canon)
    if not m:
        return None
    n = max(1, int(m.group(1)))
    unit = m.group(2)
    if unit == "日":
        return start + dt.timedelta(days=n)
    if unit == "月":
        return _add_months(start, n)
    if unit == "年":
        return _add_years(start, n)
    return None


def effective_end_date(start: dt.date, duration_str: Optional[str]) -> dt.date:
    cap = max_retention_days_safe()
    cap_end = start + dt.timedelta(days=cap)
    if not duration_str or not isinstance(duration_str, str):
        return cap_end
    parsed = duration_to_end_date(start, duration_str)
    if parsed is None:
        return cap_end
    return min(parsed, cap_end)


def normalize_physical_entry(
    item: Any,
    *,
    workflow_date: Optional[dt.date] = None,
) -> Optional[Dict[str, Any]]:
    wf = workflow_date or today_local()
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        return {
            "since_yymmdd": format_yymmdd(wf),
            "duration": _DEFAULT_DURATION,
            "text": text,
        }
    if not isinstance(item, dict):
        return None
    text = (item.get("text") or item.get("description") or "").strip()
    if not text:
        return None
    since = item.get("since_yymmdd") or item.get("since")
    sd = parse_yymmdd(str(since)) if since else None
    if sd is None:
        sd = wf
    since_s = format_yymmdd(sd)
    raw_dur = item.get("duration")
    if not isinstance(raw_dur, str) or not raw_dur.strip():
        dur = _DEFAULT_DURATION
    else:
        dur = canonicalize_duration(raw_dur.strip()) or _DEFAULT_DURATION
    if duration_to_end_date(sd, dur) is None:
        dur = _DEFAULT_DURATION
    return {"since_yymmdd": since_s, "duration": dur, "text": text}


def prune_expired_physical_state(
    user_profile: Dict[str, Any],
    *,
    today: Optional[dt.date] = None,
) -> int:
    """Remove expired short_term_state entries. Returns removed count."""
    if not isinstance(user_profile, dict):
        return 0
    lst = _load_state_list(user_profile)
    if not isinstance(lst, list):
        return 0
    day = today or today_local()
    kept: List[Any] = []
    removed = 0
    for item in lst:
        norm = normalize_physical_entry(item, workflow_date=day)
        if norm is None:
            removed += 1
            continue
        sd = parse_yymmdd(norm["since_yymmdd"]) or day
        end = effective_end_date(sd, norm.get("duration"))
        if day > end:
            removed += 1
            continue
        kept.append(norm)
    user_profile[SHORT_TERM_STATE_KEY] = kept
    user_profile.pop(LEGACY_PHYSICAL_STATE_KEY, None)
    return removed


def apply_physical_state_delta(base_user_profile: dict, delta: Optional[dict]) -> None:
    if not isinstance(delta, dict) or not delta:
        return
    lst = _load_state_list(base_user_profile)
    if not isinstance(lst, list):
        lst = []
        base_user_profile[SHORT_TERM_STATE_KEY] = lst
    base_user_profile.pop(LEGACY_PHYSICAL_STATE_KEY, None)

    wf = today_local()
    for item in delta.get("add") or []:
        norm = normalize_physical_entry(item, workflow_date=wf)
        if norm:
            lst.append(norm)

    for item in delta.get("modify") or []:
        if not isinstance(item, dict):
            continue
        match = item.get("match") or item.get("from") or item.get("old")
        to_val = item.get("to") if "to" in item else item.get("value")
        if to_val is None:
            continue
        match_s = str(match).strip() if match is not None else ""
        new_norm = normalize_physical_entry(to_val, workflow_date=wf)
        if not new_norm:
            continue
        found = False
        for i, cur in enumerate(lst):
            cur_norm = normalize_physical_entry(cur, workflow_date=wf)
            if not cur_norm:
                continue
            blob = cur_norm.get("text", "") + str(cur_norm)
            if match_s and (match_s == blob or match_s in blob or blob in match_s):
                lst[i] = new_norm
                found = True
                break
        if not found:
            lst.append(new_norm)


def postprocess_user_profile_physical(up: Dict[str, Any]) -> None:
    if not isinstance(up, dict):
        return
    lst = _load_state_list(up)
    if not isinstance(lst, list):
        return
    wf = today_local()
    new_lst = []
    for item in lst:
        n = normalize_physical_entry(item, workflow_date=wf)
        if n:
            new_lst.append(n)
    up[SHORT_TERM_STATE_KEY] = new_lst
    up.pop(LEGACY_PHYSICAL_STATE_KEY, None)
    prune_expired_physical_state(up, today=wf)


_LIST_PATHS = {
    "identity_facts",
    SHORT_TERM_STATE_KEY,
    "preference_and_taboos.likes",
    "preference_and_taboos.dislikes",
    "preference_and_taboos.habits",
}


def _get_list_by_path(root: dict, path: str) -> Optional[list]:
    parts = path.split(".")
    cur: Any = root
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur if isinstance(cur, list) else None


def _ensure_list_path(root: dict, path: str) -> list:
    parts = path.split(".")
    cur = root
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    last = parts[-1]
    if last not in cur or not isinstance(cur[last], list):
        cur[last] = []
    return cur[last]


def _norm_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _list_modify(lst: list, match: str, new_value: Any) -> bool:
    match_s = _norm_str(match)
    if not match_s:
        return False
    for i, item in enumerate(lst):
        item_s = _norm_str(item)
        if item_s == match_s or (match_s in item_s) or (item_s in match_s):
            lst[i] = new_value
            return True
    return False


def apply_list_delta(
    root: dict,
    path: str,
    delta: Optional[dict],
    *,
    allow_delete: bool = False,
) -> None:
    if not isinstance(delta, dict) or not delta:
        return
    lst = _ensure_list_path(root, path)

    for item in delta.get("add") or []:
        val = _norm_str(item)
        if not val:
            continue
        if val not in [_norm_str(x) for x in lst]:
            lst.append(item if not isinstance(item, str) else val)

    for item in delta.get("modify") or []:
        if not isinstance(item, dict):
            continue
        match = item.get("match") or item.get("from") or item.get("old")
        to_val = item.get("to") if "to" in item else item.get("value")
        if to_val is None:
            continue
        matched = _list_modify(lst, _norm_str(match), to_val)
        if not matched:

            add_s = _norm_str(to_val)
            if add_s and add_s not in [_norm_str(x) for x in lst]:
                lst.append(to_val)

    if allow_delete:
        for item in delta.get("delete") or []:
            match_s = _norm_str(item)
            if not match_s:
                continue
            lst[:] = [x for x in lst if _norm_str(x) != match_s and match_s not in _norm_str(x)]


def apply_user_profile_delta(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(patch, dict) or not patch:
        return out
    up = out.setdefault("user_profile", {})
    if not isinstance(up, dict):
        up = {}
        out["user_profile"] = up

    apply_list_delta(up, "identity_facts", patch.get("identity_facts"))
    apply_physical_state_delta(up, patch.get(SHORT_TERM_STATE_KEY) or patch.get(LEGACY_PHYSICAL_STATE_KEY))
    pnt = patch.get("preference_and_taboos")
    if isinstance(pnt, dict):
        apply_list_delta(up, "preference_and_taboos.likes", pnt.get("likes"))
        apply_list_delta(up, "preference_and_taboos.dislikes", pnt.get("dislikes"))
        apply_list_delta(up, "preference_and_taboos.habits", pnt.get("habits"))
    postprocess_user_profile_physical(up)
    return out


def apply_relationship_dynamics_delta(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(patch, dict) or not patch:
        return out
    rd = out.setdefault("relationship_dynamics", {})
    if not isinstance(rd, dict):
        rd = {}
        out["relationship_dynamics"] = rd

    nick_delta = patch.get("special_nicknames")
    if isinstance(nick_delta, dict):
        sn = rd.setdefault("special_nicknames", {})
        if not isinstance(sn, dict):
            sn = {}
            rd["special_nicknames"] = sn
        for k, v in (nick_delta.get("add") or {}).items():
            if k and v is not None:
                sn[k] = v
        for item in nick_delta.get("modify") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or item.get("path")
            if not key:
                continue
            to_val = item.get("to") if "to" in item else item.get("value")
            if to_val is not None:
                sn[key] = to_val
    rd.pop("interaction_style", None)
    return out


def extract_delta_sections(summary_obj: dict) -> Tuple[dict, dict, dict, dict]:
    """
    Returns (user_profile_delta, relationship_delta, deprecated_op, narrative_memory).
    Supports new *_delta keys and legacy full-object keys (converted to add-only where safe).

    第 3 项原为 operational_log patch，已废弃：summary 中即便带有 operational_log
    也不再读取，恒返回 {}（保留 4 元组仅为兼容调用方签名）。
    """
    if not isinstance(summary_obj, dict):
        return {}, {}, {}, {}

    up_delta = summary_obj.get("user_profile_delta")
    if not isinstance(up_delta, dict):
        up_delta = _legacy_object_to_add_delta(summary_obj.get("user_profile"))

    rd_delta = summary_obj.get("relationship_dynamics_delta")
    if not isinstance(rd_delta, dict):
        rd_delta = _legacy_relationship_to_delta(summary_obj.get("relationship_dynamics"))

    nm = summary_obj.get("narrative_memory")
    if not isinstance(nm, dict):
        nm = {}
    return up_delta or {}, rd_delta or {}, {}, nm


def _legacy_object_to_add_delta(obj: Any) -> dict:
    if not isinstance(obj, dict):
        return {}
    delta: dict = {}
    for key in ("identity_facts", SHORT_TERM_STATE_KEY, LEGACY_PHYSICAL_STATE_KEY):
        vals = obj.get(key)
        if isinstance(vals, list) and vals:
            target = SHORT_TERM_STATE_KEY if key in (SHORT_TERM_STATE_KEY, LEGACY_PHYSICAL_STATE_KEY) else key
            slot = delta.setdefault(target, {"add": []})
            slot["add"].extend([x for x in vals if _norm_str(x)])
    pnt = obj.get("preference_and_taboos")
    if isinstance(pnt, dict):
        p_delta = {}
        for slot in ("likes", "dislikes", "habits"):
            vals = pnt.get(slot)
            if isinstance(vals, list) and vals:
                p_delta[slot] = {"add": [x for x in vals if _norm_str(x)]}
        if p_delta:
            delta["preference_and_taboos"] = p_delta
    return delta


def _legacy_relationship_to_delta(obj: Any) -> dict:
    if not isinstance(obj, dict):
        return {}
    delta: dict = {}
    sn = obj.get("special_nicknames")
    if isinstance(sn, dict) and sn:
        delta["special_nicknames"] = {"add": dict(sn)}
    return delta


def has_meaningful_summary_payload(summary_obj: dict) -> bool:
    up_d, rd_d, _op_unused, nm = extract_delta_sections(summary_obj)
    events = (nm.get("significant_events") or []) if isinstance(nm, dict) else []

    def _delta_nonempty(d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        for v in d.values():
            if isinstance(v, dict) and (v.get("add") or v.get("modify")):
                return True
            if isinstance(v, list) and v:
                return True
        return False

    if isinstance(events, list) and len(events) > 0:
        return True
    if _delta_nonempty(up_d):
        return True
    if _delta_nonempty(rd_d):
        return True
    return False
