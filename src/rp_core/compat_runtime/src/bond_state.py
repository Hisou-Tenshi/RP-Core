# SPDX-License-Identifier: AGPL-3.0-only





























from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

try:
    import tenshi_config as _cfg
except Exception:
    _cfg = None




from config.persona import INNER_HEADING, INNER_LEAD, TYPING_HEADING, TYPING_LEAD


def default_path() -> str:
    p = getattr(_cfg, "BOND_STATE_FILE_PATH", None) if _cfg is not None else None
    if p:
        return p
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "bond_state.json")


def load_bond_state(path: str | None = None) -> dict:
    """读状态卡；文件不存在/损坏时返回空 dict（注入为空，行为与没有这一层时一致）。"""
    p = path or default_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _today(today: _dt.date | str | None) -> _dt.date:
    if isinstance(today, _dt.date):
        return today
    if isinstance(today, str) and today:
        try:
            return _dt.date.fromisoformat(today[:10])
        except ValueError:
            pass
    return _dt.date.today()


def _texts(items: Any) -> list[str]:
    out = []
    for it in items or []:
        t = (it.get("text") if isinstance(it, dict) else it) or ""
        t = str(t).strip()
        if t:
            out.append(t)
    return out


def active_recent(state: dict, today: _dt.date | str | None = None) -> list[str]:
    """recent 里还没到期的（until 缺省视为一直有效，until 当天仍有效）。"""
    d = _today(today)
    out = []
    for it in state.get("recent") or []:
        if not isinstance(it, dict):
            continue
        until = str(it.get("until") or "").strip()
        if until:
            try:
                if _dt.date.fromisoformat(until[:10]) < d:
                    continue
            except ValueError:
                pass
        t = str(it.get("text") or "").strip()
        if t:
            out.append(t)
    return out





NICKNAME_LABELS = (
    ("天子称用户", "我叫他"),
    ("用户称天子", "他叫我"),
    ("用户自称", "他自称"),
    ("共用", "一起叫的"),
)


def _nickname_value(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "、".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def _nickname_lines(nicknames: Any) -> list[str]:
    """relationship_dynamics.special_nicknames → 「我叫他：…」几行；只取 NICKNAME_LABELS 里的键。"""
    if not isinstance(nicknames, dict):
        return []
    lines = []
    for key, label in NICKNAME_LABELS:
        v = _nickname_value(nicknames.get(key))
        if v:
            lines.append(f"- {label}：{v}")
    return lines


def format_bond_for_inner(
    state: dict | None,
    nicknames: Any = None,
    today: _dt.date | str | None = None,
) -> str:
    """心里那一步（及感知层）system 里的「## 我和他」整段；什么都没有时返回空串。"""
    state = state or {}
    standing = str(state.get("standing") or "").strip()
    settled = _texts(state.get("settled"))
    recent = active_recent(state, today)
    nick = _nickname_lines(nicknames)
    if not (standing or settled or recent or nick):
        return ""
    parts = [INNER_HEADING, INNER_LEAD]
    if standing:
        parts += ["", standing]
    if settled:
        parts += [""] + [f"- {t}" for t in settled]
    if nick:
        parts += ["", "我们之间的叫法："] + nick
    if recent:
        parts += ["", "这阵子心里还挂着的："] + [f"- {t}" for t in recent]
    return "\n".join(parts).strip() + "\n"


def format_bond_for_typing(state: dict | None, today: _dt.date | str | None = None) -> str:
    """打字那一步 system 里的精简版：只给定论，不给叫法清单与档案（打字不看资料）。"""
    state = state or {}
    standing = str(state.get("standing") or "").strip()
    settled = _texts(state.get("settled"))
    recent = active_recent(state, today)
    if not (standing or settled or recent):
        return ""
    parts = [TYPING_HEADING, TYPING_LEAD]
    if standing:
        parts.append(standing)
    parts += [f"- {t}" for t in settled]
    if recent:
        parts += ["这阵子心里还挂着的："] + [f"- {t}" for t in recent]
    return "\n".join(parts).strip()


def inner_block(kb_data: dict | None = None, *, path: str | None = None, today=None) -> str:
    """调用点用的便捷入口：读文件 + 取 KB 里的 special_nicknames → 心里那段。"""
    rd = (kb_data or {}).get("relationship_dynamics") if isinstance(kb_data, dict) else None
    nick = rd.get("special_nicknames") if isinstance(rd, dict) else None
    try:
        return format_bond_for_inner(load_bond_state(path), nick, today)
    except Exception:
        return ""


def typing_block(*, path: str | None = None, today=None) -> str:
    try:
        return format_bond_for_typing(load_bond_state(path), today)
    except Exception:
        return ""
