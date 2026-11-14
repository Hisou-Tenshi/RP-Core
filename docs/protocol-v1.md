# rp/1 标准 I/O 协议

公开核心与私密适配器的标准边界。JSON Schema 位于 `schemas/`；也可运行 `rp-core schema request` 导出。采用 JSON Schema Draft 2020-12。

## 传输

UTF-8 JSON Lines，每个请求/响应独占一行。单帧最多 1,048,576 bytes（含换行），同一连接顺序请求，一次一个未完成请求。stdout 仅限协议；适配器 stderr 不转发到公开 Actions。私密诊断由适配器写入自己的日志。适配器是受信任代码，进程协议不是沙箱。

```json
{"protocol":"rp/1","id":"request-1","method":"capabilities","params":{}}
{"protocol":"rp/1","id":"request-1","ok":true,"result":{}}
{"protocol":"rp/1","id":"request-1","ok":false,"error":{"code":"invalid_params","message":"Safe diagnostic.","retryable":false}}
```

ID 为非空字符串，最多 256 字符。未知可选字段忽略；未知方法明确失败。不接受重复 JSON key、NaN、Infinity、错误主版本或错误响应 ID。请求/响应必须是对象；无法解析请求时响应 ID 可为 null。

核心替换私密适配器的自由文本错误，保留重试标志，不转发错误正文或任意自定义代码。并发冲突使用 state.commit 的结构化 status=conflict。

## 核心入口

`rp-core serve --manifest /private/instance.json` 接收以下方法。其 stdout 可包含用户消息，必须接入私密管道，不能直接放入公开工作流日志。

| 方法 | params | result |
|---|---|---|
| capabilities | 空对象 | 版本、方法和能力 |
| event.process | event | 幂等回执与外发 actions |
| maintenance.run | event、job | 无外发消息的维护回执 |

维护 job 为 summarize、compact、reflect；compact 可附 keep_last（0–100，默认 8）。参考适配器压缩时保留原始归档；reflect 只生成提案，不自动修改关系卡。

### Event

```json
{
  "id":"opaque-event-123",
  "session_id":"opaque-session",
  "kind":"message",
  "occurred_at":"2026-01-01T12:00:00+00:00",
  "actor":{"id":"opaque-user"},
  "content":[{"type":"text","text":"你好"}],
  "extensions":{"my_platform":{"source_message_id":"123"}}
}
```

kind 为 message 或 perception。时间包含时区。content 支持 text；attachment（reference、media_type，可选 caption）；location（latitude、longitude）。核心不下载附件、不打开 reference、不查询坐标，由私密端解析。平台专属字段放 extensions。reply_to 是可选的不透明平台目标。

### 回执

```json
{
  "event_id":"opaque-event-123",
  "session_id":"opaque-session",
  "status":"replied",
  "actions":[{"id":"stable-action-id","kind":"message","text":"你好。"}],
  "effects_queued":0,
  "revision":"opaque-revision",
  "replayed":false
}
```

status 为 replied、silent、maintained。沉默是成功状态，actions 为空，日记或调度副作用仍可提交。核心创建文本 message；贴纸等能力可声明为私密 deferred 工具，由适配器转成持久任务或自己的发件箱。

## 私密适配器方法

### bridge.describe

输入 `{protocols:[rp/1]}`；输出 protocol 与 methods。核心先协商。必须声明 state.load、state.commit、model.generate。提供 read 工具时还须实现 tool.execute。

### state.load

输入 session_id、event_id、event_digest；另有可选 operation/job，供适配器选择合适的上下文窗口。digest 是核心对操作、事件及相关维护参数的规范 JSON 计算的 SHA-256；适配器应将其作为不透明值保存比较。

- 已处理相同输入：返回 `{receipt:Result}`，核心直接重放，不再调用模型。
- 同 ID 不同摘要：必须拒绝。
- 新事件：返回 `{snapshot:Snapshot}`。

Snapshot 必需 revision、character（id、name、instructions）；可选 history、memories、notes、relationship、summary、tools、policy、extensions。history 是 role=user/assistant 与 content 字符串组成的列表。revision 为不透明字符串，核心不将它解释为整数。

policy 可配置 two_stage、allow_silence、history_limit、max_tool_rounds、max_messages、max_message_chars。上下文由私密端检索、裁剪到帧大小与 schema 限制内。

### model.generate

输入 purpose、messages、response_schema、tools。purpose 为 reply.plan、reply.render、maintenance.summarize、maintenance.compact、maintenance.reflect。messages 使用 role/content，不采用厂商 SDK 类型。

输出 `{output:{...},tool_calls:[]}`；需要工具时返回 `{tool_calls:[{id,name,arguments}]}`。私密端转换原生结构化输出与工具调用，核心验证结果。它是模型网关协议，不要求厂商直接支持 rp/1。凭证、模型名、重试、容灾与多媒体解析属于私密实现。

### tool.execute 与延迟工具

每个工具声明 name、description、input_schema、mode（read/deferred）。input_schema 为完整 JSON Schema，仅允许本地 # 引用，禁止外部 URL/file 引用。核心验证工具、参数、轮数与 call ID。

read：立即调用 tool.execute，输入 session_id、event_id、id、name、arguments；工具必须无副作用。deferred：只记录带稳定 effect ID 的操作，适配器在 state.commit 中原子应用或建立持久任务。模型生成期间不得写日记或发送消息。

### state.commit

输入 session_id、event_id、event_digest、expected_revision、transaction。transaction 含 event、result、messages、effects，以及可选 private、maintenance。private 包含沟通意图，不能外发。

适配器须原子提交版本检查、回执、历史、日记/记忆/调度、维护结果与待发消息。compact 的 maintenance 另含 history_count/history_digest，标识模型实际看到的历史窗口；不得删除未被该摘要覆盖的记录。参考实现每次压缩最早 200 条的有界前缀，保留原始归档与未读后缀。

- 成功：`{status:committed,result:ResultWithRevision}`。
- 已提交：`{status:duplicate,result:OriginalResultWithRevision}`。
- 状态变化：`{status:conflict}`，不得部分写入；核心返回可重试 state_conflict。

存储格式可自由替换。失败不自动重新调用模型，由调用方使用原事件 ID 重试。摘要检查和幂等必须由存储端实现，不能只在进程内缓存。

## 工作流批量接口

run-instance 还要求 runtime.next_event、runtime.finalize，可选 outbox.flush。

- runtime.next_event：输入 event_id（字符串或 null）；返回 event，可另带 method、job、keep_last；无任务返回 event=null。私密端维护持久队列。
- runtime.finalize：输入 phase（before_delivery/after_delivery/failure）、success；成功返回 durable=true、scope=local/remote。before_delivery 先持久化待发操作，Actions 强制要求 remote。failure 尽力保存现场，不能假报成功。
- outbox.flush：只投递已提交操作，以 action.id 去重，不重新生成回复。参考实现仅导出文件。

投递是可重试的至少一次尝试。平台支持幂等 key 时使用 action.id；不支持时，在发送与回执落盘之间仍可能重复。本框架不承诺跨 Git、数据库和第三方平台的分布式 exactly-once。

## 错误与版本

常见 code 包括 invalid_json、invalid_request、invalid_params、unsupported_protocol、method_not_found、frame_too_large、adapter_unavailable、adapter_timeout、adapter_disconnected、adapter_error、invalid_adapter、invalid_adapter_response、invalid_model_output、invalid_tool_arguments、tool_not_allowed、tool_limit、unsafe_model_output、state_conflict、persistence_unavailable、internal_error。

仅 retryable=true 表示适合用原 ID 重试。错误正文不会变成平台回复。兼容策略见 [compatibility.md](compatibility.md)。
