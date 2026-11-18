# SPDX-License-Identifier: AGPL-3.0-only










from __future__ import annotations

import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from config import models as m
from config import tunable as tunable_config

MODELS_PATH = os.path.join(SRC, "config", "models.py")


_PROVIDER_LIST_CASES = {
    "CLAUDE_MAX_MODELS": ("claude", "max"),
    "CLAUDE_PRO_MODELS": ("claude", "pro"),
    "CLAUDE_FLASH_MODELS": ("claude", "flash"),
    "CLAUDE_LITE_MODELS": ("claude", "lite"),
    "DEEPSEEK_PRO_MODELS": ("deepseek", "pro"),
    "DEEPSEEK_FLASH_MODELS": ("deepseek", "flash"),
    "GEMINI_PRO_MODELS": ("gemini", "pro"),
    "GEMINI_LITE_MODELS": ("gemini", "lite"),
    "MAGMAROUTER_MODELS": ("magmarouter", None),
    "OPENROUTER_MODELS": ("openrouter", None),
    "GROK_MODELS": ("grok", None),
    "GPT_MODELS": ("gpt", None),
    "GLM_PRO_MODELS": ("glm", "pro"),
    "GLM_FLASH_MODELS": ("glm", "flash"),
}


_MODELISH = re.compile(r"^(claude|gemini|grok|deepseek|glm|gpt|qwen|magma|mimo)-[a-z0-9]")
_SLUGISH = re.compile(r"^(anthropic|google|x-ai|openai|z-ai|deepseek)/")


def _models_table_node(tree: ast.Module) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "MODELS":
            return node
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "MODELS" for target in node.targets
        ):
            return node
    raise AssertionError("models.py 里找不到 MODELS 总表定义")


class SingleSourceTableTest(unittest.TestCase):
    """模型 id / slug 只写在总表里，没有第二份手抄清单。"""

    def test_ids_and_slugs_only_appear_inside_models_table(self):
        with open(MODELS_PATH, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        table = _models_table_node(tree)
        assert table.end_lineno is not None
        outside: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if not (_MODELISH.search(value) or _SLUGISH.search(value)):
                continue
            if table.lineno <= node.lineno <= table.end_lineno:
                continue
            outside.append((node.lineno, value))
        self.assertEqual(
            outside,
            [],
            "模型 id / OpenRouter slug 只允许写在 src/config/models.py 的 MODELS 总表里；"
            f"以下位置重复登记了(应改成从总表派生或添加备注): {outside}",
        )

    def test_models_table_is_valid_registry(self):
        ids = [spec.id for spec in m.MODELS]
        self.assertEqual(len(ids), len(set(ids)), "MODELS 总表里存在重复 id")
        provider_keys = {spec.key for spec in m.PROVIDERS}
        for spec in m.MODELS:
            self.assertIn(spec.provider, provider_keys, spec.id)
            self.assertIn(spec.tier, m.MODEL_TIER_RANK, spec.id)
        self.assertEqual(m.MODEL_REGISTRY, {spec.id: spec for spec in m.MODELS})


class DerivedViewsTest(unittest.TestCase):
    """列表 / 映射 / 五档 / 容灾组都是派生视图，不是手写常量。"""

    def test_provider_lists_are_derived(self):
        for name, (provider, tier) in _PROVIDER_LIST_CASES.items():
            self.assertEqual(
                getattr(m, name),
                m.provider_models(provider, tier),
                f"{name} 与 provider_models({provider!r}, {tier!r}) 不一致",
            )
        self.assertEqual(m.GEMINI_IMAGE_MODELS, m.provider_models("gemini", vision=True))
        self.assertEqual(
            m.GPT_FLASH_MODELS,
            [model for model in m.GPT_MODELS if m.MODEL_REGISTRY[model].in_chain],
        )
        self.assertEqual(tunable_config.GEMINI_IMAGE_MODELS, m.GEMINI_IMAGE_MODELS)

    def test_provider_table_drives_labels_and_fallback_order(self):
        self.assertEqual(
            m.PROVIDER_LABELS,
            {spec.key: spec.label for spec in m.PROVIDERS},
        )
        self.assertEqual(
            m.FALLBACK_PROVIDER_ORDER,
            tuple(spec.key for spec in m.PROVIDERS if spec.in_fallback_chain),
        )
        for provider in m.FALLBACK_PROVIDER_ORDER:
            self.assertIn(provider, m.PROVIDER_LABELS)

    def test_openrouter_map_is_derived_from_slug_field(self):
        self.assertEqual(
            m.OPENROUTER_FALLBACK_MAP,
            {spec.id: spec.slug for spec in m.MODELS if spec.slug},
        )

        snapshot = m.MODEL_REGISTRY["claude-haiku-4-5-20251001"]
        self.assertEqual(snapshot.slug, "")
        self.assertIsNone(m.openrouter_fallback_slug(snapshot.id))

    def test_dialog_tiers_and_fallback_are_derived(self):
        self.assertEqual(m.DIALOG_TIER_ORDER, ("max", "high", "medium", "low", "minimal"))
        self.assertEqual(m.DIALOG_TIER_MAX_MODELS, m.CLAUDE_MAX_MODELS[:1])
        self.assertEqual(m.DIALOG_TIER_HIGH_MODELS, m.CLAUDE_PRO_MODELS[:1])
        self.assertEqual(m.DIALOG_TIER_MEDIUM_MODELS, m.GEMINI_PRO_MODELS[:1])
        self.assertEqual(m.DIALOG_TIER_LOW_MODELS, m.GEMINI_FLASH_MODELS[:1])
        self.assertEqual(m.DIALOG_TIER_MINIMAL_MODELS, m.CLAUDE_FLASH_MODELS[:1])
        self.assertEqual(
            m.DIALOG_TIER_HEAD_MODELS,
            m.dedupe_models(*m.DIALOG_TIER_MODELS.values()),
        )
        self.assertEqual(
            m.DIALOG_FALLBACK_MODELS,
            m.disaster_recovery_models(m.DIALOG_TIER_HEAD_MODELS),
        )

        self.assertEqual(m.TASK_MODEL_PRIMARY["dialog_max"], m.DIALOG_TIER_MAX_MODELS[0])
        self.assertEqual(m.TASK_MODEL_PRIMARY["dialog_high"], m.DIALOG_TIER_HIGH_MODELS[0])
        self.assertEqual(m.TASK_MODEL_PRIMARY["dialog_medium"], m.DIALOG_TIER_MEDIUM_MODELS[0])
        self.assertEqual(m.TASK_MODEL_PRIMARY["dialog_low"], m.DIALOG_TIER_LOW_MODELS[0])
        self.assertEqual(m.TASK_MODEL_PRIMARY["dialog_minimal"], m.DIALOG_TIER_MINIMAL_MODELS[0])
        self.assertEqual(m.TASK_MODEL_PRIMARY["perception"], m.DIALOG_TIER_LOW_MODELS[0])
        self.assertEqual(m.task_model_chain("perception"), m.dialog_chain_from_tier("low"))


class AddOneRowPropagationTest(unittest.TestCase):
    """模拟「总表加一行」：不需要改列表/映射，派生函数就能看到新模型。"""

    def setUp(self):
        self._orig_models = m.MODELS
        self._orig_registry = m.MODEL_REGISTRY
        self.new = m.ModelSpec(
            "claude-opus-9-test",
            "claude",
            "pro",
            9.0,
            slug="anthropic/claude-opus-9.test",
            note="仅测试用，不写入文件",
        )
        m.MODELS = (self.new,) + self._orig_models
        m.MODEL_REGISTRY = {spec.id: spec for spec in m.MODELS}

    def tearDown(self):
        m.MODELS = self._orig_models
        m.MODEL_REGISTRY = self._orig_registry

    def test_new_row_becomes_provider_head_and_fallback_head(self):
        self.assertEqual(m.provider_models("claude", "pro")[0], self.new.id)
        self.assertEqual(m.provider_ranked_models("claude")[0], self.new.id)
        self.assertEqual(m.disaster_recovery_models([])[0], self.new.id)
        self.assertEqual(m.model_generation(self.new.id), 9.0)
        self.assertEqual(m.model_tier(self.new.id), "pro")
        self.assertEqual(m.model_provider(self.new.id), "claude")

        self.assertIn(self.new.id, m.provider_models("claude"))
        self.assertIn(self.new.id, m.disaster_recovery_models(m.DIALOG_TIER_HEAD_MODELS))


if __name__ == "__main__":
    unittest.main()
