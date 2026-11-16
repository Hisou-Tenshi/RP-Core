# SPDX-License-Identifier: AGPL-3.0-only
"""Credential wiring and history continuity during the site-name cutover."""

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "src/rp_core/compat_runtime"


class UniqueKeysLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently dropping credentials."""


def unique_mapping(loader, node):
    pairs = loader.construct_pairs(node)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = value
    return result


UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def test_instance_probe_and_validation_pass_the_same_site_credentials():
    tree = ast.parse((CODE / "src/config/providers.py").read_text())
    specs = []
    for node in tree.body:
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and (
            isinstance(node.target, ast.Name) and node.target.id == "PROXY_ENV_SPECS"
        ):
            specs.extend(ast.literal_eval(node.value))
    expected = {
        spec["env"]
        for spec in specs
        if spec["role"] == "key"
        and re.fullmatch(
            r"(?:CLAUDE|GEMINI|GPT|GROK)_(?:CODEYU|CODERPLAN|TOTOKENS)(?:_(?:BASE|PRIME|MAX))?_API_KEY",
            spec["env"],
        )
    }
    assert len(expected) == 14
    for name, job in (
        ("instance.yml", "run"),
        ("provider-health.yml", "probe"),
        ("full-validation.yml", "validate"),
    ):
        loader = UniqueKeysLoader((ROOT / ".github/workflows" / name).read_text())
        try:
            workflow = loader.get_single_data()
        finally:
            loader.dispose()
        env = workflow["jobs"][job]["env"]
        for key in expected:
            assert env[key] == "${{ secrets." + key + " }}"
        assert not any("PROXY" in key for key in env)


def test_claude_codeyu_url_is_public_and_does_not_need_an_actions_variable():
    catalog = json.loads((CODE / "model-config.json").read_text())
    assert catalog["environment"]["CLAUDE_CODEYU_BASE_URL"] == "https://codeyu.shop"
    for name in ("instance.yml", "provider-health.yml", "full-validation.yml"):
        source = (ROOT / ".github/workflows" / name).read_text()
        assert "CLAUDE_CODEYU_BASE_URL" not in source


@pytest.mark.parametrize(
    ("provider", "old", "new"),
    [
        ("claude", "proxy3", "totokens"),
        ("claude", "proxy1_base", "codeyu_base"),
        ("claude", "proxy1_prime", "codeyu_prime"),
        ("claude", "proxy1_max", "codeyu_max"),
        ("claude", "proxy2", "coderplan"),
        ("gemini", "proxy1", "codeyu"),
        ("gemini", "proxy2", "coderplan"),
        ("gemini", "proxy3", "totokens"),
        ("gpt", "proxy1", "codeyu"),
        ("gpt", "proxy2", "coderplan"),
        ("gpt", "proxy3", "totokens"),
        ("grok", "proxy1", "coderplan"),
        ("grok", "proxy2", "totokens"),
        ("grok", "codeyu", "codeyu"),
    ],
)
def test_historical_and_new_call_ids_merge_under_the_site_name(tmp_path, provider, old, new):
    spec = importlib.util.spec_from_file_location(
        "site_migration_probe", ROOT / "scripts/probe_public_health.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    path = tmp_path / "calls.json"
    path.write_text(
        json.dumps(
            [
                {"provider": provider, "route": old, "outcome": "success"},
                {"provider": provider, "route": new, "outcome": "failure"},
            ]
        )
    )
    result = probe.sanitize_call_history(path)
    assert result["sample_size"] == 2
    assert result["providers"] == {provider: {new: {"calls": 2, "success": 1, "failure": 1}}}
