# 私密适配器与部署

## 最小实例

用 `rp-core init-instance --path /private/path` 生成全合成示例，再在私密目录内初始化 Git。实例包括启动 manifest、私密适配器入口、角色卡、模型进程配置与事件队列。不要把旧项目数据复制进公开框架目录。

`adapter.py` 默认调用公开库中的 SQLiteAdapter。可以直接更换整个脚本为任意语言实现，只要保持 [rp/1](protocol-v1.md)。

参考适配器状态：

| 路径 | 用途 |
|---|---|
| state/state.sqlite | 会话、事务、回执、归档、待发消息 |
| state/outbox.jsonl | 离线文件接收端，包含合成或私密回复 |
| events.jsonl | 私密输入队列，每行 Event 或带 event/job/method 的对象 |

SQLite 采用数据库事务和 BEGIN IMMEDIATE 进行版本检查。参考 memory.search 是简单的文本匹配，不是向量检索。关系提案留待私密端批准。到期调度在下次 runtime.next_event 调用时产生感知事件，因此准点程度取决于外部触发器。

## 接模型

将私密 adapter.json 的 model_command 改成自己的程序 argv：

```json
{
  "model_command":["{python}","my_model_gateway.py"],
  "model_timeout_seconds":90,
  "git_sync":false,
  "git_branch":"main"
}
```

该进程接受 rp/1 model.generate 请求，把 purpose/messages/response_schema/tools 转成选定模型的输入，再返回 output/tool_calls。不要求更改公开核心。模型 key、代理地址、容灾顺序都放私密端；不要把 key 放 argv，以免进程列表泄露。

公开工作流把 RP_ADAPTER_SECRETS secret 原样提供为同名环境变量，建议内容是由私密适配器解析的 JSON。以后新增厂商凭证无需修改公开 workflow 中的变量清单。代码不自动加载 .env；如需要，由私密端显式加载被忽略的文件。

## 接平台与旧数据

私密端把 Telegram、QQ、Web 等事件标准化为 Event，并将 attachment reference、reply_to、action.id 映射回原平台。平台 webhook 验证、用户鉴权、下载、坐标查询和平台消息重试由私密端负责。

继承 SQLiteAdapter 并覆盖 flush_outbox 可实现平台投递；更换 state.load/state.commit 则可保留既有 JSON/SQLite/向量库。新增工具在 snapshot.tools 声明，read 由 tool.execute 执行，deferred 在原子提交中应用。不要在 read 工具或 model.generate 内做不可撤销发送。

## Git 持久化参考模式

独立私密 repo 根目录内将 git_sync 设为 true。运行器先调用 runtime.finalize(before_delivery) 推送 state/，随后处理发件箱，再 finalize(after_delivery) 保存投递状态。

参考实现只 stage state/，不自动 add 整仓；不 rebase、不强推、不合并二进制冲突。push 拒绝时工作流失败，尚未确认的投递不会启动。它要求实例根目录就是 Git 根目录，防止误写公开父仓库。多写者应使用远程事务存储，或在私密层实现适合自己数据的冲突处理与加密故障恢复。

只允许一个工作流写此实例；同一公开仓库的 runtime workflow 使用一个并发组。多个公开执行仓库之间不会共享该并发组。第三方平台缺少幂等接口时，发送成功但状态写回失败仍可能造成重发。

默认 git_sync=false 仅承诺本地持久化，适合离线演示。公开 Actions 使用 --require-remote-persistence，因此不会把这种本地演示误报为完成部署。

## 触发器

公开 workflow 的 repository_dispatch 类型是 rp_event，可带 opaque event_id。不要传聊天正文、定位、附件或凭证；工作流事件与运行元数据不能当作私密存储。私密适配器从已经持久化的队列领取事件。定时触发可补扫尚未处理的队列与到期计划。

参考实例不会连接真实模型或真实聊天平台；需要按上述接口提供私密实现。协议兼容测试无需任何 API key。
