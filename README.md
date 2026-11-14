# RP-Core

一个将**公开 RP 编排**与**私密实例 I/O**分离的 Python 框架。

公开仓库负责角色对话流程；角色卡、历史、记忆、日记、模型凭证、平台接入和持久化由私密适配器控制。公开核心通过版本化的 `rp/1` JSON Lines 协议调用适配器，不查找旧项目目录，不自动加载 `.env`，不包含 Telegram 或模型厂商 SDK。

许可证：**GNU AGPL 3.0 only**（`AGPL-3.0-only`），全文见 [LICENSE](LICENSE)。包版本 `0.1.0`，协议主版本 `rp/1`。

## 已实现

- 单阶段或两阶段回复：沟通意图 → 实际消息；支持主动感知与正常沉默。
- 私密上下文注入：角色卡、历史、记忆、日记、关系卡与压缩摘要。
- 声明式工具：只读查询立即执行，写入副作用在状态事务中统一提交。
- 事件幂等回执、乐观并发版本检查、原子状态与持久化发件箱。
- 总结、压缩（保留原始归档）、关系变更提案（默认不自动生效）。
- 带帧大小限制、超时、响应 ID 校验和脱敏错误的标准输入/输出通信。
- SQLite 参考适配器、离线天子示例、公开 CI 与可选双仓库运行工作流。

这是独立重新实现的第一版框架，**不声称与原 Tenshi 功能完全等价**。实时模型调用、Telegram 发送、向量检索、流式输出和多厂商容灾尚未作为内置集成提供；这些能力可在私密适配器中实现。天子只出现在合成示例中，核心角色由输入决定。

## 离线体验

需要 Python 3.11+ 和 uv。

```bash
uv sync --locked --extra dev
uv run --no-sync rp-core capabilities
uv run --no-sync rp-core init-instance --path instance
uv run --no-sync rp-core run-instance --manifest instance/instance.json
```

终端只显示处理计数。合成回复位于 `instance/state/outbox.jsonl`，状态与回执位于 `instance/state/state.sqlite`。再次执行不会重复生成或追加回复。`instance/` 已被公开仓库忽略。

示例模型是明确标记的**离线脚本**，不是 LLM，不会使用或创建 API key。`init-instance` 不覆盖非空目录。不要在公开仓库提交生成的实例。

## 私密端接入

```text
公开 RP-Core                         私密实例仓库 / 服务
  event.process  ── rp/1 JSONL ──▶  adapter.py
  maintenance.run                    ├─ 模型适配与容灾
                                     ├─ 历史 / 记忆 / 日记 / 调度
                                     ├─ 原子持久化与幂等回执
                                     └─ 平台发送与附件解析
```

`instance.json` 是固定启动接口：

```json
{
  "protocol": "rp/1",
  "adapter": {"command": ["{python}", "adapter.py"], "timeout_seconds": 120},
  "max_events": 25
}
```

`command` 以私密实例目录为工作目录，按 argv 执行，不经过 shell；`{python}` 表示运行核心的 Python。适配器可以由任意语言编写。更换模型、存储、平台或工具，只需保持协议并修改私密适配器。

- [标准 I/O 协议](docs/protocol-v1.md)
- [私密适配器与部署](docs/private-adapter.md)
- [协议兼容承诺](docs/compatibility.md)
- [Tenshi 渐进接入](docs/tenshi-migration.md)
- [公开边界与审查记录](docs/security-and-review.md)

## 公开工作流运行私密实例

`.github/workflows/instance.yml` 支持手动、定时和 `repository_dispatch: rp_event`。未配置实例时跳过运行。

1. 创建独立私密实例仓库，把生成的实例文件放在根目录。不要复制旧 Git 历史到公开仓库。
2. 在公开仓库设置变量 `RP_INSTANCE_REPOSITORY=owner/private-repo`，可选 `RP_INSTANCE_REF=main`。
3. 设置仅能访问私密实例仓库的 `RP_INSTANCE_TOKEN` secret（读取和数据写回权限）。可选 `RP_ADAPTER_SECRETS` secret，由私密适配器解析其 JSON 结构。
4. 参考 Git 模式需将私密 `adapter.json` 的 `git_sync` 设为 `true`。接入真实模型与平台时只修改私密脚本。也可以实现远程数据库适配器。
5. 平台消息先进入私密持久队列，再触发公开工作流；触发内容仅使用不含隐私的事件 ID。

公开运行器在发送前要求远程持久化确认，否则失败关闭。它不上传运行数据 artifact，不发布数据 Release，不输出回复正文。

## 开发验证

```bash
uv sync --locked --extra dev
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python scripts/export_schemas.py --check
uv run --no-sync python scripts/audit_public.py
uv run --no-sync python -m build --no-isolation
```

测试仅使用合成输入、临时数据库和本地 bare Git remote；不读取原 Tenshi 数据，不请求真实模型，不发送平台消息。
