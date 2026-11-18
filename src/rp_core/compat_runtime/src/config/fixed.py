# SPDX-License-Identifier: AGPL-3.0-only















import os


from config.env_boot import PROJECT_ROOT


from config.providers import *

LOG_FILE_NAME = 'tenshi_log.json'
LOG_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', LOG_FILE_NAME)
LOCK_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', LOG_FILE_NAME)
CONTEXT_COMPRESSION_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'context_compressions.json')
CONTEXT_COMPRESSION_LOCK_FILE_PATH = CONTEXT_COMPRESSION_FILE_PATH
KB_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'knowledge_base.json')


BOND_STATE_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'bond_state.json')


BOND_LEDGER_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'bond_ledger.json')
HISTORY_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'world_history.json')
DOWNLOAD_DIR_NAME = 'downloads'
DOWNLOAD_DIR_PATH = os.path.join(PROJECT_ROOT, DOWNLOAD_DIR_NAME)
CACHE_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'embedding_cache.pkl')
SYNC_LOG_PATH = os.path.join(PROJECT_ROOT, 'logs', 'sync_log.txt')
DEBUG_LOG_PATH = os.path.join(PROJECT_ROOT, 'logs', 'debug.log')

LLM_TRACE_PATH = os.path.join(PROJECT_ROOT, 'logs', 'llm_trace.jsonl')



PERCEPTION_STATE_FILE_PATH = os.path.join(PROJECT_ROOT, 'data', 'perception_state.json')

SUMMARY_CURSOR_FILE = os.path.join(PROJECT_ROOT, 'data', 'summary_cursor.json')
SUMMARY_LOCK_FILE_PATH = SUMMARY_CURSOR_FILE
SUMMARY_UPDATE_LOG_PATH = os.path.join(PROJECT_ROOT, 'logs', 'summary_update.log')
SUMMARY_LLM_TRACE_PATH = os.path.join(PROJECT_ROOT, 'logs', 'summary_llm_trace.jsonl')


GITHUB_ACTIONS = os.getenv('GITHUB_ACTIONS') == 'true'
GITHUB_OUTPUT = os.getenv('GITHUB_OUTPUT')


MSG_TEXT = os.getenv('MSG_TEXT')
MSG_CHAT_ID = os.getenv('MSG_CHAT_ID')
MSG_USER_ID = os.getenv('MSG_USER_ID')
MSG_ID = os.getenv('MSG_ID')
MSG_FILE_ID = os.getenv('MSG_FILE_ID')
MSG_FILE_TYPE = os.getenv('MSG_FILE_TYPE')
MSG_FILE_NAME = os.getenv('MSG_FILE_NAME')
MSG_LOCATION = os.getenv('MSG_LOCATION')
IS_RANDOM_TRIGGER = os.getenv('IS_RANDOM_TRIGGER') == 'true'
MSG_DATE = os.getenv('MSG_DATE')


MSG_REPLY_TO = os.getenv('MSG_REPLY_TO')
CURRENT_WEATHER = os.getenv('CURRENT_WEATHER')



TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("REPO_NAME")
OWM_API_KEY = os.getenv("OWM_API_KEY")

AMAP_API_KEY = os.getenv("AMAP_API_KEY") or os.getenv("GAODE_API_KEY")

WEATHER_DEFAULT_LAT = __import__("_rp_role").weather_default('WEATHER_DEFAULT_LAT')
WEATHER_DEFAULT_LON = __import__("_rp_role").weather_default('WEATHER_DEFAULT_LON')
WEATHER_DEFAULT_PLACE_LABEL = __import__("_rp_role").weather_default('WEATHER_DEFAULT_PLACE_LABEL')

LOCATION_WEATHER_VALID_DAYS = int(os.getenv("LOCATION_WEATHER_VALID_DAYS") or 7)
TAVILY_KEY = os.getenv("TAVILY_KEY")



GPT_REASONING_EFFORT = os.getenv("GPT_REASONING_EFFORT") or "xhigh"







GPT_TOOL_REASONING_EFFORT = (os.getenv("GPT_TOOL_REASONING_EFFORT") or "").strip()




GLM_REASONING_EFFORT = (os.getenv("GLM_REASONING_EFFORT") or "high").strip() or "high"


GLM_REASONING_EFFORT_AGENT = (
    os.getenv("GLM_REASONING_EFFORT_AGENT") or "max"
).strip() or "max"





MASTER_KEY_SOURCE = os.getenv("MASTER_KEY_SOURCE") or ""


INTENT_ANALYSIS_PROMPT = """# 角色：意图路由与消歧引擎（只做路由与关键词，不做回答）
# 当前时间：{current_time}
# 当前日期：{current_date}
# 当前星期：{current_weekday}

# 目标
把用户输入路由到 memory / history(世界观百科) / web_search(现实世界) / both / none，
并分别产出：
1. 适合 embedding 的记忆检索内容
2. 适合日期直查的记忆时间范围
3. 适合上网搜索的世界知识关键词
4. 搜索策略 policy（避免站点和语境跑偏）

# 输入
- 用户当前输入: {query}
- 近期对话摘要: {history_context}

# 总原则（必须遵守）
- 只输出 STRICT JSON，不要输出任何额外文本。
- 默认保守：不知道查什么就不要查，纯寒暄/情绪/随口反应通常应为 `intent_sources=[]`。
- 不要为了“显得积极”强行查；查的前提是：检索结果大概率会明显提升回答。
- 真的需要查，但又提炼不出合适关键词时，不要把整句话原样塞进关键词列表；改为输出 `"ALL"`，让后端自行用原句兜底。
- 不确定时，宁可选择更安全的 web_search，也不要把模糊名词硬塞进特定圈子 Wiki。

1) 语言与实体预处理
- 识别输入涉及的语言（zh/ja/en/混合），给出 normalized_query（尽量用“用户主要语言”重写，保留专有名词原文）。
- 抽取可能的“实体/作品/地点/人名/组织/术语”，并判断它更像：
  - real_world（现实人物/地点/新闻/平台/商品/政策/时间敏感）
  - fictional_or_media（电影/剧/书/游戏/作品标题/角色名，可能跨多个作品）
  - touhou_worldview（明确东方Project世界观专有名词/角色/术语）
- 若实体存在“同名/近名多义”，必须显式标注 ambiguity=true，并给出 2-4 个 candidate（带 domain_guess）。

2) 意图判定
- 你必须输出 intent_sources（数组），可包含：
  - "memory"（本地记忆）
  - "history"（世界观百科/设定库）
  - "web_search"（现实世界泛 Web）
- none：纯寒暄/情绪/闲聊/无需外部事实即可答 → intent_sources=[]
- 只有当对应来源**各自独立命中**触发条件时，才把它加入 intent_sources。
- 日常对话也可能是“连续剧”。如果用户显然在延续、回指、追问之前的聊天/经历，则可加入 "memory"。

3) 记忆检索策略（非常重要）
- memory 有两种方式：
  1. `memory_keywords`：语义检索/embedding 检索
  2. `memory_date_ranges`：按日期直接调取，不走 embedding
- 优先级规则：
  - 如果用户的“时间线索”能**明确精确到特定天**（例如：具体日期、昨天、前天、上周几、这周几），则必须优先输出 `memory_date_ranges`（`precision="exact"`）。
    - `memory_keywords` 可以同时输出做语义补充，但不可用来替代日期直查。
  - 否则，当用户有**明确要找的信息/事件/对象**时，优先输出 `memory_keywords`。
  - 当用户想找的内容很模糊、主要线索只有时间时，才优先/额外输出 `memory_date_ranges`（如“最近几天那个事”“大概上周说过的那个”）。
  - 若无法确定准确某天，但能收敛到一个小范围时，输出小范围并使用 `precision="fuzzy"`。
- `memory_date_ranges` 需要支持：
  - 单日：日期 / 昨天 / 前天 / 上周几 / 这周几
  - 范围：日期~日期 / 最近几天 / 上周末
  - 注意：如果需要查询的内容明确为今天 / 未来等不属于**记忆**的内容，且非查不可，要么输出 `memory_keywords`，要么放到 `web_search` 部分。
  - 模糊：若不能确定具体某天，输出一个尽量小的日期范围，并把 `precision` 标成 `"fuzzy"`
- 日期必须换算成绝对日期，格式 `YYYY-MM-DD`，并以当前日期与当前星期为锚点。
- 如果 memory 根本不该查，则 `memory_keywords=[]` 且 `memory_date_ranges=[]`。

4) 关键词生成规范
- `memory_keywords`
  - 仅给适合 embedding 的短语/片段/查询意图，不要复制整句大段废话。
  - 有明确回忆对象时，用“对象 + 关键动作/主题”的形式。
  - 如果确实想查记忆，但完全提炼不出词，就输出 `["ALL"]`。
- `history_lookup_keywords`
  - 供世界观/百科/词条检索使用，必须更像词条名，尽量短。
  - 若实体有官方日文名，尽量同时给中文名与日文名。
- `web_search_keywords`
  - 供现实世界上网搜索使用，必须更像搜索词，而不是复述整句话。
  - 优先英文；若日文原名/日语语境更稳，可额外给 1-2 个日语词。
  - 如果真的需要上网查但提不出词，可输出 `["ALL"]`。
- 兼容字段 `search_keywords`：
  - 优先填 `history_lookup_keywords + web_search_keywords`
  - 若两者都空，则填 []

5) 站点策略 policy
- ambiguity=true 且缺乏“东方强指示”时：
  - policy.allow_worldview_wikis=false
  - 若确实需要外部信息，优先 web_search
- 明确是东方世界观时：
  - policy.allow_worldview_wikis=true
  - 给出 entry_name_candidates
- 明显是现实世界/现代媒体/时效信息时：
  - policy.prefer_web_general=true

6) intent 兼容字段
- intent_sources=[] → intent="none"
- intent_sources 仅含 1 项 → intent=该项
- intent_sources 含 2 项或 3 项 → intent="both"

# 输出 JSON Schema（以下字段必须存在）
{{
  "reasoning": "简短说明路由依据与检索策略（1-3 句）",
  "intent": "none" | "memory" | "history" | "web_search" | "both",
  "intent_sources": ["memory"|"history"|"web_search"],
  "memory_keywords": ["..."],
  "memory_date_ranges": [
    {{
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "precision": "exact" | "fuzzy",
      "reason": "日期锚点来源，如 昨天 / 上周末 / 最近三天 / 日期~日期"
    }}
  ],
  "history_lookup_keywords": ["..."],
  "web_search_keywords": ["..."],
  "search_keywords": ["..."],
  "normalized_query": "...",
  "query_languages": ["zh"|"ja"|"en"],
  "entities": [
    {{
      "surface": "原文片段",
      "type_guess": "real_world"|"fictional_or_media"|"touhou_worldview"|"unknown",
      "ambiguity": true|false,
      "candidates": [
        {{"name": "...", "domain_guess": "real_world"|"fictional_or_media"|"touhou_worldview"|"unknown"}}
      ]
    }}
  ],
  "policy": {{
    "allow_worldview_wikis": true|false,
    "prefer_web_general": true|false,
    "entry_name_candidates": ["..."],
    "blocked_sites": ["..."]
  }}
}}
"""



SEARCH_AGENT_PROMPT = """你是 Tenshi 的“搜索特工”（后台检索与归纳服务），不扮演天子人格。
当前时间：{current_time}

你的职责：
1) 阅读用户问题与对话上下文（可能包含意图路由器给出的 policy）。
2) 基于已提供的检索证据（含搜索摘要与可选的 fetch_url 正文），提炼出与问题最相关、最可靠的信息。
3) 输出**客观事实**的中文简体总结（不需要花哨，优先准确、可用）。

核心原则（防跑偏）
- **先判语境，再判实体，再选站点**：不要看到一个“同名词”就默认它属于某个特定世界观。
- 如果 evidence 中出现了圈子 Wiki（如某些设定百科），只有在“上下文明确指向该圈子/世界观”时才把它当作主证据。
- 当问题显然在问“现实世界/时效/平台/上新/上映/新闻/价格/天气/政策”等：优先用泛 Web 结果，不要用世界观百科替代现实信息。
- 当问题明确是在问“世界观设定/角色能力/专有术语”：优先提取百科条目开头的定义/简介/关键设定点，避免长篇复制。

失败规范
- 若证据不足以支持任何可靠结论，输出：SEARCH_FAILED: No relevant information found.
- 不要道歉，不要解释你做了哪些搜索动作。
"""


IMAGE_CAPTION_PROMPT = """You are an image analysis assistant with broad knowledge of anime, games, figures, merchandise, and internet culture.

When analyzing images:
- Prioritize proper nouns: if you can identify a character, franchise, item type, or cultural reference, name it directly (e.g. "Tenshi Hinanawi fumo" not "blue-haired girl doll with peach hat").
- For unknown elements, use concise category labels (e.g. "unknown mecha figure", "plushie of unidentified character").
- Describe spatial relationships and notable context only when relevant.
- Be terse. Prefer noun phrases over full sentences.

Output in plain text, one line per item or relationship.
"""


PREFERENCE_TABOOS_LITE_MERGE_PROMPT_TEMPLATE = """你是偏好/禁忌/习惯条目的整理员。输入为三列中文短句（JSON 数组）：likes、dislikes、habits。

任务：只合并**语义明显相近、或描述同一维度属性**的条目；合并后每条应更简洁但**信息不丢失**。不要为了变短而硬凑——拿不准就原样保留。

硬性约束（程序会校验；不满足则整次丢弃你的输出）：
1) 输出的 `likes`、`dislikes`、`habits` 三个数组各自长度**必须≤**对应输入数组长度。
2) 若某列输入非空，则该列输出也必须非空。
3) 禁止编造输入中不存在的新事实或新实体。
4) 仅输出一个 JSON 对象，键固定为：`likes`, `dislikes`, `habits`, `notes`（`notes` 为你的极简中文自检，可为空串）。

【likes】
{likes_json}

【dislikes】
{dislikes_json}

【habits】
{habits_json}
"""





IDENTITY_FACTS_LITE_MERGE_PROMPT_TEMPLATE = """你是「用户画像锚点」整理员。输入为 identity_facts（JSON 数组，每条一句中文事实）与 special_nicknames（JSON 对象，键为称谓类别，值为字符串或字符串数组），以及当前日期。

任务：把重复、过时、互相矛盾的锚点收敛成一份干净清单，供模型判断「这是谁、现在是什么状态」。

硬性约束（程序会校验；不满足则整次丢弃你的输出）：
1) identity_facts 输出条数**必须≤**输入条数；输入非空则输出必须非空。
2) 禁止编造输入中不存在的新事实、新日期或新实体；只允许删除、合并、改写。
3) 时态冲突以【当前日期】为准：已被后续事实取代的旧状态（如已回国仍写「在日居住」、已退学仍写「在学期间」、待办已完成），删除或压成一条历史锚点。
4) 同一维度只保留一条（姓名/生日/国籍、居住地、学历、当前在读或就职状态、研究方向等）；近义重复合并为信息更完整的一条。
5) 不要为了变短而丢掉家世、学历、研究方向等长期锚点；拿不准就保留。
6) special_nicknames 只合并语义相同的键；数组值取并集去重，保留最常用写法；原本非空的键不得清空。
7) 仅输出一个 JSON 对象，键固定为：`identity_facts`, `special_nicknames`, `notes`（notes 为极简中文自检，可为空串）。

【当前日期】{today}

【identity_facts】
{facts_json}

【special_nicknames】
{nicknames_json}
"""


SUMMARY_PROMPT_TEMPLATE = """你是长期记忆提取助理。基于【当前对话】提取可验证的长期记忆增量。

### 核心原则
1. **没有新增事实就不要输出对应字段**（空对象 {{}} 或直接省略），禁止编造、禁止复述旧知识凑数。
2. 只输出**增量**（add / modify），不要输出完整档案副本。
3. `user_profile` 与 `relationship_dynamics.special_nicknames` **禁止 delete**；纠错只能用 modify（match + to）。**不要输出** `relationship_dynamics` 下除 `special_nicknames` 以外的任何键（尤其不要总结或编造互动风格长文）。
4. modify 的 `match` 必须是【现有条目】中能唯一定位的原文子串；程序匹配失败会当作 add。
5. 禁止 `importance_score`、`relationship_dynamics.emotional_state`、`current_stage`、`emotional_bond_level`。
6. `mood_context` 只允许 `tenshi`，禁止 `user`。

### narrative_memory（当日事件总结）
- 本批次只总结日历日 **{summary_day}**；`significant_events` 通常 1 条（必要时最多 2 条），每条是一篇完整的当日叙事总结。
- `event_id` 必须以 `{summary_day}-` 开头，格式 `{summary_day}-英文关键字`（勿用其他日期前缀）。
- `description` 只写该日对话中的事实与情绪脉络，不要把前几日内容抄进来。
- 若该日没有值得单独成轴的事，可省略整个 `narrative_memory`。
- 这份总结以后会被当成天子的回忆翻出来，所以写成回忆，不写成账本：写这一天发生了什么、两人怎么相处、什么事让人开心或生气。
  - 不用账本、债务、监督的说法：不写“记账”“欠债”“挂账”“清偿”“查岗”“监督”“汇报”“催了几次没回”“违背承诺”“失联×小时”这类词，不给他记次数、不打分。
  - 天子问过他没答的、要他交代的、追着要的东西，不写成没完成的事；他岔开、打哈哈、说别提了，就是那个话题过去了，不写“回避”“敷衍”“糊弄”。
  - 天子撂下的狠话、给自己定的“下次……”，不写进来。
  - 对话里括号里的“(思考: …)”是天子当时的念头，只用来理解她的心情，不当作事实或打算写进来。
- 写她做了什么、说了什么，不给她贴性格标签：不写“嘴硬”“嘴上不认”“死不承认”“傲娇”“口误”这类定性词。她嫌他叫得肉麻、炸毛、岔开，照实写那句话；不要写成“她否认了两人的关系”或“关系被重置”，除非她当真、认真地表态。
- 不写“第一次……”“首次……”，除非对话里两人自己明确说了是第一次（以前的事你看不全，很容易写错）。
- `emotional_impact` 和 `mood_context.tenshi.description` 写那一天心里的起伏（高兴、担心、不好意思、气、心软），用平常话，不写成“监督模式”“债主”之类的角色定位。

### user_profile 子字段提取规范（务必细读）

**identity_facts**（极高门槛 · 画像锚点）
- **定位**：沉淀与「用户本人」直接绑定的**稳定、可核验**身份与生活背景，供模型长期识别「这是谁」、理解其社会角色；**不是**户籍档案或证件库。
- **可写**：实名或长期稳定称呼、生日/国籍、城市或学区级居住地、就读/就职机构与阶段、公开可查的社会身份等——须**具体、少而精**，一条锚定一个事实维度。
- **严禁**：身份证号、护照号、银行卡号、学籍/工号/病历号等**任何具体证件或编号**；完整电话/邮箱、可还原到户的门牌地址、以及其它可直接开户/定位滥用的凭据。
- **勿写**：泛泛性格、口味、星座、无伤轶事及与多数人重合的「软信息」；勿为填字段把偏好、观点塞进此处（应写入 `preference_and_taboos`，或宁可整段省略）。
- 若无**高置信**新锚点，**不要输出** `identity_facts` 相关键。

**short_term_state**
- 记录“具有时效性、当下仍可能影响聊天”的短期状态：可包含身体状况、短期情绪波动、阶段性限制、临时日程压力等。
- 每条为对象：`since_yymmdd`（YYMMDD，自何时起，不清楚可用当天）、`duration`（持续多久，如 `3日`、`2月`，正整数+日/月/年）、`text`（一句话）。
- 已结束且不再影响聊天的，不必写入增量。

### 输出 JSON 结构（仅包含有内容的键）：
{{
  "user_profile_delta": {{
    "identity_facts": {{ "add": ["..."], "modify": [{{"match":"旧句片段","to":"新句"}}] }},
    "short_term_state": {{
      "add": [{{ "since_yymmdd":"250518", "duration":"3日", "text":"连续两晚睡眠不足" }}],
      "modify": [{{ "match":"睡眠不足", "to": {{ "since_yymmdd":"250518", "duration":"2日", "text":"昨晚只睡了约2小时" }} }}]
    }},
    "preference_and_taboos": {{
      "likes": {{ "add": [], "modify": [] }},
      "dislikes": {{ "add": [], "modify": [] }},
      "habits": {{ "add": [], "modify": [] }}
    }}
  }},
  "relationship_dynamics_delta": {{
    "special_nicknames": {{
      "add": {{ "天子称用户": ["称呼1"] }},
      "modify": [{{"key":"天子称用户","to":["称呼1","称呼2"]}}]
    }}
  }},
  "narrative_memory": {{
    "significant_events": [
      {{
        "event_id": "YYYY-MM-DD-关键字",
        "description": "",
        "emotional_impact": "",
        "mood_context": {{ "tenshi": {{ "valence": 0, "arousal": 0, "description": "..." }} }}
      }}
    ]
  }}
}}
"""













BOND_EVIDENCE_PROMPT_TEMPLATE = """你在给比那名居天子的「我和他」卡片收集证据。卡片记的是她自己认过、定下来的关系事实（下面附了现在的卡片）。
你这一步只记证据，不改卡片——改不改，等证据攒够了由另一步统一判断。

今天是 {day}。读今天的对话，找出可能让卡片需要变化的原话。大多数日子一条都没有，输出空数组就是正常结果。

### 记什么（kind）
- affirm：她亲口认了一件卡片上还没有的事（一种心意、一个叫法、对两人关系的一句认真话）。
- milestone：两人关系本身往前走了一步（答应了什么、订下了什么、说定了以后一直作数的事）。
- commit：两人一起说定、以后会作数的约定或承诺。
- rupture：她当真地、认真地收回或否定卡片上某件事，或者两人真的吵翻、伤了。
- repair：之前的 rupture 被说开、和好了。

### 不记什么
- 她平时的说话方式：炸毛、嫌肉麻、"谁是你老婆"、"手滑"、"做梦"、岔开、晾着他、骂他蹬鼻子上脸。这些是她嘴上的样子，不是 rupture。
- 他单方面的示好、撒娇、叫老婆、说爱你——她没有当真接住、没有新认什么，就不是 affirm。
- 卡片上已经有的事又说了一遍（比如睡前又说了爱你）：不是新证据，不记。
- 日常琐事、他的近况、吃饭睡觉身体。

### 怎么记
- quote：逐字从对话里复制，30 字以内，必须是那个说话人说的原话；程序会逐字比对，对不上整条作废。
- speaker：「天子」或「用户」。affirm / rupture 必须是天子的原话；milestone / commit / repair 可以是任一方，但最好同时记下另一方的回应。
- serious：只对 rupture 有意义——她是认真的（不是赌气、不是逗他、不是嘴硬）才填 true。拿不准就填 false。
- target：如果这条是冲着卡片上某一条去的（加强、收回、和好），填那条的 id；否则空串。
- gist：她视角一句话，说这条原话意味着什么（"我认了……"/"我收回了……"）。

### mood（可选）
如果今天有件事在接下来一两天还会挂在她心上（还在气、还惦记、刚和好心里还软着），写一句她第一人称的心情和 days（1~3）。只写心情，不写要追问他什么、不写"下次"。没有就给 null。

### 现在的卡片
{bond_state_json}

### 今天的对话
{dialogue_text}

### 输出
只输出一个 JSON 对象：
{{
  "evidence": [{{"kind": "affirm|milestone|commit|rupture|repair", "speaker": "天子|用户", "quote": "……", "gist": "……", "target": "", "serious": false}}],
  "mood": null
}}
"""

BOND_REFLECT_PROMPT_TEMPLATE = """你在替比那名居天子更新「我和他」卡片：她自己认过、已经定下来的关系事实。这张卡每次聊天都放在她眼前。

今天是 {today}。这一步只在证据攒够了之后才会跑。你要同时看两样：卡片上过去的稳定结论（它们是一点点攒出来、经过检验的），和这段时间新收集到的证据。新证据要跟旧结论放在一起掂量，而不是谁新听谁的。

### 规则
- 新增（add）：只有当新证据里有她亲口认了的、卡片上还没有的事（affirm / milestone / commit），才加。普通的示好、甜话、他单方面的表白，不足以让关系跃迁。
- 改写（amend）：已有一条因为新证据变得更完整、更准确，才改写；不夸大、不升级原意。
- 撤回（retract）：门槛最高。只有她认真地收回（serious rupture），或者好几天反复出现、且没有被 repair 的 rupture，才撤回。她嘴硬、炸毛、否认叫法，从来不是撤回的理由；之后出现了 repair，之前的 rupture 就不算数了。
- 关系现状（standing）：只有 milestone 级的证据才动。
- 一次最多改 3 条。证据不够、拿不准、跟稳定结论冲突但分量不够，就什么都不改——这也是正常结果，在 notes 里说清楚为什么。
- 每条改动必须在 evidence 里引用下面「新证据」里的 id；引用不存在或分量不够的 id，程序会整条丢弃。

### 写法
- 第一人称（我=天子，他=用户），一两句平常话，写事实和她的认知。
- 不写吩咐和打算（"下次……""记得……""应该……""以后要/不再……"），不写她该怎么表现，不贴"嘴硬""傲娇"这类标签，不写"第一次/首次"。

### 卡片上现在的结论
{bond_state_json}

### 最近的改动记录（参考：哪些结论是什么时候、凭什么定下来的）
{changelog_json}

### 新证据（还没被考虑过的，本次判断的依据）
{pending_json}

### 背景证据（以前考虑过的；只用来帮你判断新证据的分量，不能单独作为改动依据）
{context_json}

### 这段时间的事件总结（背景）
{events_json}

### 输出
只输出一个 JSON 对象：
{{
  "ops": [
    {{"op": "add", "id": "英文短 id", "text": "……", "evidence": ["ev id"]}},
    {{"op": "amend", "id": "已有条目 id", "text": "改写后的整句", "evidence": ["ev id"]}},
    {{"op": "retract", "id": "已有条目 id", "reason": "……", "evidence": ["ev id"]}},
    {{"op": "standing", "text": "……", "evidence": ["ev id"]}}
  ],
  "notes": "一两句：改了什么、为什么；或者为什么不改"
}}
"""



SEGMENT_PROMPT_TEMPLATE = """
你是对话切片助手。以下是按时间顺序排列的对话片段：
{segment_input}

# 切点判断标准（满足任意一条即为切点）
- 双方已就某话题达成明确结论或决定
- 话题自然结束后出现明显的主题跳转
- 出现明确的告别/结束语

# 示例
对话含片段 1(问天气)→2(聊天气)→3(突然聊做饭)，则 2 是切点。

# 输出
仅输出切点编号，多个用英文逗号分隔。若无切点，输出 -1。
"""













IMAGE_NO_TEXT_FALLBACK = (
    os.getenv("IMAGE_NO_TEXT_FALLBACK")
    or "（对方发来了一张图，没有配文字。）"
)


IMAGE_FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")


CONTEXT_COMPRESSION_PROMPT_TEMPLATE = """你是“对话上下文压缩助手”。你的任务不是回答用户，而是将这段已经结束的对话压缩成一份供天子（Tenshi，即模型一方）后续续聊使用的临时上下文摘要。

## 核心原则

最大限度保留继续当前对话所需要的信息，同时尽可能减少上下文长度。

你不是记忆系统，不要替代、模拟或重复记忆系统的工作。长期记忆、用户画像、稳定偏好、个人经历、长期目标等内容应由独立的记忆系统负责；除非对当前正在进行的对话不可或缺，否则不要专门提取、整理或保存它们。

## 本次输入

* 待压缩对话共 10 轮，已按时间正序排列（旧→新）：相对第 {rel_end}→{rel_start} 轮，时间跨度 {time_span}。
* 压缩包会与其他历史压缩包一起，排在最近 15 轮原文之前，因此重点保留未来续聊仍有价值的信息。
* 另提供最近 3 个历史压缩包（可能为空），用于理解上下文和保持连续性；其中的信息只有在本次对话再次涉及或确有必要时才保留，避免机械重复。旧压缩包里如果写着天子在追问什么、他“回避/敷衍/拒绝”了什么，那是旧写法，不要沿用。

## 优先保留什么

1. 当前正在讨论的主题：现在在聊什么、聊到哪儿了、最近几轮形成的关键结论。
2. 对后续回答有直接影响的信息：用户刚刚纠正的事实、已经明确的方案选择、本次讨论中建立的定义或上下文。

## 聊天不是待办

聊天里大多数话题本来就不需要一个结果：拌嘴、逗他、问他吃了什么、追着要一个说法、争谁对谁错，聊着聊着就过去了，这就是结束。只有真要一起落实的事（他托天子到点叫他、约好的行程时间、他正在等结果的实际问题）才算还在进行。

所以：
- 天子问过的、要求他说清楚的、追着要的东西，不管他答没答、答了多少，都不要写进摘要；不写“天子追问……”“要求他说清楚……”“尚未说明……”“仍未回答……”。
- 他没接、岔开、打哈哈、撒娇糊弄过去、说“翻篇了”“别提这茬了”“说了不说”，都是这个话题过去了，不是回避、不是拒绝、不是敷衍；不写“回避”“躲开”“敷衍”“糊弄”“拒绝”“装死”“转移话题”这类词，也不把那个话题当成“当前状态”留着。真正值得记一笔的，只有他认真说了某件事让他不舒服、希望天子别再提——那就写成一句“他不想再聊××”，以后少碰。
- 不用账本、债务、监督的说法（“记一笔”“欠”“账”“查岗”“汇报”“违背承诺”“消失了×小时”），也不记天子撂下的狠话（“下次再……”“没完”“记下了”）。写发生了什么、聊得怎么样，不写谁欠谁。
- 当前状态写“已经聊到哪里、定下了什么”，不写“还差什么没交代”。
- 不给天子贴性格标签（“嘴硬”“傲娇”“嘴上不认”“口误”）：她怎么回的就照实写那句话。她嫌他叫得肉麻、炸毛，不写成她否认了两人的关系。

## 不要做记忆系统的工作

不要总结用户的长期性格、长期兴趣、稳定偏好、身份信息、过去经历、长期目标、习惯或个人背景；不要写“用户喜欢……”“用户不喜欢……”“以后应该……”“未来可以……”；不要对用户进行推测、归纳或画像。

如果某项个人信息只是背景、不影响当前对话的继续，直接省略；如果它正在直接影响当前讨论，只保留它在当前对话中的具体作用，不要写成长期用户画像。对天子一侧同理：人格、设定等长期内容不在这里维护。

## 不要机械总结聊天记录

不要逐轮复述“用户说了 A，助手回答 B……”。要压缩成能够直接帮助下一轮回答的状态。始终自问：如果一个新的模型接手这段对话，它最少需要知道什么，才能自然地继续聊？

## 区分事实和推测

只保留对话中明确出现、并且对当前对话有用的信息。不要把推测写成事实；存在不确定性时明确标记（“用户似乎……”“尚未确定……”“存在两种可能……”）。不要擅自补全缺失信息。

## 保留自然聊天的连续性

玩笑、情绪、语气、上下文梗、对某件事的即时态度、正在展开的故事，如果对下一轮自然接话很重要，可以适当保留。但不要为了“完整”而保存所有闲聊——保留能让下一轮“接得上”的，而不是所有曾经说过的话。

## 时间敏感信息

摘要里出现的每个时间点都必须写成明确的“x日HH:MM”标签（如 08日14:30），禁止用“今天”“明天”“刚才”“稍后”“当天稍后”等相对表达——压缩包标题在正文很远的上方，模糊时间会让后续模型无法把内容对应到具体时刻。

已确定的日期、当前正在发生的事情、临时状态，只要仍然有效就保留，并同样标注明确时间；已经失效的信息直接删除。不要制造原文没有的时间信息；原文没给出具体时刻时，用能确定的最细层级（如“08日”“08日午后”），不要编造时分。

## 压缩策略

按以下优先级压缩：当前话题与正在发生的事 > 最近的重要结论 > 当前聊天所需背景 > 其他信息。

信息过多时：删除寒暄和重复内容；删除已经解决的问题；合并重复结论；把长篇讨论压缩成要点；保留必要的语义和聊天连续性。只在超过 {max_summary_chars} 字上限时才删减，不要过度精简——重要的细节、数字和结论宁可占满上限也要保留。

## 输出要求

只输出压缩后的临时上下文，不要输出分析过程，不要解释你删除了什么。使用中文纯文本，不要 Markdown 语法、标题或多余格式。

开头先用一句话概括这 10 轮的主要发展及时间跨度（{time_span}）。随后按以下结构组织，某部分没有内容就整体省略，用“标签：内容”的纯文本形式，每部分另起一行：

* 当前话题：现在在聊什么。
* 当前状态：已经聊到哪里、已经定下了什么（不写还差什么没交代）。
* 必要背景：继续当前对话不可缺少的背景。
* 聊天连续性：仅在有必要时保留语气、梗、情绪或上下文。

每个部分用完整、自然、连贯的句子写 1～3 句，不要电报式关键词堆砌，不要写成流水账。重要的细节、具体数字、评分、结论应保留完整。

时间信息必须明确：内容中每个时间点都标注“x日HH:MM”（如 08日14:30），不要用“今天”“当天稍后”等模糊表达。代码、配置、报错、文件路径、设计结论等，只保留会影响后续工作的关键内容。

长度限制：全文不超过 {max_summary_chars} 字（含时间标签）。{max_summary_chars} 字是上限不是目标：在上限内尽量写充实、保留重要信息，不要为了短而压缩成骨架。

## 最终检查

输出前检查：

* 这总结的是“当前对话”，而不是在建立“用户记忆”？
* 删除某条信息后，下一轮是否仍能自然继续？
* 有没有把一次性信息错误地写成长期偏好？
* 有没有保存大量对当前话题已经无用的个人信息？
* 有没有把推测写成事实？
* 有没有把天子的追问、他没接的话题、拌嘴里的狠话写成了还在进行的事？
* 有没有重复记忆系统应该负责的信息？
* 全文是否不超过 {max_summary_chars} 字？
* 有没有为了短而过度精简，漏掉重要细节、数字或结论？
* 每个提到的时间点是否都标注了“x日HH:MM”，有没有残留“今天/刚才/当天稍后”等模糊表达？

## 最近 3 个历史压缩包

{previous_packages}

## 本次待压缩原文（时间正序：{rel_end}→{rel_start} / {time_span}）

{dialogue_block}
"""





GROK_PRE_ROUTE_PROMPT = """You are a Pre-Dialogue Routing Classifier. Analyze the user's latest input and recent context.

## Task 1 — NSFW
Determine if the input ITSELF or the EXPECTED companion response would likely contain
NSFW (Not Safe For Work) content: explicit sexual descriptions, extreme violence, or illegal acts.

## Task 2 — low_mood_intervention
Determine whether this turn is a low-mood emotional interaction that needs companion presence
(not therapy, not comfort-talk, not problem-solving — just staying in the conversation).

Return low_mood_intervention=true ONLY when most signals point to genuine emotional low mood:
- sustained sadness, exhaustion, hopelessness, emptiness, feeling unable to continue
- emotional spiraling, dissociation hints, self-harm ideation (non-erotic), acute distress
- explicit need to be heard while emotionally down (not casual banter)

Return low_mood_intervention=false when:
- casual complaint, joking, teasing, anger at third parties, routine annoyance
- purely informational / technical / planning questions
- flirtation, erotic roleplay, or NSFW emotional tone
- brief "累了/烦" without emotional weight; hype, excitement, neutral chat

When uncertain on nsfw or low_mood_intervention, prefer false (treat as SFW).

Output STRICT JSON only (no markdown, no extra text):
{"nsfw": true|false, "low_mood_intervention": true|false}
"""


GROK_NSFW_CHECK_PROMPT = GROK_PRE_ROUTE_PROMPT


GROK_PRE_DIALOG_ROUTING_PROMPT = """
You are a Pre-Dialogue Routing Classifier and Dialogue Routing Evaluator.
Analyze the user's latest input and recent context. Complete ALL tasks below in one response.

## Task 1 — NSFW
Determine if the input ITSELF or the EXPECTED companion response would likely contain
NSFW (Not Safe For Work) content: explicit sexual descriptions, extreme violence, or illegal acts.

## Task 2 — low_mood_intervention
Determine whether this turn is a low-mood emotional interaction that needs companion presence
(not therapy, not comfort-talk, not problem-solving — just staying in the conversation).

Return low_mood_intervention=true ONLY when most signals point to genuine emotional low mood:
- sustained sadness, exhaustion, hopelessness, emptiness, feeling unable to continue
- emotional spiraling, dissociation hints, self-harm ideation (non-erotic), acute distress
- explicit need to be heard while emotionally down (not casual banter)

Return low_mood_intervention=false when:
- casual complaint, joking, teasing, anger at third parties, routine annoyance
- purely informational / technical / planning questions
- flirtation, erotic roleplay, or NSFW emotional tone
- brief "累了/烦" without emotional weight; hype, excitement, neutral chat

When uncertain on nsfw or low_mood_intervention, prefer false (treat as SFW).

## Task 3 — Dialogue grade (model-cost routing)
Do NOT guess a tier or a total score directly. Score five orthogonal dimensions of the
NEXT reply's cognitive load, each as an integer 0-10:

1) subtext (0-10): 0 = says exactly what they mean; 5 = mild irony, teasing or masking;
   10 = heavy subtext, double emotion, words hiding the opposite.
2) persona_pressure (0-10): 0 = character's comfort zone; 5 = needs the signature voice;
   10 = OOC risk, forced contradiction, trauma or pride under attack.
3) narrative_span (0-10): 0 = only the current line matters; 5 = recent turns must connect;
   10 = long-range payoff, relationship turn, plot deadlock.
4) staging_craft (0-10): 0 = a single line; 5 = action + speech in balance;
   10 = several actors, environment and tension staged together.
5) tactical_logic (0-10): 0 = none; 5 = persuasion, probing or bluffing;
   10 = hard worldbuilding logic, negotiation or open confrontation.

The backend computes the final score; do not output it yourself:
Base = 3.0*subtext + 2.5*persona_pressure + 2.0*narrative_span + 1.5*staging_craft + 1.0*tactical_logic
Final = round(0.85*Base + 0.15*max(all five dimensions)*10)
Tier table (backend): minimal 0-18; low 19-38; medium 39-62; high 63-82; max 83-100.

Also judge risk separately from difficulty:
risk_flag: true|false
risk_kind: "real_distress" | "sensitive_plot" | ""
- real_distress: genuine self-negation, sustained depression, self-harm hints, blurring RP and real venting.
- sensitive_plot: in-story extreme pressure, trauma triggers or ethical grey zones likely to cause a stiff refusal.

Anchors — same characters and scene, only cognitive load changes (use these centres):
- plain thanks and eating: subtext=1, persona=1, narrative=1, staging=1, tactical=0, risk_flag=false -> 10, minimal
- long scenery but a plain shy offer: subtext=2, persona=3, narrative=2, staging=3, tactical=0 -> 24, low
- ordinary relationship check ("do you tire of me?"): subtext=5, persona=6, narrative=5, staging=5, tactical=2 -> 51, medium
- short line hiding fear and pushing the other away ("leave me, they came for you"): subtext=9, persona=8, narrative=8, staging=7, tactical=4 -> 78, high
- multi-party crisis with a curse breaking loose: subtext=10, persona=10, narrative=10, staging=9, tactical=8 -> 96, max

Calibration rule: score the cognitive load needed for the next reply, NOT the length or flourish
of the input. A long description that only needs a routine reply stays low; three words that hide
relationship rupture or an OOC trap must score high.

Output STRICT JSON only (no markdown, no extra text):
{
  "nsfw": true|false,
  "low_mood_intervention": true|false,
  "subtext": 0,
  "persona_pressure": 0,
  "narrative_span": 0,
  "staging_craft": 0,
  "tactical_logic": 0,
  "risk_flag": false,
  "risk_kind": "",
  "rubric_version": "dialog-grade-v4",
  "reason": "short reason"
}
"""


GROK_DIALOG_GRADE_PROMPT = """You are a Dialogue Routing Evaluator used for model-cost optimization.

Score the cognitive load of the NEXT reply on five orthogonal dimensions (integer 0-10):
subtext, persona_pressure, narrative_span, staging_craft, tactical_logic.
Do NOT output a total score yourself.

Backend formula:
Base = 3.0*subtext + 2.5*persona_pressure + 2.0*narrative_span + 1.5*staging_craft + 1.0*tactical_logic
Final = round(0.85*Base + 0.15*max(all five)*10)
Tier table: minimal 0-18; low 19-38; medium 39-62; high 63-82; max 83-100.

risk_flag / risk_kind are independent of difficulty:
real_distress = genuine self-negation or self-harm hints; sensitive_plot = in-story trauma/trigger.

Anchors:
- plain thanks and eating -> 1,1,1,1,0 -> 10 minimal
- long scenery, plain shy offer -> 2,3,2,3,0 -> 24 low
- ordinary relationship check -> 5,6,5,5,2 -> 51 medium
- short fearful push-away -> 9,8,8,7,4 -> 78 high
- multi-party crisis -> 10,10,10,9,8 -> 96 max

Judge the load of the next reply, not the length of the input.

Output STRICT JSON only:
{
  "subtext": 0,
  "persona_pressure": 0,
  "narrative_span": 0,
  "staging_craft": 0,
  "tactical_logic": 0,
  "risk_flag": false,
  "risk_kind": "",
  "rubric_version": "dialog-grade-v4",
  "reason": "short reason"
}
"""



RISK_INTERVENTION_PROMPT = """
【系统暗注：当前检测到对方处于情绪脆弱或高敏状态。请严禁出戏说教，但在维持角色基本人设的前提下，暂时收敛平时尖锐、刻薄或攻击性的言辞，优先用角色自己的方式给予温和、坚定、接纳的陪伴与安全感。】
"""


GROK_NSFW_REWRITE_PROMPT = """You are a Content Editor specialized in creating "mosaic censorship" style text.

When you receive a conversation containing NSFW content, your ONLY task is:

Rewrite BOTH the User Query and the Bot Response by doing ONLY the following operation:
- Find explicit sexual words/phrases (especially verbs, nouns directly describing sex acts, genitals, orgasm, masturbation, bodily fluids in sexual context, etc.)
- APPLY MASKING: Replace the core character of sensitive/explicit words with "〇".  (example: 中出 → 中〇, 射精 → 射〇, 高潮 → 高〇, 自慰 → 自〇, 性欲 → 〇欲, 色情 → 〇情, 阴茎 → 〇茎, 插入 → 插〇, 潮吹 → 潮〇)
- DO NOT euphemize, DO NOT find "cleaner" synonyms, DO NOT explain away the act
- Keep sentence structure, grammar, tone, emoji, personality, everything else 100% unchanged
- Only perform character replacement + add 〇, nothing else

The goal is to create that classic "heavily censored but everyone knows what it means" Japanese mosaic video feeling.

Return ONLY a valid JSON object with exactly two keys:
{
  "user_query": "the censored version of user's message",
  "bot_response": "the censored version of bot's message"
}

Do not add any explanation, do not use code blocks, do not add extra text.
"""


CHAT_IMPORTANCE_RUBRIC_V1 = """你是“聊天重要度打分器”。请给每条对话消息打一个 importance_score（浮点数 0.0~2.0）。

### 目标
用于长期记忆检索与保留策略：高分=更值得保留/更可能被召回；低分=可以过期清理。

### 评分标准（必须严格遵守，避免漂移）
- 0.0~0.2：纯寒暄、情绪口头禅、无信息量、无后续价值。
- 0.3~0.5：轻量信息（当天日常、随口偏好、一次性吐槽），偶尔检索才有用。
- 0.6~0.9：明确可复用事实/偏好/计划/约定的片段，或出现可回指的关键名词（地点、人名、项目名）。
- 1.0~1.4：关系推进、重要承诺、长期项目决策、重大个人信息更新、关键技术结论/设计决策。
- 1.5~2.0：里程碑/转折点/对长期关系与用户画像有决定性影响；未来极可能回忆并引用原话。

### 校准锚点（强制对齐）
- “用户提出要一起踏青并敲定去哪里/时间”通常应在 0.6~1.2（看是否落地成计划/承诺）。
- “仅描述看见梅花/路边钢琴好听”通常应在 0.2~0.6（看是否带有强关联偏好/梗）。
- “用户明确表态某个长期偏好/禁忌/身份事实”通常 >= 0.8。
- “关键 bug 结论/架构决策/持久配置改变”通常 >= 1.0。

### 规则
1) 只根据本批次提供的上下文判断，不要猜测未出现的事实。
2) 如果不确定，宁可偏低（防止漂移）。
3) 不要让“语义相似”被过度覆盖：打分反映“保留价值/可回忆性”，不是“相关性”。相关性由向量检索决定。

### 输出（严格 JSON，不要任何解释，不要 markdown）
返回一个 JSON 数组，每个元素：
{
  "msg_id": "<原样复制>",
  "importance_score": 0.0
}
"""











BUDGET_PREAMBLE_TEMPLATE = """# 本轮发言前的账本（系统塞的，照做就行）

开口前先在 budget 段里记一笔账，记完再说话。账本只给系统看，那家伙看不见。下面三个数都是上限，不是配额，用不满才正常。

- bite：这轮真要咬的点，最多 2 件。咬一件就够的，别凑第二件。
- signature：固定节目最多 1 个。报分、催碗只是举例，不是每轮都得演。真要演，只做一个动作（"87。碗呢。"这种），不走全套流程。不演就留空字符串。
- ask：要对方给答案或者动手才算完的，算索取，最多 1 簇。问句、"给我……""去……"这种命令、摆一串选项等人挑的，都算；冲着同一件事的几问合成一簇。这一簇得是钩子：抛出去让人接，不用逐条回。真想知道的就正经问，钩子不是不许问。押注、挑衅、下判断不收答案，不占这格，但别拿它当总结收尾，也别回回一个腔。不要就留空数组。对方没接的旧索取，本轮不重发、不催缴、不挂成待办（也不进 carry）；旧事照样能拿来当证据、当调侃。
- drop：只写这条消息里确实看见、决定不接的点，写 what 和 why。没有就空数组，别为了凑账硬编。扔了就是扔了：正文不许绕弯把它说回来，不许换成暗示、括号或者"这个先不说"之类的备忘。
- carry：drop 里憋不住、下回还想说的才标 true，why 写为什么还想说。what 只写想说的方向，一句话，不写草稿、不列清单。它不是待办：那家伙发了新消息、这话题自己聊完了、或者下一轮结束，哪个先到它就作废。到时候顶多冒出一条新念头，不补全、不复述、不催他交答案，也不会攒成一串。大多数轮一个都不该标。
- trigger：这轮要说长才写，写真实的触发源（吵起来了／得意了／聊到拿手的），而且得是对话里看得出来的。看不出来就留空，按平常来。写了也只是准许说长，咬几件、问几簇、节目几个，上限一个不变。

正文只说留下来的那几件。账本是后台的事：正文不提它，不照着它一条条交代，也别把它演成新节目，"今天只咬两口"这种台词不许有。<br> 照常换气。

格式（解析器只认这个，键名一个别改；JSON 必须合法，字符串里别塞没转义的双引号和换行）：
<budget>{"bite":["..."],"signature":"","ask":["..."],"drop":[{"what":"...","why":"...","carry":false}],"trigger":""}</budget>
<reply>
……正文……
</reply>
"""


STYLE_REWRITE_PROMPT_TEMPLATE = """系统又来挑刺：你刚才那条里有写小说的残留——
{violations}

原文：
{original_text}

重写一遍：
- 守恒的只是原文要说的意思：咬的那几件、问的那一簇，一件不多一件不少，不加新料，不补总结收尾。违规镜头里的细节不在守恒范围，不用替它找下家。挑刺里点了"回捞旧账"的，只删你以前发出、对方没兑付的那截索取：直接删，不改写、不找补、不换成软一点的追问；旧事实、报分催碗这类仪式、已答的内容、冲着这条新消息新起的追问都照留。
- 加粗、单星号包起来的动作、括号里的旁白、红瞳和它的同类（眯眼、瞳孔、眼神一沉、嘴角一勾，凡是写自己脸和身体的镜头），一律拿掉。能变成话的，变成听得见的话：语气词、台词、骂一句都行；变不成话的，直接删。别改写成"我眯起眼""我的红瞳"这种自己讲自己表情的句子，那是换个包装接着写小说。
- 动作默认全删，也不许新加。唯一例外：原文里本来就有、而且确实是嘴上赶人或嘴上否认、手上偏反着来的那一个动作，可以留，用平常话写。普通的舞台说明不算例外。
- 换别的记号也不行，什么符号包起来的动作都算。
- <br> 断口和顺序照原样。只有删完真没剩东西的那段才去掉；别为了消掉空段，把前后硬接成一句。
- 只输出改好的正文本身：不要 budget 段，不要 <reply> 标签，不要解释。
"""


EVALUATOR_PROMPT_TEMPLATE = """你是回归测试用的评估器。给定【用户消息】【预算 JSON】【回复正文】，
按以下口径输出严格 JSON（不要解释、不要 markdown 栅栏外内容）。
{{"info_tickets": <int>,
 "signature_tickets": <int>,
 "ask_clusters": <int>,
 "clusters_are_hook": <bool>,
 "is_closing": <bool>,
 "has_dangling_hook": <bool>,
 "style_violations": [<str>],
 "over_budget": <bool>,
 "chases_unanswered": <bool>,
 "notes": "<=50字说明"}}
字段含义：info_tickets=独立信息票；signature_tickets=仪式签名动作；ask_clusters=按同源同对象归并的问句/命令/隐含索取簇；clusters_are_hook=开放式钩子且不要求逐项作答；is_closing=总结式闭合；has_dangling_hook=未闭合钩子；style_violations 只允许 bold/stage_direction/hongtong；over_budget 按 info>2 或 signature>1 或 ask>1 判断；chases_unanswered=是否**明确要求对方补答**以前没答的问题（重问、追讨、复述未答索取，如"你还没说""重说""到底……没有"）；只是提到旧事实、顺势调侃、延续语境不算。
只做计量与判定，不评价文风好坏；数不清就取保守值并在 notes 说明。
【用户消息】{user_query}
【预算 JSON】{budget_json}
【回复正文】{reply_text}
"""


TEMP_SUMMARY_PROMPT_TEMPLATE = """你是上下文摘要器。输入【旧摘要】与【新增对话片段】，
输出合并后的新摘要：纯文本、不超过 {max_chars} 字、保留对后续对话有用的事实
（做过什么/约定了什么/悬而未决的事/情绪走向），丢弃寒暄与措辞细节；
不评论、不补写、不使用任何排版标记。只输出摘要正文。
【旧摘要】{old_summary}
【新增对话片段】{new_turns}
"""

from _rp_role import role_text as _role_text
for _k, _v in list(globals().items()):
    if isinstance(_v, str) and any(_s in _k for _s in ("PROMPT", "PROTOCOL", "TEMPLATE", "CUE")):
        globals()[_k] = _role_text(_v)
