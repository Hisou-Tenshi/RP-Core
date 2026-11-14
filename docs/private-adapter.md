# 私密数据仓与声明式 I/O

路径沿用旧文档链接；0.2 的默认架构已不再使用私密代码适配器。全部格式转换、模型调用与平台收发均由公开仓实现。

## 新角色仓

```text
private-character/
  character.json       实际角色卡
  identity.json        允许交互的用户、聊天目标
  io.json              版本化数据格式及路径映射
  events.jsonl         可选本地输入
  inbox/*.json         持久输入队列
  state/               历史、记忆、日记、回执与待发状态
```

不需要 adapter.py、model_gateway.py、平台发送器、模型参数、SDK、依赖或工作流。已有私密仓中的旧代码可以暂时保留，公开运行器不会加载它。

最小 I/O：

```json
{"schema_version":1,"driver":"native"}
```

角色卡为 id、name、instructions。身份数据示例中的数字应换成自己的真实 Telegram ID，并保留在私密仓：

```json
{"allowed_user_ids":[123],"telegram":{"chat_id":123,"allowed_chat_ids":[123]}}
```

空授权列表默认不接收消息。聊天目标也必须在私密身份数据中获准。

## 复用已有格式

`io.json` 可以声明 driver=tenshi-v1，并通过 files 映射 character、identity、history、notes、knowledge、relationship、memory、state、inbox、events。session_map 可把新会话标识映射到旧 conversation_id。

示例在 `examples/io/tenshi-v1.json`。路径必须位于数据根目录内，不能指向 .git，不能指定模块或命令。格式与路径兼容逻辑由公开 `legacy.py` 维护；修复后所有角色受益。

## 公开运行配置

模型列表、厂商端点、任务分配、重试、备用链、温度、预算、平台选择、Git 分支都放公开 `config/runtime/`。配置可通过 extends 继承同目录中的公共配置，仅覆盖角色确实需要不同的非私密策略。密钥字段只填写环境变量名。

命令：

```bash
uv run --no-sync rp-core run --config config/runtime/default.json --data-root /path/to/private-character
```

`--io` 可显式指定已有映射文件；`inspect-data` 只检查映射，不建库、不迁移、不写入。

0.1 的 run-instance --manifest 和 rp/1 子进程协议仍可用于高级兼容接入，但不再是角色仓部署要求。
