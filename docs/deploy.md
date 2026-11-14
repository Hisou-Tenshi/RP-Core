> 0.3 新增完整引擎；本文保留 0.2 原生引擎说明。完整功能与最新部署步骤见 [完整运行时](full-runtime.md)。

# Fork 部署与多角色共享

## 不需要服务器的路径

Fork 公开框架 + 私密角色数据仓 + 凭证配置，即可使用 GitHub-hosted runner 的 Telegram 轮询模式。默认每 15 分钟检查一次；定时运行可能延迟，不提供实时性承诺。

GitHub 当前对公开仓的标准 hosted runner 免费；这不包括模型 API、较大型 runner 或其他服务费用。官方说明：

- https://docs.github.com/en/actions/concepts/billing-and-usage
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows

Fork 后定时工作流默认禁用，需要在 Actions 启用。公开仓长期无活动也可能停用定时工作流，应检查运行状态。

## 配置一次角色

1. Fork 并启用 Actions，保留公开仓可见性。
2. 创建独立私密数据仓并 clone；在公开框架目录运行 init-instance --path /path/to/private-data。不会覆盖已有角色卡。编辑 character.json 和 identity.json，把生成的数据提交到私密仓 main。
3. 在公开 fork 的 Settings → Secrets and variables → Actions → Variables 创建 RP_TENSHI_REPOSITORY，值为 owner/private-data。该变量需在 repository 层，供计划任务筛选已配置实例。
4. 在 Settings → Environments 建立 tenshi，添加三个独立 Secrets：

| Secret | 用途 |
|---|---|
| RP_INSTANCE_TOKEN | 限定私密数据仓的 contents read/write 凭证 |
| RP_MODEL_API_KEY | 默认公开配置使用的 OpenRouter key |
| RP_TELEGRAM_TOKEN | 自己 Bot 的 Telegram token |

5. 手动运行 Run private characters，instance_id=tenshi。公开代码会校验数据仓为 PRIVATE，读取数据、生成并保存结果，再发消息。
6. 没有 webhook 的新 Bot 可直接轮询；原 Bot 已设置 webhook 时，先安排停用旧入口并删除 webhook，或使用下面的 webhook 模式。不要让旧运行器与新框架同时消费同一角色。

不要在工作流参数、公开配置或 issue 中填消息正文和凭证。生成实例时没有真实事件；可以先用 --demo-event 与 offline.json 在本地验证，再创建正式空输入数据仓。

## 增加角色与切换数据仓

在公开 config/instances.json 的 instances 列表追加：

```json
{"id":"another-role","repository_variable":"RP_ANOTHER_REPOSITORY","environment":"another-role","runtime":"config/runtime/default.json"}
```

再建立相应 repository variable、Environment 和私密数据仓。调度时自动展开已注册且变量已配置的实例；每个角色有独立并发组与凭证。instance_id=all 可手动处理全部实例，单个失败不取消其他角色。

角色卡变化只改私密数据；模型、工具、兼容驱动或平台修复改公开仓。模型参数不同可在公开配置建立继承 profile，并在注册表选择。没有逐角色复制的运行代码。

同一轮矩阵使用同一个公开提交；框架更新后，后续运行使用新提交。其他用户的 fork 需要同步上游一次，不会跨 fork 自动升级。

## 可选低延迟 webhook

公开 integrations/cloudflare/telegram.mjs 可部署到自己的 Cloudflare Worker；私密角色仓不新增代码。该模式仍需要自己的 Cloudflare 账户，属于轮询之外的可选入口。

Worker 普通变量：DATA_REPOSITORY、PUBLIC_REPOSITORY、INSTANCE_ID，可选 DATA_BRANCH=main。DATA_REPOSITORY 指自己的私密数据仓，PUBLIC_REPOSITORY 指自己的公开 fork。

Worker Secrets：DATA_TOKEN（私密数据仓 contents read/write）、DISPATCH_TOKEN（公开 fork 的 repository_dispatch 权限）、WEBHOOK_SECRET。用 Telegram setWebhook 配置 Worker URL，并把 secret_token 设为同一个 WEBHOOK_SECRET。

将该实例的公开 runtime 改为 config/runtime/webhook.json。Worker 校验 Telegram secret header 与私密身份授权，先把事件存入私密 inbox，再向公开仓发 rp_event；client_payload 仅含 instance_id/event_id。失败会返回非成功状态供 Telegram 重试；定时任务也会补扫持久队列。

## 持久化与恢复

发送前先提交并推送状态和待发消息，再发送，再保存回执。私密 Git 同时收到 inbox 新文件时允许普通 rebase 合并互不重叠的改动；状态或数据库冲突时中止，不强推、不整体取舍。

平台不支持幂等发送，发送成功后、回执推送前中断仍可能重发。公开仓不会上传明文恢复 artifact；同一数据仓不得有两个独立框架写者。

需要更强并发或远程事务存储时，应在公开仓实现新数据驱动，所有角色共用，不把数据库实现放回私密仓。
