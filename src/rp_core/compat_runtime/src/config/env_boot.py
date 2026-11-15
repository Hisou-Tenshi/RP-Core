# SPDX-License-Identifier: AGPL-3.0-only













import os

from _rp_model_config import apply_environment

apply_environment()

PROJECT_ROOT = os.environ["RP_RUNTIME_ROOT"]

_claude_key = os.getenv("CLAUDE_API_KEY", "") or ""
_anthropic_base = os.getenv("ANTHROPIC_BASE_URL", "") or ""
if _claude_key.startswith("sk-ant-") and _anthropic_base and "anthropic.com" not in _anthropic_base:
    os.environ.pop("ANTHROPIC_BASE_URL", None)


def __getattr__(name: str):

    if name == "PROJECT_ROOT":
        return PROJECT_ROOT
    raise AttributeError(name)
