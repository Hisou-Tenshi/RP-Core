# SPDX-License-Identifier: AGPL-3.0-only








import logging
import os
import re
import threading
from contextvars import copy_context
from _rp_cancellation import CallCancelled, ensure_active
import time

import requests
from error_text import format_exception_inline
from llm_tool_text import has_tool_call_markup, strip_tool_call_markup
from message_split import describe_split, newlines_to_br, split_reply, unescape_model_text
import stickers
import reactions



_TYPING_REFRESH_SEC = 4.0


def normalize_final_newlines(text: str | None) -> str:
    """最终外发前的换行收敛：真实换行一律变成 ``<br>``（空行 = 分两条发）。

    模型给的修正稿/覆写文本常带 ``\n\n``；不收敛的话 split_reply 看不到
    ``<br>``，整段会挤成一条消息。
    """
    if not isinstance(text, str):
        return ""
    return newlines_to_br(text)


logger = logging.getLogger("TenshiReply")










_MARKUP_TAG_RE = re.compile(
    r"<\s*(/?)\s*(?:\w+:)?(tool_?calls?|function_?calls?|tool_?call|function_?call|invoke|parameter|tool_?use|budget)\b",
    re.IGNORECASE,
)
_STREAM_HOLD_MAX = 4000





_BUDGET_BLOCK_RE = re.compile(r"<\s*budget\s*>.*?<\s*/\s*budget\s*>", re.IGNORECASE | re.DOTALL)
_REPLY_TAG_RE = re.compile(r"<\s*/?\s*reply\s*>", re.IGNORECASE)


def strip_choice_layer_markup(text: str) -> str:
    """剥掉 budget 段与 reply 外壳标签。

    已闭合的整段删除；**未闭合**的从 <budget> 起删到 <reply>（有则保留其后）
    或整段丢弃——模型被截断时宁可这轮没有账本，也不能把预算 JSON 发给对方。
    """
    if not text or "<" not in text:
        return text
    cleaned = _BUDGET_BLOCK_RE.sub("", text)
    if re.search(r"<\s*budget\s*>", cleaned, re.IGNORECASE):
        cleaned = re.sub(
            r"<\s*budget\s*>.*?(?=<\s*reply\s*>|$)", "", cleaned, flags=re.IGNORECASE | re.DOTALL
        )


    cleaned = re.sub(r"<\s*/?\s*(?:budget|reply)\s*>", "", cleaned, flags=re.IGNORECASE)
    return cleaned




_KNOWN_TAG_PREFIXES = (
    "toolcalls",
    "tool_calls",
    "toolcall",
    "tool_call",
    "functioncalls",
    "function_calls",
    "functioncall",
    "function_call",
    "invoke",
    "parameter",
    "tooluse",
    "tool_use",

    "budget",
    "reply",
)
_PARTIAL_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z_:]*)")



_BR_ANY_RE = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.IGNORECASE)
_BR_DISPLAY_RE = re.compile(r"<\s*/?\s*br\s*/?\s*>\s*", re.IGNORECASE)
_BR_PARTIAL_RE = re.compile(r"<\s*/?\s*b(?:r)?\s*/?\s*$", re.IGNORECASE)


def strip_br_markup(text: str) -> str:
    """去掉换气标记(渲染成换行),用于流式显示与最终展示。

    顺带把模型偶发的转义残留（字面「\\n」/双反斜杠）还原成真实字符——
    显示层是最后经手文本的出口之一，绝不让「\n」两个字原样闪在屏幕上。
    """
    if not text:
        return text
    return _BR_DISPLAY_RE.sub("\n", unescape_model_text(text))


def _hold_partial_br(pending: str) -> tuple[str, str]:
    """末尾是不是半截 <br?是就先攒着,别发出去。"""
    idx = pending.rfind("<")
    if idx == -1:
        return pending, ""
    tail = pending[idx:]
    if ">" in tail:
        return pending, ""
    if _BR_PARTIAL_RE.match(tail):
        return pending[:idx], tail
    return pending, ""


def _looks_like_partial_tag(tail: str) -> bool:
    """末尾这半个东西是不是「正在输入的 (调用) 标签」？"""
    match = _PARTIAL_TAG_RE.match(tail)
    if not match or match.end() != len(tail):
        return False
    name = match.group(2).lower()
    if not name:
        return True
    name = name.split(":")[-1]
    return any(known.startswith(name) for known in _KNOWN_TAG_PREFIXES)


def _clean(text: str) -> str:
    if not text:
        return text
    text = strip_choice_layer_markup(text)
    return strip_tool_call_markup(text) if has_tool_call_markup(text) else text





def _split_streamable(pending: str) -> tuple[str, str]:
    """把待发缓冲切成 (可以安全发出的文本, 继续攒着的文本)。"""
    if not pending:
        return "", ""
    depth = 0
    hold_from: int | None = None
    for match in _MARKUP_TAG_RE.finditer(pending):
        if match.group(1) == "/":
            if depth > 0:
                depth -= 1
            if depth == 0:
                hold_from = None
        else:
            if depth == 0:
                hold_from = match.start()
            depth += 1

    if hold_from is not None:
        head, held = pending[:hold_from], pending[hold_from:]
        if len(held) > _STREAM_HOLD_MAX:

            return _clean(head + held), ""
        return _clean(head), held


    idx = pending.rfind("<")
    if idx != -1:
        tail = pending[idx:]
        if ">" not in tail and _looks_like_partial_tag(tail):
            return _clean(pending[:idx]), tail

    safe, held_br = _hold_partial_br(pending)
    if held_br:
        return _clean(safe), held_br
    return _clean(pending), ""









_DEFAULT_FOLLOWUP_BASE_SEC = 0.8
_DEFAULT_FOLLOWUP_CHARS_PER_SEC = 15.0
_DEFAULT_FOLLOWUP_MAX_SEC = 6.0

_FOLLOWUP_MAX_429_WAITS = 5
_FOLLOWUP_MAX_RETRY_AFTER_SEC = 60.0


def _env_float(name: str, default: float) -> float:
    """读环境变量里的浮点数:没设/空/非法一律回退 default。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _followup_delay_sec(segment: str) -> float:
    """按「这一条气泡的字数」算出发送前要等她打多久(秒)。

    空段不等待(0);第 1 条由调用方直接发,不走这里。
    环境变量 REPLY_FOLLOWUP_INTERVAL_SEC 若被显式设置且可解析,按固定延时处理
    (含 0=不等待),优先于动态公式 —— 兼容旧行为与测试。
    """
    text = (segment or "").strip()
    if not text:
        return 0.0

    legacy_raw = os.getenv("REPLY_FOLLOWUP_INTERVAL_SEC")
    if legacy_raw is not None and str(legacy_raw).strip():
        try:
            return max(0.0, float(legacy_raw))
        except (TypeError, ValueError):
            pass

    base = max(0.0, _env_float("REPLY_FOLLOWUP_BASE_SEC", _DEFAULT_FOLLOWUP_BASE_SEC))
    chars_per_sec = _env_float("REPLY_FOLLOWUP_CHARS_PER_SEC", _DEFAULT_FOLLOWUP_CHARS_PER_SEC)
    if chars_per_sec <= 0:
        chars_per_sec = _DEFAULT_FOLLOWUP_CHARS_PER_SEC
    max_sec = _env_float("REPLY_FOLLOWUP_MAX_SEC", _DEFAULT_FOLLOWUP_MAX_SEC)
    if max_sec < 0:
        max_sec = _DEFAULT_FOLLOWUP_MAX_SEC
    return max(0.0, min(max_sec, base + len(text) / chars_per_sec))


class TelegramStreamer:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.message_id = None
        self.sent_message_ids: list[int] = []
        self.delivery_status = "delivery_unknown"
        self.full_text = ""
        self.last_sent_text = ""
        self.last_update_time = 0
        self.update_interval = 1
        self.is_active = False
        self._lock = threading.Lock()
        self._sending_thread = None
        self._typing_stop = threading.Event()
        self._typing_thread = None




        self.stream_buffer = ""
        self.raw_stream_buffer = ""

        self._pending = ""

    def _send_typing_action(self):
        if not self.token or not self.chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendChatAction"
            ensure_active()
            requests.post(
                url,
                json={"chat_id": self.chat_id, "action": "typing"},
                timeout=5,
            )
        except Exception:
            pass

    def _typing_loop(self):
        while not self._typing_stop.wait(timeout=_TYPING_REFRESH_SEC):
            self._send_typing_action()

    def start_typing(self):
        """Keep Telegram 'typing…' active until stop_typing()."""
        if not self.token or not self.chat_id:
            return
        if self._typing_thread and self._typing_thread.is_alive():
            return
        self._typing_stop.clear()
        self._send_typing_action()
        self._typing_thread = threading.Thread(target=self._typing_loop, daemon=True)
        self._typing_thread.start()

    def stop_typing(self):
        self._typing_stop.set()
        t = self._typing_thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        self._typing_thread = None

    def start(self):
        """开始流式：只起 typing 提示，不再发「Thinking...」占位消息。

        首条真实文本到达时由 _send_edit 懒创建消息（见 _send_new_message），
        在那之前绝不发送任何空文本 / 占位文本。
        """
        if not self.token or not self.chat_id:
            logger.warning("缺少 Telegram token 或 chat_id，已禁用流式输出。")
            return

        self.start_typing()
        self.message_id = None
        self.last_sent_text = ""
        self.is_active = True
        logger.info("Stream started (typing only; message created on first text).")

    def reset_text(self):
        """清空已累积的文本（含 Telegram 侧已显示内容）。

        用于丢弃「错误占位」类文本：这类内容绝不能落盘或被当成回复外发。
        仅清空，不结束流；后续仍可继续 update()。
        占位消息已废弃：这里尽力删掉已创建的那条流式消息并复位 message_id，
        否则下一条正确文本会去编辑一条仍显示着错误内容的消息；
        is_active 保持不变，下一段文本到达时会重新懒创建。
        """
        with self._lock:
            self.full_text = ""
            self.stream_buffer = ""
            self.raw_stream_buffer = ""
            self._pending = ""
            had_message = bool(self.message_id)
            self.last_sent_text = ""

        if had_message:
            try:
                self._delete_current_message()
            except Exception as e:
                logger.warning(f"reset_text 删除消息失败（忽略）：{format_exception_inline(e)}")
        self.message_id = None

    def update(self, chunk):
        """Accumulates text and updates Telegram if interval passed."""
        ensure_active()
        sender_to_start = None
        with self._lock:
            if not self.is_active:
                return

            self.raw_stream_buffer += chunk


            self._pending += chunk
            emit, self._pending = _split_streamable(self._pending)
            if emit:
                self.full_text += emit
                self.stream_buffer += emit
            has_visible_text = bool(self.full_text.strip())
            if has_visible_text:
                current_time = time.time()
                if (
                    current_time - self.last_update_time >= self.update_interval
                    and (self._sending_thread is None or not self._sending_thread.is_alive())
                ):
                    context = copy_context()

                    def send_scoped():
                        try:
                            context.run(self._send_edit)
                        except CallCancelled:
                            pass

                    sender_to_start = threading.Thread(target=send_scoped)
                    self._sending_thread = sender_to_start
                    self.last_update_time = current_time
        if sender_to_start is not None:
            sender_to_start.start()

    def _send_edit(self, max_retries=1):
        """把当前缓冲同步到 Telegram：还没消息就先新建，之后走编辑。"""
        if not self.is_active:
            return

        with self._lock:

            text_to_send = strip_br_markup(self.full_text.strip())


        if not text_to_send:
            return


        if self.message_id is None:
            self._send_new_message(text_to_send, max_retries)
            return


        if text_to_send == self.last_sent_text:
            return

        self._send_legacy(text_to_send, max_retries)

    def _send_new_message(self, text_to_send, max_retries=1):
        """首条真实文本到达时新建消息（Markdown，解析失败降级纯文本）。"""
        if not self.token or not self.chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": text_to_send,
            "parse_mode": "Markdown",
        }
        attempts = max(1, int(max_retries or 1))
        for attempt in range(attempts):
            try:
                ensure_active()
                resp = requests.post(url, json=data, timeout=10)
                if resp.status_code == 200:
                    try:
                        result = resp.json()
                    except Exception:
                        result = {}
                    if result.get("ok"):
                        self.message_id = result["result"]["message_id"]
                        self._remember_sent_message(self.message_id)
                        self.last_sent_text = text_to_send
                        logger.info(f"Stream message created. Message ID: {self.message_id}")
                        return True
                    logger.error(f"Failed to create stream message: {result}")
                    return False


                if resp.status_code == 400 and (
                    "can't parse entities" in resp.text or "Bad Request" in resp.text
                ):
                    logger.debug(f"Markdown parse failed, retrying as plain text. Error: {resp.text}")
                    data.pop("parse_mode", None)
                    ensure_active()
                    resp_retry = requests.post(url, json=data, timeout=10)
                    if resp_retry.status_code == 200:
                        try:
                            result_retry = resp_retry.json()
                        except Exception:
                            result_retry = {}
                        if result_retry.get("ok"):
                            self.message_id = result_retry["result"]["message_id"]
                            self._remember_sent_message(self.message_id)
                            self.last_sent_text = text_to_send
                            logger.info(
                                f"Stream message created (plain text). Message ID: {self.message_id}"
                            )
                            return True
                    logger.warning(f"Stream create retry (plain text) failed: {resp_retry.text}")
                    return False

                logger.warning(
                    f"Stream create failed (Attempt {attempt+1}/{attempts}): "
                    f"{resp.status_code} {resp.text}"
                )
                if attempt < attempts - 1:
                    time.sleep(1.0)
            except Exception as e:
                logger.warning(
                    f"Stream create exception (Attempt {attempt+1}/{attempts}): "
                    f"{format_exception_inline(e)}"
                )
                if attempt < attempts - 1:
                    time.sleep(1.0)
        return False

    def _delete_current_message(self):
        """尽力删除当前流式消息；不动 is_active（供 reset_text 复用）。"""
        if not self.token or not self.chat_id or not self.message_id:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
            data = {"chat_id": self.chat_id, "message_id": self.message_id}
            ensure_active()
            resp = requests.post(url, json=data, timeout=8)
            if resp.status_code == 200:
                try:
                    ok = bool(resp.json().get("ok"))
                except Exception:
                    ok = True
                return ok
            return False
        except Exception:
            return False

    def _send_legacy(self, text_to_send, max_retries=1):
        if not self.message_id:
            return

        url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        data = {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": text_to_send,
            "parse_mode": "Markdown"
        }

        for attempt in range(max_retries):
            try:
                ensure_active()
                resp = requests.post(url, json=data, timeout=8)

                if resp.status_code == 200:
                    self.last_sent_text = text_to_send
                    return


                if resp.status_code == 400 and "message is not modified" in resp.text:
                    self.last_sent_text = text_to_send
                    return


                if resp.status_code == 400 and ("can't parse entities" in resp.text or "Bad Request" in resp.text):
                    logger.debug(f"Markdown parse failed, retrying as plain text. Error: {resp.text}")
                    data.pop("parse_mode", None)

                    ensure_active()
                    resp_retry = requests.post(url, json=data, timeout=8)
                    if resp_retry.status_code == 200:
                        self.last_sent_text = text_to_send
                        return
                    else:
                        logger.warning(f"Stream retry (plain text) failed: {resp_retry.text}")


                logger.warning(f"Stream update failed (Attempt {attempt+1}/{max_retries}): {resp.status_code} {resp.text}")
                if attempt < max_retries - 1:
                    time.sleep(1.0)

            except Exception as e:
                logger.warning(f"Stream update exception (Attempt {attempt+1}/{max_retries}): {format_exception_inline(e)}")
                if attempt < max_retries - 1:
                    time.sleep(1.0)

    def _send_followup(self, text: str, max_retries: int = 2):
        """以新消息发一条后续(用于分条发送的第 2..N 条)。"""
        if not text.strip():
            return None
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        attempt = 0
        rate_limit_waits = 0
        while attempt < max_retries:
            try:
                ensure_active()
                resp = requests.post(url, json=data, timeout=10)
                if resp.status_code == 200:
                    out = resp.json()
                    if out.get("ok"):
                        message_id = out["result"]["message_id"]
                        self._remember_sent_message(message_id)
                        return message_id
                if resp.status_code == 429 and rate_limit_waits < _FOLLOWUP_MAX_429_WAITS:

                    retry_after = 1.0
                    try:
                        params = (resp.json() or {}).get("parameters") or {}
                        retry_after = float(params.get("retry_after", retry_after))
                    except Exception:
                        pass
                    retry_after = min(max(retry_after, 0.5), _FOLLOWUP_MAX_RETRY_AFTER_SEC)
                    rate_limit_waits += 1
                    logger.warning(
                        f"Follow-up send rate-limited (429), waiting {retry_after:.1f}s "
                        f"({rate_limit_waits}/{_FOLLOWUP_MAX_429_WAITS})"
                    )
                    time.sleep(retry_after)
                    continue
                if resp.status_code == 400 and (
                    "can't parse entities" in resp.text or "Bad Request" in resp.text
                ):
                    data.pop("parse_mode", None)
                    ensure_active()
                    resp_retry = requests.post(url, json=data, timeout=10)
                    if resp_retry.status_code == 200:
                        out = resp_retry.json()
                        if out.get("ok"):
                            return out["result"]["message_id"]
                logger.warning(
                    f"Follow-up send failed (Attempt {attempt+1}/{max_retries}): "
                    f"{resp.status_code} {resp.text}"
                )
                if attempt < max_retries - 1:
                    time.sleep(0.8)
            except Exception as e:
                logger.warning(
                    f"Follow-up send exception (Attempt {attempt+1}/{max_retries}): "
                    f"{format_exception_inline(e)}"
                )
                if attempt < max_retries - 1:
                    time.sleep(0.8)
            attempt += 1
        return None

    def finalize(self):
        """Performs a final update with the complete text."""
        return self.finalize_with_text(None)

    def finalize_with_text(self, final_text: str | None):
        """
        在流式结束后，用"最终结果"覆盖并重写 Telegram 消息。

        天子在正文里用 `<br>` 表示"换气/分条"。这里按标记切条:
        第 1 条覆盖已存在的流式消息，其余作为新消息依次发出——
        渲染层看到的就是她连着发了几条，标记本身不外泄。
        没有标记时与旧行为完全一致(只覆盖一条,不新增消息)。
        """
        self.delivery_status = "delivery_unknown"
        try:
            if not self.is_active:
                return


            if self._sending_thread and self._sending_thread.is_alive():
                self._sending_thread.join(timeout=5.0)


            if isinstance(final_text, str):
                with self._lock:
                    self.full_text = final_text

            with self._lock:
                pending_tail = self._pending

                if pending_tail:
                    pending_tail = _clean(pending_tail)
                buffered = self.full_text
                if pending_tail:
                    buffered = buffered + pending_tail
                    self._pending = ""
                    self.full_text = buffered




                unescaped = unescape_model_text(buffered)
                if unescaped != buffered:
                    buffered = unescaped
                    self.full_text = buffered

                # 最终外发前把所有真实换行收敛成 <br>：空行 → 两条消息，单换行 → 下一条。
                # 模型给的修正稿/覆写文本常带空行，不收敛会被当成一条长消息发出去。
                br_ready = normalize_final_newlines(buffered)
                if br_ready != buffered:
                    buffered = br_ready
                    self.full_text = buffered




            if reactions.has_marker(buffered):
                buffered = reactions.strip_markers(buffered)
                with self._lock:
                    self.full_text = buffered
                if not buffered.strip():
                    logger.info("[reaction] 这一轮只有表情回应，不发送任何文字消息。")
                    self.delivery_status = "no_send"
                    return



            if stickers.has_marker(buffered):
                segments = stickers.split_for_send(buffered)
                if not segments:

                    logger.warning("[sticker] 正文只有未知贴纸标记，删掉后无可发内容。")
                    with self._lock:
                        self.full_text = ""
                    return
                if any(stickers.sticker_name_of_segment(seg) for seg in segments):
                    self._finalize_segments_with_stickers(segments)
                    return
            else:
                segments = split_reply(buffered)
            if len(segments) > 1:
                logger.info(f"分条发送：{describe_split(segments)}")

                with self._lock:
                    self.full_text = segments[0]
                self._send_edit(max_retries=3)


                self._ensure_first_segment_sent(segments[0])
                confirmed = bool(self.message_id) and self.last_sent_text == strip_br_markup(segments[0])

                for seg in segments[1:]:
                    delay = _followup_delay_sec(seg)
                    if delay > 0:
                        time.sleep(delay)
                    confirmed = bool(self._send_followup(seg)) and confirmed
                if confirmed:
                    self.delivery_status = "sent"
                return


            if segments:
                with self._lock:
                    self.full_text = segments[0]
            self._send_edit(max_retries=3)
            if segments:
                self._ensure_first_segment_sent(segments[0])
                if self.message_id and self.last_sent_text == strip_br_markup(segments[0]):
                    self.delivery_status = "sent"
        finally:
            self.stop_typing()

    def _send_sticker(self, name: str, *, first: bool, max_retries: int = 2):
        """发一张贴纸：先 choose_sticker，非首段等一个短的固定延时，再 sendSticker。

        失败（含重试用尽 / 429 等太久）就跳过、记日志，**不降级成文字**。返回 message_id 或 None。
        """
        st = stickers.by_name(name)
        if not st or not self.token or not self.chat_id:
            logger.warning(f"[sticker] 贴纸不可用，跳过：{name!r}")
            return None
        try:
            ensure_active()
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendChatAction",
                json={"chat_id": self.chat_id, "action": "choose_sticker"},
                timeout=5,
            )
        except Exception:
            pass
        if not first:
            delay = stickers.send_delay_sec()
            if delay > 0:
                time.sleep(delay)
        url = f"https://api.telegram.org/bot{self.token}/sendSticker"
        data = {"chat_id": self.chat_id, "sticker": st["file_id"]}
        attempt = 0
        rate_limit_waits = 0
        while attempt < max_retries:
            try:
                ensure_active()
                resp = requests.post(url, json=data, timeout=10)
                if resp.status_code == 200:
                    out = resp.json()
                    if out.get("ok"):
                        logger.info(f"[sticker] 已发贴纸：{name}")
                        message_id = out["result"]["message_id"]
                        self._remember_sent_message(message_id)
                        return message_id
                if resp.status_code == 429 and rate_limit_waits < _FOLLOWUP_MAX_429_WAITS:
                    retry_after = 1.0
                    try:
                        params = (resp.json() or {}).get("parameters") or {}
                        retry_after = float(params.get("retry_after", retry_after))
                    except Exception:
                        pass
                    retry_after = min(max(retry_after, 0.5), _FOLLOWUP_MAX_RETRY_AFTER_SEC)
                    rate_limit_waits += 1
                    logger.warning(f"[sticker] sendSticker 被限流（429），等 {retry_after:.1f}s 重试")
                    time.sleep(retry_after)
                    continue
                logger.warning(
                    f"[sticker] sendSticker 失败（{attempt+1}/{max_retries}）：{resp.status_code} {resp.text}"
                )
            except Exception as e:
                logger.warning(
                    f"[sticker] sendSticker 异常（{attempt+1}/{max_retries}）：{format_exception_inline(e)}"
                )
            attempt += 1
            if attempt < max_retries:
                time.sleep(0.8)
        logger.error(f"[sticker] 贴纸 {name!r} 没发出去，跳过（不改发文字）。")
        return None

    def _finalize_segments_with_stickers(self, segments: list) -> None:
        """含贴纸的分条外发。文字段沿用原来的首条覆盖/懒创建 + _send_followup；贴纸段走 sendSticker。

        第一段就是贴纸时不去 edit 文本消息：已有流式文本消息（两步模式下通常没有）就先删掉，
        直接发贴纸；message_id 记成第一条真正发出去的消息（贴纸或文字），落库的 bot_message_id 照旧。
        """
        logger.info(f"分条发送（含贴纸）：{describe_split(segments)}")
        first_sticker = stickers.sticker_name_of_segment(segments[0]) if segments else None
        if first_sticker and self.message_id is not None:
            if not self._delete_current_message():
                logger.warning("[sticker] 首段是贴纸，但删除已有流式文本消息失败（保留它）。")
            self.message_id = None
            self.last_sent_text = ""
        first_sent_id = None
        sent_any = False
        all_sent = True
        for i, seg in enumerate(segments):
            name = stickers.sticker_name_of_segment(seg)
            if name:
                mid = self._send_sticker(name, first=not sent_any)
            elif i == 0:

                with self._lock:
                    self.full_text = seg
                self._send_edit(max_retries=3)
                self._ensure_first_segment_sent(seg)
                mid = self.message_id if self.last_sent_text == strip_br_markup(seg) else None
            else:
                if sent_any:
                    delay = _followup_delay_sec(seg)
                    if delay > 0:
                        time.sleep(delay)
                mid = self._send_followup(seg)
            all_sent = bool(mid) and all_sent
            if mid:
                sent_any = True
                if first_sent_id is None:
                    first_sent_id = mid
        if self.message_id is None and first_sent_id is not None:
            self.message_id = first_sent_id
        if all_sent and sent_any:
            self.delivery_status = "sent"
        if not sent_any:
            logger.error("含贴纸的回复一条都没发出去，本条只落库。")

    def _ensure_first_segment_sent(self, first_segment: str) -> None:
        """首条懒创建失败时用 _send_followup 兜底发出，保证第一条绝不缺席。

        只在 message_id 仍为 None（即 _send_new_message 重试后仍失败）时触发；
        兜底发送**不延时**（第一条不等待），并沿用 _send_followup 的
        429/parse_mode 降级逻辑。兜底成功也回填 message_id/last_sent_text，
        让落库仍能记下这条消息的 bot_message_id。
        """
        if self.message_id is not None:
            return
        fallback_message_id = self._send_followup(first_segment)
        if fallback_message_id:
            self.message_id = fallback_message_id
            self.last_sent_text = first_segment
            logger.warning("首条流式消息创建失败，已用 _send_followup 兜底发出。")
        else:
            logger.error("首条消息创建与兜底发送均失败，本条只落库。")

    def delete_message(self):
        """Best-effort delete the streamed placeholder message."""
        self.stop_typing()
        if not self.is_active or not self.message_id:
            return False
        if self._delete_current_message():
            self.is_active = False
            if self.message_id in self.sent_message_ids:
                self.sent_message_ids.remove(self.message_id)
            self.message_id = None
            self.last_sent_text = ""
            return True
        return False

    def _remember_sent_message(self, message_id):
        if message_id is not None and message_id not in self.sent_message_ids:
            self.sent_message_ids.append(message_id)

    def withdraw(self) -> bool:
        """Delete every Telegram message emitted by this turn."""
        self.stop_typing()
        with self._lock:
            self.is_active = False
        sender = self._sending_thread
        if sender and sender.is_alive() and sender is not threading.current_thread():
            # A send that began just before withdrawal can return a new message ID.
            # Wait for the bounded Telegram request to finish before snapshotting IDs.
            sender.join(timeout=25.0)
            if sender.is_alive():
                logger.error("Cannot confirm withdrawal while a Telegram send is still in flight.")
                return False
        ids = list(dict.fromkeys([*self.sent_message_ids, self.message_id]))
        ids = [message_id for message_id in ids if message_id is not None]
        if not ids:
            return True
        if not self.token or not self.chat_id:
            return False
        success = True
        url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
        for message_id in reversed(ids):
            try:
                response = requests.post(
                    url, json={"chat_id": self.chat_id, "message_id": message_id}, timeout=8
                )
                body = response.json() if response.status_code == 200 else {}
                already_missing = (
                    response.status_code == 400
                    and "message to delete not found" in str(body.get("description") or "").lower()
                )
                success = (bool(body.get("ok")) or already_missing) and success
            except Exception:
                success = False
        if success:
            self.sent_message_ids.clear()
            self.message_id = None
            self.last_sent_text = ""
            self.full_text = self.stream_buffer = self.raw_stream_buffer = self._pending = ""
            self._sending_thread = None
        return success
