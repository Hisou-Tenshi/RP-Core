# SPDX-License-Identifier: AGPL-3.0-only




































from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
from typing import Any, Callable, Iterable

import bond_state as _bs

try:
    import tenshi_config as _cfg
except Exception:
    _cfg = None

logger = logging.getLogger("BondReflect")

KINDS = ("affirm", "milestone", "commit", "rupture", "repair")
SPEAKERS = ("天子", "用户")
WEIGHTS = {"milestone": 3.0, "commit": 2.0, "rupture": 2.0, "affirm": 1.0, "repair": 1.0}
SERIOUS_RUPTURE_WEIGHT = 4.0
TENSHI_ONLY_KINDS = ("affirm", "rupture")
ADD_KINDS = ("affirm", "milestone", "commit")


BANNED_TEXT_PATTERNS = (
    r"下次", r"记得", r"应该", r"以后(要|不再|别|得)", r"要记住", r"不要忘",
    r"嘴硬", r"傲娇", r"嘴上不认", r"死不承认", r"口误",
    r"第一次", r"首次",
)
_BANNED_RE = re.compile("|".join(BANNED_TEXT_PATTERNS))
TEXT_MIN, TEXT_MAX = 8, 120
QUOTE_MIN, QUOTE_MAX = 2, 40
MOOD_MAX_CHARS = 60
RECENT_MAX = 3
LEDGER_KEEP_CONSIDERED_DAYS = 180
LEDGER_KEEP_PENDING_DAYS = 60
LEDGER_KEEP_REFLECTIONS = 120
CHANGELOG_KEEP = 200

LLM = Callable[[str, str], str]






def _c(name: str, default):
    v = getattr(_cfg, name, None) if _cfg is not None else None
    return default if v is None else v


def ledger_path() -> str:
    p = _c("BOND_LEDGER_FILE_PATH", None)
    if p:
        return p
    return os.path.join(os.path.dirname(_bs.default_path()), "bond_ledger.json")


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _write_json_atomic(path: str, data: Any) -> bool:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"[bond] 写入失败 {path}：{e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def empty_ledger() -> dict:
    return {"version": 1, "evidence": [], "reflections": [], "retired": [], "last_reflect_day": ""}


def load_ledger(path: str | None = None) -> dict:
    data = _read_json(path or ledger_path(), {})
    base = empty_ledger()
    if not data:
        return base
    for k, v in base.items():
        if not isinstance(data.get(k), type(v)):
            data[k] = v
    return data


def save_ledger(ledger: dict, path: str | None = None) -> bool:
    return _write_json_atomic(path or ledger_path(), ledger)


def load_card(path: str | None = None) -> dict:
    return _bs.load_bond_state(path)


def save_card(card: dict, path: str | None = None) -> bool:
    if not isinstance(card, dict) or not card:
        logger.error("[bond] 拒绝保存空卡片。")
        return False
    return _write_json_atomic(path or _bs.default_path(), card)






_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"〔贴纸：[^〕]*〕")
_WS_RE = re.compile(r"[\s\u3000]+")
_MD_RE = re.compile(r"[*_`]+")
_TS_PREFIX_RE = re.compile(r"^\s*[（(][^）)\n]{1,40}[）)]\s*")


def normalize_quote(s: str | None) -> str:
    """比对用：去 <br>、贴纸标记、markdown 记号、所有空白；全角半角引号统一。"""
    t = _BR_RE.sub("", s or "")
    t = _TAG_RE.sub("", t)
    t = _MD_RE.sub("", t)
    t = t.replace("“", "\"").replace("”", "\"").replace("‘", "'").replace("’", "'")
    t = t.replace("「", "\"").replace("」", "\"").replace("『", "\"").replace("』", "\"")
    t = t.replace(",", "，").replace("?", "？").replace("!", "！")
    return _WS_RE.sub("", t)


def bad_text_reason(text: str) -> str:
    t = (text or "").strip()
    if len(t) < TEXT_MIN:
        return "too_short"
    if len(t) > TEXT_MAX:
        return "too_long"
    m = _BANNED_RE.search(t)
    if m:
        return f"banned:{m.group(0)}"
    return ""


def _day_of(ts: Any) -> str:
    tz = _dt.timezone(_dt.timedelta(hours=float(_c("TIMEZONE_OFFSET", 8) or 8)))
    return _dt.datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d")


def _clock_of(ts: Any) -> str:
    tz = _dt.timezone(_dt.timedelta(hours=float(_c("TIMEZONE_OFFSET", 8) or 8)))
    return _dt.datetime.fromtimestamp(float(ts), tz).strftime("%H:%M")


def _date(s: str) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _days_between(a: str, b: str) -> int | None:
    da, db = _date(a), _date(b)
    if da is None or db is None:
        return None
    return (db - da).days


def _parse_json_obj(text: str | None) -> dict:
    if not text:
        return {}
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}






def _clean_line(s: str) -> str:
    t = _BR_RE.sub(" / ", s or "")
    t = _TS_PREFIX_RE.sub("", t, count=1)
    return t.strip()


def dialogue_lines(messages: Iterable[dict]) -> list[dict]:
    """日志条目 → [{speaker, text, msg_id, time}]；不含她的思考（只有说出口的话能当证据）。"""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        ts = m.get("time")
        mid = str(m.get("msg_id") or "")
        q = (m.get("user_query") or "").strip()
        if q and not q.startswith("[系统"):
            out.append({"speaker": "用户", "text": _clean_line(q), "msg_id": mid, "time": ts})
        a = (m.get("bot_answer") or "").strip()
        if a:
            out.append({"speaker": "天子", "text": _clean_line(a), "msg_id": mid, "time": ts})
    return out


def dialogue_text(lines: list[dict], max_chars: int = 60000) -> str:
    rows = []
    for ln in lines:
        clock = _clock_of(ln["time"]) if ln.get("time") is not None else "--:--"
        rows.append(f"[{clock}] {ln['speaker']}：{ln['text']}")
    text = "\n".join(rows)
    if len(text) > max_chars:
        text = "……（前略）\n" + text[-max_chars:]
    return text


def locate_quote(quote: str, speaker: str, lines: list[dict]) -> str | None:
    """quote 在该说话人当天的话里逐字出现（规范化后）则返回 msg_id（可能为空串），否则 None。"""
    nq = normalize_quote(quote)
    if len(nq) < QUOTE_MIN:
        return None
    for ln in lines:
        if ln["speaker"] == speaker and nq in normalize_quote(ln["text"]):
            return ln.get("msg_id") or ""
    return None






def _card_for_prompt(card: dict) -> str:
    view = {
        "standing": card.get("standing", ""),
        "settled": [{"id": s.get("id"), "text": s.get("text")} for s in card.get("settled") or [] if isinstance(s, dict)],
    }
    return json.dumps(view, ensure_ascii=False, indent=1)


def _next_ev_id(ledger: dict, day: str) -> str:
    prefix = f"ev-{day}-"
    n = 0
    for e in ledger.get("evidence") or []:
        eid = str(e.get("id") or "")
        if eid.startswith(prefix):
            try:
                n = max(n, int(eid[len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{n + 1}"


def validate_evidence_items(items: Any, lines: list[dict], card_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """模型给的 evidence → (合格, 被拒[带 reason])。"""
    ok, rejected = [], []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "").strip()
        speaker = str(it.get("speaker") or "").strip()
        quote = str(it.get("quote") or "").strip()
        reason = ""
        if kind not in KINDS:
            reason = "bad_kind"
        elif speaker not in SPEAKERS:
            reason = "bad_speaker"
        elif kind in TENSHI_ONLY_KINDS and speaker != "天子":
            reason = "kind_requires_tenshi"
        elif not (QUOTE_MIN <= len(normalize_quote(quote)) <= QUOTE_MAX * 2):
            reason = "quote_length"
        msg_id = None if reason else locate_quote(quote, speaker, lines)
        if not reason and msg_id is None:
            reason = "quote_not_found"
        target = str(it.get("target") or "").strip()
        if target and target not in card_ids:
            target = ""
        row = {
            "kind": kind,
            "speaker": speaker,
            "quote": quote[: QUOTE_MAX * 2],
            "gist": str(it.get("gist") or "").strip()[:120],
            "target": target,
            "serious": bool(it.get("serious")) if kind == "rupture" else False,
        }
        if reason:
            rejected.append({**row, "reason": reason})
        else:
            ok.append({**row, "msg_id": msg_id})
    return ok, rejected


def apply_mood(card: dict, mood: Any, day: str) -> bool:
    """mood → card.recent（带到期日）。短命信息，不走反思门槛，但同样过文字校验。返回是否改动。"""
    changed = False

    recent = []
    for r in card.get("recent") or []:
        if not isinstance(r, dict):
            continue
        until = _date(r.get("until") or "")
        if until is not None and _date(day) is not None and until < _date(day):
            changed = True
            continue
        recent.append(r)
    if isinstance(mood, dict):
        text = str(mood.get("text") or "").strip()
        try:
            days = int(mood.get("days") or 1)
        except Exception:
            days = 1
        days = max(1, min(3, days))
        if text and not bad_text_reason(text) and len(text) <= MOOD_MAX_CHARS:
            if all(normalize_quote(r.get("text")) != normalize_quote(text) for r in recent):
                until = (_date(day) + _dt.timedelta(days=days)).isoformat() if _date(day) else ""
                recent.append({"text": text, "since": day, "until": until})
                changed = True
        elif text:
            logger.info(f"[bond] mood 被拒（{bad_text_reason(text) or 'too_long'}）：{text[:60]}")
    recent = recent[-RECENT_MAX:]
    if changed or recent != (card.get("recent") or []):
        card["recent"] = recent
        return True
    return False


def collect_day_evidence(
    messages: list[dict],
    day: str,
    llm: LLM,
    *,
    card_path: str | None = None,
    ledger_path_: str | None = None,
    dry_run: bool = False,
) -> dict:
    """一个总结日（或分片）→ 台账新增 pending 证据 + 卡片 recent。返回本次结果摘要。"""
    result = {"day": day, "added": [], "rejected": [], "mood_changed": False, "skipped": ""}
    if not _c("BOND_REFLECT_ENABLED", True):
        result["skipped"] = "disabled"
        return result
    lines = dialogue_lines(messages)
    if not any(ln["speaker"] == "天子" for ln in lines):
        result["skipped"] = "no_tenshi_lines"
        return result

    card = load_card(card_path)
    ledger = load_ledger(ledger_path_)
    tpl = _c("BOND_EVIDENCE_PROMPT_TEMPLATE", "")
    if not tpl:
        result["skipped"] = "no_prompt"
        return result
    prompt = tpl.format(day=day, bond_state_json=_card_for_prompt(card), dialogue_text=dialogue_text(lines))
    raw = llm(prompt, "bond.evidence")
    obj = _parse_json_obj(raw)
    if not obj:
        result["skipped"] = "llm_no_json"
        return result

    card_ids = {str(s.get("id")) for s in card.get("settled") or [] if isinstance(s, dict)}
    ok, rejected = validate_evidence_items(obj.get("evidence"), lines, card_ids)
    seen = {(e.get("day"), e.get("kind"), normalize_quote(e.get("quote"))) for e in ledger["evidence"]}
    for row in ok:
        key = (day, row["kind"], normalize_quote(row["quote"]))
        if key in seen:
            continue
        seen.add(key)
        row = {"id": _next_ev_id(ledger, day), "day": day, **row, "status": "pending"}
        ledger["evidence"].append(row)
        result["added"].append(row)
    result["rejected"] = rejected

    result["mood_changed"] = apply_mood(card, obj.get("mood"), day)
    if dry_run:
        return result
    if result["added"] or rejected:
        prune_ledger(ledger, day)
        save_ledger(ledger, ledger_path_)
    if result["mood_changed"]:
        save_card(card, card_path)
    logger.info(
        f"[bond] {day} 证据：+{len(result['added'])}（拒 {len(rejected)}）"
        + (f"；拒因 {[r['reason'] for r in rejected]}" if rejected else "")
        + ("；recent 已更新" if result["mood_changed"] else "")
    )
    return result


def prune_ledger(ledger: dict, today: str) -> None:
    keep = []
    for e in ledger.get("evidence") or []:
        age = _days_between(e.get("day", ""), today)
        if age is None:
            keep.append(e)
            continue
        if e.get("status") == "pending" and age > LEDGER_KEEP_PENDING_DAYS:
            e["status"] = "expired"
        if e.get("status") in ("considered", "expired") and age > LEDGER_KEEP_CONSIDERED_DAYS:
            continue
        keep.append(e)
    ledger["evidence"] = keep
    ledger["reflections"] = (ledger.get("reflections") or [])[-LEDGER_KEEP_REFLECTIONS:]






def evidence_weight(e: dict) -> float:
    if e.get("kind") == "rupture" and e.get("serious"):
        return SERIOUS_RUPTURE_WEIGHT
    return WEIGHTS.get(e.get("kind"), 0.0)


def pending_evidence(ledger: dict) -> list[dict]:
    return [e for e in ledger.get("evidence") or [] if e.get("status") == "pending"]


def should_reflect(ledger: dict, today: str) -> tuple[bool, str]:
    pend = pending_evidence(ledger)
    if not pend:
        return False, "no_pending"
    strong = [e for e in pend if e.get("kind") == "milestone" or (e.get("kind") == "rupture" and e.get("serious"))]
    if strong:
        return True, f"strong:{strong[0]['id']}"
    last = ledger.get("last_reflect_day") or ""
    cooldown = int(_c("BOND_REFLECT_COOLDOWN_DAYS", 3) or 0)
    if last:
        gap = _days_between(last, today)
        if gap is not None and gap < cooldown:
            return False, f"cooldown:{gap}<{cooldown}"
    total = sum(evidence_weight(e) for e in pend)
    days = len({e.get("day") for e in pend})
    need_w = float(_c("BOND_REFLECT_MIN_WEIGHT", 3) or 3)
    need_d = int(_c("BOND_REFLECT_MIN_DAYS", 2) or 2)
    if total >= need_w and days >= need_d:
        return True, f"accumulated:w={total:g},days={days}"
    return False, f"below:w={total:g}/{need_w:g},days={days}/{need_d}"






_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


def _rupture_days_for(target: str, evidence: list[dict], today: str) -> tuple[set[str], str]:
    """指向 target 的 rupture 出现在哪些日子（窗口内），以及最后一次 repair 的日子。"""
    window = int(_c("BOND_RETRACT_WINDOW_DAYS", 14) or 14)
    days, last_repair = set(), ""
    for e in evidence:
        if e.get("target") != target:
            continue
        age = _days_between(e.get("day", ""), today)
        if age is None or age > window or age < 0:
            continue
        if e.get("kind") == "rupture" and e.get("speaker") == "天子":
            days.add(e["day"])
        if e.get("kind") == "repair":
            last_repair = max(last_repair, e.get("day", ""))
    return days, last_repair


def validate_ops(ops: Any, card: dict, ledger: dict, today: str) -> tuple[list[dict], list[dict]]:
    """模型给的 ops → (可应用, 被拒[带 reason])。规则见模块 docstring。"""
    accepted, rejected = [], []
    ev_all = {e["id"]: e for e in ledger.get("evidence") or [] if e.get("id")}
    pend_ids = {e["id"] for e in pending_evidence(ledger)}
    settled = {str(s.get("id")): s for s in card.get("settled") or [] if isinstance(s, dict)}
    max_ops = int(_c("BOND_REFLECT_MAX_OPS", 3) or 3)
    max_adds = int(_c("BOND_REFLECT_MAX_ADDS", 2) or 2)
    retract_min_days = int(_c("BOND_RETRACT_MIN_DAYS", 3) or 3)
    n_adds = 0
    touched: set[str] = set()

    def reject(op, reason):
        rejected.append({**(op if isinstance(op, dict) else {"op": op}), "reason": reason})

    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip()
        cites = [str(x) for x in (op.get("evidence") or []) if str(x)]
        cited = [ev_all[x] for x in cites if x in ev_all]
        cited_pending = [e for e in cited if e["id"] in pend_ids]
        text = str(op.get("text") or "").strip()
        oid = str(op.get("id") or "").strip()

        if len(accepted) >= max_ops:
            reject(op, "max_ops")
            continue
        if kind not in ("add", "amend", "retract", "standing"):
            reject(op, "bad_op")
            continue
        if not cited_pending:
            reject(op, "no_pending_evidence")
            continue
        if kind in ("add", "amend", "standing"):
            why = bad_text_reason(text)
            if why:
                reject(op, f"text:{why}")
                continue
        if oid and oid in touched:
            reject(op, "duplicate_target")
            continue

        if kind == "add":
            if not _ID_RE.match(oid) or oid in settled:
                reject(op, "bad_or_existing_id")
                continue
            if n_adds >= max_adds:
                reject(op, "max_adds")
                continue
            if not any(e["kind"] in ADD_KINDS and e["speaker"] == "天子" for e in cited_pending):
                reject(op, "add_needs_tenshi_affirm")
                continue
            if any(normalize_quote(text) == normalize_quote(s.get("text")) for s in settled.values()):
                reject(op, "duplicate_text")
                continue
            n_adds += 1
        elif kind == "amend":
            if oid not in settled:
                reject(op, "unknown_id")
                continue
            if not any(e["kind"] in ADD_KINDS + ("repair",) for e in cited_pending):
                reject(op, "amend_needs_affirm_or_repair")
                continue
            if normalize_quote(text) == normalize_quote(settled[oid].get("text")):
                reject(op, "no_change")
                continue
        elif kind == "retract":
            if oid not in settled:
                reject(op, "unknown_id")
                continue
            ruptures = [e for e in cited if e["kind"] == "rupture" and e["speaker"] == "天子"]
            if not ruptures:
                reject(op, "retract_needs_rupture")
                continue
            serious = any(e.get("serious") and e.get("target") in (oid, "") for e in ruptures)
            days, last_repair = _rupture_days_for(oid, list(ev_all.values()), today)
            repeated = len(days) >= retract_min_days and (not last_repair or last_repair < max(days))
            if not (serious or repeated):
                reject(op, f"retract_insufficient:days={len(days)}/{retract_min_days}")
                continue
            if last_repair and days and last_repair >= max(days) and not serious:
                reject(op, "repaired")
                continue
        elif kind == "standing":
            if not any(e["kind"] == "milestone" or (e["kind"] == "rupture" and e.get("serious")) for e in cited_pending):
                reject(op, "standing_needs_milestone")
                continue
            if normalize_quote(text) == normalize_quote(card.get("standing")):
                reject(op, "no_change")
                continue

        if oid:
            touched.add(oid)
        accepted.append({**op, "op": kind, "text": text, "evidence": [e["id"] for e in cited]})
    return accepted, rejected


def _ev_ref(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "day", "speaker", "quote", "msg_id") if e.get(k) is not None}


def apply_ops(card: dict, ledger: dict, ops: list[dict], today: str) -> list[dict]:
    """把校验过的 ops 应用到 card（就地），返回 changelog 条目。撤回的条目进 ledger.retired。"""
    ev_all = {e["id"]: e for e in ledger.get("evidence") or [] if e.get("id")}
    settled = [s for s in card.get("settled") or [] if isinstance(s, dict)]
    changes = []
    for op in ops:
        kind, oid, text = op["op"], op.get("id", ""), op.get("text", "")
        refs = [_ev_ref(ev_all[x]) for x in op.get("evidence", []) if x in ev_all]
        src = sorted({x for x in op.get("src", []) if isinstance(x, str)}) if isinstance(op.get("src"), list) else []
        if kind == "add":
            settled.append({"id": oid, "text": text, "since": today, "src": src, "evidence": refs})
            changes.append({"day": today, "op": "add", "id": oid, "text": text, "evidence": [r["id"] for r in refs]})
        elif kind == "amend":
            for s in settled:
                if s.get("id") == oid:
                    changes.append({"day": today, "op": "amend", "id": oid, "from": s.get("text"), "text": text,
                                    "evidence": [r["id"] for r in refs]})
                    s["text"] = text
                    s["evidence"] = (s.get("evidence") or []) + refs
                    s["amended"] = today
                    break
        elif kind == "retract":
            keep = []
            for s in settled:
                if s.get("id") == oid:
                    ledger.setdefault("retired", []).append(
                        {**s, "retired_on": today, "reason": op.get("reason", ""), "retract_evidence": refs}
                    )
                    changes.append({"day": today, "op": "retract", "id": oid, "from": s.get("text"),
                                    "reason": op.get("reason", ""), "evidence": [r["id"] for r in refs]})
                else:
                    keep.append(s)
            settled = keep
        elif kind == "standing":
            changes.append({"day": today, "op": "standing", "from": card.get("standing", ""), "text": text,
                            "evidence": [r["id"] for r in refs]})
            card["standing"] = text
    card["settled"] = settled
    if changes:
        card["version"] = int(card.get("version") or 1) + 1
        card["updated_at"] = today
        card["changelog"] = ((card.get("changelog") or []) + changes)[-CHANGELOG_KEEP:]
    return changes


def _recent_events_json(today: str, days: int = 14, limit_chars: int = 12000) -> str:
    try:
        from kb_storage import load_events_for_month
    except Exception:
        return "[]"
    d = _date(today) or _dt.date.today()
    months = sorted({(d - _dt.timedelta(days=i)).strftime("%Y-%m") for i in range(days + 1)})
    rows = []
    for mth in months:
        for e in load_events_for_month(mth) or []:
            eid = str(e.get("event_id") or "")
            age = _days_between(eid[:10], today)
            if age is None or age < 0 or age > days:
                continue
            rows.append({"event_id": eid, "description": str(e.get("description") or "")[:600],
                         "emotional_impact": str(e.get("emotional_impact") or "")[:200]})
    text = json.dumps(rows, ensure_ascii=False, indent=1)
    return text[-limit_chars:] if len(text) > limit_chars else text


def _context_evidence(ledger: dict, today: str, days: int = 45, limit: int = 30) -> list[dict]:
    rows = []
    for e in ledger.get("evidence") or []:
        if e.get("status") != "considered":
            continue
        age = _days_between(e.get("day", ""), today)
        if age is not None and 0 <= age <= days:
            rows.append(e)
    return rows[-limit:]


def _ev_for_prompt(rows: list[dict]) -> str:
    keys = ("id", "day", "kind", "speaker", "quote", "gist", "target", "serious")
    return json.dumps([{k: r.get(k) for k in keys} for r in rows], ensure_ascii=False, indent=1)


def reflect(
    today: str,
    llm: LLM,
    *,
    force: bool = False,
    card_path: str | None = None,
    ledger_path_: str | None = None,
    dry_run: bool = False,
) -> dict:
    """门槛通过（或 force）→ 让模型给 ops → 校验 → 应用 → 落盘。返回本次记录。"""
    ledger = load_ledger(ledger_path_)
    go, why = should_reflect(ledger, today)
    rec = {"day": today, "trigger": why, "ran": False, "applied": [], "rejected": [], "notes": ""}
    if not _c("BOND_REFLECT_ENABLED", True):
        rec["trigger"] = "disabled"
        return rec
    if not (go or (force and pending_evidence(ledger))):
        logger.info(f"[bond] 不反思：{why}")
        return rec

    card = load_card(card_path)
    pend = pending_evidence(ledger)
    changelog = (card.get("changelog") or [])[-8:]
    tpl = _c("BOND_REFLECT_PROMPT_TEMPLATE", "")
    prompt = tpl.format(
        today=today,
        bond_state_json=json.dumps(
            {"standing": card.get("standing", ""),
             "settled": [{"id": s.get("id"), "text": s.get("text"), "since": s.get("since")}
                         for s in card.get("settled") or [] if isinstance(s, dict)]},
            ensure_ascii=False, indent=1),
        changelog_json=json.dumps(changelog, ensure_ascii=False, indent=1),
        pending_json=_ev_for_prompt(pend),
        context_json=_ev_for_prompt(_context_evidence(ledger, today)),
        events_json=_recent_events_json(today),
    )
    raw = llm(prompt, "bond.reflect")
    obj = _parse_json_obj(raw)
    rec["ran"] = True
    if not obj:
        rec["notes"] = "llm_no_json"
        logger.warning("[bond] 反思输出不是 JSON；本次不改，pending 保留。")
        return rec

    accepted, rejected = validate_ops(obj.get("ops"), card, ledger, today)
    rec["rejected"] = rejected
    rec["notes"] = str(obj.get("notes") or "")[:400]
    rec["considered"] = [e["id"] for e in pend]
    if dry_run:
        rec["applied"] = accepted
        return rec

    changes = apply_ops(card, ledger, accepted, today)
    rec["applied"] = changes
    for e in pend:
        e["status"] = "considered"
        e["reflected_on"] = today
    ledger["last_reflect_day"] = today
    ledger.setdefault("reflections", []).append(rec)
    prune_ledger(ledger, today)
    if changes:
        save_card(card, card_path)
    save_ledger(ledger, ledger_path_)
    logger.info(
        f"[bond] 反思（{why}）：应用 {len(changes)}，拒 {len(rejected)}"
        + (f"；拒因 {[r['reason'] for r in rejected]}" if rejected else "")
        + f"；{rec['notes'][:120]}"
    )
    return rec






def default_llm(prompt: str, label: str) -> str:
    system = "你是一个严谨的整理助手，只输出要求的 JSON。"
    out = ""
    try:
        from llm_routing import claude_summary_model_chain, run_claude_model_fallback
        from tenshi_reply import call_claude

        def _invoke(model_id: str) -> str:
            r = call_claude(model_id, system, [{"role": "user", "content": prompt}])
            return ((r[0] if isinstance(r, tuple) and r else r) or "").strip()

        out = run_claude_model_fallback(
            claude_summary_model_chain(), _invoke,
            max_attempts_per_model=int(_c("LOG_UPDATE_LLM_MAX_ATTEMPTS_PER_MODEL", 3) or 3),
            base_sleep=float(_c("LOG_UPDATE_LLM_BASE_SLEEP", 2.5) or 2.5), log=logger,
        ) or ""
    except Exception as e:
        logger.warning(f"[bond] {label} Claude 链失败：{e}")
    if not _parse_json_obj(out):
        try:
            from deepseek_modules import deepseek_generate_json

            for mn in (_cfg.deepseek_merge_models() if _cfg is not None else []):
                try:
                    out = deepseek_generate_json(mn, prompt, system_prompt=system) or ""
                    if _parse_json_obj(out):
                        break
                except Exception as e:
                    logger.warning(f"[bond] {label} DeepSeek({mn}) 失败：{e}")
                if _parse_json_obj(out):
                    break

                try:
                    from llm_routing import try_openrouter_fallback_path

                    fb = try_openrouter_fallback_path(
                        mn,
                        lambda slug, path, _s=system, _p=prompt: deepseek_generate_json(
                            slug, _p, system_prompt=_s, llm_path=path
                        ),
                        log=logger,
                    )
                    if fb and _parse_json_obj(fb):
                        out = fb
                        break
                except Exception as fb_exc:
                    logger.warning(f"[bond] {label} OpenRouter 兜底失败：{fb_exc}")
        except Exception as e:
            logger.warning(f"[bond] {label} DeepSeek 兜底不可用：{e}")
    _trace(label, prompt, out)
    return out


def _trace(label: str, prompt: str, out: str) -> None:
    path = _c("SUMMARY_LLM_TRACE_PATH", None)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"source": f"log_update.{label}", "prompt": prompt, "response_text": out,
                                "ts": _dt.datetime.now().timestamp()}, ensure_ascii=False) + "\n")
    except Exception:
        pass






def _load_log_messages_for_day(day: str) -> list[dict]:
    import glob

    root = os.path.dirname(_bs.default_path())
    files = sorted(glob.glob(os.path.join(root, "archive", "tenshi_log-*.json"))) + [os.path.join(root, "tenshi_log.json")]
    seen, out = set(), []
    for f in files:
        d = _read_json(f, [])
        for conv in d if isinstance(d, list) else [d]:
            for m in (conv or {}).get("chat_history", []) if isinstance(conv, dict) else []:
                mid = m.get("msg_id")
                if mid in seen or m.get("time") is None:
                    continue
                if _day_of(m["time"]) == day:
                    seen.add(mid)
                    out.append(m)
    return sorted(out, key=lambda m: m.get("time", 0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="「我和他」关系沉淀：证据台账与带门槛的反思")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="看门槛状态与 pending 证据")
    c = sub.add_parser("collect", help="对某天的聊天日志收集证据")
    c.add_argument("day")
    c.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("reflect", help="按门槛反思（--force 忽略门槛）")
    r.add_argument("--day", default=_dt.date.today().isoformat())
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.cmd == "status":
        led = load_ledger()
        today = _dt.date.today().isoformat()
        print(json.dumps({"should_reflect": should_reflect(led, today), "last_reflect_day": led.get("last_reflect_day"),
                          "pending": pending_evidence(led)}, ensure_ascii=False, indent=1))
    elif a.cmd == "collect":
        res = collect_day_evidence(_load_log_messages_for_day(a.day), a.day, default_llm, dry_run=a.dry_run)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif a.cmd == "reflect":
        res = reflect(a.day, default_llm, force=a.force, dry_run=a.dry_run)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
