# SPDX-License-Identifier: AGPL-3.0-only







from __future__ import annotations

import os
import re

_SKILLS_DIR = os.path.abspath(
    os.path.join(os.environ["RP_RUNTIME_ROOT"], "skills")
)

_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


SKILL_CATALOG: dict[str, str] = {
    "search_memory": "翻长期记忆、共同梗、往期对话（本地 RAG）",
    "search_web": "联网搜索实时信息（Tavily→DDG，每轮最多 2 次）",
    "fetch_url": "抓取网页/JSON/Markdown/纯文本正文（每轮最多 3 次）",
    "read_config_file": "读项目内文件或先看目录树（每轮最多 3 次）",
}


def skills_root() -> str:
    return _SKILLS_DIR


def list_skill_ids() -> list[str]:
    out: list[str] = []
    if not os.path.isdir(_SKILLS_DIR):
        return list(SKILL_CATALOG.keys())
    for name in sorted(os.listdir(_SKILLS_DIR)):
        if name.endswith(".md"):
            sid = name[:-3]
            if _SKILL_ID_RE.match(sid):
                out.append(sid)
    for sid in SKILL_CATALOG:
        if sid not in out:
            out.append(sid)
    return out


def format_skill_catalog_for_prompt() -> str:
    lines = ["# 扩展技能目录（用 read_skill 读全文后再调用对应工具）"]
    for sid in list_skill_ids():
        brief = SKILL_CATALOG.get(sid, "（见技能文件）")
        lines.append(f"- {sid}: {brief}")
    lines.append("")
    lines.append("不确定用法时先 `read_skill(skill_id=...)`，再调用工具。")
    return "\n".join(lines)


def read_skill(skill_id: str) -> str:
    sid = (skill_id or "").strip().lower().replace("-", "_")
    if not _SKILL_ID_RE.match(sid):
        return f"[read_skill] Invalid skill_id: {skill_id!r}"
    path = os.path.join(_SKILLS_DIR, f"{sid}.md")
    if not os.path.isfile(path):
        known = ", ".join(list_skill_ids())
        return f"[read_skill] Skill not found: {sid}. Known: {known}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
    except Exception as e:
        return f"[read_skill] Failed to read {sid}: {e}"
    return f"[SKILL: {sid}]\n{body.strip()}\n"
