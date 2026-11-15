# SPDX-License-Identifier: AGPL-3.0-only
import logging
import os
import json
from re import match
import re
import random
import time
import sys
import asyncio
import argparse
from typing import Optional, Tuple
from datetime import datetime, timedelta, timezone
from telegram import Bot, constants
from tenshi_utils import (
    FileLock,
    setup_logger,
    decrypt_content_wrapped,
    ensure_tenshi_log_newest_first,
    prune_empty_fields,
    dedupe_back_to_back_long_repeat,
    REPLY_SCRIPT_TIMEOUT_SEC,
    recover_reply_text_from_llm_trace,
    persist_partial_bot_answer,
    is_reply_marked_incomplete,
    format_reply_failure_telegram_message,
    format_no_reply_notice_telegram_message,
)
import tenshi_config
import user_location
from error_text import format_exception_inline
from message_split import newlines_to_br, split_reply
import stickers




TOKEN = tenshi_config.TELEGRAM_TOKEN
BASE_DIR = tenshi_config.PROJECT_ROOT
LOG_FILE = tenshi_config.LOG_FILE_PATH
LOCK_FILE = tenshi_config.LOCK_FILE_PATH
DEBUG_LOG_PATH = tenshi_config.DEBUG_LOG_PATH
DOWNLOAD_DIR = tenshi_config.DOWNLOAD_DIR_PATH



os.makedirs(DOWNLOAD_DIR, exist_ok=True)



logger = setup_logger("TenshiLogger", DEBUG_LOG_PATH, level=logging.DEBUG)


async def _notify_reply_failure(bot: Bot, chat_id: int, **kwargs) -> None:
    """发送报错 Telegram 消息（不落盘）。"""
    text = format_reply_failure_telegram_message(**kwargs)
    if len(text) > 4000:
        text = text[:3990] + "\n…（略）"
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
    except Exception as e:
        logger.error(f"Failed to send reply failure notice: {format_exception_inline(e)}")


def is_intentional_no_send(msg: dict) -> bool:
    """她这轮是否**主动**选择不回（tenshi_reply 落盘的 ``no_send`` 记号）。

    不能拿「bot_answer 为空」来推断：链路崩了/追踪丢失时也是空，那种情况必须照旧
    报错并抢救。只有 ``no_send`` 记号才代表正常结果（2026-11-14 事故：
    主动不回被当成「未找到回复内容」，发了系统报错还从 llm_trace 补发了一句旧正文）。
    """
    if not isinstance(msg, dict):
        return False
    value = msg.get("no_send")
    if value is True:
        return True
    return str(value).strip().lower() in ("true", "1", "yes")


async def _notify_no_reply(bot: Bot, chat_id: int) -> None:
    """主动不回时发一行小提醒（不落盘、不算报错）。"""
    if not bool(getattr(tenshi_config, "REPLY_NO_REPLY_NOTICE_ENABLED", True)):
        logger.info("主动不回的小提醒已关闭（REPLY_NO_REPLY_NOTICE_ENABLED=0）。")
        return
    text = format_no_reply_notice_telegram_message()
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
        logger.info("已发送「主动不回」小提醒。")
    except Exception as e:

        logger.warning(f"发送「主动不回」小提醒失败：{format_exception_inline(e)}")


def _write_runtime_result(status: str) -> None:
    """Write an atomic, private outcome marker for the full-runtime host."""
    path = os.environ.get("RP_RESULT_PATH")
    if not path:
        return
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as result_file:
        json.dump({"status": status}, result_file)
        result_file.flush()
        os.fsync(result_file.fileno())
    os.replace(temporary, path)


def _log_message_is_intentional_no_send(msg_id, user_id) -> bool:
    """子进程非 0 退出时再确认一次：这条消息是不是崩溃前已经落盘了主动不回。"""
    try:
        if not os.path.exists(LOG_FILE):
            return False
        with FileLock(LOCK_FILE, timeout=10):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data, _ = ensure_tenshi_log_newest_first(data)
        for conv in data:
            if str(conv.get("conversation_id")) != str(user_id) and str(conv.get("user_id")) != str(user_id):
                continue
            for msg in conv.get("chat_history", []):
                if str(msg.get("msg_id")) == str(msg_id):
                    return is_intentional_no_send(msg)
    except Exception as e:
        logger.warning(f"检查主动不回记号失败（按普通失败处理）：{format_exception_inline(e)}")
    return False


def _recover_and_persist_partial_reply(
    *,
    msg_id,
    user_id,
    since_ts=None,
    bot_message_id=None,
) -> Optional[Tuple[str, bool]]:
    """从追踪日志里抢救本轮回复并落盘。

    返回 ``(文本, 是否为「完整的 final 响应」)``；没抢救到返回 ``None``。

    只有逐字流式 token（脚本被杀/整轮超时留下的半截）才盖「未完整生成」尾标；
    若捞到的是某次模型调用**已正常收尾**的完整回复，就照常落盘、不加尾标——
    否则一条好好的回复会被误标成半截（2026-09-15 事故）。
    """
    text, is_complete_final = recover_reply_text_from_llm_trace(since_ts=since_ts)
    if not text:
        return None
    ok = persist_partial_bot_answer(
        msg_id=str(msg_id),
        user_id=user_id,
        bot_answer=text,
        bot_message_id=bot_message_id,
        reply_incomplete=not is_complete_final,
    )
    if ok:
        logger.warning(
            "Recovered "
            f"{'complete' if is_complete_final else 'partial'} reply from llm_trace "
            f"(len={len(text)})."
        )
        return text, is_complete_final
    return None


async def _typing_indicator_loop(bot: Bot, chat_id: int, interval_sec: float = 4.0):
    """Refresh Telegram typing action until cancelled (indicator expires after ~5s)."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        raise


async def _stream_subprocess_output(stream: asyncio.StreamReader, write_fn, prefix: str):
    """
    将子进程输出实时转发到 Action 控制台（GITHUB_ACTIONS 下经 stdout 脱敏）。
    重要：write_fn 应为 sys.stdout.write 或 sys.stderr.write（不要用 logger.*），
    否则可能会被重复写入到 logs/debug.log。
    """
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            try:
                text = line.decode("utf-8", errors="replace")
            except Exception:
                text = str(line)
            if prefix:
                write_fn(prefix + text)
            else:
                write_fn(text)
            try:
                if write_fn is sys.stdout.write:
                    sys.stdout.flush()
                elif write_fn is sys.stderr.write:
                    sys.stderr.flush()
            except Exception:
                pass
    except Exception:

        return








def split_reply_into_bubbles(reply_text):
    """
    将最终回复拆分为多个 Telegram 气泡。

    规则：
      * 分割线（`---`）代表一个新的气泡（旧口径）；
      * `<br>` 是她"按一次发送"的标记，现在 bot_answer 落盘会保留它，
        所以这条兜底外发路径同样要按 `<br>` 切条，绝不能把标记本身发出去。
    """
    text = (reply_text or "").strip()
    if not text:
        return []

    parts = [seg.strip() for seg in re.split(r"---", text) if seg and seg.strip()]
    bubbles = []
    for part in parts or [text]:



        bubbles.extend(stickers.split_for_send(part, chunk_long=False))
    if not bubbles and stickers.has_marker(text):
        return []
    return bubbles or [text]

async def _send_markdown_with_fallback(bot, chat_id, text):
    sticker_name = stickers.sticker_name_of_segment(text)
    if sticker_name:

        try:
            await bot.send_sticker(chat_id=chat_id, sticker=stickers.by_name(sticker_name)["file_id"])
            return True
        except Exception as e:
            logger.warning(f"[sticker] 贴纸 {sticker_name!r} 发送失败，跳过：{format_exception_inline(e)}")
            return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
        return True
    except Exception as e:
        logger.error(f"发送回复失败：{format_exception_inline(e)}")
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "bad request" in err_str:
            try:
                logger.warning("Markdown 发送失败，改用纯文本重试。")
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
                return True
            except Exception as e2:
                logger.error(f"发送回复失败（纯文本）：{e2}")
        return False

async def _match_known_sticker(bot, file_id):
    """他发来的贴纸在不在库里：先比 file_id（同一个 bot 拿到的通常一致），
    不中再 getFile 取 file_unique_id 比对（跨 bot 稳定）。库关闭 / 出错一律返回 None（走原有识图）。"""
    try:
        if not stickers.available():
            return None
        hit = stickers.by_file_id(file_id)
        if hit:
            return hit
        f = await bot.get_file(file_id)
        return stickers.by_file_unique_id(getattr(f, "file_unique_id", None))
    except Exception as e:
        logger.warning(f"贴纸比对失败，按普通图片处理：{format_exception_inline(e)}")
        return None


async def download_file(bot, file_id, file_name_hint="file", retries=2):
    '''从 Telegram 下载文件到本地 downloads/。失败时会重试，并校验文件非空。'''
    last_err = None
    for attempt in range(max(1, int(retries))):
        try:
            new_file = await bot.get_file(file_id)

            safe_name = "".join(
                [c for c in file_name_hint if c.isalpha() or c.isdigit() or c in (" ", ".", "_", "-")]
            ).rstrip()
            if not safe_name:
                safe_name = "file"

            save_name = f"{time.time()}_{safe_name}"
            save_path = os.path.join(DOWNLOAD_DIR, save_name)

            await new_file.download_to_drive(save_path)
            if os.path.isfile(save_path) and os.path.getsize(save_path) > 0:
                logger.info(f"文件已下载到：{save_path} ({os.path.getsize(save_path)} bytes)")
                return save_path
            last_err = RuntimeError(f"empty file after download: {save_path}")
            logger.warning(f"下载结果为空，重试 {attempt + 1}/{retries}: {save_path}")
        except Exception as e:
            last_err = e
            logger.warning(f"文件下载失败（尝试 {attempt + 1}/{retries}）: {format_exception_inline(e)}")
            await asyncio.sleep(0.8 * (attempt + 1))
    logger.error(f"文件下载最终失败 file_id={file_id}: {last_err}")
    return None














































































































































_REPLY_TO_TEXT_MAX_CHARS = 1500
_REPLY_TO_QUOTE_MAX_CHARS = 500
_REPLY_TO_MEDIA_TYPES = frozenset(
    {"photo", "document", "voice", "audio", "sticker", "video", "video_note", "animation"}
)


def _reply_to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _reply_to_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if limit > 0 and len(text) > limit:
        text = text[:limit]
    return text


def _reply_to_truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "bot")
    return bool(value)


def parse_reply_to(raw) -> dict | None:
    """把 MSG_REPLY_TO 归一成落盘用的 `reply_to`；坏输入一律当无引用。

    落盘契约：{"message_id": int, "from": "bot"|"user", "date": int,
               "text": str, "quote_text": str, "has_media": bool, "media_type": str}
    - from 由 from_is_bot 决定（true→bot 她，false→user 他）；
    - 空字符串 / 0 / false 字段尽量省略（不塞垃圾键）；
    - 既无 text/quote_text 又无媒体且 message_id<=0 → None。
    """
    try:
        if raw is None:
            return None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped or stripped.lower() == "null":
                return None
            raw = json.loads(stripped)
        if not isinstance(raw, dict):
            return None

        message_id = _reply_to_int(raw.get("message_id"))
        date = _reply_to_int(raw.get("date"))
        text = _reply_to_text(raw.get("text"), _REPLY_TO_TEXT_MAX_CHARS)
        quote_text = _reply_to_text(raw.get("quote_text"), _REPLY_TO_QUOTE_MAX_CHARS)
        media_type = str(raw.get("media_type") or "").strip().lower()
        if media_type not in _REPLY_TO_MEDIA_TYPES:
            media_type = ""
        has_media = _reply_to_truthy(raw.get("has_media")) or bool(media_type)

        if "from_is_bot" in raw:
            from_who = "bot" if _reply_to_truthy(raw.get("from_is_bot")) else "user"
        elif str(raw.get("from") or "").strip().lower() in ("bot", "user"):
            from_who = str(raw.get("from")).strip().lower()
        else:
            from_who = "user"

        if not text and not quote_text and not has_media and message_id <= 0:
            return None

        normalized: dict = {"from": from_who}
        if message_id > 0:
            normalized["message_id"] = message_id
        if date > 0:
            normalized["date"] = date
        if text:
            normalized["text"] = text
        if quote_text:
            normalized["quote_text"] = quote_text
        if has_media:
            normalized["has_media"] = True
        if media_type:
            normalized["media_type"] = media_type
        return normalized
    except Exception as e:
        logger.warning(f"MSG_REPLY_TO 解析失败，按无引用处理：{format_exception_inline(e)}")
        return None


def message_already_replied(history, msg_id):
    """同一条 update 是否已经入档且回复过（重复投递的幂等保护，纯函数）。

    只认「已回复」的条目：上次若在写入后、回复前崩溃（replied_flag 未置位），
    返回 False，让重跑把本该有的回复补上，而不是无声跳过。
    """
    target = str(msg_id)
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get('msg_id')) != target:
            continue
        if str(entry.get('replied_flag', '')).lower() == 'true' or entry.get('bot_answer'):
            return True
    return False


async def process_single_message(text, chat_id, user_id, msg_id, file_id=None, file_type=None, file_name=None, location_data=None, is_random_trigger=False, timestamp=None, reply_to=None):
    '''
    这个函数是整个消息处理流程的核心，负责处理从 Telegram 接收到的单条消息。它的职责包括：
    1. 验证用户身份，确保只有管理员可以触发后续逻辑。
    2. 处理文件下载，如果消息中包含文件（如图片），则调用 download_file 函数将文件保存到本地，并记录相关信息。
    3. 如果消息包含图片，尝试使用 Flash 模型从图片中提取位置信息，并根据置信度决定是否将其作为位置数据。
    4. 解析消息中的位置数据，如果存在，尝试将其转换为可读的地址或坐标格式。
    5. 根据消息内容和提取的信息，构造一个消息对象，并根据是否需要回复来决定是否调用回复函数。
    这个函数还包含了一些关于随机触发的逻辑（目前被注释掉了），以及对日志的详细记录，以便在处理过程中能够追踪每一步的状态和可能出现的问题。
    '''

    delivery_started = False
    if int(user_id) != int(tenshi_config.ADMIN_ID):
        logger.warning(f"未授权访问尝试：{user_id}")
        return "retryable_failure"

    logger.info(f"正在处理来自 {user_id} 的消息。随机触发：{is_random_trigger}，位置：{location_data}")

    bot = Bot(token=TOKEN)
    local_files = []



    tz = timezone(timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
    current_time_obj = datetime.now(tz)
    current_time = current_time_obj.strftime("%Y-%m-%d %H:%M:%S")


    """
    [天子特别指令]
    这段代码被注释掉了，因为天子不希望有一个随机模块在模仿她。
    严禁删除。
    最多仅允许修改部分功能间的连接，让注释后仍在运行的代码逻辑更自洽。
    保留这段黑历史。
    """
    if is_random_trigger:
        logger.info("随机触发已被天子下令禁用。")
        return "no_send"



    file_specs = []
    raw_multi = os.environ.get("MSG_FILE_IDS")
    if raw_multi:
        try:
            parsed = json.loads(raw_multi)
            if isinstance(parsed, list):
                file_specs.extend(parsed)
        except Exception as e:
            logger.warning(f"MSG_FILE_IDS 解析失败，回退单文件: {format_exception_inline(e)}")
    if file_id and not file_specs:
        file_specs.append(
            {"file_id": file_id, "file_type": file_type or "file", "file_name": file_name or "file.bin"}
        )

    sticker_marks = []
    for spec in file_specs:
        fid = (spec or {}).get("file_id")
        if not fid:
            continue
        ftype = (spec or {}).get("file_type") or "file"
        fname = (spec or {}).get("file_name") or f"{ftype}.bin"
        if ftype == "sticker":

            known = await _match_known_sticker(bot, fid)
            if known:
                sticker_marks.append(stickers.incoming_marker(known))
                logger.info(f"认出他发来的贴纸：{known.get('name')}")
                continue
        logger.info(f"正在下载文件：{fid}（{ftype}）")
        path = await download_file(bot, fid, fname)
        if path:
            local_files.append(path)
        else:
            logger.error(f"附件下载失败，本轮可能无法识图: file_id={fid}")

    if sticker_marks:
        text = "<br>".join([t for t in [(text or "").strip()] if t] + sticker_marks)


    coords = user_location.parse_telegram_location_payload(location_data)
    if coords:
        if not timestamp:
            timestamp = int(time.time())
        saved = user_location.save_user_location_from_coords(
            coords[0], coords[1], timestamp
        )
        if not saved:
            logger.warning("定位写入 KB 失败。")
            if not text and not local_files:
                return "retryable_failure"
        if not text and not local_files:
            logger.info("仅定位更新，已写入 KB，跳过日志与回复。")
            return "no_send"

    should_reply = True










    if not text and any(
        str(p).lower().endswith(tenshi_config.IMAGE_FILE_EXTENSIONS) for p in local_files
    ):
        text = tenshi_config.IMAGE_NO_TEXT_FALLBACK
        logger.info("有图无文：已写入固定提示文字，并入主回复流程。")


    conversation_id = None
    if not timestamp:
        timestamp = int(time.time())

    try:
        with FileLock(LOCK_FILE, timeout=30):
            current_logs = []
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, 'r', encoding='utf-8') as f:
                        current_logs = json.load(f)
                except (OSError, ValueError):
                    # A failed read must not silently replace existing private history.
                    raise

            current_logs, _ = ensure_tenshi_log_newest_first(current_logs)

            target_conversation = None
            for conv in current_logs:
                if conv.get('user_id') == user_id:
                    target_conversation = conv
                    break

            if not target_conversation:
                conversation_id = str(user_id)
                target_conversation = {
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "chat_history": []
                }

                current_logs.insert(0, target_conversation)

            conversation_id = target_conversation['conversation_id']



            if (text or local_files) and message_already_replied(
                target_conversation.get('chat_history'), msg_id
            ):
                existing = next(
                    entry for entry in target_conversation.get("chat_history", [])
                    if isinstance(entry, dict) and str(entry.get("msg_id")) == str(msg_id)
                )
                if existing.get("no_send") or str(existing.get("bot_decision", "")).upper() == "SILENCE":
                    return "no_send"
                if existing.get("rp_delivery_quarantined") or existing.get("delivery_status") != "sent":
                    return "delivery_unknown"
                logger.warning(f"重复投递的消息已确认送达，跳过（msg_id={msg_id}）")
                return "already_completed"

            if text or local_files:
                message_entry = {
                    "msg_id": str(msg_id),
                    "time": timestamp,



                    "user_query": newlines_to_br(text) if (text or "").strip() else text,
                    "local_files": local_files,
                }

                if isinstance(reply_to, dict) and reply_to:
                    message_entry["reply_to"] = reply_to

                target_conversation['chat_history'].insert(0, prune_empty_fields(message_entry))


            current_logs, _ = ensure_tenshi_log_newest_first(current_logs)
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                json.dump(current_logs, f, ensure_ascii=False, indent=2)

        logger.info(f"已保存到日志。是否需要回复：{should_reply}")

        if not should_reply:
            return "no_send"


        reply_script_path = os.path.join(BASE_DIR, 'src', 'tenshi_reply.py')
        logger.debug(f"正在调用回复脚本：{reply_script_path}")







        env = os.environ.copy()
        env['MSG_CHAT_ID'] = str(chat_id)

        env["PYTHONUNBUFFERED"] = "1"

        _write_runtime_result("delivery_unknown")
        delivery_started = True
        typing_task = asyncio.create_task(_typing_indicator_loop(bot, chat_id))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", reply_script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )



        stdout_task = asyncio.create_task(_stream_subprocess_output(process.stdout, sys.stdout.write, "[reply] "))
        stderr_task = asyncio.create_task(_stream_subprocess_output(process.stderr, sys.stderr.write, "[reply][stderr] "))

        try:
            await asyncio.wait_for(process.wait(), timeout=REPLY_SCRIPT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            process.kill()
            logger.error("回复脚本执行超时")
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except Exception:
                pass
            recovered = _recover_and_persist_partial_reply(
                msg_id=msg_id,
                user_id=user_id,
                since_ts=timestamp,
            )
            recovered_text = recovered[0] if recovered else ""
            await _notify_reply_failure(
                bot,
                chat_id,
                reason="timeout",
                msg_id=msg_id,
                partial_saved=bool(recovered),
                partial_chars=len(recovered_text),
            )
            return "delivery_unknown"
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

            try:
                await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=5.0)
            except Exception:
                pass

        if process.returncode == 0:



            reply_text = None
            bot_message_id = None
            log_patch_msg_id = None
            reply_incomplete = False

            with FileLock(LOCK_FILE, timeout=10):
                if os.path.exists(LOG_FILE):
                    with open(LOG_FILE, 'r', encoding='utf-8') as f:
                        updated_logs = json.load(f)
                    updated_logs, _ = ensure_tenshi_log_newest_first(updated_logs)

                    target_conv = None
                    for conv in updated_logs:
                        if conv.get('conversation_id') == str(user_id):
                             target_conv = conv
                             break


                    if not target_conv:
                         target_conv = next((c for c in updated_logs if c.get('user_id') == user_id), None)

                    if target_conv:

                        for msg in target_conv.get('chat_history', []):
                            if msg['msg_id'] == str(msg_id):

                                if str(msg.get("bot_decision", "")).upper() == "SILENCE":
                                    bot_message_id = msg.get('bot_message_id')
                                    if bot_message_id:

                                        try:
                                            await bot.delete_message(chat_id=chat_id, message_id=bot_message_id)
                                            logger.info(f"已删除 SILENCE 占位消息（message_id={bot_message_id}）。")
                                        except Exception as e:
                                            logger.warning(f"删除 SILENCE 占位消息失败（message_id={bot_message_id}）：{format_exception_inline(e)}")
                                    logger.info(f"消息 {msg_id} 最终判定为 SILENCE，跳过 Telegram 发送。")
                                    return "no_send"




                                if is_intentional_no_send(msg):
                                    logger.info(f"消息 {msg_id} 是天子主动选择不回（no_send），跳过发送与抢救。")
                                    await _notify_no_reply(bot, chat_id)
                                    return "no_send"


                                if msg.get('bot_answer'):
                                    reply_incomplete = bool(msg.get('reply_incomplete')) or is_reply_marked_incomplete(
                                        msg.get('bot_answer') or ""
                                    )


                                    if msg.get("delivery_status") == "delivery_unknown":
                                        return "delivery_unknown"
                                    if msg.get("delivery_status") == "no_send":
                                        return "no_send"
                                    if msg.get('replied_flag') is True:
                                        if reply_incomplete:
                                            ba = msg.get("bot_answer") or ""
                                            await _notify_reply_failure(
                                                bot,
                                                chat_id,
                                                reason="incomplete_sent",
                                                msg_id=msg_id,
                                                partial_saved=bool(ba.strip()),
                                                partial_chars=len(ba.strip()),
                                            )
                                        logger.info(f"消息 {msg_id} 已由回复脚本标记为已回复，跳过重复发送。")
                                        return "sent"

                                    reply_text = msg['bot_answer']
                                    bot_message_id = msg.get('bot_message_id')


                                    nsfw_payload = msg.get('nsfw_content')
                                    if nsfw_payload:
                                        try:
                                            decrypted_str = decrypt_content_wrapped(nsfw_payload)
                                            try:
                                                nsfw_obj = json.loads(decrypted_str)
                                                if nsfw_obj.get('bot_answer'):
                                                    reply_text = nsfw_obj['bot_answer']
                                                    logger.info("已解密 NSFW 内容用于 Telegram 发送。")
                                            except json.JSONDecodeError:
                                                reply_text = decrypted_str
                                                logger.info("已解密 NSFW 内容（原始字符串）用于 Telegram 发送。")
                                        except Exception as e:
                                            logger.error(f"解密 NSFW 内容失败：{format_exception_inline(e)}。将改发 SFW 版本。")
                                    log_patch_msg_id = str(msg_id)
                                    break

                                elif msg.get('bot_message_id'):
                                    bot_message_id = msg.get('bot_message_id')
                                    logger.warning(f"找到 bot_message_id={bot_message_id} 但缺少 bot_answer，子进程可能中途崩溃。")
                                    recovered = _recover_and_persist_partial_reply(
                                        msg_id=msg_id,
                                        user_id=user_id,
                                        since_ts=timestamp,
                                        bot_message_id=bot_message_id,
                                    )
                                    if recovered:
                                        reply_text = recovered[0]


                                        reply_incomplete = not recovered[1]
                                        if recovered[1]:
                                            await _notify_reply_failure(
                                                bot,
                                                chat_id,
                                                reason="final_recovered",
                                                msg_id=msg_id,
                                                partial_saved=True,
                                                partial_chars=len(recovered[0]),
                                            )

            if reply_incomplete and reply_text:
                await _notify_reply_failure(
                    bot,
                    chat_id,
                    reason="incomplete",
                    msg_id=msg_id,
                    partial_saved=True,
                    partial_chars=len(reply_text),
                )

            if reply_text:
                cleaned_reply, dup_removed = dedupe_back_to_back_long_repeat(reply_text)
                if dup_removed:
                    logger.warning(
                        "Removed back-to-back duplicate from bot_answer before Telegram final edit "
                        f"(len_before={len(reply_text)}, len_after={len(cleaned_reply)})."
                    )
                    reply_text = cleaned_reply
                    if log_patch_msg_id:
                        try:
                            with FileLock(LOCK_FILE, timeout=10):
                                if os.path.exists(LOG_FILE):
                                    with open(LOG_FILE, 'r', encoding='utf-8') as f:
                                        patch_logs = json.load(f)
                                    patch_logs, _ = ensure_tenshi_log_newest_first(patch_logs)
                                    patch_conv = next(
                                        (
                                            c
                                            for c in patch_logs
                                            if c.get('conversation_id') == str(user_id)
                                            or c.get('user_id') == user_id
                                        ),
                                        None,
                                    )
                                    if patch_conv:
                                        for pm in patch_conv.get('chat_history', []):
                                            if pm.get('msg_id') == log_patch_msg_id:
                                                pm['bot_answer'] = reply_text
                                                break
                                    with open(LOG_FILE, 'w', encoding='utf-8') as f:
                                        json.dump(patch_logs, f, ensure_ascii=False, indent=2)
                        except Exception as e:
                            logger.error(f"Failed to persist deduped bot_answer to log: {format_exception_inline(e)}")

            if reply_text:
                bubbles = split_reply_into_bubbles(reply_text)
                if not bubbles:
                    logger.error("回复文本存在，但拆分后没有有效气泡。")
                    return "delivery_unknown"

                delivery_confirmed = True

                if bot_message_id and not stickers.sticker_name_of_segment(bubbles[0]):


                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=bot_message_id,
                            text=bubbles[0],
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                        delivery_confirmed = True
                        logger.info(f"Final reply first bubble edited for {user_id} (msg_id: {bot_message_id})")
                    except Exception as e:
                        logger.error(f"编辑最终消息失败：{format_exception_inline(e)}")
                        err_str = str(e).lower()
                        edited_ok = False
                        if "can't parse entities" in err_str or "bad request" in err_str:
                            try:
                                logger.warning("Markdown 编辑失败，改用纯文本重试。")
                                await bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=bot_message_id,
                                    text=bubbles[0],
                                    parse_mode=None
                                )
                                delivery_confirmed = True
                                logger.info(f"已以纯文本方式编辑最终回复的第一段（用户 {user_id}）")
                                edited_ok = True
                            except Exception as e2:
                                logger.error(f"编辑最终消息失败（纯文本）：{e2}")
                        if not edited_ok:
                            delivery_confirmed = await _send_markdown_with_fallback(
                                bot, chat_id, bubbles[0]
                            ) and delivery_confirmed


                    for bubble in bubbles[1:]:
                        delivery_confirmed = await _send_markdown_with_fallback(
                            bot, chat_id, bubble
                        ) and delivery_confirmed
                else:
                    for idx, bubble in enumerate(bubbles):
                        sent_ok = await _send_markdown_with_fallback(bot, chat_id, bubble)
                        delivery_confirmed = sent_ok and delivery_confirmed
                        if sent_ok and idx == 0:
                            logger.info(f"已发送回复给 {user_id}")
                return "sent" if delivery_confirmed else "delivery_unknown"
            else:
                if bot_message_id:
                     logger.error("回复脚本已结束但未找到回复文本，将保留已有内容（不覆盖）。")
                     recovered = _recover_and_persist_partial_reply(
                         msg_id=msg_id,
                         user_id=user_id,
                         since_ts=timestamp,
                         bot_message_id=bot_message_id,
                     )
                     if recovered:
                         reply_incomplete = not recovered[1]
                         await _notify_reply_failure(
                             bot,
                             chat_id,
                             reason=(
                                 "final_recovered" if recovered[1] else "missing_answer_recovered"
                             ),
                             msg_id=msg_id,
                             partial_saved=True,
                             partial_chars=len(recovered[0]),
                         )
                else:
                     logger.error("回复脚本正常结束，但在日志中未找到回复内容。")
                     recovered = _recover_and_persist_partial_reply(
                         msg_id=msg_id,
                         user_id=user_id,
                         since_ts=timestamp,
                     )
                     if recovered:
                         await _notify_reply_failure(
                             bot,
                             chat_id,
                             reason=(
                                 "final_recovered" if recovered[1] else "missing_answer_recovered"
                             ),
                             msg_id=msg_id,
                             partial_saved=True,
                             partial_chars=len(recovered[0]),
                         )
                     else:
                         await _notify_reply_failure(
                             bot,
                             chat_id,
                             reason="missing_answer",
                             msg_id=msg_id,
                         )
                return "delivery_unknown"
        else:

            err_msg = "回复脚本崩溃（详见上方实时 stderr 输出）"
            logger.error(f"回复脚本崩溃，返回码 {process.returncode}：{err_msg}")


            if _log_message_is_intentional_no_send(msg_id, user_id):
                logger.error(f"消息 {msg_id} 崩溃前已落盘主动不回（no_send），按正常不回处理。")
                await _notify_no_reply(bot, chat_id)
                return "no_send"
            recovered = _recover_and_persist_partial_reply(
                msg_id=msg_id,
                user_id=user_id,
                since_ts=timestamp,
            )
            recovered_text = recovered[0] if recovered else ""
            await _notify_reply_failure(
                bot,
                chat_id,
                reason="crash",
                msg_id=msg_id,
                return_code=process.returncode,
                partial_saved=bool(recovered),
                partial_chars=len(recovered_text),
                detail=err_msg,
            )
            return "delivery_unknown"

    except Exception as e:
        logger.error(f"process_single_message 发生错误：{format_exception_inline(e)}", exc_info=True)
        return "delivery_unknown" if delivery_started else "retryable_failure"

async def debug_run(args):
    """
    本地调试专用接口
    python src/telegram_tenshi_logger.py --debug → 「情况 A」 它会往你的 tenshi_log.json 里塞一条测试消息，但是否触发 reply.py 取决于 process_single_message 原有的 should_reply 判定逻辑（通常如果没有文本和文件，它会被判定为仅位置更新而不触发回复）
    python src/telegram_tenshi_logger.py --debug --force_reply → 「情况 B」 它会直接调用 tenshi_reply.py，强不从 Telegram 读新信息（跳过 Webhook 触发），直接利用本地已经存在的日志上下文，强行让天子生成一次回复并推送到你的手机上（前提是你之前已经有过至少一条消息记录在日志里，这样才有上下文可读）。如果你同时传了 --text 参数，比如
    python src/telegram_tenshi_logger.py --debug --force_reply --text "现在的天气怎么样" → 「情况 B」 强行触发回复，并且存档里这条消息的内容是 "现在的天气怎么样"
    """
    logger.info("【Debug Mode】正在启动本地调试接口...")


    test_chat_id = int(tenshi_config.ADMIN_ID)
    test_user_id = int(tenshi_config.ADMIN_ID)
    test_msg_id = f"debug_{int(time.time())}"

    debug_reply_to = parse_reply_to(getattr(args, "reply_to", None))


    if not args.force_reply:
        test_text = args.text or "这是一条纯调试存档消息"
        logger.info(f"模式：仅存档测试。内容: {test_text}")
        await process_single_message(
            text=test_text,
            chat_id=test_chat_id,
            user_id=test_user_id,
            msg_id=test_msg_id,
            timestamp=int(time.time()),
            reply_to=debug_reply_to,
        )


    else:
        logger.info("模式：【强行触发回复】。正在跳过 Telegram 读取，直接调用回复脚本...")



        if args.text:
            await process_single_message(
                args.text, test_chat_id, test_user_id, test_msg_id, reply_to=debug_reply_to
            )


        bot = Bot(token=TOKEN)
        await bot.send_chat_action(chat_id=test_chat_id, action=constants.ChatAction.TYPING)

        reply_script_path = os.path.join(BASE_DIR, 'src', 'tenshi_reply.py')
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", reply_script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_task = asyncio.create_task(_stream_subprocess_output(process.stdout, sys.stdout.write, "[reply] "))
        stderr_task = asyncio.create_task(_stream_subprocess_output(process.stderr, sys.stderr.write, "[reply][stderr] "))
        await process.wait()
        try:
            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=5.0)
        except Exception:
            pass
        logger.info("【Debug Mode】强制回复脚本已执行完毕。")

def main():
    parser = argparse.ArgumentParser(description='Tenshi Telegram 记录器')
    parser.add_argument('--text', help='Message text')
    parser.add_argument('--chat_id', type=int, help='Chat ID')
    parser.add_argument('--user_id', type=int, help='User ID')
    parser.add_argument('--msg_id', help='Message ID')
    parser.add_argument('--file_id', help='File ID')
    parser.add_argument('--file_type', help='File Type')
    parser.add_argument('--file_name', help='File Name')

    parser.add_argument('--location', help='Location JSON')
    parser.add_argument('--random', action='store_true', help='Is random trigger')
    parser.add_argument('--date', help='Message timestamp')
    parser.add_argument('--reply_to', help='Quoted/replied message JSON (same contract as MSG_REPLY_TO)')


    parser.add_argument('--debug', action='store_true', help='本地直接调试运行，不接收环境变量')
    parser.add_argument('--force_reply', action='store_true', help='调试时强行触发 reply 脚本')

    args = parser.parse_args()


    if args.debug:

        asyncio.run(debug_run(args))
        return



    text = args.text or tenshi_config.MSG_TEXT
    chat_id = args.chat_id or (int(tenshi_config.MSG_CHAT_ID) if tenshi_config.MSG_CHAT_ID else None)
    user_id = args.user_id or (int(tenshi_config.MSG_USER_ID) if tenshi_config.MSG_USER_ID else None)
    msg_id = args.msg_id or tenshi_config.MSG_ID

    file_id = args.file_id or tenshi_config.MSG_FILE_ID
    file_type = args.file_type or tenshi_config.MSG_FILE_TYPE
    file_name = args.file_name or tenshi_config.MSG_FILE_NAME

    location_data = args.location or tenshi_config.MSG_LOCATION
    is_random = args.random or tenshi_config.IS_RANDOM_TRIGGER

    msg_date = args.date or tenshi_config.MSG_DATE
    timestamp = int(msg_date) if msg_date else int(time.time())


    reply_to = parse_reply_to(args.reply_to or tenshi_config.MSG_REPLY_TO)

    if (text or file_id or location_data or is_random) and chat_id and user_id:
        _write_runtime_result("retryable_failure")
        result = asyncio.run(process_single_message(text, chat_id, user_id, msg_id, file_id, file_type, file_name, location_data, is_random, timestamp, reply_to))
        _write_runtime_result(result or "delivery_unknown")
        if result in ("retryable_failure", "delivery_unknown"):
            raise SystemExit(1)
    else:

        if not tenshi_config.GITHUB_ACTIONS:
            print("提示：当前未检测到环境变量或输入参数。")
            print("若要本地调试请使用: python src/telegram_tenshi_logger.py --debug")
        else:
            logger.error("Action 环境中缺少必要参数，流程终止。")
        print("Usage: python telegram_tenshi_logger.py ...")

if __name__ == '__main__':
    main()
