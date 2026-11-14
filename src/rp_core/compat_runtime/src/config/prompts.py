# SPDX-License-Identifier: AGPL-3.0-only

























INNER_CUE = "（这条你先别回。下面写的是你看到这条时脑子里过了什么，用「他」指他；不是对他说的话。）"


PERCEPTION_SITUATION_HEADER = "【此刻】他没发新消息，是你自己想起了他。"







PERCEPTION_EVENT_SCHEDULED_DESCRIPTION = (
    "这不是用户主动对你说话，也不是随机概率触发的噪声。"
    "这是你在更早的对话里通过 `schedule_perception` 为自己登记的**定时起念**——"
    "到了（或接近）你当时写下触发时间的时候，感知层被**强制**唤醒。"
)



PERCEPTION_EVENT_SCHEDULED_MANUAL_NOTE_TEMPLATE = """
【定时附言（你当时写下的）】
{schedule_note}
【原定触发时间（北京时间）】{planned_bj}
这是你当时留给自己的念头。现在还在不在意、要不要找他，看你此刻。"""


PERCEPTION_EVENT_AUTO_DESCRIPTION = "这不是用户主动对你说话。这是你自己的感知神经元传来的世界信息。"



PERCEPTION_PROMPT_TEMPLATE = """【系统级感知层触发 - 非用户直接对话】
{description}
你现在"看到"了以下情况：
{situation_block}
{manual_note}

如果还需要更多信息，可以先调工具：
- **核心**：`read_note` / `note_patch`、`schedule_perception`、`read_skill`（读扩展技能手册）
- **扩展**（建议先 `read_skill` 再调用）：`search_memory`、`search_web`、`fetch_url`、`read_config_file`
  - `search_web`：联网查现实信息（本轮最多 2 次）
  - `fetch_url`：抓取链接正文 html/md/txt/json（本轮最多 3 次）
  - `read_config_file`：查项目内文件（总计最多 3 次；不传 path 先看结构树）

---
另外，今天的日记（`Tenshi.json`）随时可以写，和要不要开口是两回事。
今天怎么过的、想到他什么，想写就写进今天那页；写了不代表要说，说了也不一定是写进去的那些。
"""



PERCEPTION_SOURCE_QUERY_PREFIX_TEMPLATE = "[系统级感知层触发 - {role_name}主动起念]"








PERCEPTION_TIME_ANCHOR_NIGHT_OWL_NOTE_TEMPLATE = """「注」现在是凌晨过零点，主观上仍属于昨日。下文「今天」指的是 {yesterday_md} 至昨天深夜这段时间。
"""


PERCEPTION_TIME_ANCHOR_NIGHT_OWL_TEMPLATE = """{night_owl_note}现在是{year}年{month}月{day}日凌晨{hhmm}（{tz_label}），主观上今日仍属于{sub_month}月{sub_day}日。"""


PERCEPTION_TIME_ANCHOR_DAY_TEMPLATE = """现在是{year}年{month}月{day}日{tod}{hhmm}（{tz_label}）。"""


PERCEPTION_TIME_ANCHOR_FALLBACK_TEMPLATE = """现在是{tz_label} {current_time_str}。"""


PERCEPTION_TIME_OF_DAY_EARLY = "清早"
PERCEPTION_TIME_OF_DAY_MORNING = "上午"
PERCEPTION_TIME_OF_DAY_NOON = "中午"
PERCEPTION_TIME_OF_DAY_AFTERNOON = "下午"
PERCEPTION_TIME_OF_DAY_EVENING = "傍晚"
PERCEPTION_TIME_OF_DAY_NIGHT = "晚间"
PERCEPTION_TIME_OF_DAY_LATE_NIGHT = "深夜"







TIMESTAMP_PREFIX_SYSTEM_NOTE = (
    "\n\n【重要】对话上下文中如出现形如 `[YYYY-MM-DD HH:MM:SS UTC+8]` 的前缀，"
    "它只是系统注入的时间戳元数据。请不要在输出里复写/引用/保留这种前缀，直接从正文开始。"
)







SEARCH_MEMORY_TRUNCATION_NOTE = """

【此处因太长被本小姐手动切了，凑合看吧】"""


SEARCH_MEMORY_NO_RESULTS_NOTICE = "No relevant local memories found."


TOOL_NOTICE_SEARCH_WEB_LIMIT_TEMPLATE = "SYSTEM NOTICE: `search_web` 已达到本轮硬性上限（最多 {max_calls} 次）。请停止联网搜索，直接用现有信息回答。"


TOOL_NOTICE_FETCH_URL_LIMIT_TEMPLATE = "SYSTEM NOTICE: `fetch_url` 已达到本轮硬性上限（最多 {max_calls} 次）。请改用已有信息。"


TOOL_NOTICE_READ_CONFIG_LIMIT_TEMPLATE = "[SYSTEM NOTICE] read_config_file 已达到本轮硬性上限（最多 {max_calls} 次）。"


TOOL_ERROR_TOOL_NOT_FOUND_TEMPLATE = "Error: Tool {name} not found."





CLAUDE_CORE_TOOLS = [
    {
        "name": "read_note",
        "description": "Read your own diary (Tenshi.json, one entry per day). Private to Tenshi, the user never sees it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "How many most-recent days to return.",
                    "minimum": 1,
                    "maximum": 60
                }
            },
            "required": []
        }
    },
    {
        "name": "note_patch",
        "description": "Write today's diary entry (Tenshi.json). Only today's page can be written or edited; past days are read-only. op=write replaces today's whole entry, op=append adds a paragraph to it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "Usually a single operation.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["write", "append"]
                            },
                            "text": {
                                "type": "string",
                                "description": "Diary text: what happened today and what you thought about it. Not instructions for your future self."
                            }
                        },
                        "required": ["op", "text"]
                    }
                },
                "read_n": {
                    "type": "integer",
                    "description": "How many most-recent days to return in the tool result after writing.",
                    "minimum": 1,
                    "maximum": 60
                }
            },
            "required": ["operations"]
        }
    },
    {
        "name": "schedule_perception",
        "description": (
            "Schedule your own next proactive perception trigger (定时起念). "
            "Provide either an absolute Beijing-time (UTC+8) trigger time or a relative interval, plus a short note (附言) "
            "that will be injected into the perception layer when the timer fires."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "附言：触发时注入感知层的内心提示/想做的事（必填）。"
                },
                "trigger_at": {
                    "type": "string",
                    "description": "绝对触发时间（北京时间 UTC+8），如 2026-05-25 14:30 或 2026-05-25 14:30:00。与 interval_* 二选一。"
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "相对当前时间的间隔（分钟）。与 trigger_at 二选一。",
                    "minimum": 1
                },
                "interval_hours": {
                    "type": "integer",
                    "description": "相对当前时间的间隔（小时）。可与 interval_minutes 叠加。",
                    "minimum": 1
                },
                "entries": {
                    "type": "array",
                    "description": "可选：一次批量登记多组定时任务；每项含 note 与 trigger_at 或 interval_*。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "note": {"type": "string"},
                            "trigger_at": {"type": "string"},
                            "interval_minutes": {"type": "integer"},
                            "interval_hours": {"type": "integer"}
                        },
                        "required": ["note"]
                    }
                }
            },
            "required": []
        }
    },
    {
        "name": "read_skill",
        "description": (
            "Load the full markdown manual for an extended skill "
            "(search_memory, search_web, fetch_url, read_config_file). "
            "Call before using those tools when unsure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "Skill id from the catalog, e.g. search_web or fetch_url."
                }
            },
            "required": ["skill_id"]
        }
    },
]

CLAUDE_EXTENDED_TOOLS = [
    {
        "name": "search_memory",
        "description": "Search local memories and past conversations. See skill: search_memory (use read_skill first if unsure).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query or keywords."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_web",
        "description": "Search the internet for real-time information. See skill: search_web (max 2 calls per turn).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch an HTTP(S) URL and return html/md/txt/json content as text. "
            "See skill: fetch_url (max 3 calls per turn; GitHub URLs auto-auth in Actions)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "HTTP or HTTPS URL to fetch."
                },
                "output_format": {
                    "type": "string",
                    "enum": ["auto", "html", "md", "txt", "json"],
                    "description": "Output format. Default auto-detect."
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max characters to return (optional).",
                    "minimum": 500,
                    "maximum": 50000
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "read_config_file",
        "description": "Inspect a file inside the project folder. See skill: read_config_file (max 3 calls per turn).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path within the project root. Omit to get project tree first."
                }
            },
            "required": []
        }
    },
]

CLAUDE_TOOLS = CLAUDE_CORE_TOOLS + CLAUDE_EXTENDED_TOOLS




TOOL_LIMIT_SYSTEM_NOTICE = (
    "SYSTEM NOTICE: Maximum tool call limit reached. "
    "Please STOP searching. Answer the user now with whatever information you have. "
    "If you didn't find the exact answer, complain about it in character (Tenshi) and mock the situation "
    "instead of apologizing. Do NOT call tools again."
)







SEARCH_KEYWORD_REFINE_SYSTEM_PROMPT = (
    "You are a search keyword optimization engine. "
    "Output STRICT JSON only. Do not add commentary."
)



SEARCH_KEYWORD_REFINE_USER_TEMPLATE = (
    "Current time: {now_str}\n"
    "Target intent: {target}\n"
    "User query: {query}\n"
    "Conversation context (truncated):\n{history_context}\n\n"
    "Policy (may be empty): {policy_json}\n"
    "Already tried terms: {tried_terms}\n\n"
    "We have unsatisfactory/insufficient evidence. Propose improved search terms.\n"
    "Rules:\n"
    "- Return short, entry-like terms, not long natural language.\n"
    "- If target includes history: for each important entity, include BOTH a Chinese name and an official Japanese name if exists.\n"
    "- If target includes web_search: provide English terms and ALSO 1-3 Japanese terms if English may be ambiguous or if the title/name is Japanese.\n"
    "- Do NOT include site: restrictions in the terms.\n\n"
    "Current evidence snapshot (truncated):\n{current_evidence}\n\n"
    "Output JSON schema:\n"
    "{{\n"
    '  "history_cn": ["..."],\n'
    '  "history_ja": ["..."],\n'
    '  "web_en": ["..."],\n'
    '  "web_ja": ["..."],\n'
    '  "blocked_sites": ["optional domains to avoid"],\n'
    '  "reason": "one sentence"\n'
    "}}"
)



SEARCH_AGENT_SUMMARY_USER_TEMPLATE = (
    "用户问题：{query}\n\n"
    "以下是检索到的原始结果（可能含噪声/重复/无关项，请你自行甄别）：\n"
    "{evidence}\n\n"
    "请按你的输出规范，给出最终总结。"
)


MEMORY_SECTION_MARKER = "\n## 记忆与知识\n"
MEMORY_SHARED_EXPERIENCE_HEADER = (
    "\n# 我们的梗（黑历史？）\n这些梗你和这地上之人之前聊过：\n"
)
MEMORY_WORLD_HISTORY_HEADER = "\n# 世界知识 (来自联网检索)\n"
MEMORY_WORLD_HISTORY_LOCAL_HEADER = (
    "\n# 世界知识 (本地历史 - 搜索服务暂时下线，仅提供本地存档)\n"
)
MEMORY_IMAGE_CONTEXT_HEADER = (
    "\n# Image Context\n天子，你瞥了一眼地上之人递来的图，虽不屑，但你看见了：\n"
)


REPLY_TIMESTAMP_PREFIX_SYSTEM_NOTE = (
    "\n\n【重要】对话上下文中消息开头若出现时间标记"
    "（如 `（N分钟前）`、`（今天上午3点）`、`（7月23日 星期四 17时03分）` 等各类日期/相对时间前缀），"
    "或形如 `[YYYY-MM-DD HH:MM:SS UTC+8]` 的前缀，"
    "它们只是系统注入的时间戳元数据（用于定位上下文），不是用户或你需要复写的内容。"
    "请在最终回复中不要复写、不要引用、不要保留这类前缀；直接从正文开始回答。"
)



TOOL_ARGS_PARSE_ERROR_TEMPLATE = (
    "Error: 工具 {name} 的参数无法解析（{error}）。"
    "请重新发起调用，并确保参数是合法 JSON。"
)
TOOL_ARGS_PARSE_ERROR_OPENAI_TEMPLATE = (
    "Error: 工具 {name} 的参数无法解析（{error}）。"
    "请重新发起调用，并确保 arguments 是合法 JSON。"
)

from _rp_role import role_text as _role_text
for _k, _v in list(globals().items()):
    if isinstance(_v, str) and any(_s in _k for _s in ("PROMPT", "PROTOCOL", "TEMPLATE", "CUE")):
        globals()[_k] = _role_text(_v)
