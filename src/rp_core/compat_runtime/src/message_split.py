# SPDX-License-Identifier: AGPL-3.0-only




















import re
from typing import List, Sequence, Tuple

__all__ = [
    "split_reply",
    "chunk_to_limit",
    "MAX_MESSAGE_CHARS",
    "normalize_br_markup",
    "newlines_to_br",
    "br_to_space",
    "unescape_model_text",
    "typed_newlines_to_br",
]


MAX_MESSAGE_CHARS = 4000


_BR_RE = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.IGNORECASE)


def normalize_br_markup(text: str) -> str:
    """把各种写法的换气标记统一成 ``<br>``，一个都不删。

    落盘口径：bot_answer / user_query 里一律只保留 ``<br>`` 这一种写法，
    方便历史回灌时模型认出"分条"这件事；真正外发前由
    ``reply_streaming.strip_br_markup`` / ``split_reply`` 处理。
    """
    if not isinstance(text, str) or not text:
        return text
    return _BR_RE.sub("<br>", text)


def newlines_to_br(text: str) -> str:
    """把真实换行（用户按回车分几次打）统一表示成 ``<br>``。

    幂等：已有 ``<br>`` / ``<br/>`` 先归一，再换掉 \\r\\n / \\r / \\n；
    重复调用不会叠出多个标记。
    """
    if not isinstance(text, str) or not text:
        return text
    normalized = normalize_br_markup(text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "<br>")


def br_to_space(text: str) -> str:
    """把换气标记还原成空格，用于需要纯文本的查询（检索/搜索等）。"""
    if not isinstance(text, str) or not text:
        return text
    return _BR_RE.sub(" ", text)




_DOUBLE_BACKSLASH_RE = re.compile(r"\\\\+")
_ESCAPED_NEWLINE_RE = re.compile(r"\\r\\n|\\n|\\r")


def unescape_model_text(text: str) -> str:
    """把模型正文里的转义残留还原成真实字符。

    覆盖：字面 ``\\n`` / ``\\r\\n`` / ``\\r``（反斜杠+n 这两个字符）→ 真实换行；
    连写双反斜杠（含 ``\\\\n`` 双重转义）→ 单反斜杠。幂等；只动反斜杠序列，
    不碰真实换行与 ``<br>``，没有转义残留的文本逐字不变。
    """
    if not isinstance(text, str) or not text:
        return text
    t = _DOUBLE_BACKSLASH_RE.sub(lambda _m: "\\", text)
    return _ESCAPED_NEWLINE_RE.sub("\n", t)


def typed_newlines_to_br(text: str) -> str:
    """打字输出口径：所有「换行」表示一律收敛成单个 ``<br>``（= 按一次发送）。

    覆盖：真实换行（含空行）、转义残留（``\\n`` / ``\\\\n``，先过
    ``unescape_model_text``）、``<br>`` 各种写法，以及标记与换行的混写、
    连续多个分隔。打字那一步一行就是一条消息，一行就是按一次发送。
    """
    if not isinstance(text, str) or not text:
        return text
    t = unescape_model_text(text)
    t = normalize_br_markup(t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n+", "<br>", t)

    t = re.sub(r"[ \t]*(?:<br>[ \t]*)+", "<br>", t, flags=re.IGNORECASE)
    return t.strip()


_SENT_END = "。！？!?…～~；;"
_SOFT_BREAK = "，,、）)」』:;"


def _normalize(text: str) -> str:
    """统一换行符,压掉多余空白行。不碰正文里的语气词和标点。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _clean_segments(segments: Sequence[str]) -> List[str]:
    """去掉每段首尾空白,只丢弃完全为空的段;不按长度/空转词合并。"""
    out: List[str] = []
    for seg in segments:
        seg = (seg or "").strip()
        if seg:
            out.append(seg)
    return out


def _cap_total(segments: List[str], max_total: int) -> List[str]:
    """超过 max_total 条时,只把超出部分(第 max_total 条及之后)用换行并进最后一条。"""
    if max_total <= 0 or len(segments) <= max_total:
        return segments
    head = segments[: max_total - 1]
    tail = "\n".join(segments[max_total - 1 :])
    return head + [tail]


def chunk_to_limit(text: str, limit: int = MAX_MESSAGE_CHARS) -> List[str]:
    """把一段超长文本按语义边界切成不超过 limit 的若干段。"""
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for chars in (_SENT_END, "\n", _SOFT_BREAK, " "):
            idx = max((window.rfind(c) for c in chars), default=-1)
            if idx > limit // 2:
                cut = idx + 1
                break
        if cut <= 0:
            cut = limit
        head = rest[:cut].strip()
        if head:
            chunks.append(head)
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def split_reply(
    text: str,
    *,
    min_segment: int = 8,
    max_total: int = 40,
    max_chars: int = MAX_MESSAGE_CHARS,
    chunk_long: bool = True,
) -> List[str]:
    """
    按 `<br>` 把回复切成待发送的消息列表。

    返回长度 1 的列表 = 照常发一条(没有标记,或切完只剩一条)。
    chunk_long=False(感知层口径):没有标记时整条原样返回,连超长按限切
    也一并关掉——没有 <br> 就是一条,不为长度硬切;有标记时单段超限
    仍按限切兜底,不让平台拒绝发送。

    min_segment:仅为兼容旧调用方保留签名,已不再按长度合并碎片——
    角色用 `<br>` 分出的每一条(哪怕是"哦""？")都原样独立成条。
    max_total:条数上限(默认 40);超出时只把超出部分并进最后一条,
    不再整体退回一条。<=0 表示不限。
    """
    t = _normalize(text)
    if not t:
        return []
    if not _BR_RE.search(t):
        if not chunk_long:
            return [t]
        return chunk_to_limit(t, max_chars)

    segments = _cap_total(_clean_segments(_BR_RE.split(t)), max_total)
    if not segments:
        return []

    out: List[str] = []
    for seg in segments:
        out.extend(chunk_to_limit(seg, max_chars))
    return out


def describe_split(segments: Sequence[str]) -> Tuple[int, List[int]]:
    """给日志用:条数 + 每条字数。"""
    return len(segments), [len(s) for s in segments]
