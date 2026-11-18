# SPDX-License-Identifier: AGPL-3.0-only
import io
import os
import time
import sys
import logging
import base64
import hashlib
from typing import Any, Dict, List, Optional, Tuple
from logging.handlers import RotatingFileHandler
from cryptography.fernet import Fernet
import tenshi_config

from perception_policy import _perception_bot_answer_is_silence_only

from message_split import newlines_to_br
from llm_tool_text import sanitize_model_text
import json
from datetime import datetime, timezone, timedelta
import re


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def _is_empty_value(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) == 0
    return False

def prune_empty_fields(obj: Any) -> Any:
    """
    递归移除 dict/list 结构中“空值”的键。
    空值定义：None / ""（含纯空白）/ [] / {}。
    """
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            vv = prune_empty_fields(v)
            if _is_empty_value(vv):
                continue
            out[k] = vv
        return out
    if isinstance(obj, list):
        out_list: List[Any] = []
        for it in obj:
            vv = prune_empty_fields(it)
            if _is_empty_value(vv):
                continue
            out_list.append(vv)
        return out_list
    return obj

def _conv_last_time(conv: Dict[str, Any]) -> float:
    hist = conv.get("chat_history") or []
    if not hist:
        return 0

    best = 0
    for m in hist:
        try:
            t = float(m.get("time", 0) or 0)
        except Exception:
            t = 0
        if t > best:
            best = t
    return best

def ensure_tenshi_log_newest_first(data: Any) -> Tuple[Any, bool]:
    """
    确保 tenshi_log.json 的排序为“最新在前”：
    - conversations：按对话中最新消息时间（max msg.time）降序
    - chat_history：按 msg.time 降序
    返回值：(normalized_data, changed_flag)。
    """
    if not isinstance(data, list):
        return data, False

    changed = False
    new_data: List[Dict[str, Any]] = []
    ts_prefix_re = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\+\d+\]\s*")
    for conv in data:
        if not isinstance(conv, dict):
            new_data.append(conv)
            continue



        hist0 = conv.get("chat_history")
        if isinstance(hist0, list) and hist0:
            conv2 = None
            for i, m in enumerate(hist0):
                if not isinstance(m, dict):
                    continue
                ba = m.get("bot_answer")
                if not (isinstance(ba, str) and ba):
                    continue
                new_ba = ts_prefix_re.sub("", ba, count=1)
                if new_ba == ba:
                    continue
                if conv2 is None:
                    conv2 = dict(conv)
                    conv2["chat_history"] = list(hist0)
                m2 = dict(m)
                m2["bot_answer"] = new_ba.lstrip()
                conv2["chat_history"][i] = m2
                changed = True
            if conv2 is not None:
                conv = conv2

        hist = conv.get("chat_history")
        if isinstance(hist, list) and len(hist) >= 2:
            t0 = (hist[0] or {}).get("time", 0)
            t1 = (hist[1] or {}).get("time", 0)
            try:
                if float(t0) < float(t1):

                    conv = dict(conv)
                    conv["chat_history"] = sorted(hist, key=lambda m: (m or {}).get("time", 0), reverse=True)
                    changed = True
            except Exception:
                pass
        new_data.append(conv)

    if len(new_data) >= 2:

        try:
            if _conv_last_time(new_data[0]) < _conv_last_time(new_data[1]):
                new_data = sorted(new_data, key=_conv_last_time, reverse=True)
                changed = True
        except Exception:
            pass

    return new_data, changed


def select_unreplied_run(history: Any) -> List[Dict[str, Any]]:
    """从「最新在前」的 ``chat_history`` 里取出**这一轮需要回复**的未回消息（时间正序）。

    选取规则与历史实现保持一致，唯一的区别是：**感知层记录不算边界**。

    背景（2026-09-16 ca141f1）：感知层定时起念 / 沉默记录的 ``time`` 是「落盘时刻」，
    当一条用户消息还在 Actions 队列里排队时，这条感知层记录可能比它**更晚**，于是
    按 ``time`` 倒序排到了最前面。旧实现「扫到第一条有 bot_answer 或 replied_flag
    的消息就 break」，把感知层记录当成了「已答边界」，紧随其后的用户消息永远选不中：
    回复引擎报 ``No unreplied messages found.``，用户消息被静默丢弃（随后还会被
    llm_trace 兜底误配成感知层那句话，落进这条用户消息的 bot_answer）。

    感知层记录既不是用户消息，也不代表「用户的话已经被回过」，所以这里跳过它继续扫：
    扫描终点仍然由「第一条真正已答/已标记回复的用户消息」决定。
    """
    if not isinstance(history, list):
        return []

    run_newest_first: List[Dict[str, Any]] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue

        if msg.get("perception_event"):
            continue
        if msg.get("rp_delivery_quarantined"):
            # It may already have reached Telegram. Only explicit reconciliation
            # can reopen it; a new user turn must not silently retry this one.
            break
        has_answer = msg.get("bot_answer") and str(msg.get("bot_answer")).strip()
        is_replied_flag = msg.get("replied_flag", False)
        if (not has_answer) and (not is_replied_flag):
            run_newest_first.append(msg)
            continue
        break

    return list(reversed(run_newest_first))


def select_unreplied_messages(
    data: Any,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """在 ``tenshi_log`` 顶层结构里找到「需要回复」的会话与那一串未回消息。

    返回 ``(target_conv, unreplied_msgs)``；没有待回复消息时返回 ``(None, [])``。
    与 :func:`select_unreplied_run` 一样，感知层记录不参与边界判定。
    """
    if not isinstance(data, list):
        return None, []
    for conv in data:
        if not isinstance(conv, dict):
            continue
        run = select_unreplied_run(conv.get("chat_history"))
        if run:
            return conv, run
    return None, []



_RELATIVE_TS_PREFIX_RE = re.compile(r"^（[^）]{1,24}）\s*")


def _normalize_for_back_to_back_dup_check(text: str) -> str:
    """用于检测背靠背重复块：忽略行首相对时间戳前缀（如「（37秒前）」）。"""
    if not isinstance(text, str) or not text:
        return ""
    lines: List[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append(_RELATIVE_TS_PREFIX_RE.sub("", line).strip())
    return "\n".join(lines).strip()


def evaluate_stream_final_consistency(
    streamed_text: str,
    final_text: str,
    *,
    shorter_ratio: float = 0.70,
) -> Dict[str, bool]:
    """比较「已经流式发出」的文本与「最终响应」，判定这轮回复算不算完整。

    背景（2026-09-15 事故）：模型在工具轮里会把同一条回复写两遍——先写整段正文
    （后面可能跟一段被写进正文的工具调用），工具执行完再写一遍收尾/重述。于是
    ``stream_buffer`` 里躺着「正文 + 重述」，而 ``final_response`` 只剩最后那一遍。
    旧口径只判 ``streamed not in final``，把这种**最终文本是流式文本子集**的正常情形
    误判成「流式内容没进最终结果」，于是给落盘回复盖上「未完整生成」的存档尾标，
    还会给用户发一条「回复未能正常完成」的系统报错。

    返回三个布尔量：

    * ``streamed_superset`` —— 最终文本是流式文本的子集（尾部/子串）。
      工具轮收尾、模型自我重述都属于这类：流式内容一个字都不会丢（落盘走
      「流式 ∪ 最终」合并），所以**不算未完整**；但也**不能**拿最终文本去覆盖
      Telegram 上那条更长的流式消息（不缩短已经发出去的内容）。
    * ``missing_stream_content`` —— 两边互不包含：流式内容确实没出现在最终响应里，
      保守判定为「缺内容」。
    * ``shorter_and_incomplete`` —— 最终响应明显更短（不足 ``shorter_ratio``）且互不包含。
    """
    result = {
        "streamed_superset": False,
        "missing_stream_content": False,
        "shorter_and_incomplete": False,
    }
    streamed = (streamed_text or "").strip()
    final = (final_text or "").strip()
    if not streamed or not final:
        return result

    if streamed in final:

        return result

    if final in streamed:

        result["streamed_superset"] = True
        return result

    result["missing_stream_content"] = True
    result["shorter_and_incomplete"] = len(final) < int(len(streamed) * shorter_ratio)
    return result


def dedupe_back_to_back_long_repeat(
    text: str,
    *,
    min_chunk_len: int = 18,
) -> Tuple[str, bool]:
    """
    若最终消息由两段相同的长字符串首尾相接，去掉第二段。
    返回 (清理后文本, 是否发生清理)。
    """
    s = (text or "").strip()
    if len(s) < min_chunk_len * 2:
        return text, False

    n = len(s)
    lo = max(min_chunk_len, int(n * 0.30))
    hi = min(n - min_chunk_len, int(n * 0.70))
    best_split: int | None = None
    best_dist = n + 1

    for split_at in range(lo, hi + 1):
        first_raw = s[:split_at].rstrip()
        second_raw = s[split_at:].lstrip(" \t")
        if second_raw.startswith("\n"):
            second_raw = second_raw.lstrip("\n").lstrip(" \t")

        if len(first_raw) < min_chunk_len or len(second_raw) < min_chunk_len:
            continue

        norm_first = _normalize_for_back_to_back_dup_check(first_raw)
        norm_second = _normalize_for_back_to_back_dup_check(second_raw)
        if len(norm_first) < min_chunk_len or len(norm_second) < min_chunk_len:
            continue
        if norm_first != norm_second:
            continue

        dist = abs(split_at - n // 2)
        if dist < best_dist:
            best_dist = dist
            best_split = split_at

    if best_split is not None:
        return s[:best_split].rstrip(), True

    half = n // 2
    first_raw = s[:half].rstrip()
    second_raw = s[half:].lstrip(" \t")
    if second_raw.startswith("\n"):
        second_raw = second_raw.lstrip("\n").lstrip(" \t")
    if (
        len(first_raw) >= min_chunk_len
        and len(second_raw) >= min_chunk_len
        and _normalize_for_back_to_back_dup_check(first_raw)
        == _normalize_for_back_to_back_dup_check(second_raw)
    ):
        return first_raw, True

    return text, False


def scan_newest_first_until_bool_flip(
    items: List[Dict[str, Any]],
    *,
    field: str,
    desired_value: bool,
    confirm_after_flip_n: int = 0
) -> Tuple[List[Dict[str, Any]], bool, int]:
    """
    扫描一个“最新在前”的列表，返回满足 `item[field] == desired_value` 的前缀片段。

    停止条件：
    - 当首次观察到值 != desired_value（发生一次“翻转”）时，可选择继续向后扫描
      `confirm_after_flip_n` 个元素，用于确认翻转稳定（不会跳回）。
    - 若在确认窗口内又跳回 desired_value，则视为翻转不稳定，继续扫描。

    返回：(prefix_items, flip_observed, scanned_count)
    """
    if not items:
        return [], False, 0

    prefix: List[Dict[str, Any]] = []
    flip_observed = False
    scanned = 0
    confirm_remaining = 0

    for it in items:
        scanned += 1
        v = bool((it or {}).get(field, False))

        if not flip_observed:
            if v == desired_value:
                prefix.append(it)
                continue
            flip_observed = True
            confirm_remaining = max(0, int(confirm_after_flip_n or 0))
            if confirm_remaining == 0:
                break
            confirm_remaining -= 1
            continue


        if v == desired_value:

            flip_observed = False
            prefix.append(it)
            continue

        if confirm_remaining <= 0:
            break
        confirm_remaining -= 1

    return prefix, flip_observed, scanned

def get_master_key():
    """从源字符串派生一个稳定的 32 字节 URL 安全 Base64 密钥。"""
    source = tenshi_config.MASTER_KEY_SOURCE


    if source:

        source = source.strip()

        for char in ['"', "'", "\\", "="]:
            source = source.replace(char, "")

        source = source.lower()
    else:

        raise ValueError("灯！！MASTER_KEY_SOURCE 竟然是空的！没它我可没法帮你守护秘密。")


    digest = hashlib.sha256(source.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)

def encrypt_content_wrapped(data):
    """
    用随机生成的消息密钥加密数据，然后用 Master Key 加密这个消息密钥，最后以 Base64 编码的形式返回加密后的数据和加密后的密钥。
    返回格式：{ "data": <enc_data_b64>, "key": <enc_key_b64> }
    """
    try:

        msg_key = Fernet.generate_key()
        f_msg = Fernet(msg_key)


        if isinstance(data, str):
            data = data.encode('utf-8')
        encrypted_data = f_msg.encrypt(data)


        master_key = get_master_key()
        f_master = Fernet(master_key)
        encrypted_key = f_master.encrypt(msg_key)

        return {
            "data": base64.b64encode(encrypted_data).decode('utf-8'),
            "key": base64.b64encode(encrypted_key).decode('utf-8')
        }
    except Exception as e:
        print(f"加密失败：{e}")

        return {
            "data": base64.b64encode(data if isinstance(data, bytes) else data.encode('utf-8')).decode('utf-8'),
            "key": "UNENCRYPTED"
        }

def decrypt_content_wrapped(wrapped_obj):
    """
    解密函数，首先检查是否有加密数据，如果没有则返回一个提示信息。如果有数据但密钥标记为 "UNENCRYPTED"，则直接解码数据并返回。
    如果有加密数据和加密密钥，则先用 Master Key 解密消息密钥，再用解密后的消息密钥解密数据，最后返回解密后的字符串。
    """
    try:
        enc_data_b64 = wrapped_obj.get("data")
        enc_key_b64 = wrapped_obj.get("key")

        if not enc_data_b64:
            return "[Empty Data]"


        if enc_key_b64 == "UNENCRYPTED":
            return base64.b64decode(enc_data_b64).decode('utf-8')


        master_key = get_master_key()
        f_master = Fernet(master_key)

        encrypted_key_bytes = base64.b64decode(enc_key_b64)
        msg_key = f_master.decrypt(encrypted_key_bytes)


        f_msg = Fernet(msg_key)
        encrypted_data_bytes = base64.b64decode(enc_data_b64)
        decrypted_data = f_msg.decrypt(encrypted_data_bytes)

        return decrypted_data.decode('utf-8')

    except Exception as e:
        return f"[Decryption Failed: {e}]"

class FileLock:
    def __init__(self, lock_file_path, timeout=30):
        self.lock_file = lock_file_path + ".lock"
        self.timeout = timeout
        self.check_interval = 0.5

    def __enter__(self):
        start_time = time.time()
        while True:
            try:

                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.close(fd)
                return self
            except FileExistsError:

                try:
                    mtime = os.path.getmtime(self.lock_file)
                    if time.time() - mtime > self.timeout + 60:

                        try:
                            os.remove(self.lock_file)
                            print(f"已移除失效锁文件：{self.lock_file}")
                        except OSError:
                            pass
                        continue
                except FileNotFoundError:

                    continue

                if time.time() - start_time > self.timeout:
                    raise TimeoutError(f"在 {self.timeout}s 内无法获取锁：{self.lock_file}")
                time.sleep(self.check_interval)
            except OSError as e:

                if time.time() - start_time > self.timeout:
                    raise e
                time.sleep(self.check_interval)

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            os.remove(self.lock_file)
        except FileNotFoundError:
            pass

class PollingFilter(logging.Filter):
    """从文件日志中过滤掉轮询相关日志。"""
    def filter(self, record):
        msg = record.getMessage().lower()
        if "polling" in msg or "getupdates" in msg:
            return False
        return True


_actions_console_redaction_installed = False


def actions_console_redaction_enabled() -> bool:
    """GitHub Actions 控制台是否启用脱敏（文件日志不受影响）。"""
    flag = os.getenv("ACTIONS_CONSOLE_REDACT", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return tenshi_config.GITHUB_ACTIONS


def _redact_payload(label: str, body: str) -> str:
    body = body or ""
    n = len(body)
    return f"{label}[已脱敏, {n} 字符]"


def _sanitize_actions_message_body(msg: str) -> str:
    """抹除用户/模型/RAG 正文，保留可读的运维元数据。"""
    if not msg:
        return msg

    block_markers = (
        "Perception Prompt:\n",
        "Search Agent Result Content:\n",
    )
    for marker in block_markers:
        if marker in msg:
            idx = msg.index(marker)
            body = msg[idx + len(marker):]
            return msg[: idx + len(marker)] + _redact_payload("", body)

    single_prefixes = (
        "Model raw output: ",
        "Random trigger generated prompt: ",
        "Analyzing intent for query: ",
        "Executing search_memory tool with query: ",
        "Executing search_web tool with query: ",
    )
    for prefix in single_prefixes:
        if msg.startswith(prefix):
            return prefix + _redact_payload("", msg[len(prefix):])

    if " with input: " in msg and msg.startswith("Executing tool: "):
        head, rest = msg.split(" with input: ", 1)
        return f"{head} with input: {_redact_payload('', rest)}"

    if " Preview: " in msg:
        head, rest = msg.split(" Preview: ", 1)
        return f"{head} Preview: {_redact_payload('', rest)}"

    if "tail_preview=" in msg:
        head, rest = msg.split("tail_preview=", 1)
        return f"{head}tail_preview={_redact_payload('', rest)}"

    if "Remaining: " in msg and "Stripped thinking" in msg:
        head, rest = msg.split("Remaining: ", 1)
        return f"{head}Remaining: {_redact_payload('', rest)}"

    if re.search(r"(?m)^(User|Elaina|天子|Assistant|Human|assistant|user):\s", msg):
        return _redact_payload("对话内容", msg)

    if len(msg) > 500:
        return msg[:120] + f"...[已脱敏, 总长 {len(msg)} 字符]"

    return msg


def sanitize_actions_console_line(text: str) -> str:
    """对单行（可含日志前缀 / 子进程前缀）做 Actions 控制台脱敏。"""
    if not text or not actions_console_redaction_enabled():
        return text

    newline = ""
    if text.endswith("\n"):
        newline = "\n"
        text = text[:-1]
    if text.endswith("\r"):
        newline = "\r\n" if newline else "\r"
        text = text.rstrip("\r")

    proc_prefix = ""
    for p in ("[reply][stderr] ", "[reply] "):
        if text.startswith(p):
            proc_prefix = p
            text = text[len(p):]
            break

    log_prefix = ""
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - [^-]+ - \w+ - )",
        text,
    )
    if m:
        log_prefix = m.group(1)
        text = text[len(log_prefix):]

    sanitized = _sanitize_actions_message_body(text)
    return proc_prefix + log_prefix + sanitized + newline


def install_actions_console_redaction() -> None:
    """包装 stdout/stderr.write，使 print 与子进程转发也走脱敏。"""
    global _actions_console_redaction_installed
    if _actions_console_redaction_installed or not actions_console_redaction_enabled():
        return
    _actions_console_redaction_installed = True

    def _wrap_stream(stream):
        orig_write = stream.write

        def write(data):
            if not data:
                return orig_write(data)
            if isinstance(data, bytes):
                try:
                    text = data.decode(stream.encoding or "utf-8", errors="replace")
                except Exception:
                    return orig_write(data)
                return orig_write(sanitize_actions_console_line(text).encode(stream.encoding or "utf-8", errors="replace"))
            return orig_write(sanitize_actions_console_line(str(data)))

        stream.write = write

    _wrap_stream(sys.stdout)
    _wrap_stream(sys.stderr)


class ActionsConsoleRedactFilter(logging.Filter):
    """Actions 控制台 handler 脱敏（文件 handler 不挂此过滤器）。

    ⚠️ 不要再把它挂到任何 handler 上：Python logging 会把**同一个 LogRecord** 依次交给
    每个 handler，这里 `record.msg = ...` 是**就地改写**，于是控制台被截断/脱敏之后，
    排在后面的文件 handler 写出来的也成了脱敏版——`logs/debug.log` 里出现
    `...[已脱敏, 总长 1136 字符]` 就是这么来的（与 setup_logger 文档里
    「文件始终全量」的说法相反，也让线上排查拿不到完整原文）。
    正确做法见 `_RedactingFormatter`：脱敏放在**控制台 handler 自己的 formatter** 里做，
    只影响这一个 handler 的输出。
    """

    def filter(self, record):
        if actions_console_redaction_enabled():
            record.msg = sanitize_actions_console_line(record.getMessage())
            record.args = ()
        return True


class _BeijingFormatter(logging.Formatter):
    """日志时间戳统一按北京时间（UTC+8，由 TIMEZONE_OFFSET 决定）渲染。

    不依赖进程 TZ / 镜像 tzdata：用 gmtime(created + offset) 手动换算，
    因此 GitHub runner（默认 UTC）和容器里写出的 logs/*.log 都是北京时间。
    """

    def formatTime(self, record, datefmt=None):
        try:
            offset = int(getattr(tenshi_config, "TIMEZONE_OFFSET", 8) or 8)
        except Exception:
            offset = 8
        ct = time.gmtime(record.created + offset * 3600)
        if datefmt:
            return time.strftime(datefmt, ct)
        return time.strftime("%Y-%m-%d %H:%M:%S", ct) + f",{int(record.msecs):03d}"


class _RedactingFormatter(_BeijingFormatter):
    """控制台专用：格式化之后再脱敏，绝不改 LogRecord（因此不影响文件 handler）。"""

    def format(self, record):
        text = super().format(record)
        if actions_console_redaction_enabled():
            return sanitize_actions_console_line(text)
        return text


def setup_logger(name, log_file_path, level=logging.DEBUG):
    """
    配置一个同时输出到控制台与文件的 logger。
    - 控制台：本地全量；GitHub Actions 脱敏后输出（仅系统信息）
    - 文件：输出除轮询/心跳信息以外的全部消息（始终全量）
    - 时间戳：统一北京时间（UTC+8）
    """
    install_actions_console_redaction()

    logger = logging.getLogger(name)
    logger.setLevel(level)


    if logger.hasHandlers():
        logger.handlers.clear()


    formatter = _BeijingFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')


    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    if actions_console_redaction_enabled():

        console_handler.setFormatter(_RedactingFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    else:
        console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)



    log_dir = os.path.dirname(log_file_path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(log_file_path, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)


    file_handler.addFilter(PollingFilter())

    logger.addHandler(file_handler)
    return logger


def append_llm_trace(record: Dict[str, Any], *, trace_path: str | None = None) -> bool:
    """
    追加一条 JSONL 追踪记录。
    默认写入 tenshi_config.LLM_TRACE_PATH（回复侧）；
    log_update 等应传入 trace_path=tenshi_config.SUMMARY_LLM_TRACE_PATH 与回复 trace 脱钩。
    """
    try:
        def _truncate_str(s: str, *, max_len: int = 2000, head: int = 1200, tail: int = 300) -> str:
            if not isinstance(s, str):
                return s
            if len(s) <= max_len:
                return s
            if head + tail + 50 > max_len:
                head = max(200, max_len - 200)
                tail = 0
            suffix = f"\n...[已截断 {len(s) - (head + tail)} 个字符]...\n"
            return s[:head] + suffix + (s[-tail:] if tail else "")

        path = trace_path or getattr(tenshi_config, "LLM_TRACE_PATH", None) or os.path.join(
            tenshi_config.PROJECT_ROOT, "logs", "llm_trace.jsonl"
        )
        log_dir = os.path.dirname(path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)


        if "ts" not in record:
            record["ts"] = time.time()
        if "source" not in record:
            record["source"] = "unknown"


        for k in ["prompt", "system_prompt", "user_prompt"]:
            v = record.get(k)
            if isinstance(v, str) and len(v) > 80:
                record[f"{k}_len"] = len(v)
                record[k] = _truncate_str(v, max_len=180, head=150, tail=0)

        for k in ["response_text"]:
            v = record.get(k)
            if isinstance(v, str) and len(v) > 30000:
                record[f"{k}_len"] = len(v)
                record[k] = _truncate_str(v, max_len=30000, head=22000, tail=4000)

        line = json.dumps(record, ensure_ascii=False)

        with FileLock(path, timeout=30):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception:
        return False



REPLY_SCRIPT_TIMEOUT_SEC = float(os.getenv("REPLY_SCRIPT_TIMEOUT_SEC") or 1260.0)


INCOMPLETE_REPLY_SUFFIX = (
    "\n\n——（以上回复因系统原因未能完整生成，此处为已输出部分的存档。）"
)

_REPLY_FAILURE_REASON_TEXT = {
    "timeout": "回复脚本执行超时（已超过 {timeout_min} 分钟上限）",
    "incomplete": "回复生成未完成（流式输出与最终结果不一致，或生成流程中断）",
    "incomplete_sent": "回复已部分发出到 Telegram，但系统判定尚未完整生成",
    "missing_answer_recovered": "回复脚本已结束，但日志中缺少完整回复；已从流式追踪中抢救部分内容",
    "final_recovered": "回复脚本已结束但内容未落盘；已从追踪日志中找回这一轮的完整回复（未截断）",
    "missing_answer": "回复脚本已结束，但未能生成任何可落盘的回复内容",
    "crash": "回复脚本异常退出",
}


def format_reply_failure_telegram_message(
    *,
    reason: str,
    msg_id=None,
    partial_saved: bool = False,
    partial_chars: int = 0,
    return_code: Optional[int] = None,
    detail: str = "",
) -> str:
    """构造发往 Telegram 的完整报错说明（不落盘）。"""
    timeout_min = int(REPLY_SCRIPT_TIMEOUT_SEC // 60)
    situation = _REPLY_FAILURE_REASON_TEXT.get(reason, "回复生成失败").format(timeout_min=timeout_min)
    if reason == "crash" and return_code is not None:
        situation += f"（退出码 {return_code}）"

    lines = [
        "【系统报错】回复未能正常完成",
        "",
        f"情况：{situation}",
    ]
    if msg_id is not None and str(msg_id).strip():
        lines.append(f"关联消息 ID：{msg_id}")
    if partial_saved:
        n = max(0, int(partial_chars or 0))
        lines.append(f"已抢救落盘：是（约 {n} 字）")
    else:
        lines.append("已抢救落盘：否")

    lines.extend(
        [
            "",
            "说明：",
            "· 若 Telegram 上已有半截回复，请以日志存档为准；未完整回复会在正文末尾标注。",
            "· 详细技术日志见 GitHub Actions / logs/debug.log、logs/llm_trace.jsonl。",
            "· 本条为系统通知，不会写入对话记录。",
        ]
    )
    if detail and str(detail).strip():
        lines.extend(["", f"补充：{str(detail).strip()}"])
    return "\n".join(lines)


def format_no_reply_notice_telegram_message() -> str:
    """她主动选择不回时发往 Telegram 的一行小提醒（不落盘）。

    与「系统报错」严格区分：报错只在链路真的没产出回复时发；主动不回是**正常结果**，
    只发这一行，让对面知道不是掉线/故障。文案见 ``REPLY_NO_REPLY_NOTICE_TEXT``。
    """
    text = str(getattr(tenshi_config, "REPLY_NO_REPLY_NOTICE_TEXT", "") or "").strip()
    if text:
        return text
    return "【系统小提醒】天子这次选择不回你。"

_LLM_TRACE_STREAM_SOURCES = frozenset(
    {
        "tenshi_reply.gemini.stream",
        "tenshi_reply.deepseek.stream",
    }
)





PERCEPTION_TRACE_SOURCE_PREFIX = "perception_layer."
PERCEPTION_TRACE_BEGIN_SOURCE = "perception_layer.begin"
PERCEPTION_TRACE_END_SOURCE = "perception_layer.end"


PERCEPTION_TRACE_MAX_SPAN_SEC = 30 * 60



INNER_TRACE_BEGIN_SOURCE = "tenshi_reply.inner.begin"
INNER_TRACE_END_SOURCE = "tenshi_reply.inner.end"
_EXCLUDED_TURN_BEGIN_SOURCES = frozenset({PERCEPTION_TRACE_BEGIN_SOURCE, INNER_TRACE_BEGIN_SOURCE})
_EXCLUDED_TURN_END_SOURCES = frozenset({PERCEPTION_TRACE_END_SOURCE, INNER_TRACE_END_SOURCE})


def is_reply_marked_incomplete(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return INCOMPLETE_REPLY_SUFFIX.strip() in t


def mark_reply_incomplete(text: str) -> str:
    t = (text or "").rstrip()
    if not t:
        return t
    if is_reply_marked_incomplete(t):
        return t
    return t + INCOMPLETE_REPLY_SUFFIX


def recover_reply_text_from_llm_trace(since_ts: Optional[float] = None) -> Tuple[str, bool]:
    """
    从 llm_trace.jsonl 恢复本轮回复文本（从新到旧扫描，按时间戳拼接 token）。

    返回 ``(文本, 是否为「已完成的完整回复」)``：

    * ``True``  —— 文本取自某次模型调用**已正常收尾**的 ``response_text``。
      这类内容本身就是一条完整回复，只是脚本没来得及落盘；再给它盖「未完整生成」
      尾标会把好好的回复误标成半截（2026-09-15 事故：一笔完整回复与一条 SILENCE
      控制符都被这样盖过章）。调用方应当**照常落盘、但不加尾标**。
    * ``False`` —— 文本是逐字流式 token（``.stream`` 记录）或 final 记录里
      ``streamed_tokens`` 的拼接。脚本被杀/整轮超时时这类内容只写了一半，
      落盘必须带「未完整生成」尾标。

    SILENCE 是感知层的控制符、不是回复正文：命中即跳过该候选，绝不把 ``SILENCE``
    当成一句话存进对话日志或发给用户。

    **感知层轮次隔离（2026-09-16 ca141f1）**：感知层的模型调用与回复引擎共用
    ``invoke_dialog_model_chain``，trace 里同样是 ``tenshi_reply.<provider>.final``。
    感知层在每轮调用前后写 ``perception_layer.begin`` / ``perception_layer.end`` 标记；
    落在 ``[begin, end]`` 区间里的候选一律作废 —— 否则感知层那句主动起念会被当成本轮
    回复，写进用户消息的 ``bot_answer``。区间之外的候选（包括感知层结束之后回复引擎
    自己写下的 trace）照旧按老规则抢救；宁可这次抢救不到、照常报「未找到回复内容」，
    也不能把别人的正文记成这一轮的回答。
    """
    path = getattr(tenshi_config, "LLM_TRACE_PATH", None) or os.path.join(
        tenshi_config.PROJECT_ROOT, "logs", "llm_trace.jsonl"
    )
    if not os.path.exists(path):
        return "", False
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return "", False

    stream_tokens: list[tuple[float, str]] = []

    final_response_candidates: list[tuple[float, str]] = []

    final_stream_candidates: list[tuple[float, str]] = []

    perception_intervals: list[tuple[float, float]] = []
    open_end: Optional[float] = None

    for line in reversed(lines[-8000:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts = float(rec.get("ts") or 0)
        source = str(rec.get("source") or "")


        if source.startswith(PERCEPTION_TRACE_SOURCE_PREFIX) or source in _EXCLUDED_TURN_BEGIN_SOURCES or source in _EXCLUDED_TURN_END_SOURCES:
            if source in _EXCLUDED_TURN_END_SOURCES:
                if open_end is None:
                    open_end = ts
            elif source in _EXCLUDED_TURN_BEGIN_SOURCES:
                if open_end is None:

                    perception_intervals.append((ts, ts + PERCEPTION_TRACE_MAX_SPAN_SEC))
                else:
                    perception_intervals.append((ts, open_end))
                    open_end = None
            continue

        if since_ts is not None and ts < float(since_ts):
            continue

        if source in _LLM_TRACE_STREAM_SOURCES:
            tok = rec.get("token") or rec.get("chunk_text") or ""
            if isinstance(tok, str) and tok:
                stream_tokens.append((ts, tok))
            continue

        if source.endswith(".final") or "streamed_tokens" in rec:
            resp = (rec.get("response_text") or "").strip()

            if resp and _perception_bot_answer_is_silence_only(resp):
                resp = ""
            if resp:
                final_response_candidates.append((ts, resp))
                continue
            streamed = rec.get("streamed_tokens")
            if isinstance(streamed, list) and streamed:
                joined = "".join(str(x) for x in streamed if x).strip()
                if joined and not _perception_bot_answer_is_silence_only(joined):
                    final_stream_candidates.append((ts, joined))

    if open_end is not None:

        perception_intervals.append((float("-inf"), open_end))


    if perception_intervals:
        def _belongs_to_perception_turn(candidate_ts: float) -> bool:
            return any(lo <= candidate_ts <= hi for lo, hi in perception_intervals)

        dropped = sum(
            1
            for bucket in (stream_tokens, final_response_candidates, final_stream_candidates)
            for ts, _ in bucket
            if _belongs_to_perception_turn(ts)
        )
        if dropped:


            logging.getLogger("TenshiLogger").warning(
                "[llm-trace-recover] 窗口内有 %d 段感知层轮次；已丢弃 %d 条落在其中的 trace 候选，"
                "避免把感知层的正文当成本轮回复。",
                len(perception_intervals),
                dropped,
            )
        stream_tokens = [c for c in stream_tokens if not _belongs_to_perception_turn(c[0])]
        final_response_candidates = [
            c for c in final_response_candidates if not _belongs_to_perception_turn(c[0])
        ]
        final_stream_candidates = [
            c for c in final_stream_candidates if not _belongs_to_perception_turn(c[0])
        ]

    if stream_tokens:

        stream_tokens.sort(key=lambda x: x[0])
        return "".join(t for _, t in stream_tokens).strip(), False

    if final_response_candidates:
        final_response_candidates.sort(key=lambda x: x[0])
        return final_response_candidates[-1][1].strip(), True

    if final_stream_candidates:
        final_stream_candidates.sort(key=lambda x: x[0])
        return final_stream_candidates[-1][1].strip(), False

    return "", False


def recover_stream_text_from_llm_trace(since_ts: Optional[float] = None) -> str:
    """兼容包装：只要文本，不要「是否完整」的判据（见 recover_reply_text_from_llm_trace）。"""
    return recover_reply_text_from_llm_trace(since_ts=since_ts)[0]


def persist_partial_bot_answer(
    *,
    msg_id: str,
    user_id,
    bot_answer: str,
    bot_message_id=None,
    reply_incomplete: bool = True,
) -> bool:
    """将部分回复写入 tenshi_log.json（不含 Telegram 报错正文）。"""
    from message_split import normalize_br_markup

    text = normalize_br_markup((bot_answer or "").strip())
    if not text:
        return False
    if reply_incomplete:
        text = mark_reply_incomplete(text)

    log_path = tenshi_config.LOG_FILE_PATH
    lock_path = tenshi_config.LOCK_FILE_PATH
    try:
        with FileLock(lock_path, timeout=30):
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                return False
            data, _ = ensure_tenshi_log_newest_first(data)
            msg_id_s = str(msg_id)
            user_id_s = str(user_id)
            patched = False
            for conv in data:
                if str(conv.get("conversation_id")) != user_id_s and str(conv.get("user_id")) != user_id_s:
                    continue
                for msg in conv.get("chat_history", []):
                    if str(msg.get("msg_id")) != msg_id_s:
                        continue
                    msg["bot_answer"] = text
                    msg.pop("bot_decision", None)
                    if bot_message_id:
                        msg["bot_message_id"] = bot_message_id
                    if reply_incomplete:
                        msg["reply_incomplete"] = True
                    patched = True
                    break
                if patched:
                    break
            if not patched:
                return False
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def format_chat_memory_content(*, ts: float, user_query: Any = None, bot_answer: Any = None) -> str:
    """
    聊天记忆内容与 content_hash 的规范化格式化函数。

    重要：此格式必须保持稳定（不得随意更改），涉及：
    - src/log_update.py（DB 同步 insert/upsert）
    - scripts/rescore_chat_importance.py（DB importance_score 更新）
    - src/migrate_memory.py（首次 DB 重建）
    """
    tz = timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
    try:
        time_str = datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        time_str = datetime.fromtimestamp(time.time(), tz).strftime("%Y-%m-%d %H:%M")

    uq = user_query if isinstance(user_query, str) else ("" if user_query is None else str(user_query))
    ba = bot_answer if isinstance(bot_answer, str) else ("" if bot_answer is None else str(bot_answer))
    uq = uq.strip()
    ba = ba.strip()

    if uq:
        return f"[{time_str}]\nUser: {uq}\n{tenshi_config.CHAT_SCORE_BOT_LABEL}: {ba}"
    if ba:
        return f"[{time_str}]\n{tenshi_config.CHAT_SCORE_BOT_LABEL}: {ba}"
    return ""











_REPLY_QUOTE_MEDIA_LABELS = {
    "photo": "图片",
    "document": "文件",
    "voice": "语音",
    "audio": "音频",
    "sticker": "贴纸",
    "video": "视频",
    "video_note": "圆形视频",
    "animation": "动图",
}


def _reply_quote_timestamp_label(date_value: Any) -> str:
    """把 unix 时间戳按 TIMEZONE_OFFSET 渲染成 `%m-%d %H:%M`；无效/缺失返回空串。"""
    try:
        ts = int(float(date_value))
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        offset = int(getattr(tenshi_config, "TIMEZONE_OFFSET", 8) or 8)
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=offset))).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _truncate_reply_quote_body(text: str, max_chars: int) -> str:
    """截断被引正文并加省略号；只截正文，括号/中文引号由调用方完整拼接。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1] + "…"


def format_reply_quote_block(msg: dict, max_chars: int | None = None) -> str:
    """把一条历史消息里的 `reply_to` 渲染成引用说明块；没有引用返回 ""。

    例：
      （引用她 10-26 13:20 的消息：「碗！谁！刷！的！」）
      （引用他 12:58 的消息片段：「上次说的那个汤」）（时间缺失时省略时间）
      （引用他的图片，10-26 13:20）
    """
    reply_to = msg.get("reply_to") if isinstance(msg, dict) else None
    if not isinstance(reply_to, dict) or not reply_to:
        return ""

    if max_chars is None:
        try:
            max_chars = int(getattr(tenshi_config, "REPLY_QUOTE_MAX_CHARS", 600) or 600)
        except (TypeError, ValueError):
            max_chars = 600
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 600

    from_bot = str(reply_to.get("from") or "").strip().lower() == "bot"
    who = "她" if from_bot else "他"
    when = _reply_quote_timestamp_label(reply_to.get("date"))

    quote_text = reply_to.get("quote_text")
    quote_text = quote_text.strip() if isinstance(quote_text, str) else ""
    body_text = reply_to.get("text")
    body_text = body_text.strip() if isinstance(body_text, str) else ""


    is_partial = bool(quote_text)
    chosen = quote_text or body_text
    if chosen:
        if from_bot:

            chosen = sanitize_model_text(chosen)

        chosen = _truncate_reply_quote_body(chosen, max_chars)
        chosen = newlines_to_br(chosen) if chosen else ""
    if chosen:
        tail = "的消息片段" if is_partial else "的消息"
        if when:
            return f"（引用{who} {when} {tail}：「{chosen}」）"
        return f"（引用{who} {tail}：「{chosen}」）"


    if reply_to.get("has_media"):
        media_type = str(reply_to.get("media_type") or "").strip().lower()
        label = _REPLY_QUOTE_MEDIA_LABELS.get(media_type, "消息")
        if when:
            return f"（引用{who}的{label}，{when}）"
        return f"（引用{who}的{label}）"
    return ""


def render_user_text_with_quote(msg: dict, user_text: str) -> str:
    """把引用块拼到用户消息正文前面（`引用块<br>原文`）；无引用时原样返回。

    主回复的当前用户消息与历史 user turn 都用它；感知层经
    build_messages_payload_from_chat_history 自动覆盖。
    """
    block = format_reply_quote_block(msg if isinstance(msg, dict) else {})
    if not block:
        return user_text
    if not user_text:
        return block
    return f"{block}<br>{user_text}"
