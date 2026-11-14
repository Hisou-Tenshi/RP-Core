# RP-Core

完整运行时（0.3）：见 [功能对应、Secrets 验证和接入说明](docs/full-runtime.md)。

**一个公开运行框架，多个私密角色数据仓。所有角色共用同一份逻辑。**

模型驱动与配置、工具、平台收发、记忆与状态维护、数据兼容、Git 写回、工作流都在公开仓维护。私密仓只存角色卡、历史、记忆、日记、身份数据、运行状态，以及声明式 `io.json`。运行器不导入或执行私密仓的代码。

许可证 **AGPL-3.0-only**，版本 **0.3.0**。这是对 0.1 架构边界的修正：不再要求每个私密角色仓维护 adapter.py、模型网关或发送器。

## 架构

```text
公开 RP-Core / 你自己的公开 fork
  ├─ src/rp_core/              全部共享逻辑与内置驱动
  ├─ config/runtime/           模型、路由、策略、接入方式、持久化设置
  ├─ config/instances.json     多角色仓注册表
  ├─ integrations/cloudflare/ 可选低延迟入口
  └─ .github/workflows/        所有角色共用的工作流
             │ 同一公开提交 + 每角色环境 Secrets
             ├── 私密角色 A：角色卡 + io.json + 个人数据
             └── 私密角色 B：角色卡 + io.json + 个人数据
```

同一公开仓下的角色使用共享代码；修复公开仓后，后续触发统一使用更新后的逻辑。独立 fork 获取上游修复时需要 Sync fork，不需要逐个修改私密角色仓。

## 离线运行

需要 Python 3.11+ 和 uv。

```bash
uv sync --locked --extra dev
uv run --no-sync rp-core init-instance --path instance --demo-event
uv run --no-sync rp-core run --config config/runtime/offline.json --data-root instance
```

私密示例仅生成数据，没有 Python 或模型配置脚本。终端只输出计数；合成回复在 `instance/state/outbox.jsonl`。再次运行不会重复生成。天子示例是重新编写的合成数据；离线脚本模型不是 LLM，不访问任何 API。

## Fork 后部署

1. Fork 此公开仓，启用 Actions。
2. 新建私密角色数据仓，用 init-instance 生成数据，填写实际角色卡与 identity.json，提交到私密仓。
3. 在公开 fork 中设置 repository variable `RP_TENSHI_REPOSITORY=你的账号/私密数据仓`。
4. 建立 GitHub Environment `tenshi`，设置 `RP_INSTANCE_TOKEN`、`RP_MODEL_API_KEY`、`RP_TELEGRAM_TOKEN`。
5. 手动运行 Run private characters，instance_id 选 tenshi。后续由定时器轮询 Telegram。

新增角色只需再建数据仓，并在公开 `config/instances.json` 登记其数据仓变量、Environment 与公开运行配置。共用通用工作流，不复制代码或逐角色维护脚本。

完整步骤、权限、低延迟入口与平台切换见 [部署指南](docs/deploy.md)。GitHub 公开仓标准 runner 的免费使用不包含模型 API 费用；默认在线配置通过 OpenRouter 调用模型。

## 已提供的公开逻辑

- 两阶段回复、正常沉默、主动感知、受控工具与延迟副作用。
- 原子状态、幂等回执、版本冲突检查、待发队列、总结、压缩及关系提案。
- Chat Completions 兼容模型驱动、任务路由、重试与备用链；参数全部放公开配置。
- Telegram 轮询、身份校验、文本发送与投递回执；可选 Cloudflare webhook 私密队列入口。
- 原生 SQLite 数据驱动，以及直接复用旧 Tenshi JSON / SQLite 的兼容驱动。
- 每角色独立 Secrets、独立并发组、远程持久化确认；无运行数据公开 artifact。

当前是可部署的文本 RP 基础框架，不声称与原 Tenshi 全量等价。旧向量保留不重建，兼容检索目前使用关键词；图片 reference 可进入上下文，但尚无完整视觉分析、流式回复、贴纸等原系统能力。今后这些能力也应在公开仓补齐。

## 接口与旧数据

- [私密数据仓与声明式 I/O](docs/private-adapter.md)
- [旧 Tenshi 接入与保留数据库](docs/tenshi-migration.md)
- [rp/1 编排接口](docs/protocol-v1.md)
- [兼容承诺](docs/compatibility.md)
- [安全边界与审查](docs/security-and-review.md)

数据 `io.json` 选择内置格式和文件映射，不包含可执行命令。`rp/1` 仍是内部分层与可选标准输入/输出接口，但默认部署直接使用公开内置驱动，不要求私密进程。

## 验证

```bash
uv sync --locked --extra dev
uv run --no-sync pytest
node --test tests/test_cloudflare.mjs
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python scripts/export_schemas.py --check
uv run --no-sync python scripts/audit_public.py
uv run --no-sync python -m build --no-isolation
```

测试使用合成数据、临时旧格式数据库、本地 HTTP 服务与 bare Git remote。不会调用真实模型或发送真实平台消息。
