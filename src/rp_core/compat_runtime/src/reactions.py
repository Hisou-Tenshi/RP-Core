# SPDX-License-Identifier: AGPL-3.0-only


























from __future__ import annotations

import logging
import re
from typing import Any

import requests



from config.persona import TYPING_REACTION_PROTOCOL

logger = logging.getLogger("TenshiReply")


MAX_PER_TURN = 1



_MARKER_RE = re.compile(
    r"[〔【\[]\s*(?:反应|表情|回应)\s*[：:]\s*([^〔〕【】\[\]\n<>]{1,16}?)\s*[〕】\]]"
)

_BR_RE = re.compile(r"<\s*/?\s*br\s*/?>|\n", re.IGNORECASE)


_VS16 = "\ufe0f"








CATALOG: dict[str, str] = {
    "👍": "行吧、满意",
    "👎": "不行、嫌弃",
    "🔥": "有点东西",
    "🥰": "被哄到了、心里软了",
    "👏": "夸他",
    "😁": "得意",
    "🤔": "琢磨",
    "🤯": "离谱",
    "😱": "吓一跳",
    "😢": "心疼、难过",
    "🎉": "庆祝",
    "🤩": "眼睛一亮",
    "🙏": "拜托你了",
    "👌": "知道了",
    "🤡": "笑他傻",
    "🥱": "无聊",
    "🥴": "受不了",
    "😍": "稀罕他",
    "💯": "服气",
    "🤣": "笑死",
    "⚡": "来电了",
    "🏆": "我赢了",
    "💔": "伤心",
    "🤨": "不信",
    "😐": "无语",
    "😴": "困了",
    "😭": "委屈",
    "🤓": "别装了",
    "👀": "看着呢",
    "🙈": "不好意思",
    "😇": "装乖",
    "😨": "怕了",
    "🤝": "说定了",
    "🤗": "抱一下",
    "🫡": "遵命",
    "🤭": "偷笑",
    "🤫": "别说了",
    "🤪": "犯傻",
}






def _cfg(name: str, default):
    """读 tenshi_config 上的开关；拿不到（例如单测里没加载配置）就用默认值。"""
    try:
        import tenshi_config

        return getattr(tenshi_config, name, default)
    except Exception:
        return default


def enabled() -> bool:
    return bool(_cfg("REPLY_REACTIONS_ENABLED", True))






def catalog_text() -> str:
    """每行「emoji 含义」，保持 CATALOG 顺序；关掉时返回空串。"""
    if not enabled():
        return ""
    return "\n".join(f"{emoji} {meaning}" for emoji, meaning in CATALOG.items())


def typing_prompt_block() -> str:
    """追加在贴纸说明后面的反应说明 + 清单；关掉时返回空串（提示词逐字不变）。"""
    cat = catalog_text()
    if not cat:
        return ""
    return f"{TYPING_REACTION_PROTOCOL}\n\n{cat}"






def normalize_emoji(raw: str | None) -> str | None:
    """模型写的 emoji → 清单里的规范写法；不在清单里返回 None。"""
    e = str(raw or "").strip().replace(_VS16, "")
    if not e:
        return None
    return e if e in CATALOG else None


def marker(emoji: str) -> str:
    """规范化标记。"""
    return f"〔反应：{emoji}〕"


def has_marker(text: str | None) -> bool:
    """正文里有没有反应标记（含清单外的，那种也要剥掉）。"""
    return bool(text) and bool(_MARKER_RE.search(text))


def _pieces(text: str) -> list[str]:
    """按 <br>/换行切段，去掉空段。"""
    return [p.strip() for p in _BR_RE.split(text or "") if p.strip()]


def parse(text: str | None) -> tuple[str, str | None]:
    """正文 → (剥掉反应标记后的正文, 第一个可用 emoji 或 None)。

    * 标记可以单独一段，也可以混在一句话里（「哈哈〔反应：😂〕你说得对」）——都剥得干净；
    * 清单外的 emoji 一并删掉并 warning，绝不当文字发出去；
    * 一轮只认第一个（超出的 warning 后丢掉）；
    * 没有任何标记时原样返回，逐字不变（不开功能时与旧行为完全一致）。
    """
    if not isinstance(text, str) or not has_marker(text):
        return (text if isinstance(text, str) else ""), None

    chosen: str | None = None
    dropped: list[str] = []

    def _sub(m: re.Match) -> str:
        nonlocal chosen
        emoji = normalize_emoji(m.group(1))
        if emoji is None:
            dropped.append(m.group(1))
            return ""
        if chosen is None:
            chosen = emoji
        else:
            dropped.append(m.group(1))
        return ""

    cleaned = _MARKER_RE.sub(_sub, text)
    if dropped:
        logger.warning(f"[reaction] 丢掉不可用的反应 emoji（清单外或超出每轮上限）：{dropped!r}")
    body = "<br>".join(_pieces(cleaned))
    return body, chosen


def strip_markers(text: str | None) -> str:
    """只剥标记、不关心是什么表情（外发前的最后一道：标记绝不当文字发出去）。"""
    return parse(text)[0]


def reaction_of_text(text: str | None) -> str | None:
    """正文里第一个可用的反应 emoji；没有/不可用返回 None。"""
    return parse(text)[1]






def _as_message_id(message_id: Any) -> int | None:
    try:
        mid = int(str(message_id).strip())
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


def _fail_reason(resp: requests.Response) -> str:
    try:
        return f"{resp.status_code} {str(resp.text)[:300]}"
    except Exception:
        return f"{getattr(resp, 'status_code', '?')}（读不到响应体）"


def send(*, token: str | None, chat_id: Any, message_id: Any, emoji: str | None,
         timeout: float = 10.0) -> bool:
    """在他那条消息上按一个表情（同步，requests）。成功 True，其他一律 False + warning。"""
    if not enabled():
        logger.info("[reaction] 功能已关闭，跳过。")
        return False
    e = normalize_emoji(emoji)
    if not e:
        logger.warning(f"[reaction] emoji 不在清单里，不按：{emoji!r}")
        return False
    mid = _as_message_id(message_id)
    if mid is None:
        logger.warning(f"[reaction] msg_id 不可用（{message_id!r}），这一轮不按表情。")
        return False
    if not token or not chat_id:
        logger.warning("[reaction] 缺少 token / chat_id，这一轮不按表情。")
        return False

    url = f"https://api.telegram.org/bot{token}/setMessageReaction"
    payload = {"chat_id": chat_id, "message_id": mid,
               "reaction": [{"type": "emoji", "emoji": e}]}
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        try:
            out = resp.json() or {}
        except Exception:
            out = {}
        if resp.status_code == 200 and out.get("ok"):
            logger.info(f"[reaction] 已按上 {e}（msg_id={mid}）")
            return True
        logger.warning(f"[reaction] setMessageReaction 失败（忽略，不影响正文）：{_fail_reason(resp)}")
        return False
    except Exception as exc:
        logger.warning(f"[reaction] setMessageReaction 异常（忽略，不影响正文）：{exc!r}")
        return False


async def send_async(bot: Any, *, chat_id: Any, message_id: Any, emoji: str | None) -> bool:
    """异步版（感知层用同一个 Bot 会话）。语义与 send 一致：失败只 warning。"""
    if not enabled():
        logger.info("[reaction] 功能已关闭，跳过。")
        return False
    e = normalize_emoji(emoji)
    if not e:
        logger.warning(f"[reaction] emoji 不在清单里，不按：{emoji!r}")
        return False
    mid = _as_message_id(message_id)
    if mid is None:
        logger.warning(f"[reaction] msg_id 不可用（{message_id!r}），这一轮不按表情。")
        return False
    try:
        from telegram import ReactionTypeEmoji

        await bot.set_message_reaction(
            chat_id=chat_id, message_id=mid, reaction=[ReactionTypeEmoji(emoji=e)]
        )
        logger.info(f"[reaction] 已按上 {e}（msg_id={mid}）")
        return True
    except Exception as exc:
        logger.warning(f"[reaction] 感知层按表情失败（忽略，不影响正文）：{exc!r}")
        return False
