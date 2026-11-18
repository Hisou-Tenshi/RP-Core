# SPDX-License-Identifier: AGPL-3.0-only
















import os

from config.fixed import PROJECT_ROOT







CHAT_KEEP_ALL_DAYS = int(os.getenv("CHAT_KEEP_ALL_DAYS") or 7)
CHAT_KEEP_IMPORTANT_DAYS = int(os.getenv("CHAT_KEEP_IMPORTANT_DAYS") or 30)
CHAT_IMPORTANT_THRESHOLD = float(os.getenv("CHAT_IMPORTANT_THRESHOLD") or 0.6)
CHAT_PERMANENT_THRESHOLD = float(os.getenv("CHAT_PERMANENT_THRESHOLD") or 1.2)









CHAT_LOG_ARCHIVE_KEEP_WEEKS = int(os.getenv("CHAT_LOG_ARCHIVE_KEEP_WEEKS") or 2)

CHAT_LOG_ARCHIVE_MIN_MESSAGES_PER_SLICE = int(os.getenv("CHAT_LOG_ARCHIVE_MIN_MESSAGES_PER_SLICE") or 35)

CHAT_LOG_ARCHIVE_MIN_RETAIN_MESSAGES = int(
    os.getenv("CHAT_LOG_ARCHIVE_MIN_RETAIN_MESSAGES") or CHAT_LOG_ARCHIVE_MIN_MESSAGES_PER_SLICE
)

CHAT_LOG_ARCHIVE_DIR_PATH = os.path.join(PROJECT_ROOT, "data", "archive")



PERCEPTION_CONTEXT_MAX_MESSAGES = int(os.getenv("PERCEPTION_CONTEXT_MAX_MESSAGES") or 12)

PERCEPTION_CONTEXT_TRUNCATE_CHARS = int(os.getenv("PERCEPTION_CONTEXT_TRUNCATE_CHARS") or 800)



CONTEXT_RECENT_TURNS = int(os.getenv("CONTEXT_RECENT_TURNS") or 6)

MAIN_CONTEXT_PREVIOUS_TURNS = int(os.getenv("MAIN_CONTEXT_PREVIOUS_TURNS") or 15)




CONTEXT_COMPRESSION_TRIGGER_TURNS = int(os.getenv("CONTEXT_COMPRESSION_TRIGGER_TURNS") or 15)
CONTEXT_COMPRESSION_ANCHOR_TRIGGER_NEWER = int(os.getenv("CONTEXT_COMPRESSION_ANCHOR_TRIGGER_NEWER") or 14)
CONTEXT_COMPRESSION_MAX_PACKAGES_PER_REPLY = int(os.getenv("CONTEXT_COMPRESSION_MAX_PACKAGES_PER_REPLY") or 5)
CONTEXT_COMPRESSION_WINDOW_TURNS = int(os.getenv("CONTEXT_COMPRESSION_WINDOW_TURNS") or 10)
CONTEXT_COMPRESSION_RAW_TURNS = int(os.getenv("CONTEXT_COMPRESSION_RAW_TURNS") or 15)
CONTEXT_COMPRESSION_OVERLAP_TURNS = int(os.getenv("CONTEXT_COMPRESSION_OVERLAP_TURNS") or 5)
CONTEXT_COMPRESSION_KEEP_MIN_PACKAGES = int(os.getenv("CONTEXT_COMPRESSION_KEEP_MIN_PACKAGES") or 5)
CONTEXT_COMPRESSION_MAX_PACKAGES = int(os.getenv("CONTEXT_COMPRESSION_MAX_PACKAGES") or 20)
CONTEXT_COMPRESSION_CONTINUITY_PACKAGES = int(os.getenv("CONTEXT_COMPRESSION_CONTINUITY_PACKAGES") or 3)

CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS = int(os.getenv("CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS") or 500)

CONTEXT_IMAGE_FILE_TURNS = int(os.getenv("CONTEXT_IMAGE_FILE_TURNS") or 5)

CLAUDE_PROMPT_CACHE_ENABLED = (os.getenv("CLAUDE_PROMPT_CACHE_ENABLED") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
)





MAIN_CONTEXT_BUDGET_CHARS = int(os.getenv("MAIN_CONTEXT_BUDGET_CHARS") or 1_000_000)

MAIN_USER_INPUT_MAX_CHARS = int(os.getenv("MAIN_USER_INPUT_MAX_CHARS") or 10_000)

MAIN_TEXT_ATTACHMENT_MAX_CHARS = int(os.getenv("MAIN_TEXT_ATTACHMENT_MAX_CHARS") or 100_000)

MAIN_TOOL_IO_RESERVE_CHARS = int(os.getenv("MAIN_TOOL_IO_RESERVE_CHARS") or 200_000)

MAIN_HISTORY_TURN_TRUNCATE_CHARS = int(
    os.getenv("MAIN_HISTORY_TURN_TRUNCATE_CHARS") or PERCEPTION_CONTEXT_TRUNCATE_CHARS
)



TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET") or 8)






DAILY_SUMMARY_CUTOFF_HOUR = int(os.getenv("DAILY_SUMMARY_CUTOFF_HOUR") or 4)
DAILY_SUMMARY_SILENCE_THRESHOLD_SECONDS = int(
    os.getenv("DAILY_SUMMARY_SILENCE_THRESHOLD_SECONDS") or 3 * 60 * 60
)




DAILY_SUMMARY_SCHEDULE_UTC = os.getenv("DAILY_SUMMARY_SCHEDULE_UTC") or "14:11-23:11"





_SUMMARY_GUARD_RAW = (os.getenv("SUMMARY_EXECUTION_GUARD_ENABLED") or "1").strip().lower()
SUMMARY_EXECUTION_GUARD_ENABLED = _SUMMARY_GUARD_RAW not in ("0", "false", "no", "off")
SUMMARY_EXECUTION_WINDOWS = os.getenv("SUMMARY_EXECUTION_WINDOWS") or "22:00-08:00"
SUMMARY_ACTIVITY_IDLE_SECONDS = int(os.getenv("SUMMARY_ACTIVITY_IDLE_SECONDS") or 45 * 60)

SUMMARY_EXECUTION_CATCHUP_HOURS = int(os.getenv("SUMMARY_EXECUTION_CATCHUP_HOURS") or 10)


LOG_UPDATE_LLM_MAX_ATTEMPTS = int(os.getenv("LOG_UPDATE_LLM_MAX_ATTEMPTS") or 8)
LOG_UPDATE_LLM_MAX_ATTEMPTS_PER_MODEL = int(os.getenv("LOG_UPDATE_LLM_MAX_ATTEMPTS_PER_MODEL") or 5)
LOG_UPDATE_LLM_BASE_SLEEP = float(os.getenv("LOG_UPDATE_LLM_BASE_SLEEP") or 2.5)
LOG_UPDATE_LLM_MAX_SLEEP = float(os.getenv("LOG_UPDATE_LLM_MAX_SLEEP") or 90.0)





LLM_CLIENT_MAX_RETRIES = int(os.getenv("LLM_CLIENT_MAX_RETRIES") or 0)


LLM_CLIENT_READ_TIMEOUT_SEC = float(os.getenv("LLM_CLIENT_READ_TIMEOUT_SEC") or 600.0)
LLM_CLIENT_CONNECT_TIMEOUT_SEC = float(os.getenv("LLM_CLIENT_CONNECT_TIMEOUT_SEC") or 10.0)









PRE_ROUTING_PROVIDER_TIMEOUT_SEC = float(os.getenv("PRE_ROUTING_PROVIDER_TIMEOUT_SEC") or 40.0)





PRE_ROUTING_TOTAL_TIMEOUT_SEC = float(os.getenv("PRE_ROUTING_TOTAL_TIMEOUT_SEC") or 150.0)







PRE_ROUTING_MIN_ATTEMPT_SEC = float(os.getenv("PRE_ROUTING_MIN_ATTEMPT_SEC") or 0.1)






















REPLY_TOTAL_DEADLINE_SEC = float(os.getenv("REPLY_TOTAL_DEADLINE_SEC") or 1080.0)

REPLY_DEADLINE_RESERVE_SEC = float(os.getenv("REPLY_DEADLINE_RESERVE_SEC") or 60.0)





REPLY_MODEL_CALL_TIMEOUT_SEC = float(os.getenv("REPLY_MODEL_CALL_TIMEOUT_SEC") or 300.0)


REPLY_TOOL_LOOP_TIMEOUT_SEC = float(os.getenv("REPLY_TOOL_LOOP_TIMEOUT_SEC") or 300.0)



REPLY_AUX_LLM_TIMEOUT_SEC = float(os.getenv("REPLY_AUX_LLM_TIMEOUT_SEC") or 120.0)
REPLY_AUX_LLM_MAX_ATTEMPTS = int(os.getenv("REPLY_AUX_LLM_MAX_ATTEMPTS") or 2)







REPLY_AUX_LLM_TOTAL_TIMEOUT_SEC = float(
    os.getenv("REPLY_AUX_LLM_TOTAL_TIMEOUT_SEC") or 240.0
)



REPLY_IMAGE_ANALYSIS_TIMEOUT_SEC = float(os.getenv("REPLY_IMAGE_ANALYSIS_TIMEOUT_SEC") or 90.0)

REPLY_WAVE1_TIMEOUT_SEC = float(os.getenv("REPLY_WAVE1_TIMEOUT_SEC") or 120.0)

REPLY_MEMORY_RETRIEVAL_TIMEOUT_SEC = float(
    os.getenv("REPLY_MEMORY_RETRIEVAL_TIMEOUT_SEC") or 45.0
)





PRIVATE_NOTES_INJECT_COUNT = int((os.getenv("PRIVATE_NOTES_INJECT_COUNT") or "2").strip() or 2)




BOND_REFLECT_ENABLED = (os.getenv("BOND_REFLECT_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


BOND_REFLECT_MIN_WEIGHT = float(os.getenv("BOND_REFLECT_MIN_WEIGHT") or 3)
BOND_REFLECT_MIN_DAYS = int(os.getenv("BOND_REFLECT_MIN_DAYS") or 2)

BOND_REFLECT_COOLDOWN_DAYS = int(os.getenv("BOND_REFLECT_COOLDOWN_DAYS") or 3)


BOND_RETRACT_WINDOW_DAYS = int(os.getenv("BOND_RETRACT_WINDOW_DAYS") or 14)
BOND_RETRACT_MIN_DAYS = int(os.getenv("BOND_RETRACT_MIN_DAYS") or 3)

BOND_REFLECT_MAX_OPS = int(os.getenv("BOND_REFLECT_MAX_OPS") or 3)
BOND_REFLECT_MAX_ADDS = int(os.getenv("BOND_REFLECT_MAX_ADDS") or 2)



REPLY_TWO_STAGE_ENABLED = (os.getenv("REPLY_TWO_STAGE_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")




REPLY_TYPING_HISTORY_MESSAGES = int(os.getenv("REPLY_TYPING_HISTORY_MESSAGES") or 30)


REPLY_TYPING_WITH_BACKGROUND = (os.getenv("REPLY_TYPING_WITH_BACKGROUND") or "1").strip().lower() not in ("0", "false", "no", "off")

REPLY_TYPING_MAX_MODELS = int(os.getenv("REPLY_TYPING_MAX_MODELS") or 3)

REPLY_TYPING_TIMEOUT_SEC = float(os.getenv("REPLY_TYPING_TIMEOUT_SEC") or 150.0)

REPLY_TYPING_TOTAL_TIMEOUT_SEC = float(os.getenv("REPLY_TYPING_TOTAL_TIMEOUT_SEC") or 300.0)



REPLY_STICKERS_ENABLED = (os.getenv("REPLY_STICKERS_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")

REPLY_STICKERS_MAX_PER_TURN = int(os.getenv("REPLY_STICKERS_MAX_PER_TURN") or 2)

REPLY_STICKER_DELAY_SEC = float(os.getenv("REPLY_STICKER_DELAY_SEC") or 1.0)




REPLY_REACTIONS_ENABLED = (os.getenv("REPLY_REACTIONS_ENABLED") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)





REPLY_NO_REPLY_NOTICE_ENABLED = (os.getenv("REPLY_NO_REPLY_NOTICE_ENABLED") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

REPLY_NO_REPLY_NOTICE_TEXT = (
    os.getenv("REPLY_NO_REPLY_NOTICE_TEXT") or "【系统小提醒】天子这次选择不回你。"
).strip()



REPLY_QUOTE_MAX_CHARS = int(os.getenv("REPLY_QUOTE_MAX_CHARS") or 600)






FAILED_REPLY_RETRY_ENABLED = (os.getenv("FAILED_REPLY_RETRY_ENABLED") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


FAILED_REPLY_RETRY_INTERVAL_MINUTES = int(os.getenv("FAILED_REPLY_RETRY_INTERVAL_MINUTES") or 2)



FAILED_REPLY_RETRY_MAX_ATTEMPTS = int(os.getenv("FAILED_REPLY_RETRY_MAX_ATTEMPTS") or 3)

def _default_tz_label(offset: int) -> str:
    """
    根据时区偏移给出一个默认的时间标签。
    - UTC+8 → 北京时间（项目统一口径）
    - 其他 → 形如 \"UTC+X 时间\"
    """
    try:
        off = int(offset)
    except Exception:
        off = 0
    if off == 8:
        return "北京时间"
    sign = "+" if off >= 0 else "-"
    return f"UTC{sign}{abs(off)} 时间"



TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL") or _default_tz_label(TIMEZONE_OFFSET)



IMAGE_MAX_EDGE_PX = int(os.getenv("IMAGE_MAX_EDGE_PX") or 2048)
IMAGE_MAX_BYTES = int(os.getenv("IMAGE_MAX_BYTES") or 4_500_000)
IMAGE_STITCH_THRESHOLD = int(os.getenv("IMAGE_STITCH_THRESHOLD") or 3)
IMAGE_STITCH_MAX_EDGE_PX = int(os.getenv("IMAGE_STITCH_MAX_EDGE_PX") or 1600)
IMAGE_ARCHIVE_CONTEXT_CHARS = int(os.getenv("IMAGE_ARCHIVE_CONTEXT_CHARS") or 1200)





from config.models import *
from config.models import (
    _OPENROUTER_FALLBACK_MAP_RESOLVED,
    _resolve_openrouter_fallback_map,
)
from config.providers import (
    CLAUDE_PROXIES,
    CLAUDE_CODEYU_TIERS,
    CLAUDE_CODERPLAN,
    GEMINI_PROXIES,
    GEMINI_CODEYU,
    GEMINI_CODERPLAN,
    GPT_PROXIES,
    GPT_CODEYU,
    GROK_PROXIES,
    GROK_CODERPLAN,
)



CONVERSATION_GRADE_MAX_THRESHOLD = 83
CONVERSATION_GRADE_HIGH_THRESHOLD = 63
CONVERSATION_GRADE_MEDIUM_THRESHOLD = 39
CONVERSATION_GRADE_LOW_THRESHOLD = 19










from datetime import datetime, time, timedelta, timezone as _dt_timezone


def tzinfo() -> _dt_timezone:
    """当前配置时区 timezone 对象(默认 UTC+8)。"""
    return _dt_timezone(timedelta(hours=int(TIMEZONE_OFFSET)))


def now_local() -> datetime:
    """当前配置时区的 aware datetime。"""
    return datetime.now(tzinfo())


def format_local(ts: float | int | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """时间戳(unix 秒)→ 本地时区字符串;缺省用当前时刻。"""
    try:
        if ts is None:
            return datetime.now(tzinfo()).strftime(fmt)
        return datetime.fromtimestamp(float(ts), tzinfo()).strftime(fmt)
    except Exception:
        return datetime.now(tzinfo()).strftime(fmt)





REPLY_CONTINUE_MAX_ROUNDS = int(os.getenv("REPLY_CONTINUE_MAX_ROUNDS") or 3)
REPLY_CONTINUE_USER_MESSAGE = (
    "（系统续写指令）请继续完成你上一条没有写完的回复："
    "直接接着断句处往下写，不要从头复述，也不要重复已经写出的内容。"
)








BUDGET_STAGE_ENABLED = (os.getenv("BUDGET_STAGE_ENABLED") or "1").strip() not in ("0", "false", "no")

BUDGET_PARSE_STRICT = (os.getenv("BUDGET_PARSE_STRICT") or "0").strip() in ("1", "true", "yes")

EVALUATOR_ENABLED = (os.getenv("EVALUATOR_ENABLED") or "1").strip() not in ("0", "false", "no")

EVALUATOR_MAX_REWRITES = int(os.getenv("EVALUATOR_MAX_REWRITES") or 1)

INFO_TICKET_MAX = int(os.getenv("INFO_TICKET_MAX") or 2)
SIGNATURE_MAX = int(os.getenv("SIGNATURE_MAX") or 1)
SOLICITATION_CLUSTER_MAX = int(os.getenv("SOLICITATION_CLUSTER_MAX") or 1)




HISTORY_RAW_INJECTION = (os.getenv("HISTORY_RAW_INJECTION") or "1").strip() in ("1", "true", "yes")
TEMP_SUMMARY_ENABLED = (os.getenv("TEMP_SUMMARY_ENABLED") or "1").strip() not in ("0", "false", "no")
TEMP_SUMMARY_MAX_CHARS = int(os.getenv("TEMP_SUMMARY_MAX_CHARS") or 600)
TEMP_SUMMARY_REFRESH = (os.getenv("TEMP_SUMMARY_REFRESH") or "1").strip() not in ("0", "false", "no")
TEMP_SUMMARY_STORE = os.getenv("TEMP_SUMMARY_STORE") or os.path.join(
    PROJECT_ROOT, "data", "temp_context_summary.json"
)

DEFERRED_INTENT_ENABLED = (os.getenv("DEFERRED_INTENT_ENABLED") or "1").strip() not in ("0", "false", "no")
DEFERRED_INTENT_TTL_TURNS = int(os.getenv("DEFERRED_INTENT_TTL_TURNS") or 1)
DEFERRED_INTENT_MAX_CARRY = int(os.getenv("DEFERRED_INTENT_MAX_CARRY") or 1)
DEFERRED_INTENT_STORE = os.getenv("DEFERRED_INTENT_STORE") or os.path.join(
    PROJECT_ROOT, "data", "deferred_intents.json"
)
DEFERRED_PILOT_STATS = os.getenv("DEFERRED_PILOT_STATS") or os.path.join(
    PROJECT_ROOT, "data", "choice_layer_pilot_stats.json"
)

STYLE_GUARD_ENABLED = (os.getenv("STYLE_GUARD_ENABLED") or "1").strip() not in ("0", "false", "no")
STYLE_GUARD_MAX_REWRITES = int(os.getenv("STYLE_GUARD_MAX_REWRITES") or 1)
