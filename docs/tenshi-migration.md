> 0.3 新增完整引擎；本文保留 0.2 原生引擎说明。完整功能与最新部署步骤见 [完整运行时](full-runtime.md)。

# 旧 Tenshi 接入：保留数据与数据库

目标是补数据接口，不重建聊天历史、记忆库或 embedding。兼容实现位于公开 src/rp_core/legacy.py。此文档不代表已经切换原 Bot；真实数据和线上配置不由示例命令自动修改。

## 角色仓需增加什么

- 实际角色卡 character.json。
- 私密身份 identity.json。
- 从 examples/io/tenshi-v1.json 调整的 io.json，选择 tenshi-v1。

不需要私密 adapter.py、SDK、发送器、requirements 或工作流。旧源代码可以暂时留着但不会被新运行器加载。公开框架的修复自动用于该角色。

## 已有文件怎样使用

| 旧文件 | 兼容方式 |
|---|---|
| data/tenshi_log.json | 按 conversation_id 读取旧对话，保留原字段，新事件按旧结构追加 |
| Tenshi.json | 读取原日记，按日追加新笔记并记录幂等标识 |
| data/knowledge_base.json | 作为私密知识上下文读取 |
| data/bond_state.json | 作为关系上下文读取 |
| data/tenshi_memory.db | 复用原 memory_items；新记忆追加原表，不重建数据库 |
| 原 memory_vectors / embedding_cache | 保留原行、向量与缓存，不删除、不重算 |

.rp/state.sqlite 只保存新框架的事务、版本、回执、发件箱和投影进度；它不是旧记忆库的替代品，也不是重新导入整库。输出到旧 JSON / SQLite 的过程可幂等重放，在远程持久化确认后才发送消息。

当前旧记忆检索为关键词兼容，未声称复刻原 FAISS 混合检索、视觉处理、全部一次性调度或旧平台行为。后续补齐也在公开仓完成，无需逐个角色改私密代码。

## 切换步骤

1. 在测试副本中加入三份数据文件，用公开 offline.json 验证读取与写回。
2. 在公开注册表登记角色仓，配置 Environment Secrets。
3. 确认身份映射、历史会话与发送目标正确，再停止旧写入者并切换 Telegram 入口。
4. 第一次正式运行检查私密提交与 Telegram 回执，保留旧数据备份。

只检查路径绑定、不创建任何新库：

```bash
uv run --no-sync rp-core inspect-data --data-root /path/to/Tenshi --io examples/io/tenshi-v1.json
```

不存在角色卡时该命令仍可检查映射范围；真正运行会验证角色卡等必需数据。不会把任何真实数据复制到公开仓。
