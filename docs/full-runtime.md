# 完整运行时（0.3）

## 两仓边界

公开仓包含所有 Python/Worker/工作流、模型注册表、参数、提示词过程模板、
路由、工具、检索、摘要和持久化算法。私密仓只有 character.json、identity.json、
io.json，以及历史、日记、知识、记忆数据库、向量缓存、贴纸目录和运行状态。
密钥放 GitHub Secrets / Worker Secrets，不能写进任一仓库的普通配置。
角色独有的身份、口癖和指令仍属于角色卡。

完整引擎通过临时视图将公开 src/scripts/skills 与私密 data/logs/downloads
组合起来，绝不导入私密仓里的 Python，也不加载其 .env。旧仓存在源码不妨碍
过渡，但新建角色仓无需这些源码。更新公开仓即可让同一工作流的所有角色共用
最新逻辑；角色数据不需要逐仓更新执行代码。

## 功能对应与证据

| 能力 | 公开实现 | 验证方式 |
| --- | --- | --- |
| 多供应商原生 SDK、代理、分层和故障切换 | compat_runtime/src/config、llm_routing、各 provider 模块 | 原回归套件 + SDK 自检；真实通路另用 probe |
| 两阶段回复、流式编辑、分段、补救和主动不回 | tenshi_reply、reply_streaming、telegram_tenshi_logger | 原回归 + 合成 shadow 回复 |
| 图片、文档、音频输入、相册与引用 | logger、image_utils、full.mjs | Python 附件回归 + Node 合并/私密队列测试 |
| 贴纸、表情回应 | stickers、reactions | 原回归；目录和 file_id 留私密仓 |
| SQLite/FTS/FAISS、Qwen embedding、缓存 | tenshi_memory_manager、embedding_client、rag_modules | 原检索/embedding 回归；保留旧二进制文件 |
| 日记、摘要、KB 月分片、上下文压缩 | log_update、kb_storage、context_compression | 原回归；定期任务由 full 配置调度 |
| 关系证据、反思、延迟意图、回复预算 | bond_state、bond_reflect、deferred_intent、reply_budget | 原回归 |
| 网页、搜索、天气、位置、工具手册 | fetch_tools、weather_utils、user_location、skills | 原回归；位置默认值由私密角色数据给出 |
| 随机/定时/强制感知、沉默冷却、KV 同步 | perception_*、full.mjs | 原回归 + Node 定时队列回归 |
| Git 重试、并发合并、staging 汇流、周归档、清理 | tenshi_git_sync、full_service、维护脚本 | 原 Git 回归 + 新队列隔离测试 |

采用审查后的成熟实现，未用简化版替代这些算法。source-manifest.json 记录每个
源文件的原始/公开 SHA256 和函数 AST 差异。可核查哪些函数仅为数据根目录、
通用角色文案和外部 Git 持久化作了改动。

这些测试不代表已实机试遍每一种模型、图片格式或 Telegram 权限。当前机器人
切流与 Worker 部署仍是独立部署动作，验证流程不自动执行它们。

## 稳定私密 I/O

完整模式使用 tenshi-v1 的既有布局，历史数据库直接继续使用：

    character.json               角色卡和可选 role/weather_defaults
    identity.json                用户/会话授权
    io.json                      {"schema_version":1,"driver":"tenshi-v1"}
    Tenshi.json                   原日记
    data/tenshi_log.json          原历史
    data/tenshi_memory.db         原 SQLite/FTS/向量
    data/embedding_cache.pkl      原向量缓存
    data/knowledge_base.json      原 KB；月分片等整个 data 目录保留
    data/stickers.json            私密贴纸目录
    inbox/<opaque-id>.json        待处理原始事件
    .rp/full-receipts/            JSON 执行回执，不新建替代记忆数据库

新角色仓运行 rp-core init-instance --path <目录> --driver tenshi-v1，
编辑角色卡与 identity，然后 rp-core full --task prepare --data-root <目录>。
为已有 Tenshi 仓执行 prepare 时，只从旧 persona.py 的 AST 抽取字面量到 JSON，
迁移贴纸目录及位置默认值；不执行旧源码、不覆盖已有卡、不重建/重算旧数据库。
原 src/config/persona.py 可以在确认切换后由仓主自行移除。

0.2 的 native 引擎和自定义文件映射仍支持。完整模式遇到 native 或非标准绑定
会明确报错，绝不悄悄新建一份空的 canonical 记忆库。

## 使用现有 Secrets 验证

Validate full runtime 工作流默认 preflight：只验证私密仓访问和依赖，输出
布尔检查项。selfcheck/probe/reply 只使用全新合成卡和空历史：

- selfcheck：离线 SDK 参数兼容性；
- probe：供应商连接/模型列表探活，不等同于真实生成成功；
- reply：先只读探测供应商通路，生成本次测试的健康缓存，再由真实模型生成合成回复。Telegram SDK 与 requests 直连 HTTP 都在子进程内替换成假发送器，
  并阻断 Telegram DNS、禁用旧定时同步 URL，绝不读取私密历史作为提示词；历史写入和发送捕获必须同时通过，公开结果只包含通路可用数量，不含地址、密钥或消息内容；
- prepare-io：仅在私密仓 rp-core-full-io 分支新增数据接口，保留 main。

Calling 的 TG_TOKEN、TENSHI_BOT_ACTION_TOKEN、GH_REPO 会分别映射到原运行时的
TELEGRAM_TOKEN、GITHUB_TOKEN、REPO_NAME。其他供应商的 Secret 名称原样转接。
非敏感模型默认配置位于公开 compat_runtime/src/config 中。

## 正式接入

1. 合并确认后的私密 I/O 数据到角色仓；配置相应 Secrets。
2. config/instances.json 注册公开实例 ID、对应 GitHub Environment 和公开运行配置
   config/runtime/full.json。新增角色共享引擎，不复制执行代码。私密仓名可放
   RP_<INSTANCE>_REPOSITORY Variable；默认天子兼容根 GH_REPO Secret。
   各 Environment 可覆盖 TG_TOKEN、ADMIN_ID、GH_REPO、供应商密钥等。
3. 部署 integrations/cloudflare/full.mjs，绑定 KV，设置 DATA_REPOSITORY、
   DATA_BRANCH=main、PUBLIC_REPOSITORY、INSTANCE_ID 以及 DATA_TOKEN、
   DISPATCH_TOKEN、TELEGRAM_WEBHOOK_SECRET、PERCEPTION_SCHEDULE_SYNC_SECRET。
   Worker 的 RP_LIVE_ENABLED 默认关闭；确认切换时设为 true。
4. 将 PERCEPTION_SCHEDULE_SYNC_URL 指向新 Worker 的 /internal/perception-schedule。
   停止同一角色的旧 Calling/Tenshi 触发源，再启用公开仓 Variable
   RP_LIVE_ENABLED=true 和新 webhook。一个 bot 只保留一个生产执行入口。

Worker 先把消息全文、引用、附件和定时 note 写入私密 inbox，再向公开仓派发
仅 instance_id、event_id；不再向旧 Calling 或旧私密引擎 fallback。
公开仓定时任务默认每 15 分钟处理 inbox 和公开配置中的维护间隔；Worker 的
分钟级随机/定时调度可独立启用。所有同角色任务共用 concurrency group。
full.json 默认交由 Worker 做分钟级感知轮询，不再额外执行 15 分钟感知，避免双重轮询。

## 失败与恢复

完整引擎在可能对外发送前先把 running 回执提交到私密仓；成功后再标记 completed。
已完成事件不会重新执行。running/uncertain 事件会阻止自动重放，需检查私密历史和
Telegram 后决定修正回执还是重试。Telegram API 与 Git 之间无法形成一个原子事务，
这条保护避免在确认不了发送状态时自动重复发言。

原生 Git helper 处理重试/合并；主分支写回失败时，先尝试保存到独立的私密 bot-staging 恢复分支，并停止继续发送。staging 分支在下一次角色任务开始时汇流。日常只浅克隆最新数据；确实存在待恢复分支时才补取 Git 祖先图，保留 blob:none 过滤，避免每轮拉取庞大的二进制历史。
若远端完全不可写，工作流必须失败，不能声称已落库；本机可检查私密 logs。
公开 Actions 不上传明文或加密的私密运行 artifact，不发布私密快照 Release，
也不缓存角色数据。私密 GitHub 数据本身和公开 Actions 执行环境是两种不同的
信任边界，仓主仍需信任所运行的公开源码与固定版本的 Actions。

## 本地回归

    uv sync --locked --extra dev --extra full
    uv run --no-sync pytest
    uv run --no-sync python scripts/run_compat_tests.py
    node --test tests/test_cloudflare*.mjs
    uv run --no-sync rp-core full --task shadow

公开 compat 回归在临时目录里使用审查后的测试和合成数据，屏蔽外部 DNS。
原测试中依赖生产状态文件、旧作者名、跨角色兄弟仓扫描的断言，分别调整为
合成夹具、通用作者和角色隔离；不把不适用的断言简单跳过。

## 公开仓维护

月度 keepalive 与角色执行共用 RP_LIVE_ENABLED 开关，仅更新公开的
.github/activity.json，不访问私密角色数据。写权限可使用现有 KEEPALIVE_TOKEN，
否则使用工作流自身的令牌；角色回复、摘要、归档、健康检查仍由统一角色任务执行。

## 后续只维护公开仓

日常修改直接发生在本公开仓；无需再次读取 Tenshi 私仓的 Python 源码。
scripts/import_compat_runtime.py 仅用于最初的代码导入审查，不是构建或运行前置步骤。
修改公开运行时并完成审查后，运行：

    python scripts/audit_public.py
    python scripts/review_runtime_manifest.py --refresh
    python scripts/review_runtime_manifest.py --check

原始 source_sha256 / published_sha256 与函数 AST 数量保留为初次导入的来源记录；
后续公开修改由 reviewed_sha256 和正常 Git 提交记录。所有角色在后续执行时使用
同一公开提交，不需要在各私密仓更新代码、复制脚本或重建数据库。

## 原 Calling 部署平滑切换

Worker 支持沿用 GH_REPO/GH_TOKEN/CALLING_GH_TOKEN/SCHEDULE_SYNC_SECRET 绑定，
无需读取或重设旧密钥。只需配置 PUBLIC_REPOSITORY、INSTANCE_ID 和
RP_LIVE_ENABLED 三个非敏感变量，保留原 KV 命名空间和 webhook 地址。
使用 node scripts/build_cloudflare.mjs 构建单文件 dashboard-worker.mjs，
可直接粘贴到 Cloudflare Dashboard 的原入口文件；CI 检查产物与源模块一致。

/health 只公开服务标识、协议与启用状态。/internal/probe 和
/internal/drain-inbox 必须使用既有 schedule-sync Bearer 密钥；前者只排队健康探测，
不发送聊天消息，后者将旧 KV 中已确认但未派发的消息转存私密 inbox，再推进游标。
暂停 Worker 时允许授权控制操作，以便先保全旧消息，再切换公开执行开关。
