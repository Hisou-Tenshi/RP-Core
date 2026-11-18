# SPDX-License-Identifier: AGPL-3.0-only














from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any



from config.persona import TYPING_STICKER_PROTOCOL
from message_split import newlines_to_br

logger = logging.getLogger("TenshiReply")

LIBRARY_PATH = os.path.join(os.environ["RP_RUNTIME_ROOT"], "data", "stickers.json")

MARKER_PREFIX = "贴纸"


_MARKER_RE = re.compile(r"[〔【\[]\s*贴纸\s*[：:]\s*([^〔〕【】\[\]\n<>]{1,40}?)\s*[〕】\]]")
_BR_RE = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.IGNORECASE)

_lock = threading.Lock()
_cache: dict[str, Any] = {}






def _cfg(name: str, default):
    """读 tenshi_config 上的开关；拿不到（例如单测里没加载配置）就用默认值。"""
    try:
        import tenshi_config

        return getattr(tenshi_config, name, default)
    except Exception:
        return default


def enabled() -> bool:
    return bool(_cfg("REPLY_STICKERS_ENABLED", True))


def max_per_turn() -> int:
    try:
        return max(0, int(_cfg("REPLY_STICKERS_MAX_PER_TURN", 2)))
    except (TypeError, ValueError):
        return 2


def send_delay_sec() -> float:
    """非首段贴纸发出前等多久（秒）。贴纸是顺手甩的，比打字短。"""
    try:
        return max(0.0, float(_cfg("REPLY_STICKER_DELAY_SEC", 1.0)))
    except (TypeError, ValueError):
        return 1.0






def _load(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"贴纸库读取失败，贴纸功能本轮关闭：{e}")
        return None
    items = data.get("stickers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    by_name: dict[str, dict] = {}
    by_uid: dict[str, dict] = {}
    by_fid: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        fid = str(it.get("file_id") or "").strip()
        if not name or not fid:
            continue
        by_name[name] = it
        uid = str(it.get("file_unique_id") or "").strip()
        if uid:
            by_uid[uid] = it
        by_fid[fid] = it
    if not by_name:
        return None


    alias_hits: dict[str, set[str]] = {}
    for name, it in by_name.items():
        srcs = [name] + re.findall(r"「([^」]{1,30})」", str(it.get("desc") or ""))
        if it.get("text"):
            srcs.append(str(it["text"]))
        for s in srcs:
            k = _alias_key(s)
            if k:
                alias_hits.setdefault(k, set()).add(name)
    by_alias = {k: by_name[next(iter(v))] for k, v in alias_hits.items() if len(v) == 1 and k not in by_name}
    cats = [str(c) for c in (data.get("categories") or []) if str(c or "").strip()]
    return {"by_name": by_name, "by_alias": by_alias, "by_uid": by_uid, "by_fid": by_fid,
            "categories": cats, "items": list(by_name.values())}


_ALIAS_STRIP_RE = re.compile(r"[\s—－\-~～·・、，,。．.！!？?…「」『』\"'“”]+")


def _alias_key(s: str | None) -> str:
    return _ALIAS_STRIP_RE.sub("", str(s or "")).lower()


def library() -> dict | None:
    """加载并缓存贴纸库；开关关闭或库不可用时返回 None。"""
    if not enabled():
        return None
    path = LIBRARY_PATH
    with _lock:
        if path not in _cache:
            _cache[path] = _load(path)
        return _cache[path]


def reload() -> None:
    """清缓存（测试 / 热更新用）。"""
    with _lock:
        _cache.clear()


def available() -> bool:
    return library() is not None


def by_name(name: str | None) -> dict | None:
    lib = library()
    if not lib or not name:
        return None
    n = str(name).strip()
    hit = lib["by_name"].get(n)
    if hit is None:

        hit = lib.get("by_alias", {}).get(_alias_key(n))
    return hit


def by_file_unique_id(uid: str | None) -> dict | None:
    lib = library()
    if not lib or not uid:
        return None
    return lib["by_uid"].get(str(uid).strip())


def by_file_id(fid: str | None) -> dict | None:
    lib = library()
    if not lib or not fid:
        return None
    return lib["by_fid"].get(str(fid).strip())







def catalog_text() -> str:
    """按 category 分组的贴纸清单，每行「短名：desc」。库不可用返回空串。"""
    lib = library()
    if not lib:
        return ""
    groups: dict[str, list[dict]] = {}
    order: list[str] = list(lib["categories"])
    for it in lib["items"]:
        cat = str(it.get("category") or "").strip() or "其他"
        if cat not in order:
            order.append(cat)
        groups.setdefault(cat, []).append(it)
    lines: list[str] = []
    for cat in order:
        items = groups.get(cat)
        if not items:
            continue
        lines.append(f"【{cat}】")
        for it in items:
            desc = str(it.get("desc") or "").strip()
            lines.append(f"{it['name']}：{desc}" if desc else str(it["name"]))
    return "\n".join(lines)


def typing_prompt_block() -> str:
    """追加在媒介说明后面的贴纸说明 + 清单；库关闭时返回空串（提示词逐字不变）。"""
    cat = catalog_text()
    if not cat:
        return ""
    return f"{TYPING_STICKER_PROTOCOL}\n\n{cat}"






def marker(name: str) -> str:
    """规范化标记。"""
    return f"〔{MARKER_PREFIX}：{name}〕"


def has_marker(text: str | None) -> bool:
    return bool(text) and bool(_MARKER_RE.search(text))


def sticker_name_of_segment(segment: str | None) -> str | None:
    """这一段恰好是一个贴纸标记（且在库里）→ 返回短名；否则 None。"""
    s = (segment or "").strip()
    m = _MARKER_RE.fullmatch(s)
    if not m:
        return None
    st = by_name(m.group(1).strip())
    return st["name"] if st else None


def _split_piece(piece: str) -> list[tuple[str, str]]:
    """一段（不含 <br>）→ [("text", 字) | ("sticker", 短名) | ("unknown", 原标记)]。"""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _MARKER_RE.finditer(piece):
        before = piece[pos:m.start()].strip()
        if before:
            out.append(("text", before))
        st = by_name(m.group(1).strip())
        out.append(("sticker", st["name"]) if st else ("unknown", m.group(0)))
        pos = m.end()
    tail = piece[pos:].strip()
    if tail:
        out.append(("text", tail))
    return out


def segment_reply(text: str | None, *, limit: int | None = None) -> list[tuple[str, str]]:
    """正文 → 段列表：("text", 文字) / ("sticker", 短名)。

    按 <br> 切开；贴纸标记混在一句话里时拆成前文、贴纸、后文三段。
    未知短名的标记删掉并 warning；超出每轮上限的贴纸丢掉。空段不保留。
    """
    cap = max_per_turn() if limit is None else max(0, int(limit))
    out: list[tuple[str, str]] = []
    used = 0
    for piece in _BR_RE.split(text or ""):
        for kind, val in _split_piece(piece):
            if kind == "unknown":
                logger.warning(f"[sticker] 未知贴纸已删掉（不外发）：{val!r}")
                continue
            if kind == "sticker":
                if used >= cap:
                    logger.warning(f"[sticker] 超出每轮上限 {cap}，丢掉：{val!r}")
                    continue
                used += 1
            out.append((kind, val))
    return out


def join_segments(segments: list[tuple[str, str]]) -> str:
    return "<br>".join(marker(v) if k == "sticker" else v for k, v in segments)


def normalize_reply(text: str | None) -> str:
    """把正文里的贴纸标记规整成统一表示（单独一段的规范标记）。

    没有任何标记时原样返回（逐字不变）。删完什么都不剩 → 返回空串（= 不回）。
    """
    if not isinstance(text, str) or not has_marker(text):
        return text if isinstance(text, str) else ""
    return join_segments(segment_reply(text))


def split_for_send(text: str | None, **split_kwargs) -> list[str]:
    """外发切段：先 normalize，再按 message_split.split_reply 切条，
    最后保证每个贴纸标记都单独成段（split_reply 的条数上限合并、长块切分不能把它吞进文字里）。
    """
    from message_split import split_reply

    # 真实换行（模型常写 \n\n 分段）先收敛成 <br>，否则 split_reply 看不到分段点，
    # 整段会挤成一条消息发出去。
    t = newlines_to_br(normalize_reply(text or ""))
    segs = split_reply(t, **split_kwargs)
    if not has_marker(t):
        return segs
    out: list[str] = []
    for seg in segs:
        if not has_marker(seg):
            out.append(seg)
            continue
        for kind, val in _split_lines(seg):
            if kind == "sticker":
                out.append(marker(val))
            elif kind == "text":
                out.append(val)
    return out


def _split_lines(seg: str) -> list[tuple[str, str]]:
    """被 split_reply 并进同一条的多段（以换行相连）里拆出贴纸；文字部分保持原样合并。"""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    for line in seg.split("\n"):
        pieces = _split_piece(line)
        if not any(k == "sticker" for k, _ in pieces):
            buf.append(line)
            continue
        for kind, val in pieces:
            if kind == "text":
                buf.append(val)
            elif kind == "sticker":
                if buf and "\n".join(buf).strip():
                    parts.append(("text", "\n".join(buf).strip()))
                buf = []
                parts.append(("sticker", val))
    if buf and "\n".join(buf).strip():
        parts.append(("text", "\n".join(buf).strip()))
    return parts






def extract_for_rewrite(text: str | None) -> tuple[str, list[tuple[int, str]]]:
    """→ (只剩文字段、以 <br> 相连的正文, [(原段序号, 规范标记)])。没有贴纸时原样返回。"""
    if not isinstance(text, str) or not has_marker(text):
        return text or "", []
    segs = segment_reply(text, limit=10**6)
    slots = [(i, marker(v)) for i, (k, v) in enumerate(segs) if k == "sticker"]
    body = "<br>".join(v for k, v in segs if k == "text")
    return body, slots


def restore_after_rewrite(rewritten: str | None, slots: list[tuple[int, str]]) -> str:
    """把抽出的贴纸按原位置放回改写后的正文（改写器自己编出来的标记一律删掉）。"""
    if not slots:
        return rewritten or ""
    body = _MARKER_RE.sub("", rewritten or "")
    texts = [p.strip() for p in _BR_RE.split(body) if p.strip()]
    out: list[str] = []
    ti = 0
    si = 0
    total = len(texts) + len(slots)
    for pos in range(total):
        if si < len(slots) and (slots[si][0] <= pos or ti >= len(texts)):
            out.append(slots[si][1])
            si += 1
        elif ti < len(texts):
            out.append(texts[ti])
            ti += 1
    return "<br>".join(out)






def incoming_marker(sticker: dict | None) -> str:
    """命中库的贴纸 → 他这条消息落库/进上下文的文字（规范标记）。"""
    if not sticker or not sticker.get("name"):
        return ""
    return marker(str(sticker["name"]))


def annotate_user_markers(text: str | None) -> str:
    """他消息里的贴纸标记后面补一句这张贴纸是什么意思（只进上下文，不落库）。

    例：「〔贴纸：嫌弃〕」→「〔贴纸：嫌弃〕（这张贴纸：嫌弃地斜眼看你）」。没有 desc 或库关闭时原样。
    """
    if not isinstance(text, str) or not has_marker(text):
        return text

    def _sub(m: re.Match) -> str:
        st = by_name(m.group(1).strip())
        if not st:
            return m.group(0)
        desc = str(st.get("desc") or "").strip()
        mk = marker(st["name"])
        return f"{mk}（这张贴纸：{desc}）" if desc else mk

    return _MARKER_RE.sub(_sub, text)
