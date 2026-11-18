# SPDX-License-Identifier: AGPL-3.0-only






from __future__ import annotations

import json
import re
from typing import Any



HONGTONG_PATTERNS = (
    re.compile(r"红瞳.{0,6}(?:眯|盯|危险|一沉|微)"),
    re.compile(r"瞳孔.{0,6}(?:缩|收|竖|危险|一沉|微)"),
    re.compile(r"(?:红色|猩红|赤红)的眼(?:睛|眸).{0,6}(?:眯|盯|危险|一沉|微)"),
)






_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


_CJK_GAP_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff][ \u3000]+"
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)


_MARTIAN_CONFUSABLES = frozenset("茬莪丕扌缇寰舛")
_MARTIAN_SOFT_COUNT = 4
_MARTIAN_SOFT_RATIO = 0.05
_MARTIAN_SOFT_GAPS = 3

_MARTIAN_HARD_COUNT = 6
_MARTIAN_HARD_RATIO = 0.12
_MARTIAN_HARD_COUNT2 = 8
_MARTIAN_HARD_RATIO2 = 0.04
_MARTIAN_HARD_GAPS = 6


def _is_common_simplified_char(ch):
    """该汉字能否用 GB2312 编码（≈ 常用简体字集）；异常按常见字处理。"""
    try:
        ch.encode("gb2312")
        return True
    except UnicodeEncodeError:
        return False
    except Exception:
        return True


def detect_martian_text(text):
    """检测形近字/繁体/异体替换（「火星文」）与汉字间空格。

    返回 ``{"hit", "level", "count", "ratio", "spaced", "samples"}``：

    - ``count``：非 GB2312 常用简体的汉字数 + 形近替代字（茬/莪/丕…）出现数；
    - ``ratio``：``count`` / 全文汉字数；
    - ``spaced``：汉字之间插空格的处数；
    - ``level``：``"hard"``（必须撤回重写）、``"soft"``（值得重写）、``""``。

    任何异常 → ``hit=False``（纯函数软失败，绝不阻断回复）。
    """
    empty = {
        "hit": False,
        "level": "",
        "count": 0,
        "ratio": 0.0,
        "spaced": 0,
        "samples": "",
    }
    try:
        if not isinstance(text, str) or not text:
            return empty
        cjk = _CJK_RE.findall(text)
        if not cjk:
            return empty
        suspicious = [ch for ch in cjk if not _is_common_simplified_char(ch)]
        confusables = [ch for ch in cjk if ch in _MARTIAN_CONFUSABLES]
        count = len(suspicious) + len(confusables)
        ratio = count / float(len(cjk))
        spaced = len(_CJK_GAP_RE.findall(text))
        soft = (
            count >= _MARTIAN_SOFT_COUNT and ratio >= _MARTIAN_SOFT_RATIO
        ) or spaced >= _MARTIAN_SOFT_GAPS
        hard = (
            (count >= _MARTIAN_HARD_COUNT and ratio >= _MARTIAN_HARD_RATIO)
            or (count >= _MARTIAN_HARD_COUNT2 and ratio >= _MARTIAN_HARD_RATIO2)
            or spaced >= _MARTIAN_HARD_GAPS
        )
        level = "hard" if hard else ("soft" if soft else "")
        return {
            "hit": bool(level),
            "level": level,
            "count": count,
            "ratio": round(ratio, 4),
            "spaced": spaced,
            "samples": "".join(dict.fromkeys(suspicious + confusables))[:24],
        }
    except Exception:
        return empty


_TAG_RE = re.compile(r"<\s*/?\s*(?:budget|reply)\s*>", re.IGNORECASE)
_BUDGET_RE = re.compile(r"<\s*budget\s*>(.*?)<\s*/\s*budget\s*>", re.I | re.S)
_REPLY_RE = re.compile(r"<\s*reply\s*>(.*?)<\s*/\s*reply\s*>", re.I | re.S)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.I | re.S)



_UNCLOSED_BUDGET_RE = re.compile(r"<\s*budget\s*>.*?(?=<\s*reply\s*>|$)", re.I | re.S)


def _strip_unclosed_budget(text):
    """删掉未闭合的 <budget> 段；非字符串原样返回，绝不抛异常。"""
    try:
        if isinstance(text, str) and re.search(r"<\s*budget\s*>", text, re.I)\
                and not re.search(r"<\s*/\s*budget\s*>", text, re.I):
            return _UNCLOSED_BUDGET_RE.sub("", text)
        return text
    except Exception:
        return text


def parse_dual_segment(raw: str) -> tuple[dict | None, str]:
    """解析 ``<budget>`` 与 ``<reply>``；坏 JSON 或任何异常均软失败。

    标签大小写和标签内空白均可容忍。缺少 ``reply`` 时，去掉 budget 段后
    的其余内容视为正文；未闭合的 budget 段整段丢弃（防账本外泄）；
    失败返回 ``(None, 原文去标记文本)``，绝不抛异常。
    """
    original = raw
    try:
        if not isinstance(raw, str):
            return None, raw
        raw = _strip_unclosed_budget(raw)
        budget_match = _BUDGET_RE.search(raw)
        budget: dict | None = None
        if budget_match:
            parsed = json.loads(budget_match.group(1).strip())
            if not isinstance(parsed, dict):
                raise ValueError("budget JSON 不是对象")
            budget = parsed
        reply_match = _REPLY_RE.search(raw)
        if reply_match:
            reply_text = reply_match.group(1).strip()
        else:
            reply_text = _BUDGET_RE.sub("", raw).strip()


        reply_text = _TAG_RE.sub("", reply_text).strip()
        if budget_match is None:

            return None, reply_text
        return budget, reply_text
    except Exception:
        try:
            cleaned = _TAG_RE.sub(
                "", _BUDGET_RE.sub("", _strip_unclosed_budget(original if isinstance(original, str) else ""))
            )
            return None, cleaned.strip()
        except Exception:
            return None, original


def _sentences(text: str) -> list[str]:

    return [part for part in re.split(r"[。！!；;\n]+", text) if part.strip()]


def count_clusters(reply_text: str) -> dict:
    """启发式计数正文中的选择层簇与文体信号。

    连续问号以及同一句内针对同一回应对象的多问合并为 1 个问句簇；命令句
    （如「给我……」「去……」「报……」）按 1 个索取簇计。收表格式的列举
    （如「主食几碗？汤几碗？肚子？」）按独立索取项归并为少数对象簇，而不
    按标点逐个计数。计数仅用于第二意见，异常输入返回零值结构。
    """
    empty = {
        "question_clusters": 0,
        "imperative_clusters": 0,
        "ask_cluster_total": 0,
        "bold_count": 0,
        "stage_direction_count": 0,
        "hongtong_hits": 0,
        "char_len": 0,
    }
    try:
        if not isinstance(reply_text, str):
            return empty
        question_clusters = 0
        imperative_clusters = 0
        for sentence in _sentences(reply_text):
            has_question = bool(re.search(r"[？?]", sentence))
            has_imperative = bool(re.search(r"(?:给我|去(?:给我|把|做|拿|报)?|报(?:上来|一下)?|告诉我|说说|回答)", sentence))
            if has_question:
                question_clusters += 1
            if has_imperative and not has_question:
                imperative_clusters += 1

        ask_total = max(question_clusters, imperative_clusters)

        hongtong_hits = sum(len(pattern.findall(reply_text)) for pattern in HONGTONG_PATTERNS)
        return {
            "question_clusters": question_clusters,
            "imperative_clusters": imperative_clusters,
            "ask_cluster_total": ask_total,
            "bold_count": reply_text.count("**"),
            "stage_direction_count": len(re.findall(r"(?<!\*)\*(?!\*)(?:[^*\n]+)\*(?!\*)", reply_text)),
            "hongtong_hits": hongtong_hits,
            "char_len": len(reply_text),
        }
    except Exception:
        return empty


def detect_style_violations(reply_text: str) -> list[str]:
    """返回去重后的 ``bold``、``stage_direction``、``hongtong`` 违规类型。"""
    try:
        if not isinstance(reply_text, str):
            return []
        violations: list[str] = []
        if "**" in reply_text:
            violations.append("bold")
        if re.search(r"(?<!\*)\*(?!\*)(?:[^*\n]+)\*(?!\*)", reply_text):
            violations.append("stage_direction")
        if any(pattern.search(reply_text) for pattern in HONGTONG_PATTERNS):
            violations.append("hongtong")
        return violations
    except Exception:
        return []


def evaluate_limits(counts: dict, info_max: int, sig_max: int, ask_max: int) -> list[str]:
    """按信息票、仪式签名、索取簇阈值返回超标描述。"""
    try:
        info = counts.get("info_ticket_count", counts.get("bite_count", counts.get("bite", 0)))
        if isinstance(info, (list, tuple)):
            info = len(info)
        sig = counts.get("signature_count", counts.get("signature", 0))
        if isinstance(sig, str):
            sig = 1 if sig.strip() else 0
        ask = counts.get("ask_cluster_total", counts.get("ask_count", 0))
        result: list[str] = []
        if int(info) > info_max:
            result.append(f"information tickets exceed limit: {info}>{info_max}")
        if int(sig) > sig_max:
            result.append(f"signatures exceed limit: {sig}>{sig_max}")
        if int(ask) > ask_max:
            result.append(f"solicitation clusters exceed limit: {ask}>{ask_max}")
        return result
    except Exception:
        return []


def parse_evaluator_verdict(raw: str) -> dict | None:
    """从前后杂文或 markdown JSON 栅栏中提取对象裁决；坏 JSON 返回 ``None``。"""
    try:
        if not isinstance(raw, str):
            return None
        candidates = _JSON_FENCE_RE.findall(raw) or [raw]
        for candidate in candidates:
            text = candidate.strip()
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end <= start:
                    continue
                value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        return None
    except Exception:
        return None
