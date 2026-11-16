# SPDX-License-Identifier: AGPL-3.0-only
"""Who a private change belongs to, and what one line of history says it is.

Authorship is data, never capability: the public framework only ships neutral
placeholders, and a private data repository may name its owner, its character
and its background systems in the ``git`` section of ``identity.json``. Nothing
in this module can authorize an event, send a message or read a secret — it only
labels the commit so a human can tell a message apart from a reply, an
observation, or a routine sync.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rp_core.settings import read_json

# Placeholders stay generic on purpose: private names live in the data repo.
DEFAULT_OWNER = ("RP owner", "owner@rp.invalid")
DEFAULT_CHARACTER = ("RP character", "character@rp.invalid")
DEFAULT_SYSTEMS = {
    "sync": ("rp-sync", "rp-sync@rp.invalid"),
    "maintenance": ("rp-maintenance", "rp-maintenance@rp.invalid"),
    "probe": ("rp-probe", "rp-probe@rp.invalid"),
    "keepalive": ("rp-keepalive", "rp-keepalive@rp.invalid"),
}
SYSTEM_ROLES = ("sync", "maintenance", "probe", "keepalive")

# task -> (role, category, event wording). The wording never repeats a name:
# the author already says who changed what.
TASKS = {
    "queue": ("owner", "收讯", "消息已入队"),
    "reply": ("character", "回复", "回音已落档"),
    "perception": ("character", "感知", "世界观测已入档"),
    "force-perception": ("character", "感知", "定向观测已入档"),
    "summary": ("character", "记忆", "卷宗摘要已更新"),
    "archive": ("system:maintenance", "归档", "旧卷宗已归档"),
    "cleanup": ("system:maintenance", "维护", "运行时日志已清理"),
    "probe": ("system:probe", "探活", "通路健康快照"),
    "daily-backup": ("system:maintenance", "维护", "每日备份已封存"),
    "weekly-backup": ("system:maintenance", "维护", "每周备份已封存"),
    "recovery": ("system:sync", "恢复", "私有状态已保全"),
}
FALLBACK = ("system:maintenance", "维护", "状态已落档")


def _person(value) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    email = value.get("email")
    if not isinstance(name, str) or not isinstance(email, str):
        return None
    name, email = name.strip(), email.strip()
    if not name or "@" not in email or email.startswith("@") or email.endswith("@"):
        return None
    return name, email


def _declared(root: Path) -> dict:
    try:
        identity = read_json(root / "identity.json", {})
    except Exception:
        # A private file that cannot be read must not stop a commit: fall back
        # to the neutral placeholders instead of failing persistence.
        return {}
    git = identity.get("git") if isinstance(identity, dict) else {}
    return git if isinstance(git, dict) else {}


def resolve(root: Path, role: str) -> tuple[str, str]:
    """Return the ``(name, email)`` a role should commit as."""
    git = _declared(root)
    if role == "owner":
        return _person(git.get("owner")) or DEFAULT_OWNER
    if role == "character":
        return _person(git.get("character")) or DEFAULT_CHARACTER
    if role.startswith("system:"):
        key = role.split(":", 1)[1]
        if key in SYSTEM_ROLES:
            systems = git.get("systems")
            declared = _person(systems.get(key)) if isinstance(systems, dict) else None
            return declared or DEFAULT_SYSTEMS[key]
        return DEFAULT_SYSTEMS["maintenance"]
    return DEFAULT_SYSTEMS["maintenance"]


def offset_hours() -> int:
    raw = (os.environ.get("TIMEZONE_OFFSET") or "").strip()
    try:
        value = int(float(raw))
    except ValueError:
        return 8
    return value if -12 <= value <= 14 else 8


def stamp(now: datetime | None = None) -> str:
    """``MM-DD HH:MM:SS`` in the instance's local time, not UTC."""
    moment = now or datetime.now(timezone(timedelta(hours=offset_hours())))
    return moment.strftime("%m-%d %H:%M:%S")


def title(category: str, event: str, now: datetime | None = None) -> str:
    return f"{category}｜{event} · {stamp(now)} [skip ci]"


def plan(task: str) -> tuple[str, str, str]:
    return TASKS.get(str(task or ""), FALLBACK)


def commit(root: Path, task: str, *, now: datetime | None = None) -> tuple[str, str, str]:
    """Return ``(name, email, message)`` for persisting after ``task``."""
    role, category, event = plan(task)
    name, email = resolve(root, role)
    return name, email, title(category, event, now)


def recovery(root: Path, *, now: datetime | None = None) -> tuple[str, str, str]:
    return commit(root, "recovery", now=now)


def merge(root: Path, branch: str, *, now: datetime | None = None) -> tuple[str, str, str]:
    name, email = resolve(root, "system:sync")
    return name, email, title("汇流", f"{branch or 'staging'} 并入 main", now)
