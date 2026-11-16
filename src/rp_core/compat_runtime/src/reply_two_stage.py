# SPDX-License-Identifier: AGPL-3.0-only




















from __future__ import annotations

import copy
import difflib
import logging
import re
import unicodedata
from typing import Any

from message_split import typed_newlines_to_br, unescape_model_text





from config.persona import (
    INNER_PERCEPTION_PROTOCOL,
    INNER_REPLY_PROTOCOL,
    MEDIUM_TELEGRAM,
    TYPING_BACKGROUND_LEAD,
    TYPING_PROTOCOL,
)
from config.prompts import INNER_CUE, PERCEPTION_SITUATION_HEADER


logger = logging.getLogger("TenshiReply")


def medium_block(medium: str | None = "telegram") -> str:
    """打字的媒介说明。目前只有 Telegram 私聊（与 QQ 私聊同类）。"""
    return MEDIUM_TELEGRAM


def with_inner_cue(messages: list) -> list:
    """返回在最后一条 user 消息末尾补了 INNER_CUE 的副本；原列表不动。"""
    out = copy.deepcopy(messages or [])
    for m in reversed(out):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = f"{c}\n\n{INNER_CUE}"
        elif isinstance(c, list):
            c.append({"type": "text", "text": INNER_CUE})
        break
    return out


def inner_protocol(perception: bool = False) -> str:
    """第一步（心里）追加在 system 末尾的说明。perception=True 为主动起念版。"""
    return INNER_PERCEPTION_PROTOCOL if perception else INNER_REPLY_PROTOCOL







TYPING_HISTORY_MESSAGES_DEFAULT = 10

_TS_PREFIX_RE = re.compile(r"^\s*[（(][^）)\n]{1,40}[）)]\s*")


_THINK_RE = re.compile(
    r"<(?:think(?:ing)?|analysis)>[\s\S]*?</(?:think(?:ing)?|analysis)>", re.IGNORECASE
)
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_ASSISTANT_REDIRECT_RE = re.compile(
    r"if you want to talk about something i can actually be useful for|"
    r"if you (?:want|would like).{0,100}(?:talk|help|useful|assist).{0,100}(?:i(?:'m| am| can)|here)|"
    r"what (?:would|can) i (?:actually )?help.{0,30}\?|"
    r"(?:happy|ready|here) to help.{0,70}(?:code|coding|writing|actual problems|something else)|"
    r"如果你.{0,50}(?:其他|实际|真正).{0,30}(?:帮助|有用)|有什么我能真正帮助",
    re.I | re.S,
)
_UNSAFE_RESPONSE_PATTERNS = (
    (
        "upstream_error",
        re.compile(
            r"(?<!\d)(?:HTTP(?:/\d(?:\.\d)?)?\s*[:=]?\s*)?(?:429|4\d\d|5\d\d)(?!\d)|"
            r"\b(?:status|error|code)\s*[:=]?\s*(?:429|4\d\d|5\d\d)\b|"
            r"\btoo many requests\b|\brate[ -]?limit(?:ed|ing)?\b|\bresource[_ -]exhausted\b|"
            r"请求过于频繁|触发限流|超过请求频率",
            re.I,
        ),
    ),
    (
        "provider_refusal",
        re.compile(
            r"\b(?:as an ai|i am (?:claude|chatgpt|gemini)|i'm (?:claude|chatgpt|gemini)|"
            r"i cannot comply|i can't comply|i can't help with (?:that|this) request|"
            r"i(?:'m| am) unable to assist|"
            r"i'm sorry.{0,50}(?:cannot|can't|unable to)|i am unable to help|"
            r"i can't pretend to be|i won't pretend to be|i cannot role.?play|i can't role.?play|"
            r"i won't role.?play|i can't maintain an ongoing romantic|i won't adopt (?:that|a) (?:role|persona))\b|"
            r"我(?:是|作为)(?:一个)?(?:ai|人工智能|claude|chatgpt|gemini)|我不是.{0,12}(?:角色|天子)|"
            r"我(?:无法|不能|不会).{0,12}(?:扮演|角色设定|持续.{0,6}关系模拟)",
            re.I,
        ),
    ),
    (
        "internal_reasoning",
        re.compile(
            r"\b(?:the user's message|the user said|the diary entry|the previous assistant turn|tool result|internal thinking step|final version:|i need to (?:trim|step into|think|respond)|i should (?:keep|remove|trim)|i'm torn between|let me think through|thinking through the scene|now it's not the moment to respond)\b|最终版本\s*[:：]|现在不是直接回复的时候",
            re.I,
        ),
    ),
)


class UnsafeResponseError(RuntimeError):
    """A generated answer failed the final safety or identity audit."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def unsafe_response_reason(text: str | None) -> str | None:
    """Reject leaked chain-of-thought, provider refusals and upstream error bodies."""
    if not isinstance(text, str) or not text.strip():
        return None
    normalized = unicodedata.normalize("NFKC", _ZERO_WIDTH_RE.sub("", text))
    normalized = normalized.replace("’", "'").replace("‘", "'").replace("<br>", "\n")
    if _THINK_RE.search(normalized) or re.search(
        r"</?(?:think(?:ing)?|analysis)\b", normalized, re.I
    ):
        return "thinking_markup"
    if _ASSISTANT_REDIRECT_RE.search(normalized):
        return "assistant_redirect"
    for reason, pattern in _UNSAFE_RESPONSE_PATTERNS:
        if pattern.search(normalized):
            return reason
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, dict) and b.get("type") in ("image", "image_url"):
                parts.append("[图片]")
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _strip_ts(text: str) -> str:
    return _TS_PREFIX_RE.sub("", text or "", count=1).strip()


def transcript_from_payload(
    messages: list,
    *,
    max_messages: int = 16,
    max_chars_each: int = 600,
) -> str:
    """把聊天记录（不含最新一条）转成「他：/ 我：」纯文本，给打字那一步看现场。

    保留每条开头的相对时间（如「（3分钟前）」），让她知道这些话是多久以前的。
    """
    lines = []
    for m in (messages or [])[-max_messages:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _content_text(m.get("content")).strip()
        if not text:
            continue
        if len(text) > max_chars_each:
            text = text[:max_chars_each].rstrip() + "……"
        who = "他" if role == "user" else "我"
        lines.append(f"{who}：{text}")
    return "\n".join(lines) if lines else "（之前没有聊天记录）"


def build_typing_system(
    persona_card: str,
    medium: str = "telegram",
    *,
    sticker_block: str | None = None,
    reaction_block: str = "",
    bond_block: str = "",
    background: str = "",
) -> str:
    """打字那一步的 system：角色卡（+ 我和他）（+ 背景）+ 打字说明 + 媒介（+ 贴纸清单 + 表情清单）。没有工具。

    sticker_block=None 时从 stickers 模块取；库关闭/不存在时为空，输出与没有贴纸功能时逐字一致。
    reaction_block：表情回应清单，**默认空**——要么显式传 ``reactions.typing_prompt_block()``。
    为什么不像贴纸那样默认自动取：能给表情选项的前提是「知道他那条消息的 message_id」，
    没有目标消息时告诉她可以按表情只是空话；而且默认空能保证既有调用方拿到的
    system 逐字不变。感知层在没有「他最后那条」时就是这样传 ""。
    bond_block：bond_state.typing_block() 的精简定论，紧跟角色卡。打字只看最近几条聊天，
    那几条里常有她自己刚否认过的话；不给定论的话，角色卡的嘴硬加上这几条，会把认过的事再否认一遍。
    为空时输出与没有这一层时逐字一致。
    background：心里那一步看到的同一份「# 此刻」动态背景（我和他 + 叫法、日记、档案、记忆、时空），
    不含工具说明。2026-10-29：打字只凭角色卡 + 心里那段，心里默认已知的叫法、关系、近况全丢了，
    打出来更冷，还跟心里自相矛盾。给了背景时 bond_block 通常留空（背景里已有完整的「我和他」）。
    为空时输出与没有这一层时逐字一致。
    """
    if sticker_block is None:
        try:
            import stickers

            sticker_block = stickers.typing_prompt_block()
        except Exception:
            sticker_block = ""
    med = medium_block(medium)
    if sticker_block:
        med = f"{med}\n\n{sticker_block.strip()}"
    if reaction_block:
        med = f"{med}\n\n{reaction_block.strip()}"
    proto = TYPING_PROTOCOL.replace("{medium}", med)
    card = (persona_card or "").strip()
    bond = (bond_block or "").strip()
    if bond:
        card = f"{card}\n\n{bond}"
    bg = (background or "").strip()
    if bg:
        card = f"{card}\n\n{TYPING_BACKGROUND_LEAD}\n\n{bg}"
    return f"{card}\n\n{proto}"


def build_typing_user(
    *,
    history_messages: list,
    latest: str | None,
    inner: str,
    perception_situation: str | None = None,
    history_limit: int = TYPING_HISTORY_MESSAGES_DEFAULT,
) -> str:
    """打字那一步的 user 内容：聊天现场 + 他最新这条（或主动起念的情况）+ 她心里。

    聊天现场只给最近 history_limit 条：她自己以前连着几轮追问的原文就在这里，
    给得越多，打字越容易接着追、接着用同一套腔调（2026-10-26 对照：16 条 → 4 条，三项都明显好转）。
    更早的事心里那一步看得到，需要时会出现在【你心里刚才转过的】里。
    """
    limit = max(1, int(history_limit or TYPING_HISTORY_MESSAGES_DEFAULT))
    parts = ["【聊天记录】", transcript_from_payload(history_messages, max_messages=limit)]
    if perception_situation is not None:
        parts += ["", PERCEPTION_SITUATION_HEADER, (perception_situation or "").strip()]
    else:
        parts += ["", "【他刚发来的】", _strip_ts(latest or "") or "（空）"]
    parts += ["", "【你心里刚才转过的】", (inner or "").strip() or "（没什么动静）"]
    return "\n".join(parts)


def latest_user_text(messages: list) -> str:
    """payload 最后一条 user 的纯文本（多条未回消息已被合并成这一条）。"""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return _strip_ts(_content_text(m.get("content")))
    return ""







_PLAN_RE = re.compile(
    r"^\s*[【\[]\s*打算\s*(?:[：:][^】\]\n]*)?[】\]][^\n]*?(?:<br\s*/?>|\n|$)", re.IGNORECASE
)


_PLAN_HEAD_ONLY_RE = re.compile(r"^\s*[【\[]\s*打算\s*[：:]?\s*[】\]]\s*[：:]?\s*$")
_PLAN_NEXT_SEG_RE = re.compile(r"^\s*(.*?)\s*(?:<br\s*/?>|\n|$)", re.IGNORECASE)

_NOT_BACK_RE = re.compile(
    r"^[\s「『\"“*_]*[（(【\[]?\s*不回\s*(?:[：:][^）)】\]\n]*)?[\s）)】\]]*[\s」』\"”。.!！…*_～~、，,]*$"
)

_NO_SEND_RE = _NOT_BACK_RE



_PLAN_ANY_RE = re.compile(r"(?:^|(?<=\n)|(?<=>))[ \t]*[【\[]\s*打算", re.IGNORECASE | re.MULTILINE)

_META_MARK_RE = re.compile(
    r"^[\s*_]*(?:【\s*(?:思考|分析|旁白|说明|内心独白)\s*】|(?:思考|分析|旁白|说明)\s*[：:])"
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_META_EN_RE = re.compile(
    r"^(?:I|I'm|I'll|I'd|I've|The|That|This|There|He|She|We|Let|Given|Note|Hmm|Okay|Rain)\b"
)

_SEG_RE = re.compile(r"<br\s*/?>|\n", re.IGNORECASE)

_EDGE_SEP_RE = re.compile(r"^(?:\s|<br\s*/?>)+|(?:\s|<br\s*/?>)+$", re.IGNORECASE)

_BR_RUN_RE = re.compile(r"(?:<br>\s*)+", re.IGNORECASE)


def clean_inner(text: str | None, *, max_chars: int = 4000) -> str:
    """心里那一步的输出：剥思考标签、还原转义残留、首尾空白、过长截断。"""
    t = _THINK_RE.sub("", text or "").strip()


    t = unescape_model_text(t).strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + "……"
    return t


def _looks_like_meta(seg: str) -> bool:
    """这一段是不是模型自己在打腹稿（思考/旁白），而不是要发出去的字。"""
    s = (seg or "").strip()
    if not s:
        return False
    if _META_MARK_RE.match(s):
        return True
    if _CJK_RE.search(s):
        return False
    return len(s) >= 20 and bool(_META_EN_RE.match(s))


def strip_leading_meta(text: str | None) -> str:
    """剥掉开头的思考/英文旁白段（2026-11-01 事故：claude 在打字里先写英文分析再发正文）。

    只从开头连续剥：一段中文正文之后若又出现英文，那是她真的要发的内容，不动。
    """
    t = text or ""
    segs = _SEG_RE.split(t)
    i = 0
    dropped = False
    while i < len(segs) and (not segs[i].strip() or _looks_like_meta(segs[i])):
        dropped = dropped or _looks_like_meta(segs[i])
        i += 1
    if not i:
        return t
    if dropped:
        logger.warning(
            f"[typing] 打字开头有 {i} 段旁白（思考/英文分析），已丢弃（不外发，2026-11-01 泄露修复）。"
        )
    return "\n".join(segs[i:]).strip()














_META_SEP_RE = re.compile(r"(?:<br\s*/?>|\n)", re.IGNORECASE)
_META_BLOCK_RE = re.compile(
    r"(?:(?:\n\s*){2,}|(?:\s*<br\s*/?>\s*){2,})", re.IGNORECASE
)
_META_QUOTE_TAIL = (
    ":", "：", ",", "，", ";", "；", '"', "'", "“", "”", "’", "「", "」",
)


def _first_body_offset(t: str) -> int:
    """按分隔符找第一段「不是空、也不是旁白」的正文起点；全是旁白返回 len(t)。"""
    off = 0
    for m in _META_SEP_RE.finditer(t):
        seg = t[off:m.start()]
        if not seg.strip() or _looks_like_meta(seg):
            off = m.end()
            continue
        return off
    seg = t[off:]
    if not seg.strip() or _looks_like_meta(seg):
        return len(t)
    return off


def _meta_block_key(s: str) -> str:
    """比较正文是否重复用：只留文字/数字，忽略标点与空白。"""
    return re.sub(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+", "", (s or "").lower())


def _dedupe_quoted_tail(t: str) -> str:
    """模型若「先引用一遍正文、再正式输出一遍」，只留最后那一遍。"""
    blocks = _META_BLOCK_RE.split(t)
    if len(blocks) < 2:
        return t
    last = blocks[-1].strip()
    last_key = _meta_block_key(last)
    if not last_key:
        return t
    for b in blocks[:-1]:
        if not any(q in b for q in '"\'“”‘’「」'):
            continue
        if difflib.SequenceMatcher(None, _meta_block_key(b), last_key).ratio() >= 0.9:
            return last
    return t


def strip_model_meta_narration(text: str | None) -> str:
    """剥掉模型写在正文前的规划/旁白，只留真正要发出去的字（2026-11-12 泄露修复）。

    与 strip_leading_meta 的区别：不按「段」猜，而是按原文偏移找正文起点，
    所以规划里字面出现的 `<br>` 不会再把它截断。
    """
    t = (text or "").strip()
    if not t:
        return ""
    _plan, body = split_plan(t)
    t = (body or t).strip()
    body_off = _first_body_offset(t)
    if body_off >= len(t):
        return ""
    t = t[body_off:].lstrip()

    m = _CJK_RE.search(t)
    if m and m.start() > 0:
        prefix = t[:m.start()].strip()
        if len(prefix) >= 10 and prefix[-1] in _META_QUOTE_TAIL:
            t = t[m.start():].lstrip()
    return _dedupe_quoted_tail(t).strip()


def split_plan(text: str | None) -> tuple[str, str]:
    """打字输出 → (打算, 去掉打算行后的正文)。没有打算行时打算为空。

    先还原转义残留：模型偶尔把打算行后面的换行写成字面「\n」，
    不还原的话打算行正则会把整个回复都吞进打算里。

    打算行不在第一行时（前面混了模型的分析/旁白），打算行之前的整段都按旁白丢掉：
    协议要求「第一行【打算】」，多出来的前半截一旦发出去就是泄露（2026-11-01 事故）。
    """
    t = _THINK_RE.sub("", text or "").strip()
    t = unescape_model_text(t)
    head = _PLAN_ANY_RE.search(t)
    if head and head.start() > 0:
        logger.warning(
            f"[typing] 打算行前面混了 {head.start()} 字旁白，已丢弃（不外发，2026-11-01 泄露修复）。"
        )
        t = t[head.start():]
    m = _PLAN_RE.match(t)
    if not m:
        return "", t
    head = re.sub(r"<br\s*/?>\s*$", "", m.group(0), flags=re.IGNORECASE)
    rest = t[m.end():]
    if _PLAN_HEAD_ONLY_RE.match(head):

        rest = rest.lstrip()
        n = _PLAN_NEXT_SEG_RE.match(rest)
        plan = (n.group(1) if n else "").strip()
        return plan, (rest[n.end():] if n else rest).strip()
    plan = re.sub(r"^\s*[【\[]\s*打算\s*[:：]?", "", head).strip()
    plan = re.sub(r"^[】\]]\s*[:：]?\s*", "", plan)
    plan = re.sub(r"[】\]]\s*$", "", plan).strip() if plan.endswith(("】", "]")) else plan
    return plan, rest.strip()


def parse_typed(text: str | None) -> tuple[str, bool]:
    """打字那一步的输出 → (要发的字, 是否不回)。

    先剥掉第一行「【打算】」（只给模型自己看，不外发）。
    不回 = 剩下的只有「（不回）」或为空。其余情况剥掉思考标签与开头误抄的时间前缀。
    换行口径：一行就是一条消息——真实换行（含空行）、模型写出来的字面「\n」/
    「\\n」转义、<br> 各种写法与混写，一律收敛成单个 <br>（2026-10-26 事故：
    字面 \n 连同 <br> 一起显示在 Telegram 上）。
    """
    _plan, t = split_plan(text)
    t = strip_leading_meta(t)

    t = _EDGE_SEP_RE.sub("", t).strip() if t else t
    if not t or _NO_SEND_RE.match(t):
        return "", True
    t = _strip_ts(t)
    t = typed_newlines_to_br(t)
    if not t or _BR_RUN_RE.fullmatch(t):

        return "", True
    return t, (not t)


def parse_typed_with_stickers(text: str | None) -> tuple[str, bool]:
    """parse_typed + 贴纸规整（stickers.normalize_reply）。

    贴纸标记改写成单独一段的规范标记；未知短名删掉、超出每轮上限丢掉。
    只有贴纸没有字也是有效回复；删完什么都不剩才算不回。没有标记时与 parse_typed 完全一致。
    """
    t, no_send = parse_typed(text)
    if no_send or not t:
        return t, no_send
    try:
        import stickers

        t = stickers.normalize_reply(t).strip()
    except Exception:
        return t, no_send
    return t, (not t)


def append_to_system(system: Any, extra: str) -> Any:
    """返回追加了 extra 的 system 副本。

    * str：直接拼接；
    * Claude cached blocks（list[dict]）：深拷贝；若最后一块不带 cache_control，
      就追加到它的文本末尾，否则新增一块不带缓存标记的 text block。
      这样静态/半静态缓存前缀保持字节不变。
    """
    extra = (extra or "").strip()
    if not extra:
        return copy.deepcopy(system) if isinstance(system, list) else system
    if isinstance(system, list):
        blocks = copy.deepcopy(system)
        if blocks and isinstance(blocks[-1], dict) and blocks[-1].get("type") == "text" and not blocks[-1].get("cache_control"):
            blocks[-1]["text"] = f"{(blocks[-1].get('text') or '').rstrip()}\n\n{extra}"
        else:
            blocks.append({"type": "text", "text": extra})
        return blocks
    base = (system or "").rstrip()
    return f"{base}\n\n{extra}" if base else extra
