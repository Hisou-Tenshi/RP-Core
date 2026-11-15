# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import copy
import datetime
import time
import re
import logging
import shutil
import subprocess
from dotenv import load_dotenv
import tenshi_config
from tenshi_utils import setup_logger, FileLock
from tenshi_utils import ensure_tenshi_log_newest_first
import sys
from tenshi_utils import append_llm_trace, format_chat_memory_content


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from tenshi_memory_manager import TenshiMemoryManager
from rag_modules import TenshiRAG
from deepseek_modules import score_chat_importance_batch
from kb_storage import (
    load_kb_composite,
    flush_narrative_from_memory_obj,
    collect_all_event_ids,
    filter_events_for_summary_day,
    migrate_monolithic_kb_if_needed,
)
from user_location import (
    preserve_user_location_in_kb,
    strip_user_location_from_kb,
    strip_user_location_in_place,
)
from kb_apply import (
    apply_relationship_dynamics_delta,
    apply_user_profile_delta,
    extract_delta_sections,
    has_meaningful_summary_payload,
    postprocess_user_profile_physical,
)
from error_text import format_exception_inline


None  # Environment is supplied explicitly by the public host.


SAVE_PATH = tenshi_config.LOG_FILE_PATH
DOWNLOAD_DIR = tenshi_config.DOWNLOAD_DIR_PATH
KB_FILE = tenshi_config.KB_FILE_PATH
SUMMARY_CURSOR_FILE = tenshi_config.SUMMARY_CURSOR_FILE
try:
    LOG_UPDATE_SOFT_TIMEOUT_SECONDS = int(os.getenv("LOG_UPDATE_SOFT_TIMEOUT_SECONDS", "1500"))
except Exception:
    LOG_UPDATE_SOFT_TIMEOUT_SECONDS = 1500


logger = setup_logger("SummaryUpdate", tenshi_config.SUMMARY_UPDATE_LOG_PATH, level=logging.DEBUG)


def _local_tz() -> datetime.timezone:
    """项目统一时区（默认北京时间 UTC+8，由 TIMEZONE_OFFSET 决定）。"""
    return datetime.timezone(datetime.timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))


def _append_summary_trace(record: dict) -> bool:
    return append_llm_trace(record, trace_path=tenshi_config.SUMMARY_LLM_TRACE_PATH)

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)




_mem_mgr = None
_rag = None


def ensure_mem_mgr():
    """惰性获取 TenshiMemoryManager(进程内单例)。"""
    global _mem_mgr
    if _mem_mgr is None:
        try:
            _mem_mgr = TenshiMemoryManager()
        except Exception as e:
            logger.error(f"初始化 TenshiMemoryManager 失败：{format_exception_inline(e)}")
            _mem_mgr = None
    return _mem_mgr


def ensure_rag():
    """惰性获取 TenshiRAG(进程内单例)。"""
    global _rag
    if _rag is None:
        try:
            _rag = TenshiRAG(memory_manager=ensure_mem_mgr())
        except Exception as e:
            logger.error(f"初始化 TenshiRAG 失败：{format_exception_inline(e)}")
            _rag = None
    return _rag




def coalesce_user_profile_misplaced_lists(user_profile: dict) -> None:
    """
    将误挂在 user_profile 根下的 likes/dislikes/habits 列表并入 preference_and_taboos，并删除编外键。
    用于合并后及落盘前修复结构漂移。
    """
    if not isinstance(user_profile, dict):
        return
    slots = ("likes", "dislikes", "habits")
    pnt = user_profile.get("preference_and_taboos")
    if not isinstance(pnt, dict):
        pnt = {s: [] for s in slots}
        user_profile["preference_and_taboos"] = pnt
    for slot in slots:
        cur = pnt.get(slot)
        if not isinstance(cur, list):
            pnt[slot] = []
    for slot in slots:
        stray = user_profile.get(slot)
        if not isinstance(stray, list) or not stray:
            continue
        logger.warning(
            f"[KB] 将编外 user_profile.{slot}（{len(stray)} 条）并入 preference_and_taboos.{slot} 并移除根键"
        )
        base = pnt[slot]
        merged = list(base)
        for x in stray:
            if x not in merged:
                merged.append(x)
        pnt[slot] = merged
        try:
            del user_profile[slot]
        except Exception:
            pass


def strip_legacy_relationship_bond_fields(relationship_dynamics: dict) -> None:
    """Remove deprecated / retired fields from relationship_dynamics (not stored or injected)."""
    if not isinstance(relationship_dynamics, dict):
        return
    relationship_dynamics.pop("current_stage", None)
    relationship_dynamics.pop("emotional_bond_level", None)
    relationship_dynamics.pop("interaction_style", None)



_RETIRED_OPERATIONAL_SLOTS = ("commitments", "unresolved_threads", "future_hooks")


def coalesce_light_fields_misplaced(merged_light: dict) -> None:
    """
    对 merge 产出的 user_profile 浅层对象做常见错位修复：
    - user_profile 根下的 likes/dislikes/habits → preference_and_taboos
    - user_profile 根下的已废弃未定事项槽（_RETIRED_OPERATIONAL_SLOTS）→ 直接丢弃
    """
    if not isinstance(merged_light, dict):
        return
    up = merged_light.get("user_profile")
    if isinstance(up, dict):
        coalesce_user_profile_misplaced_lists(up)
    if not isinstance(up, dict):
        return

    for slot in _RETIRED_OPERATIONAL_SLOTS:
        if slot not in up:
            continue
        stray = up.pop(slot, None)
        n = len(stray) if isinstance(stray, list) else 1
        logger.warning(f"[KB] 丢弃编外 user_profile.{slot}（{n} 条；未定事项槽已废弃）")
    rd = merged_light.get("relationship_dynamics")
    strip_legacy_relationship_bond_fields(rd)


def save_json_safely(data, filepath):
    """
    安全保存 JSON：包含校验、原子写入与备份。
    """

    if not data:
        logger.error(f"拒绝保存：数据为空或为 None：{filepath}")
        return False

    if isinstance(data, str):
        try:
            json_obj = json.loads(data)
        except json.JSONDecodeError:
            logger.error(f"拒绝保存：数据不是合法 JSON 字符串：{filepath}")
            return False
    elif isinstance(data, (dict, list)):
        json_obj = data
    else:
        logger.error(f"拒绝保存：数据类型 {type(data)} 不合法：{filepath}")
        return False


    if os.path.exists(filepath):
        try:
            file_size = os.path.getsize(filepath)


            if isinstance(json_obj, dict):
                if not json_obj.keys():
                    logger.error(f"拒绝保存：新数据是空 dict：{filepath}")
                    return False

                if filepath == KB_FILE:
                    required_keys = ["user_profile", "relationship_dynamics", "narrative_memory"]
                    missing = [k for k in required_keys if k not in json_obj]
                    if missing:
                        logger.error(f"拒绝保存：缺少关键键 {missing}：{filepath}")
                        return False
        except Exception as e:
            logger.warning(f"检查现有文件大小/结构时出错：{format_exception_inline(e)}")


    tmp_path = filepath + ".tmp"
    bak_path = filepath + ".bak"

    try:

        with open(tmp_path, 'w', encoding='utf-8') as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)


        if os.path.exists(filepath):
            shutil.copy2(filepath, bak_path)


        if os.path.exists(filepath):
            os.remove(filepath)
        os.rename(tmp_path, filepath)

        logger.info(f"已成功保存到 {filepath}（已创建备份）")
        return True

    except Exception as e:
        logger.error(f"安全保存失败：{filepath}：{format_exception_inline(e)}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def load_summary_cursor_timestamp():
    """读取最后一次成功总结并推送后的游标时间戳。"""
    try:
        if not os.path.exists(SUMMARY_CURSOR_FILE):
            return None
        with open(SUMMARY_CURSOR_FILE, 'r', encoding='utf-8') as f:
            obj = json.load(f)
        ts = obj.get("last_summarized_time") if isinstance(obj, dict) else None
        if ts is None:
            return None
        return float(ts)
    except Exception as e:
        logger.warning(f"读取 summary_cursor 失败，将按全量待筛选处理：{format_exception_inline(e)}")
        return None


def save_summary_cursor_timestamp(ts):
    """保存最后一次成功总结并推送后的游标时间戳。"""
    try:
        if ts is None:
            logger.warning("未提供有效 summary cursor 时间戳，跳过保存。")
            return False
        obj = {
            "last_summarized_time": float(ts),
            "updated_at": datetime.datetime.now(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")
        }
        return save_json_safely(obj, SUMMARY_CURSOR_FILE)
    except Exception as e:
        logger.error(f"保存 summary_cursor 失败：{format_exception_inline(e)}")
        return False


def remove_legacy_summarized_flags(log_data):
    """移除历史消息中的 summarized 标记，返回移除计数。"""
    removed = 0
    if not isinstance(log_data, list):
        return removed
    for conv in log_data:
        if not isinstance(conv, dict):
            continue
        history = conv.get("chat_history")
        if not isinstance(history, list):
            continue
        for msg in history:
            if not isinstance(msg, dict):
                continue
            if "summarized" in msg:
                msg.pop("summarized", None)
                removed += 1
    return removed

def load_local_data():
    """只读加载对话日志快照；总结流程不得写回 tenshi_log.json（与回复流程脱钩）。"""
    if os.path.exists(SAVE_PATH):
        try:
            with open(SAVE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                data, _changed = ensure_tenshi_log_newest_first(data)
                logger.info(f"已加载本地数据（只读快照）：{len(data)} 个会话。")
                return data
        except Exception as e:
            logger.error(f"加载本地 JSON 失败：{str(e)}")
            return []
    return []

def format_time(unix_timestamp):
    tz = datetime.timezone(datetime.timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
    dt = datetime.datetime.fromtimestamp(unix_timestamp, tz)
    return dt.strftime('%Y-%m-%d %H:%M')

def load_latest_memory_from_storage():
    """Load composite KB (core json + monthly narrative files) for summarization context."""
    try:
        if os.path.exists(KB_FILE):
            with open(KB_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
                if raw:
                    try:
                        data = json.loads(raw)
                        if isinstance(data, list):
                            if data:
                                return json.dumps(data[-1], ensure_ascii=False)
                        elif isinstance(data, dict):
                            migrate_monolithic_kb_if_needed(data)
                            return json.dumps(
                                strip_user_location_from_kb(data), ensure_ascii=False
                            )
                    except Exception:
                        pass
        composite = strip_user_location_from_kb(load_kb_composite())
        logger.info("已加载本地记忆（core + 按月 narrative，已排除 user_location）。")
        return json.dumps(composite, ensure_ascii=False)
    except Exception as e:
        logger.error(f"加载记忆失败：{str(e)}")
        return json.dumps(
            strip_user_location_from_kb(load_kb_composite()), ensure_ascii=False
        )

def _is_transient_llm_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    transient_signals = [
        "503", "unavailable", "overloaded", "high demand",
        "429", "rate limit", "resource exhausted",
        "timeout", "timed out", "deadline exceeded",
        "connection", "temporarily", "service unavailable",
    ]
    return any(sig in msg for sig in transient_signals)


def _call_with_retries(
    label: str,
    func,
    max_attempts: int | None = None,
    base_sleep: float | None = None,
    max_sleep: float | None = None,
):
    if max_attempts is None:
        max_attempts = int(getattr(tenshi_config, "LOG_UPDATE_LLM_MAX_ATTEMPTS", 8) or 8)
    if base_sleep is None:
        base_sleep = float(getattr(tenshi_config, "LOG_UPDATE_LLM_BASE_SLEEP", 2.5) or 2.5)
    if max_sleep is None:
        max_sleep = float(getattr(tenshi_config, "LOG_UPDATE_LLM_MAX_SLEEP", 90.0) or 90.0)
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            last_exc = e
            if attempt >= max_attempts or (not _is_transient_llm_error(e)):
                raise
            sleep_s = min(max_sleep, base_sleep * (2 ** (attempt - 1)))
            logger.warning(
                f"{label} 暂时失败（attempt={attempt}/{max_attempts}）：{e}；"
                f"{sleep_s:.1f}s 后重试。"
            )
            time.sleep(sleep_s)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label} 重试失败（未知错误）")


def _dedupe_models(models):
    seen = set()
    out = []
    for m in models or []:
        if isinstance(m, str) and m.strip() and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _merge_lite_models():
    return tenshi_config.deepseek_merge_models()


def _deepseek_generate_json_with_fallback(
    prompt: str,
    *,
    label: str,
    models=None,
    max_attempts_per_model: int | None = None,
    base_sleep: float | None = None,
    max_sleep: float | None = None,
):
    """Try DeepSeek models for JSON merge/repair; each model gets transient-error retries."""
    if max_attempts_per_model is None:
        max_attempts_per_model = int(
            getattr(tenshi_config, "LOG_UPDATE_LLM_MAX_ATTEMPTS_PER_MODEL", 5) or 5
        )
    if base_sleep is None:
        base_sleep = float(getattr(tenshi_config, "LOG_UPDATE_LLM_BASE_SLEEP", 2.5) or 2.5)
    if max_sleep is None:
        max_sleep = float(getattr(tenshi_config, "LOG_UPDATE_LLM_MAX_SLEEP", 90.0) or 90.0)
    from deepseek_modules import LLMTextResponse, deepseek_generate_json

    last_exc = None
    for model_name in _dedupe_models(models) if models else _merge_lite_models():
        try:
            text = _call_with_retries(
                label=f"{label} DeepSeek({model_name})",
                func=lambda mn=model_name, p=prompt: deepseek_generate_json(mn, p),
                max_attempts=max_attempts_per_model,
                base_sleep=base_sleep,
                max_sleep=max_sleep,
            )
            if text:
                return LLMTextResponse(text), model_name
            raise RuntimeError("empty response text")
        except Exception as e:
            last_exc = e
            logger.warning(f"{label} 模型 {model_name} 不可用，尝试 OpenRouter 同款：{format_exception_inline(e)}")

            try:
                from llm_routing import try_openrouter_fallback_path

                fb = try_openrouter_fallback_path(
                    model_name,
                    lambda slug, path, _p=prompt: deepseek_generate_json(slug, _p, llm_path=path),
                    log=logger,
                )
                if fb:
                    return LLMTextResponse(fb), model_name
            except Exception as fb_exc:
                logger.warning(f"{label} OpenRouter 兜底失败：{format_exception_inline(fb_exc)}")
            logger.warning(f"{label} 模型 {model_name} 及 OR 兜底均不可用，尝试下一个。")
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label}：无可用 DeepSeek 模型")


def get_summary_with_fallback(text, existing_summary=None, summary_day: str | None = None):
    """Summarize conversation using LLMs."""
    if not summary_day and existing_summary:
        try:
            mem_obj = json.loads(existing_summary)
            events = mem_obj.get("narrative_memory", {}).get("significant_events", [])
            if isinstance(events, list) and events:
                last_eid = str((events[-1] or {}).get("event_id") or "")
                if len(last_eid) >= 10 and last_eid[4] == "-" and last_eid[7] == "-":
                    summary_day = last_eid[:10]
        except Exception as e:
            logger.warning(f"解析既有 summary 失败：{format_exception_inline(e)}")

    try:
        system_prompt = tenshi_config.SUMMARY_PROMPT_TEMPLATE.format(
            summary_day=summary_day or "（由对话时间推断）",
        )
    except Exception as e:
        logger.error(f"格式化 prompt 出错：{format_exception_inline(e)}")
        return None

    user_prompt = f"以下是【当前对话记录】：\n{text}"


    try:
        from llm_routing import claude_summary_model_chain, run_claude_model_fallback
        from tenshi_reply import call_claude

        def _invoke_summary_claude(model_id: str) -> str:
            result = call_claude(
                model_id,
                system_prompt,
                [{"role": "user", "content": user_prompt}],
            )
            if isinstance(result, tuple):
                return (result[0] or "") if result else ""
            return result or ""

        out_text = run_claude_model_fallback(
            claude_summary_model_chain(),
            _invoke_summary_claude,
            max_attempts_per_model=int(
                getattr(tenshi_config, "LOG_UPDATE_LLM_MAX_ATTEMPTS_PER_MODEL", 5) or 5
            ),
            base_sleep=float(getattr(tenshi_config, "LOG_UPDATE_LLM_BASE_SLEEP", 2.5) or 2.5),
            log=logger,
        )
        if out_text:
            try:
                _append_summary_trace({
                    "source": "log_update.summary",
                    "provider": "claude",
                    "model": claude_summary_model_chain()[0],
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "response_text": out_text,
                })
            except Exception:
                pass
            return out_text
    except RuntimeError as e:
        logger.warning(f"[Summary] Claude 降级链全部失败：{format_exception_inline(e)}")
    except Exception as e:
        logger.warning(f"[Summary] Claude 调用异常：{format_exception_inline(e)}")


    from deepseek_modules import call_deepseek

    for model_name in tenshi_config.deepseek_merge_models():
        try:
            logger.info(f"[Summary] 正在尝试 DeepSeek {model_name}...")
            def _invoke_summary_deepseek(mn=model_name):
                body, reasoning = call_deepseek(
                    mn,
                    system_prompt,
                    [{"role": "user", "content": user_prompt}],
                )
                return body, reasoning

            out_text, reasoning_text = _call_with_retries(
                label=f"[Summary] DeepSeek({model_name})",
                func=_invoke_summary_deepseek,
            )
            out_text = (out_text or "").strip() if isinstance(out_text, str) else ""
            if out_text:
                try:
                    _append_summary_trace({
                        "source": "log_update.summary",
                        "provider": "deepseek",
                        "model": model_name,
                        "system_prompt": system_prompt,
                        "user_prompt": user_prompt,
                        "response_text": out_text,
                        "thinking_text": (reasoning_text or "").strip(),
                    })
                except Exception:
                    pass
                return out_text
        except Exception as e:
            logger.error(f"[Summary] DeepSeek {model_name} 失败，尝试 OpenRouter 同款兜底：{str(e)}")
            try:
                from deepseek_modules import call_deepseek as _call_ds
                from llm_routing import try_openrouter_fallback_path

                fb = try_openrouter_fallback_path(
                    model_name,
                    lambda slug, path, _sys=system_prompt, _up=user_prompt: _call_ds(
                        slug, _sys, [{"role": "user", "content": _up}], llm_path=path
                    ),
                    log=logger,
                )
                if fb:
                    fb_text, fb_reasoning = fb
                    fb_text = (fb_text or "").strip()
                    if fb_text:
                        try:
                            _append_summary_trace({
                                "source": "log_update.summary",
                                "provider": "openrouter",
                                "model": model_name,
                                "system_prompt": system_prompt,
                                "user_prompt": user_prompt,
                                "response_text": fb_text,
                                "thinking_text": (fb_reasoning or "").strip(),
                            })
                        except Exception:
                            pass
                        return fb_text
            except Exception as fb_exc:
                logger.warning(f"[Summary] OpenRouter 兜底失败：{format_exception_inline(fb_exc)}")

    return None

def _preference_slot_strings(pnt: dict, slot: str) -> list:
    v = pnt.get(slot) if isinstance(pnt, dict) else None
    if not isinstance(v, list):
        return []
    out: list = []
    for x in v:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def merge_preference_taboos_with_lite(final_memory: dict) -> None:
    """
    After summary hard-merge: optional LITE pass to fold
    near-duplicate lines in preference_and_taboos (likes / dislikes / habits).
    """
    if not isinstance(final_memory, dict):
        return
    up = final_memory.get("user_profile")
    if not isinstance(up, dict):
        return
    pnt = up.get("preference_and_taboos")
    if not isinstance(pnt, dict):
        return
    likes_in = _preference_slot_strings(pnt, "likes")
    dislikes_in = _preference_slot_strings(pnt, "dislikes")
    habits_in = _preference_slot_strings(pnt, "habits")
    if len(likes_in) + len(dislikes_in) + len(habits_in) < 2:
        return
    try:
        prompt = tenshi_config.PREFERENCE_TABOOS_LITE_MERGE_PROMPT_TEMPLATE.format(
            likes_json=json.dumps(likes_in, ensure_ascii=False),
            dislikes_json=json.dumps(dislikes_in, ensure_ascii=False),
            habits_json=json.dumps(habits_in, ensure_ascii=False),
        )
    except Exception as e:
        logger.warning(f"[PrefMerge] 提示词格式化失败，跳过：{format_exception_inline(e)}")
        return
    try:
        response, pref_model = _deepseek_generate_json_with_fallback(
            prompt,
            label="[PrefMerge] DeepSeek(preference_and_taboos)",
        )
        raw = getattr(response, "text", None) or ""
        if not raw.strip():
            logger.warning("[PrefMerge] LITE 返回空，跳过。")
            return
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            logger.warning("[PrefMerge] LITE 输出非对象，跳过。")
            return
        try:
            _append_summary_trace({
                "source": "log_update.merge_preference_taboos_lite",
                "provider": "deepseek",
                "model": pref_model,
                "prompt": prompt,
                "response_text": raw,
            })
        except Exception:
            pass

        def _take_valid(slot: str, src_in: list) -> list | None:
            raw_list = parsed.get(slot)
            if not isinstance(raw_list, list):
                return None
            cleaned: list = []
            for x in raw_list:
                if isinstance(x, str) and x.strip():
                    cleaned.append(x.strip())
            if src_in and len(cleaned) > len(src_in):
                return None
            if src_in and not cleaned:
                return None
            return cleaned

        ok_l = _take_valid("likes", likes_in)
        ok_d = _take_valid("dislikes", dislikes_in)
        ok_h = _take_valid("habits", habits_in)
        if ok_l is None and ok_d is None and ok_h is None:
            logger.warning("[PrefMerge] 校验未通过（长度/类型），跳过应用。")
            return
        if ok_l is not None:
            pnt["likes"] = ok_l
        if ok_d is not None:
            pnt["dislikes"] = ok_d
        if ok_h is not None:
            pnt["habits"] = ok_h
        logger.info(
            f"[PrefMerge] 已应用 LITE 折叠：likes {len(likes_in)}→{len(pnt.get('likes') or [])}, "
            f"dislikes {len(dislikes_in)}→{len(pnt.get('dislikes') or [])}, "
            f"habits {len(habits_in)}→{len(pnt.get('habits') or [])}"
        )
    except Exception as e:
        logger.warning(f"[PrefMerge] LITE 合并失败，保留原列表：{format_exception_inline(e)}")






_IDENTITY_FOLD_MIN_FACTS = int(os.getenv("IDENTITY_FACTS_FOLD_MIN_FACTS", "16") or 16)
_IDENTITY_FOLD_MIN_NICK_KEYS = int(os.getenv("IDENTITY_NICKNAMES_FOLD_MIN_KEYS", "10") or 10)


def _identity_facts_strings(up: dict) -> list:
    v = up.get("identity_facts") if isinstance(up, dict) else None
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


def identity_fold_triggered(up: dict, rd: dict) -> bool:
    """条数达到阈值才折叠，避免每个总结批次都多一次 LITE 调用。"""
    if len(_identity_facts_strings(up)) >= _IDENTITY_FOLD_MIN_FACTS:
        return True
    sn = rd.get("special_nicknames") if isinstance(rd, dict) else None
    if isinstance(sn, dict):
        return len([k for k in sn if isinstance(k, str) and k.strip()]) >= _IDENTITY_FOLD_MIN_NICK_KEYS
    return False


def fold_identity_and_nicknames_with_lite(final_memory: dict) -> None:
    """
    After summary hard-merge: optional LITE pass to fold near-duplicate / stale
    identity_facts and special_nicknames entries. Never grows the lists; on any
    validation failure the original content is kept untouched.
    """
    if not isinstance(final_memory, dict):
        return
    up = final_memory.get("user_profile")
    if not isinstance(up, dict):
        return
    rd = final_memory.get("relationship_dynamics")
    if not isinstance(rd, dict):
        rd = {}
    sn = rd.get("special_nicknames")
    if not isinstance(sn, dict):
        sn = {}
    facts_in = _identity_facts_strings(up)
    sn_in = {k.strip(): v for k, v in sn.items() if isinstance(k, str) and k.strip()}
    if not identity_fold_triggered(up, rd):
        return
    if not facts_in and not sn_in:
        return
    try:
        prompt = tenshi_config.IDENTITY_FACTS_LITE_MERGE_PROMPT_TEMPLATE.format(
            today=datetime.datetime.now(_local_tz()).strftime("%Y-%m-%d"),
            facts_json=json.dumps(facts_in, ensure_ascii=False, indent=1),
            nicknames_json=json.dumps(sn_in, ensure_ascii=False, indent=1),
        )
    except Exception as e:
        logger.warning(f"[IdentityFold] 提示词格式化失败，跳过：{format_exception_inline(e)}")
        return
    try:
        response, fold_model = _deepseek_generate_json_with_fallback(
            prompt,
            label="[IdentityFold] DeepSeek(identity_facts)",
        )
        raw = getattr(response, "text", None) or ""
        if not raw.strip():
            logger.warning("[IdentityFold] LITE 返回空，跳过。")
            return
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            logger.warning("[IdentityFold] LITE 输出非对象，跳过。")
            return

        def _clean_facts(value) -> list | None:
            if not isinstance(value, list):
                return None
            out: list = []
            for x in value:
                if isinstance(x, str) and x.strip():
                    s = x.strip()
                    if s not in out:
                        out.append(s)
            if not out or len(out) > len(facts_in):
                return None
            floor = 1
            if len(facts_in) >= 8:
                floor = max(4, len(facts_in) // 4)
            if len(out) < floor:
                return None
            return out

        def _clean_nicknames(value) -> dict | None:
            if not isinstance(value, dict) or not sn_in:
                return None
            out: dict = {}
            for k, v in value.items():
                if not isinstance(k, str) or not k.strip():
                    continue
                key = k.strip()
                if isinstance(v, str):
                    if not v.strip():
                        return None
                    out[key] = v.strip()
                elif isinstance(v, list):
                    items: list = []
                    for it in v:
                        if isinstance(it, str) and it.strip():
                            s = it.strip()
                            if s not in items:
                                items.append(s)
                    if not items:
                        return None
                    out[key] = items
                else:
                    return None
            if not out or len(out) > len(sn_in):
                return None
            return out

        new_facts = _clean_facts(parsed.get("identity_facts")) if facts_in else None
        new_sn = _clean_nicknames(parsed.get("special_nicknames"))
        if new_facts is None and new_sn is None:
            logger.warning("[IdentityFold] 校验未通过（长度/类型），保留原列表。")
            return
        try:
            _append_summary_trace({
                "source": "log_update.fold_identity_and_nicknames_lite",
                "provider": "deepseek",
                "model": fold_model,
                "prompt": prompt,
                "response_text": raw,
                "dropped_facts": [f for f in facts_in if f not in (new_facts or facts_in)],
            })
        except Exception:
            pass

        applied = []
        if new_facts is not None and new_facts != facts_in:
            up["identity_facts"] = new_facts
            applied.append("identity_facts")
        if new_sn is not None and new_sn != sn_in:
            rd["special_nicknames"] = new_sn
            final_memory["relationship_dynamics"] = rd
            applied.append("special_nicknames")
        if not applied:
            logger.info("[IdentityFold] LITE 输出与现有内容一致，无需改写。")
            return
        logger.info(
            f"[IdentityFold] 已应用 LITE 折叠（{'+'.join(applied)}）："
            f"identity_facts {len(facts_in)}→{len(_identity_facts_strings(up))}, "
            f"nickname_keys {len(sn_in)}→{len(rd.get('special_nicknames') or {})}"
        )
        if len(_identity_facts_strings(up)) > 30:
            logger.warning("[IdentityFold] identity_facts 仍超过 30 条，建议人工核查。")
    except Exception as e:
        logger.warning(f"[IdentityFold] LITE 合并失败，保留原列表：{format_exception_inline(e)}")


def apply_intelligent_merge(base_memory_str, new_patch_str):
    """Merge base memory and new patch (hard deltas + preference/identity LITE folds)."""
    if not base_memory_str:
        base_memory = {}
    else:
        try:
            base_memory = json.loads(base_memory_str)
        except Exception:
            base_memory = {}

    try:
        new_patch = json.loads(new_patch_str)
    except Exception as e:
        raise RuntimeError(
            f"New summary patch is not valid JSON: {format_exception_inline(e)}"
        ) from e

    strip_user_location_in_place(base_memory)
    strip_user_location_in_place(new_patch)


    up_delta, rd_delta, _op_unused, nm_patch = extract_delta_sections(new_patch)

    base_events = list(base_memory.get("narrative_memory", {}).get("significant_events", []) or [])
    new_events = (nm_patch.get("significant_events") or []) if isinstance(nm_patch, dict) else []
    known_ids = {
        str(e.get("event_id"))
        for e in base_events
        if isinstance(e, dict) and e.get("event_id")
    }
    for ne in new_events:
        if not isinstance(ne, dict):
            continue
        eid = ne.get("event_id")
        if not eid:
            continue
        eid_s = str(eid)
        if eid_s in known_ids:
            continue
        known_ids.add(eid_s)
        base_events.append(ne)

    final_memory = copy.deepcopy(base_memory)
    final_memory = apply_user_profile_delta(final_memory, up_delta)
    final_memory = apply_relationship_dynamics_delta(final_memory, rd_delta)

    if "narrative_memory" not in final_memory:
        final_memory["narrative_memory"] = {}
    final_memory["narrative_memory"]["significant_events"] = base_events
    final_memory["narrative_memory"]["shared_experience_index"] = (
        (base_memory.get("narrative_memory", {}) or {}).get("shared_experience_index", [])
    )

    rd = final_memory.get("relationship_dynamics")
    if isinstance(rd, dict):
        strip_legacy_relationship_bond_fields(rd)

    merge_preference_taboos_with_lite(final_memory)
    fold_identity_and_nicknames_with_lite(final_memory)
    strip_user_location_in_place(final_memory)

    return json.dumps(final_memory, ensure_ascii=False)


def normalize_summary_json(raw_summary_text):
    """
    Normalize LLM summary output into valid JSON text.
    - Extracts JSON object from fences/noise.
    - Attempts direct parse.
    - If invalid, asks Lite model to repair JSON and validates again.
    """
    if not raw_summary_text or not isinstance(raw_summary_text, str):
        return None

    txt = raw_summary_text.strip()
    if txt.startswith("```json"):
        txt = txt[7:].strip()
    elif txt.startswith("```"):
        txt = txt[3:].strip()
    if txt.endswith("```"):
        txt = txt[:-3].strip()


    match = re.search(r'(\{.*\})', txt, re.DOTALL)
    candidate = match.group(1).strip() if match else txt

    try:
        obj = json.loads(candidate)
        return json.dumps(obj, ensure_ascii=False)
    except Exception as first_err:
        logger.warning(f"Summary JSON 解析失败，尝试修复：{first_err}")

    repair_prompt = (
        "You are a JSON repair tool.\n"
        "Fix the following malformed JSON so it becomes valid JSON.\n"
        "Rules:\n"
        "1) Return ONLY one JSON object.\n"
        "2) Do not add markdown fences.\n"
        "3) Preserve original keys/values/meaning as much as possible.\n"
        "4) If a value is clearly broken, minimally fix syntax only.\n\n"
        f"Input JSON:\n{candidate}"
    )

    try:
        repair_res, repair_model = _deepseek_generate_json_with_fallback(
            repair_prompt,
            label="[SummaryRepair] DeepSeek",
        )
        repaired_text = getattr(repair_res, "text", None) or ""

        try:
            _append_summary_trace({
                "source": "log_update.summary_json_repair",
                "provider": "deepseek",
                "model": repair_model,
                "prompt": repair_prompt,
                "response_text": repaired_text,
            })
        except Exception:
            pass

        repaired_obj = json.loads(repaired_text)
        return json.dumps(repaired_obj, ensure_ascii=False)
    except Exception as repair_err:
        logger.error(f"Summary JSON 修复失败：{repair_err}")
        return None


def _build_parse_failure_patch(raw_summary_text: str, err_msg: str = "") -> str:
    """
    Build a minimal valid patch when summary JSON cannot be parsed.
    The raw model output is kept for manual inspection via logger + summary trace
    (no longer written into the retired pending-items slot).
    """
    raw = (raw_summary_text or "").strip()
    if len(raw) > 4000:
        raw = raw[:4000] + "...(truncated)"
    note = {
        "type": "summary_json_parse_failed",
        "time": datetime.datetime.now(_local_tz()).strftime("%Y-%m-%d %H:%M:%S"),
        "raw_output": raw,
    }
    if err_msg:
        note["error"] = str(err_msg)
    logger.error(
        "[Summary] JSON 解析失败，原始输出（截断至 4000 字）："
        + json.dumps(note, ensure_ascii=False)
    )
    try:
        _append_summary_trace({"source": "log_update.summary_parse_failed", **note})
    except Exception:
        pass
    patch = {
        "narrative_memory": {
            "significant_events": [],
            "shared_experience_index": []
        },
    }
    return json.dumps(patch, ensure_ascii=False)


def _remove_empty_placeholders(obj):
    """
    Recursively remove empty placeholder values.
    - In lists: drop empty dict/list, None and blank strings.
    - In dicts: keep keys, but clean nested values.
    This prevents accidental '{}' artifacts from indexed merge padding.
    """
    if isinstance(obj, list):
        cleaned = []
        for item in obj:
            val = _remove_empty_placeholders(item)
            if val is None:
                continue
            if isinstance(val, str) and not val.strip():
                continue
            if isinstance(val, dict) and not val:
                continue
            if isinstance(val, list) and not val:
                continue
            cleaned.append(val)
        return cleaned
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = _remove_empty_placeholders(v)
        return out
    return obj


def _count_empty_placeholders(obj) -> int:
    """
    Count removable placeholders according to _remove_empty_placeholders rules.
    """
    if isinstance(obj, list):
        n = 0
        for item in obj:
            n += _count_empty_placeholders(item)
            if item is None:
                n += 1
                continue
            if isinstance(item, str) and not item.strip():
                n += 1
                continue
            if isinstance(item, dict) and not item:
                n += 1
                continue
            if isinstance(item, list) and not item:
                n += 1
                continue
        return n
    if isinstance(obj, dict):
        n = 0
        for v in obj.values():
            n += _count_empty_placeholders(v)
        return n
    return 0


def parse_summary_execution_windows(spec: str | None):
    """Parse 'HH:MM-HH:MM,...' into [(start_minutes, end_minutes), ...]. End is exclusive.

    Overnight windows are allowed (e.g. 22:00-08:00 → start > end).
    24:00 is accepted as 1440 (end of day).
    """
    windows = []
    raw = (spec or "").strip()
    if not raw:
        return windows
    for part in raw.split(","):
        piece = part.strip()
        if not piece or "-" not in piece:
            continue
        start_s, end_s = piece.split("-", 1)
        start_s, end_s = start_s.strip(), end_s.strip()

        def _to_minutes(token: str):
            if token in ("24:00", "24:00:00"):
                return 24 * 60
            bits = token.split(":")
            if len(bits) < 2:
                return None
            try:
                hh = int(bits[0])
                mm = int(bits[1])
            except Exception:
                return None
            if hh == 24 and mm == 0:
                return 24 * 60
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return None
            return hh * 60 + mm

        start_m = _to_minutes(start_s)
        end_m = _to_minutes(end_s)
        if start_m is None or end_m is None or start_m == end_m:
            continue
        windows.append((start_m, end_m))
    return windows


def is_in_summary_execution_window(now_local: datetime.datetime, windows) -> bool:
    if not windows:
        return True
    now_m = int(now_local.hour) * 60 + int(now_local.minute)
    for start_m, end_m in windows:
        if start_m < end_m:
            if start_m <= now_m < end_m:
                return True
        else:

            if now_m >= start_m or now_m < end_m:
                return True
    return False


def is_effective_dialogue_message(m: dict) -> bool:
    if not isinstance(m, dict):
        return False
    if str(m.get("bot_decision", "")).upper() == "SILENCE":
        return False
    pe = m.get("perception_event")
    if isinstance(pe, str) and pe.strip():
        uq = m.get("user_query")
        ba = m.get("bot_answer")
        br = m.get("bot_response")
        if not (
            (isinstance(uq, str) and uq.strip())
            or (isinstance(ba, str) and ba.strip())
            or (isinstance(br, str) and br.strip())
        ):
            return False
    uq = m.get("user_query")
    ba = m.get("bot_answer")
    br = m.get("bot_response")
    return (
        (isinstance(uq, str) and uq.strip())
        or (isinstance(ba, str) and ba.strip())
        or (isinstance(br, str) and br.strip())
    )


def is_context_masked_message(m: dict) -> bool:
    """``context_masked``：该条只存档，不进长期记忆总结。

    语义与回复引擎/上下文压缩一致：它被当成"没发生过"，既不进摘要原文，
    也不参与会话切点（沉默间隔）计算；但保留在 chat_history 里供人工查看。
    """
    return isinstance(m, dict) and bool(m.get("context_masked"))


def latest_effective_message_time(log_data) -> float | None:
    latest = None
    if not isinstance(log_data, list):
        return None
    for conv in log_data:
        if not isinstance(conv, dict):
            continue
        history = conv.get("chat_history")
        if not isinstance(history, list):
            continue
        for msg in history:
            if not is_effective_dialogue_message(msg):
                continue
            t = msg.get("time")
            if t is None:
                continue
            try:
                tf = float(t)
            except Exception:
                continue
            if latest is None or tf > latest:
                latest = tf
    return latest


def evaluate_summary_execution_guard(now_local: datetime.datetime, latest_msg_ts: float | None):
    """Return (should_skip, reason). reason is always a short Chinese log line."""
    if not bool(getattr(tenshi_config, "SUMMARY_EXECUTION_GUARD_ENABLED", True)):
        return False, (
            f"执行门禁已关闭；本地 {now_local.strftime('%Y-%m-%d %H:%M')} 继续总结。"
        )

    windows_spec = str(getattr(tenshi_config, "SUMMARY_EXECUTION_WINDOWS", "22:00-08:00") or "22:00-08:00")
    windows = parse_summary_execution_windows(windows_spec)
    catchup_h = int(getattr(tenshi_config, "SUMMARY_EXECUTION_CATCHUP_HOURS", 10) or 10)
    in_window = is_in_summary_execution_window(now_local, windows)
    if not in_window:
        return True, (
            f"执行门禁：本地 {now_local.strftime('%Y-%m-%d %H:%M')} 不在允许窗口 "
            f"{windows_spec}（回补范围约 {catchup_h}h，仅窗口内补跑）；跳过本轮，不推进 cursor。"
        )

    idle_s = int(getattr(tenshi_config, "SUMMARY_ACTIVITY_IDLE_SECONDS", 45 * 60) or 2700)
    if latest_msg_ts is None:
        return False, (
            f"执行门禁：本地 {now_local.strftime('%Y-%m-%d %H:%M')} 命中窗口 {windows_spec}；"
            f"无有效对话时间戳，视为静默足够，继续总结。"
        )

    now_ts = now_local.timestamp()
    idle_for = max(0.0, now_ts - float(latest_msg_ts))
    last_dt = datetime.datetime.fromtimestamp(float(latest_msg_ts), tz=now_local.tzinfo)
    if idle_for < float(idle_s):
        remain = float(idle_s) - idle_for
        return True, (
            f"执行门禁：本地 {now_local.strftime('%Y-%m-%d %H:%M')} 命中窗口 {windows_spec}，"
            f"但最近有效对话在 {last_dt.strftime('%Y-%m-%d %H:%M')} "
            f"（静默 {idle_for:.0f}s < {idle_s}s，还需 {remain:.0f}s）；跳过本轮以免与回复争用，不推进 cursor。"
        )

    return False, (
        f"执行门禁：本地 {now_local.strftime('%Y-%m-%d %H:%M')} 命中窗口 {windows_spec}；"
        f"最近有效对话 {last_dt.strftime('%Y-%m-%d %H:%M')}，已静默 {idle_for:.0f}s "
        f"（阈值 {idle_s}s），继续总结。"
    )


def summary_cut_observation_complete(now_local: datetime.datetime, day: datetime.date, last_hour: int = 6) -> bool:
    """True iff this calendar day's last 02~06 candidate hour has already started (window fully observed)."""
    tz = now_local.tzinfo
    window_end = datetime.datetime(day.year, day.month, day.day, int(last_hour), 0, 0, tzinfo=tz)
    return now_local >= window_end


def global_segment_and_summarize(pending_messages, start_monotonic=None, soft_timeout_seconds=None):
    """Summarize pending messages by TIMEZONE_OFFSET day buckets (chronological order expected).

    Rules:
    - No LLM-based segmentation.
    - Silence-gap detection ignores perception-triggered entries and bot SILENCE entries.
    - Daily baseline split is TIMEZONE_OFFSET 04:00; allowed candidate whole-hour cut points are 02~06.
      Pick the cut point with the longest silence gap, as long as the gap >= threshold.
    - If 02~06 all fail for that night, fallback: within the surrounding 48 hours (that night’s two days),
      pick the single longest silence gap that meets the threshold and split there.
    - Hard gate (schedule safety):
      * Do not freeze **today's** 02~06 cut until that window has been fully observed
        (local time >= 06:00). Open-ended silence (no message after a candidate hour)
        is not a cut while 05:00/06:00 might still get dialogue.
      * **Pre-cutoff** (before cutoff_hour): if today's cut is not yet sealed, skip.
        Once sealed, summarize yesterday's memory day: (today−1) cut through today's cut.
      * **Post-cutoff** (at or after cutoff_hour): same seal rule; then summarize up to
        today's 02~06 cut. If 02~06 never had a qualifying gap, skip this run.
    """


    pending_messages = [m for m in pending_messages if not is_context_masked_message(m)]
    if not pending_messages:
        logger.info("待总结消息全部标记为 context_masked；跳过总结。")
        return None, None
    pending_messages.sort(key=lambda x: x['time'])
    all_pending = pending_messages

    def _safe_user_text(msg: dict) -> str:
        uq = msg.get("user_query")
        if isinstance(uq, str) and uq.strip():
            return uq
        return "(无用户输入)"

    def _safe_bot_text(msg: dict) -> str:
        ba = msg.get("bot_answer")
        if isinstance(ba, str) and ba.strip():
            return ba
        br = msg.get("bot_response")
        if isinstance(br, str) and br.strip():
            return br
        if str(msg.get("bot_decision", "")).upper() == "SILENCE":
            return "(沉默)"
        pe = msg.get("perception_event")
        if isinstance(pe, str) and pe.strip():
            return f"(感知事件) {pe}"
        return "(无回复)"

    try:
        updated = False
        current_global_memory = None
        max_summarized_time = None
        tz = datetime.timezone(datetime.timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
        cutoff_hour = int(getattr(tenshi_config, "DAILY_SUMMARY_CUTOFF_HOUR", 4) or 4)
        silence_threshold_s = int(getattr(tenshi_config, "DAILY_SUMMARY_SILENCE_THRESHOLD_SECONDS", 3 * 60 * 60) or 10800)

        def _msg_time(m):
            t = m.get("time")
            if t is None:
                return None
            try:
                return float(t)
            except Exception:
                return None

        def _is_effective_dialogue(m: dict) -> bool:
            return is_effective_dialogue_message(m)

        def _split_ts_for_day(d: datetime.date, hour: int) -> float:
            dt = datetime.datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=tz)
            return dt.timestamp()

        def _effective_times_in_range(start_ts=None, end_ts=None):
            out = []
            for m in all_pending:
                if not _is_effective_dialogue(m):
                    continue
                mt = _msg_time(m)
                if mt is None:
                    continue
                if start_ts is not None and mt < start_ts:
                    continue
                if end_ts is not None and mt >= end_ts:
                    continue
                out.append(mt)
            out.sort()
            return out

        def _gap_around(ts: float, effective_times: list[float]):
            """Return (gap_seconds, open_ended). open_ended means no message at or after ts."""
            prev_t = None
            next_t = None
            for t in effective_times:
                if t < ts:
                    prev_t = t
                    continue
                next_t = t
                break
            open_ended = next_t is None
            if prev_t is None or next_t is None:
                return float("inf"), open_ended
            return max(0.0, float(next_t) - float(prev_t)), False

        def _candidate_hours_2_to_6():
            hours = [cutoff_hour, cutoff_hour - 1, cutoff_hour + 1, cutoff_hour - 2, cutoff_hour + 2]
            filtered = []
            for h in hours:
                try:
                    hh = int(h)
                except Exception:
                    continue
                if 2 <= hh <= 6 and hh not in filtered:
                    filtered.append(hh)
            if not filtered:
                filtered = [4, 3, 5, 2, 6]
            return filtered

        last_cut_hour = max(_candidate_hours_2_to_6())

        def _best_hour_cut_for_date(d: datetime.date, effective_times: list[float]):
            """Pick 02~06 cut. Future hours ignored. Open-ended silence ignored until 06:00 has passed."""
            window_complete = summary_cut_observation_complete(now_local, d, last_cut_hour)
            now_ts = now_local.timestamp()
            best = None
            for hh in _candidate_hours_2_to_6():
                ts = _split_ts_for_day(d, hh)
                if float(ts) > float(now_ts):
                    continue
                gap, open_ended = _gap_around(ts, effective_times)
                if open_ended and not window_complete:
                    continue
                if gap >= silence_threshold_s:
                    if best is None or gap > best[0]:
                        best = (gap, ts, hh)
            return best[1] if best else None

        def _fallback_48h_longest_gap_cut(d: datetime.date, effective_times_all: list[float]):
            start_ts = _split_ts_for_day(d, 0)
            end_ts = start_ts + 48.0 * 3600.0
            times = [t for t in effective_times_all if start_ts <= t < end_ts]
            if len(times) < 2:
                return None
            best_gap = -1.0
            best_pair = None
            for a, b in zip(times, times[1:]):
                g = float(b) - float(a)
                if g >= silence_threshold_s and g > best_gap:
                    best_gap = g
                    best_pair = (a, b)
            if not best_pair:
                return None
            return (best_pair[0] + best_pair[1]) / 2.0

        def _resolve_cut_for_date(d: datetime.date, effective_times: list[float]) -> float:
            ts = _best_hour_cut_for_date(d, effective_times)
            if ts is None:
                ts = _split_ts_for_day(d, cutoff_hour)
            return float(ts)

        now_local = datetime.datetime.now(tz)
        calendar_today = now_local.date()
        pre_cutoff = int(now_local.hour) < int(cutoff_hour)
        effective_times_all = _effective_times_in_range()
        period_start_cut = None

        if not summary_cut_observation_complete(now_local, calendar_today, last_cut_hour):
            logger.info(
                f"今晚 02~{last_cut_hour:02d} 观察窗尚未结束（本地 {now_local.strftime('%Y-%m-%d %H:%M')}）；"
                f"开放式沉默不能当作切点（之后仍可能在 02~{last_cut_hour:02d} 出现对话）。本轮跳过，不推进 cursor。"
            )
            return None, None

        if pre_cutoff:
            period_end_date = calendar_today
            period_start_date = calendar_today - datetime.timedelta(days=1)
            period_end_cut = _best_hour_cut_for_date(period_end_date, effective_times_all)
            if period_end_cut is None:
                logger.info(
                    f"日界前调度：今日 02~{last_cut_hour:02d} 没有满足沉默阈值的切点；本轮跳过。"
                )
                return None, None
            period_start_cut = _resolve_cut_for_date(period_start_date, effective_times_all)
            gate_date = period_end_date
            today_cut = float(period_end_cut)
            logger.info(
                f"日界前调度（本地 {now_local.strftime('%Y-%m-%d %H:%M')}）："
                f"总结昨天记忆日 {period_start_date}～{period_end_date} 切点区间内的对话；"
                f"上界 t < {period_end_date} 封账切点，下界 t >= {period_start_date} 封账切点。"
            )
        else:
            gate_date = calendar_today
            today_cut = _best_hour_cut_for_date(gate_date, effective_times_all)
            if today_cut is None:
                logger.info("当日 02~06 未找到满足沉默阈值的切点；按硬门禁本次不做任何总结。")
                return None, None
            today_cut = float(today_cut)
            logger.info(
                f"日界后调度（本地 {now_local.strftime('%Y-%m-%d %H:%M')}）："
                f"今晚 02~{last_cut_hour:02d} 已观察完毕，上界为今日封账切点。"
            )

        filtered = []
        for m in all_pending:
            mt = _msg_time(m)
            if mt is None:
                continue
            mtf = float(mt)
            if mtf >= today_cut:
                continue
            if period_start_cut is not None and mtf < float(period_start_cut):
                continue
            filtered.append(m)
        all_pending = filtered
        if not all_pending:
            logger.info("切点之前无可总结消息；跳过。")
            return None, None
        effective_times_all = _effective_times_in_range()

        first_dt = datetime.datetime.fromtimestamp(float(all_pending[0]["time"]), tz)
        last_dt = datetime.datetime.fromtimestamp(float(all_pending[-1]["time"]), tz)

        split_points = []
        cur_date = first_dt.date()
        end_date = last_dt.date()
        while cur_date <= end_date:
            ts = _best_hour_cut_for_date(cur_date, effective_times_all)
            if ts is None:
                if cur_date != gate_date:
                    ts = _fallback_48h_longest_gap_cut(cur_date, effective_times_all)
            if ts is not None and float(ts) < float(today_cut):
                split_points.append(float(ts))
            cur_date = cur_date + datetime.timedelta(days=1)

        split_points = sorted(set(split_points))

        current_global_memory = load_latest_memory_from_storage()
        last_start = 0

        def _summarize_chunk(chunk):
            nonlocal current_global_memory, updated, max_summarized_time, last_start
            if not chunk:
                return
            if len(chunk) < 2:
                return

            if start_monotonic is not None and soft_timeout_seconds:
                if (time.monotonic() - start_monotonic) >= float(soft_timeout_seconds):
                    logger.warning(
                        f"接近运行时限（soft_timeout={soft_timeout_seconds}s），暂停后续分片总结，等待下次续跑。"
                    )
                    return



            try:
                missing = []
                for m in chunk:
                    mid = str(m.get("msg_id", "") or "").strip()
                    if not mid:
                        continue
                    if "importance_score" not in m:
                        missing.append(m)
                if not missing:
                    logger.info(f"[ChatScore] 本批次 {len(chunk)}/{len(chunk)} 条消息已存在分数，跳过打分。")
                else:
                    score_map = score_chat_importance_batch(
                        chunk, trace_path=tenshi_config.SUMMARY_LLM_TRACE_PATH
                    )
                    scored_n = 0
                    for m in chunk:
                        mid = str(m.get("msg_id", ""))
                        if not mid:
                            continue
                        if "importance_score" in m:
                            continue
                        if mid in score_map:
                            m["importance_score"] = float(score_map[mid])
                            scored_n += 1
                    logger.info(f"[ChatScore] 本批次已为缺失项打分：{scored_n}/{len(chunk)} 条。")
            except Exception as e:
                logger.warning(f"[ChatScore] 批次打分失败（{len(chunk)} 条）：{format_exception_inline(e)}")

            chunk_texts = []
            for m in chunk:
                thinking_part = ""
                if m.get('bot_thinking') and isinstance(m.get('bot_thinking'), dict) and m['bot_thinking'].get('model_thinking'):
                    thinking_part = f"\n(思考: {m['bot_thinking']['model_thinking']})"

                chunk_texts.append(
                    f"[{format_time(m['time'])}]\n用户: {_safe_user_text(m)}\n{tenshi_config.ROLE_NAME_SHORT}: {_safe_bot_text(m)}{thinking_part}"
                )

            raw_text = "\n".join(chunk_texts)

            tz = datetime.timezone(datetime.timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
            chunk_day = datetime.datetime.fromtimestamp(float(chunk[0]["time"]), tz).strftime("%Y-%m-%d")

            time.sleep(2)
            new_summary = get_summary_with_fallback(
                raw_text,
                existing_summary=current_global_memory,
                summary_day=chunk_day,
            )
            if new_summary:
                try:
                    clean_summary = normalize_summary_json(new_summary)
                    if not clean_summary:
                        logger.warning("Summary JSON 无效且修复失败；原始输出已写入日志与 summary trace，本批次按空增量处理。")
                        clean_summary = _build_parse_failure_patch(new_summary, "json_parse_failed")



                    try:
                        clean_summary_obj = json.loads(clean_summary)
                    except Exception:
                        clean_summary_obj = {}

                    events = (
                        clean_summary_obj.get("narrative_memory", {}).get("significant_events", [])
                        if isinstance(clean_summary_obj, dict)
                        else []
                    )

                    if not has_meaningful_summary_payload(clean_summary_obj):
                        logger.warning("本批次 summary payload 为空或无有效增量，跳过。")
                        return

                    if not isinstance(events, list):
                        events = []
                    raw_event_count = len(events)
                    events = filter_events_for_summary_day(events, chunk_day)
                    if raw_event_count > len(events):
                        logger.info(
                            f"已丢弃 {raw_event_count - len(events)} 条非 {chunk_day} 的 event_id。"
                        )
                    if isinstance(clean_summary_obj, dict):
                        nm_day = clean_summary_obj.get("narrative_memory")
                        if not isinstance(nm_day, dict):
                            nm_day = {}
                            clean_summary_obj["narrative_memory"] = nm_day
                        nm_day["significant_events"] = events
                        clean_summary = json.dumps(clean_summary_obj, ensure_ascii=False)

                    if not events:
                        fallback_event = {
                            "event_id": f"{chunk_day}-auto-{int(chunk[-1]['time'])}",
                            "description": f"自动补记：{chunk_day} 对话分片总结（{len(chunk)} 条消息）。",
                            "emotional_impact": "low",
                            "mood_context": {"tenshi": "neutral"},
                        }
                        if isinstance(clean_summary_obj, dict):
                            nm2 = clean_summary_obj.get("narrative_memory")
                            if not isinstance(nm2, dict):
                                nm2 = {}
                                clean_summary_obj["narrative_memory"] = nm2
                            nm2["significant_events"] = [fallback_event]
                            events = nm2["significant_events"]
                            clean_summary = json.dumps(clean_summary_obj, ensure_ascii=False)
                            logger.warning("本批次未产出 significant_events，已自动补记 1 条事件以确保事件轴推进。")


                    current_global_memory = apply_intelligent_merge(current_global_memory, clean_summary)
                    try:
                        current_global_memory_obj = json.loads(current_global_memory)
                        removed_placeholders = _count_empty_placeholders(current_global_memory_obj)
                        current_global_memory_obj = _remove_empty_placeholders(current_global_memory_obj)
                        current_global_memory = json.dumps(current_global_memory_obj, ensure_ascii=False)
                        if removed_placeholders:
                            logger.debug(f"[KB Cleanup] 合并后已移除 {removed_placeholders} 个空占位符。")
                    except Exception as e:
                        logger.warning(f"合并后占位符清理已跳过：{format_exception_inline(e)}")
                    updated = True


                    try:
                        import bond_reflect

                        bond_reflect.collect_day_evidence(chunk, chunk_day, bond_reflect.default_llm)
                    except Exception as e:
                        logger.warning(f"[bond] 证据收集失败（不影响总结）：{format_exception_inline(e)}")


                    _mm = ensure_mem_mgr(); _rg = ensure_rag()
                    if _mm and _rg:
                        try:

                            chat_items = [
                                (m, format_chat_memory_content(
                                    ts=m.get("time"), user_query=m.get("user_query"),
                                    bot_answer=m.get("bot_answer"),
                                )) for m in chunk
                            ]
                            chat_items = [(m, content) for m, content in chat_items if content]
                            embeddings = _rg.get_embeddings([content for _, content in chat_items])
                            for (m, content), emb in zip(chat_items, embeddings):
                                try:
                                    chat_importance = float(m.get("importance_score", 0.1))
                                except (TypeError, ValueError):
                                    chat_importance = 0.1
                                _mm.add_memory(
                                    content, emb, memory_type="chat", importance=chat_importance,
                                    created_at=m["time"], metadata={"source": "log_update"},
                                )


                            summary_json = json.loads(clean_summary)


                            known_event_ids = collect_all_event_ids()
                            for event in filter_events_for_summary_day(
                                summary_json.get("narrative_memory", {}).get("significant_events", []),
                                chunk_day,
                            ):
                                if not isinstance(event, dict):
                                    continue
                                eid = (event.get("event_id") or "").strip()
                                desc = event.get("description")
                                if not (eid and desc):
                                    continue
                                if eid in known_event_ids:
                                    continue
                                known_event_ids.add(eid)
                                emb = ensure_rag().get_embedding(desc)
                                mc = event.get("mood_context")
                                if isinstance(mc, dict) and "user" in mc:
                                    mc = dict(mc)
                                    mc.pop("user", None)
                                meta = {
                                    "event_id": eid,
                                    "emotional_impact": event.get("emotional_impact"),
                                    "mood_context": mc,
                                }
                                ensure_mem_mgr().add_memory(
                                    desc, emb, memory_type="event", importance=0.0, metadata=meta
                                )

                            logger.info("已将 summary 分片同步到 SQLite。")
                        except Exception as e:
                            logger.error(f"同步到 SQLite 失败：{format_exception_inline(e)}")


                    for m in chunk:
                        mt = m.get('time')
                        if mt is None:
                            continue
                        try:
                            mtf = float(mt)
                        except Exception:
                            continue
                        if max_summarized_time is None or mtf > max_summarized_time:
                            max_summarized_time = mtf
                except Exception as e:
                    logger.error(f"Summary 合并失败：{format_exception_inline(e)}")
                    raise

        sp_idx = 0
        for i, m in enumerate(all_pending):
            if sp_idx >= len(split_points):
                continue
            mt = m.get("time")
            if mt is None:
                continue
            try:
                mtf = float(mt)
            except Exception:
                continue
            while sp_idx < len(split_points) and mtf >= split_points[sp_idx]:
                chunk = all_pending[last_start:i]
                _summarize_chunk(chunk)
                last_start = i
                sp_idx += 1

        if last_start < len(all_pending):
            _summarize_chunk(all_pending[last_start:])

        return (current_global_memory, max_summarized_time) if updated else (None, None)

    except Exception as e:
        if updated:
            logger.error(
                f"分片总结中途失败；将落盘并交由后续流程推送已成功合并的记忆（latest_ts={max_summarized_time}）：{e}",
                exc_info=True,
            )
            return (current_global_memory, max_summarized_time)
        logger.critical(f"Segmentation failed: {str(e)}", exc_info=True)
        raise

def process_summaries(local_data, last_cursor_time=None):
    """Run the summarization pipeline.

    Returns:
        tuple[bool, float | None]: (kb_updated, latest_summarized_time)
    """
    run_start_monotonic = time.monotonic()
    def _normalize_shared_experience_index(kb_obj: dict) -> None:
        try:
            nm = kb_obj.get("narrative_memory")
            if not isinstance(nm, dict):
                return
            sei = nm.get("shared_experience_index")
            if not isinstance(sei, list):
                return

            best_by_tag = {}
            order = []

            def _score_desc(s: str) -> int:
                if not isinstance(s, str):
                    return 0
                ss = s.strip()
                return len(ss)

            for item in sei:
                if not isinstance(item, dict):
                    continue
                tag = (item.get("tag") or "").strip()
                if not tag:
                    continue
                desc = item.get("description")
                if not isinstance(desc, str) or not desc.strip():

                    origin = item.get("origin")
                    if isinstance(origin, str) and origin.strip():
                        desc = origin
                    else:
                        continue
                desc = desc.strip()


                src = item.get("source_event_id")
                if not isinstance(src, str) or not src.strip():
                    for k in ["origin_event", "first_appeared", "first_referenced"]:
                        vv = item.get(k)
                        if isinstance(vv, str) and vv.strip():
                            src = vv.strip()
                            break
                if not isinstance(src, str) or not src.strip():
                    src = None

                norm = {"tag": tag, "description": desc}
                if src:
                    norm["source_event_id"] = src

                prev = best_by_tag.get(tag)
                if not prev:
                    best_by_tag[tag] = norm
                    order.append(tag)
                else:

                    if _score_desc(norm["description"]) > _score_desc(prev.get("description", "")):
                        prev["description"] = norm["description"]
                    if "source_event_id" not in prev and "source_event_id" in norm:
                        prev["source_event_id"] = norm["source_event_id"]

            nm["shared_experience_index"] = [best_by_tag[t] for t in order if t in best_by_tag]
        except Exception:
            return
    all_messages = []
    for conv in local_data:
        for msg in conv.get('chat_history') or []:

            if is_context_masked_message(msg):
                continue
            all_messages.append(msg)


    pending_chronological = []
    if last_cursor_time is None:
        pending_chronological = sorted(all_messages, key=lambda x: x.get('time', 0))
        logger.info("未找到 summary cursor，按全量消息执行总结筛选。")
    else:
        for msg in all_messages:
            t = msg.get('time')
            if t is None:
                continue
            try:
                if float(t) > float(last_cursor_time):
                    pending_chronological.append(msg)
            except Exception:
                continue
        pending_chronological.sort(key=lambda x: x.get('time', 0))
        logger.info(
            f"已基于 summary cursor={last_cursor_time} 筛出 {len(pending_chronological)} 条待总结消息。"
        )

    if not pending_chronological:
        logger.info("summary cursor 之后没有新消息；跳过总结。")
        return False, None

    new_memory, latest_summarized_time = global_segment_and_summarize(
        pending_chronological,
        start_monotonic=run_start_monotonic,
        soft_timeout_seconds=LOG_UPDATE_SOFT_TIMEOUT_SECONDS,
    )

    if new_memory:
        try:
            latest_memory_obj = json.loads(new_memory)
            strip_user_location_in_place(latest_memory_obj)



            try:
                rd = latest_memory_obj.get("relationship_dynamics")
                if isinstance(rd, dict):
                    if "emotional_state" in rd:
                        rd.pop("emotional_state", None)
                        logger.info("保存 KB 前已移除 relationship_dynamics.emotional_state。")
                    strip_legacy_relationship_bond_fields(rd)
            except Exception:
                pass


            try:
                evs = latest_memory_obj.get("narrative_memory", {}).get("significant_events", [])
                if isinstance(evs, list):
                    removed = 0
                    for e in evs:
                        if isinstance(e, dict) and "importance_score" in e:
                            e.pop("importance_score", None)
                            removed += 1

                        mc = e.get("mood_context") if isinstance(e, dict) else None
                        if isinstance(mc, dict) and "user" in mc:
                            mc.pop("user", None)
                    if removed:
                        logger.info(f"保存 KB 前已从 significant_events 移除 {removed} 条 importance_score。")
            except Exception:
                pass


            _normalize_shared_experience_index(latest_memory_obj)

            try:
                coalesce_light_fields_misplaced(latest_memory_obj)
            except Exception as e:
                logger.warning(f"保存 KB 前浅层字段错位合并失败：{format_exception_inline(e)}")

            try:
                up_save = latest_memory_obj.get("user_profile")
                if isinstance(up_save, dict):
                    postprocess_user_profile_physical(up_save)
            except Exception as e:
                logger.warning(f"保存 KB 前 short_term_state 归一化/过期清理失败：{format_exception_inline(e)}")





            _mm2 = ensure_mem_mgr()
            if _mm2:
                try:


                    if ensure_rag():
                        for exp in (latest_memory_obj.get("narrative_memory", {}) or {}).get("shared_experience_index", []) or []:
                            if not isinstance(exp, dict):
                                continue
                            tag = (exp.get("tag") or "").strip()
                            desc = (exp.get("description") or "").strip()
                            if not (tag and desc):
                                continue
                            ensure_mem_mgr().delete_memory_by_metadata("tag", tag)
                            emb = ensure_rag().get_embedding(desc)
                            meta = {
                                "tag": tag,
                                "keywords": exp.get("keywords", []),
                                "trigger_hint": exp.get("trigger_hint", ""),
                                "usage_example": exp.get("usage_example", ""),
                                "first_seen": exp.get("first_seen", ""),
                                "last_mentioned": exp.get("last_mentioned", ""),
                            }
                            ensure_mem_mgr().add_memory(
                                desc,
                                emb,
                                memory_type='experience',
                                importance=0.0,
                                metadata=meta,
                            )

                    allowed_eids = collect_all_event_ids()
                    for ev in (latest_memory_obj.get("narrative_memory", {}) or {}).get("significant_events", []) or []:
                        if isinstance(ev, dict):
                            eid = (ev.get("event_id") or "").strip()
                            if eid:
                                allowed_eids.add(eid)
                    deleted_ev = ensure_mem_mgr().reconcile_events(allowed_eids)
                    if deleted_ev:
                        logger.info(f"已对齐 SQLite：删除 {deleted_ev} 条 KB 中不存在的过期 events。")
                except Exception as e:
                    logger.warning(f"SQLite event 对齐失败：{format_exception_inline(e)}")

            kb_core = preserve_user_location_in_kb(
                flush_narrative_from_memory_obj(latest_memory_obj)
            )
            _kb_saved = save_json_safely(kb_core, KB_FILE)
            if not _kb_saved:
                logger.error("KB 保存失败，返回 False 以阻止 data/ 推送。")
                return False, None
            logger.info("知识库已在本地更新（log_update）。")


            try:
                import bond_reflect

                _today = datetime.datetime.now(_local_tz()).strftime("%Y-%m-%d")
                bond_reflect.reflect(_today, bond_reflect.default_llm)
            except Exception as e:
                logger.warning(f"[bond] 反思失败（不影响总结）：{format_exception_inline(e)}")
            return True, latest_summarized_time
        except Exception as e:
            logger.error(f"保存知识库失败：{format_exception_inline(e)}")
            return False, None
    logger.info("本次未生成新的 summaries；知识库不会更新。")
    return False, None




def commit_memory_changes(kb_save_ok=True, files_to_commit_override=None, require_push_success=False):
    """
    Commit selected files to GitHub and optionally require push success.

    Returns:
        bool: True if commit/push flow satisfied the requirement, else False.
    """
    if os.getenv("RP_EXTERNAL_PERSISTENCE") == "1":
        return True
    logger.info("正在检查是否存在需要提交的记忆变更...")

    if files_to_commit_override is not None:
        files_to_commit = files_to_commit_override
    elif kb_save_ok:
        files_to_commit = [
            "data/tenshi_memory.db",
            "data/embedding_cache.pkl",
            "data/knowledge_base.json",
            "data/knowledge_base_events/",
            "data/knowledge_base_shared_experience/",
            "data/summary_cursor.json",
            "data/bond_state.json",
            "data/bond_ledger.json",
            "logs/summary_update.log",
            "logs/summary_llm_trace.jsonl",
        ]
    else:
        logger.warning("[Commit] KB 保存未成功，排除所有 data/ 文件，仅推送 artifacts。")
        files_to_commit = []


    if not os.path.exists(os.path.join(tenshi_config.PROJECT_ROOT, ".git")):
        logger.warning("当前目录不是 git 仓库，跳过提交。")
        return False

    try:

        if tenshi_config.GITHUB_ACTIONS:
            subprocess.run(["git", "config", "user.name", tenshi_config.ROLE_GIT_NAME], check=True, cwd=tenshi_config.PROJECT_ROOT)
            subprocess.run(["git", "config", "user.email", tenshi_config.ROLE_GIT_EMAIL], check=True, cwd=tenshi_config.PROJECT_ROOT)


        if files_to_commit:
            subprocess.run(["git", "add"] + files_to_commit, check=True, cwd=tenshi_config.PROJECT_ROOT)


        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=tenshi_config.PROJECT_ROOT)
        if not status.stdout.strip():
            logger.info("没有可提交的变更。")

            return not require_push_success


        timestamp = datetime.datetime.now(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["git", "commit", "-m", f"{tenshi_config.ROLE_NAME_SHORT}例行整顿：记忆与日志归档 {timestamp} [skip ci]"],
            check=True,
            cwd=tenshi_config.PROJECT_ROOT,
        )


        if tenshi_config.GITHUB_ACTIONS:

            subprocess.run(["git", "pull", "--rebase"], check=True, cwd=tenshi_config.PROJECT_ROOT)
            subprocess.run(["git", "push"], check=True, cwd=tenshi_config.PROJECT_ROOT)
            logger.info("已成功推送记忆变更到 GitHub。")
            return True
        else:
            logger.info("本地运行：已提交记忆变更，跳过 git pull/push。")
            return not require_push_success

    except subprocess.CalledProcessError as e:
        logger.error(f"Git 操作失败：{format_exception_inline(e)}")
        return False
    except Exception as e:
        logger.error(f"提交变更失败：{format_exception_inline(e)}")
        return False

def push_knowledge_base_and_check_success():
    """Gate summary cursor advancement on KB delta in this run.

    In GitHub Actions we intentionally do not push from this script anymore;
    push is handled by workflow's unified commit/push step.
    """
    if os.getenv("RP_EXTERNAL_PERSISTENCE") == "1":
        import hashlib
        from pathlib import Path
        _p = Path(tenshi_config.KB_FILE_PATH)
        return _p.is_file() and hashlib.sha256(_p.read_bytes()).hexdigest() != os.getenv("RP_KB_BASE_SHA", "")
    if not os.path.exists(os.path.join(tenshi_config.PROJECT_ROOT, ".git")):
        logger.warning("当前目录不是 git 仓库，无法校验 knowledge_base 变更。")
        return False

    try:
        subprocess.run(["git", "add", "data/knowledge_base.json"], check=True, cwd=tenshi_config.PROJECT_ROOT)

        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "data/knowledge_base.json"],
            capture_output=True,
            text=True,
            cwd=tenshi_config.PROJECT_ROOT,
        )
        if not status.stdout.strip():
            logger.warning("knowledge_base.json 无新增变更，阻止推进 summary cursor。")
            return False

        if tenshi_config.GITHUB_ACTIONS:
            logger.info("已检测到 KB 变更；Actions 模式下交由 workflow 统一 push，允许推进 summary cursor。")
            return True

        logger.warning("本地运行未执行 push，按门禁规则视为失败，不推进 summary cursor。")
        return False

    except subprocess.CalledProcessError as e:
        logger.error(f"knowledge_base 变更校验失败：{format_exception_inline(e)}")
        return False
    except Exception as e:
        logger.error(f"knowledge_base 变更校验异常：{format_exception_inline(e)}")
        return False


def main():
    logger.info("=== 开始本地更新与同步 ===")



    local_data_snapshot = load_local_data()


    if not local_data_snapshot:
        logger.warning("本地数据为空或加载失败。为避免数据丢失，终止更新。")
        return


    removed_flags = remove_legacy_summarized_flags(local_data_snapshot)
    if removed_flags > 0:
        logger.info(f"快照中仍有 {removed_flags} 条 legacy summarized 标记（已忽略，不写回日志）。")

    tz = datetime.timezone(datetime.timedelta(hours=tenshi_config.TIMEZONE_OFFSET))
    now_local = datetime.datetime.now(tz)
    latest_msg_ts = latest_effective_message_time(local_data_snapshot)
    skip_run, guard_reason = evaluate_summary_execution_guard(now_local, latest_msg_ts)
    logger.info(guard_reason)
    if skip_run:
        logger.info("因执行门禁跳过总结；summary cursor 不推进。")
        if tenshi_config.GITHUB_OUTPUT:
            with open(tenshi_config.GITHUB_OUTPUT, "a") as f:
                f.write("updated=false\n")
        logger.info("=== 任务完成 ===")
        return






    last_cursor_time = load_summary_cursor_timestamp()
    summary_success, latest_summarized_time = process_summaries(
        local_data_snapshot,
        last_cursor_time=last_cursor_time
    )
    is_updated = False

    if summary_success:

        kb_pushed = push_knowledge_base_and_check_success()

        if kb_pushed:
            logger.info("knowledge_base push 成功，正在保存 summary cursor...")
            try:
                with FileLock(tenshi_config.SUMMARY_LOCK_FILE_PATH, timeout=60):
                    if latest_summarized_time is None:
                        logger.error("本次未拿到有效 latest_summarized_time，cursor 不推进。")
                    else:
                        if save_summary_cursor_timestamp(latest_summarized_time):
                            logger.info(
                                f"已保存 summary cursor：last_summarized_time={latest_summarized_time}"
                            )
                            is_updated = True
                        else:
                            logger.error("summary cursor 保存失败。")
            except Exception as e:
                logger.error(f"保存 summary cursor 失败：{format_exception_inline(e)}")
        else:
            logger.error("knowledge_base 未完成成功 push，已阻止推进 summary cursor。")
    else:
        logger.info("本次未生成新的 summaries；跳过 summary cursor 更新。")


    if tenshi_config.GITHUB_OUTPUT:
        with open(tenshi_config.GITHUB_OUTPUT, 'a') as f:
            f.write(f"updated={'true' if is_updated else 'false'}\n")



    if ensure_mem_mgr():
        try:

            try:
                cleared = ensure_mem_mgr().clear_non_chat_importance_scores()
                if cleared:
                    logger.info(f"已清理 {cleared} 条非聊天 DB 条目的 importance_score（保留聊天保留分数）。")
            except Exception as e:
                logger.warning(f"非聊天 importance_score 清理失败：{format_exception_inline(e)}")

            deleted = ensure_mem_mgr().prune_chat_memories(
                keep_all_days=tenshi_config.CHAT_KEEP_ALL_DAYS,
                keep_important_days=tenshi_config.CHAT_KEEP_IMPORTANT_DAYS,
                important_threshold=tenshi_config.CHAT_IMPORTANT_THRESHOLD,
                permanent_threshold=tenshi_config.CHAT_PERMANENT_THRESHOLD,
            )
            if deleted:
                logger.info(
                    f"Pruned {deleted} chat memories from SQLite "
                    f"(keep_all_days={tenshi_config.CHAT_KEEP_ALL_DAYS}, "
                    f"keep_important_days={tenshi_config.CHAT_KEEP_IMPORTANT_DAYS}, "
                    f"important_threshold={tenshi_config.CHAT_IMPORTANT_THRESHOLD}, "
                    f"permanent_threshold={tenshi_config.CHAT_PERMANENT_THRESHOLD})"
                )
        except Exception as e:
            logger.warning(f"聊天裁剪失败：{format_exception_inline(e)}")



        try:
            orphan_deleted = ensure_mem_mgr().prune_orphan_embedding_cache()
            if orphan_deleted:
                logger.info(f"已裁剪 {orphan_deleted} 条孤儿 embedding_cache（与 memory_items 对齐）。")
        except Exception as e:
            logger.warning(f"embedding_cache 对齐失败：{format_exception_inline(e)}")



    _mm_stats = ensure_mem_mgr()
    if _mm_stats:
        try:
            stats = ensure_mem_mgr().get_stats()
            logger.info(f"【系统体检】记忆数:{stats['total_memories']} | "
                        f"向量数:{stats['vector_index_size']} | "
                        f"缓存数:{stats['embedding_cache_size']} | "
                        f"维度:{stats['dimension']}")
        except Exception as e:
            logger.warning(f"记录系统统计失败：{format_exception_inline(e)}")

    logger.info("=== 任务完成 ===")

if __name__ == "__main__":
    main()
