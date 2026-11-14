# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic Tenshi example. All character-specific material stays out of the engine."""

from __future__ import annotations

from pathlib import Path

from rp_core import PROTOCOL
from rp_core.protocol import RPCError, dumps

CHARACTER = {
    "id": "tenshi-example",
    "name": "比那名居天子",
    "instructions": "扮演东方 Project 中的比那名居天子。她骄傲、率直，有点爱逞强，也愿意认真听对方说话。用自然的中文交流。不要假设与用户已有任何私人经历、亲密关系或约定。此卡为独立编写的简短示例，不含真实个人数据。",
}
EVENT = {
    "id": "synthetic-message-001",
    "session_id": "demo-session",
    "kind": "message",
    "occurred_at": "2026-01-01T12:00:00+00:00",
    "actor": {"id": "demo-user", "display_name": "示例用户"},
    "content": [{"type": "text", "text": "天子，今天一起去散步吧。"}],
}


def initialize_instance(path: Path) -> None:
    path = path.resolve()
    if path.exists() and any(path.iterdir()):
        raise RPCError("instance_exists", "Refusing to overwrite a nonempty instance directory.")
    path.mkdir(parents=True, exist_ok=True)
    files = {
        "instance.json": dumps(
            {
                "protocol": PROTOCOL,
                "adapter": {"command": ["{python}", "adapter.py"], "timeout_seconds": 120},
                "max_events": 25,
            }
        ),
        "adapter.json": dumps(
            {
                "model_command": ["{python}", "-m", "rp_core", "demo-model"],
                "git_sync": False,
                "git_branch": "main",
            }
        ),
        "character.json": dumps(CHARACTER),
        "events.jsonl": dumps(EVENT) + "\n",
        "adapter.py": "# SPDX-License-Identifier: AGPL-3.0-only\nfrom pathlib import Path\nfrom rp_core.reference import serve_reference\n\nserve_reference(Path(__file__).resolve().parent)\n",
        ".gitignore": ".env\n.env.*\n__pycache__/\n.runtime/\n*.pyc\n# state/ belongs ONLY in a private repository.\n",
    }
    for name, content in files.items():
        (path / name).write_text(content, encoding="utf-8")


def model_handler(method: str, params: dict) -> dict:
    if method != "model.generate":
        raise RPCError("method_not_found", "The demo model supports model.generate only.")
    purpose = params.get("purpose")
    outputs = {
        "reply.plan": {
            "decision": "reply",
            "intent": "Accept the invitation in a proud but friendly voice.",
        },
        "reply.render": {
            "decision": "reply",
            "messages": ["哼，正好本小姐也想出去走走。", "走吧，路由你来带。"],
        },
        "maintenance.summarize": {
            "summary": "示例用户邀请天子散步。",
            "memories": ["一次虚构的散步邀请，用于离线测试。"],
        },
        "maintenance.compact": {"summary": "示例对话围绕一次散步邀请展开。"},
        "maintenance.reflect": {
            "proposal": "可继续自然交流；没有足够证据建立更深关系。",
            "evidence": ["合成示例中的散步邀请"],
        },
    }
    if purpose not in outputs:
        raise RPCError("invalid_params", "Unsupported demo purpose.")
    return {"output": outputs[purpose], "tool_calls": [], "provider": "offline-scripted-demo"}
