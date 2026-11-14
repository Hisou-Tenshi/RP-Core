# SPDX-License-Identifier: AGPL-3.0-only













import re

_LEADING_LOG_TS_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\+\d+\]\s*")




_SYNTHETIC_QUERY_MARKERS = (
    "[系统级感知层触发",
    "[感知层上报",
)

def _strip_leading_log_timestamp_prefix(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    stripped = _LEADING_LOG_TS_PREFIX_RE.sub("", text, count=1)
    return stripped.lstrip() if stripped != text else text


def _normalize_for_silence_compare(fragment: str) -> str:
    """Strip common markdown/code wrappers so `` `SILENCE` ``, **SILENCE**, etc. compare equal."""
    s = (fragment or "").strip()
    for _ in range(8):
        if not s:
            break
        changed = False
        if len(s) >= 2 and s.startswith("`") and s.endswith("`"):
            s = s[1:-1].strip()
            changed = True
            continue
        if len(s) >= 4 and s.startswith("**") and s.endswith("**"):
            s = s[2:-2].strip()
            changed = True
            continue
        if len(s) >= 2 and not s.startswith("**") and s.startswith("*") and s.endswith("*"):
            s = s[1:-1].strip()
            changed = True
            continue
        if not changed:
            break
    return s


def _line_declares_silence(line: str) -> bool:
    return _normalize_for_silence_compare(line).upper() == "SILENCE"


def _perception_bot_answer_is_silence_only(bot_answer) -> bool:
    """True when stored bot_answer is only a SILENCE token (incl. `` `SILENCE` ``), not a real proactive line."""
    if not isinstance(bot_answer, str):
        return False
    t = bot_answer.strip()
    if not t:
        return True
    if "\n" in t:
        return False

    if re.fullmatch(r"[（(]\s*不回\s*[）)]", t):
        return True
    return _line_declares_silence(t)


def _is_real_user_message(msg) -> bool:
    """该条记录是否为「用户真的说了话」，而不是感知层自己注入的合成记录。

    为什么需要它：感知层的**开口**记录存成
    ``user_query="[系统级感知层触发 - <角色>主动起念]"`` + ``replied_flag=True``，
    **不再携带** ``perception_event`` 之外的区分信息；而 SILENCE 记录过去存成
    ``user_query="[感知层上报世界状态，…选择了沉默不语]"``。
    只看 `user_query` 非空会把天子主动说的话当成「用户刚说过话」，
    于是 silence_duration 从那一刻重新起算，触发间隔判定被放水。

    判定顺序：
      1. ``perception_event`` 为真 → 一定是感知层记录，排除；
      2. user_query 为空 → 用户没说话，排除；
      3. 命中感知层合成前缀 → 排除（兼容 perception_event 标记缺失的旧数据）。
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("perception_event"):
        return False
    query = msg.get("user_query")
    if not isinstance(query, str) or not query.strip():
        return False
    stripped = query.strip()
    for marker in _SYNTHETIC_QUERY_MARKERS:
        if stripped.startswith(marker):
            return False
    return True


def _extract_silence_from_final_text(text: str) -> tuple[bool, str]:
    """
    判定最终输出是否为 SILENCE（允许前面有内容但以 SILENCE 作为最终决定）。
    行尾可为纯 SILENCE，或模型常见的 `` `SILENCE` `` / **SILENCE** 等形式。
    """
    if not isinstance(text, str):
        return False, ""
    s = text.strip()
    if not s:
        return True, ""
    if "\n" not in s and _line_declares_silence(s):
        return True, ""
    lines = [ln.strip() for ln in s.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if lines and _line_declares_silence(lines[-1]):
        return True, ""
    return False, s
