# Tenshi 私密端的渐进接入

此仓库独立建立，不包含或转换任何真实 Tenshi 数据，也不改变现有运行系统。示例只借用角色名称与一般性格，完全重新编写。

建议保留原私密仓库与历史，在其中增加 instance.json 和 adapter.py。公开 RP-Core 始终使用干净历史。

| Tenshi 概念 | 私密适配器对外映射 |
|---|---|
| Telegram webhook 字段、附件、引用 | 标准 Event 与 extensions |
| 人设、提示词、调度设置 | Snapshot.character / policy |
| tenshi_log、知识库分片、记忆 DB | Snapshot.history / memories / summary |
| Tenshi.json、关系卡 | Snapshot.notes / relationship |
| 原多厂商路由、代理与容灾 | model.generate |
| 检索、联网、天气、自省 | read 工具或预计算 context |
| 日记、记忆写入、定时任务 | deferred effects → state.commit |
| 原 Git 提交与故障恢复 | runtime.finalize 与私密存储事务 |
| Telegram 发消息、贴纸等 | 私密 outbox.flush / deferred 工具 |

第一步只实现只读 state.load 和模型协议映射，用合成影子会话验证输出；随后建立原子 state.commit、回执与发件箱，最后切换触发与真实发送。切换时停止旧的同实例写入者，明确唯一事件消费者。

不必改旧 JSON 的字段名、重做 embedding 或替换既有数据库。私密端负责转换快照和应用事务；对旧结构的差异都留在私密 repo。

本框架暂未内置原系统的流式输出、多模态 SDK、向量检索和平台专属行为；接入时需要在私密适配器中保留这些能力，或按实际需要逐项实现。
