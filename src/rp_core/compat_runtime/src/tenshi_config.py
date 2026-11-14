# SPDX-License-Identifier: AGPL-3.0-only


























from config.fixed import *
from config.tunable import *
from config.persona import *



from config.tunable import _default_tz_label






_SYSTEM_DYNAMIC_MARKER = "\n# 此刻\n"
_dyn_idx = SYSTEM_PROMPT_TEMPLATE.find(_SYSTEM_DYNAMIC_MARKER)
if _dyn_idx >= 0:
    SYSTEM_PROMPT_STATIC_CORE = SYSTEM_PROMPT_TEMPLATE[:_dyn_idx]
    SYSTEM_PROMPT_DYNAMIC_TEMPLATE = SYSTEM_PROMPT_TEMPLATE[_dyn_idx + 1 :]
else:
    SYSTEM_PROMPT_STATIC_CORE = SYSTEM_PROMPT_TEMPLATE
    SYSTEM_PROMPT_DYNAMIC_TEMPLATE = ""


def _build_skill_catalog_block() -> str:
    """生成"扩展技能目录"提示块(供 system prompt 注入);失败时用内置兜底。"""
    try:
        from tenshi_skills import format_skill_catalog_for_prompt

        return format_skill_catalog_for_prompt()
    except Exception:
        return (
            "# 扩展技能目录（用 read_skill 读全文后再调用对应工具）\n"
            "- search_memory: 翻长期记忆\n"
            "- search_web: 联网搜索\n"
            "- fetch_url: 抓取网页正文\n"
            "- read_config_file: 读项目文件"
        )


SYSTEM_PROMPT_STATIC_CORE = SYSTEM_PROMPT_STATIC_CORE.replace(
    "{skill_catalog}", _build_skill_catalog_block()
)
SYSTEM_PROMPT_TEMPLATE = SYSTEM_PROMPT_TEMPLATE.replace(
    "{skill_catalog}", _build_skill_catalog_block()
)
