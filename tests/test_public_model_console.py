# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "src/rp_core/compat_runtime"


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_schema_copy_matches_public_authority():
    assert (ROOT / "web/lib/model-config.schema.json").read_bytes() == (
        CODE / "model-config.schema.json"
    ).read_bytes()


def test_model_validation_rejects_invalid_references_and_pools():
    validator = module(CODE / "src/_rp_model_config.py", "model_config_test")
    current = json.loads((CODE / "model-config.json").read_text())
    validator.validate_config(current)
    invalid = copy.deepcopy(current)
    invalid["dialog_tiers"]["max"] = ["missing-model"]
    with pytest.raises(ValueError, match="registered"):
        validator.validate_config(invalid)
    invalid = copy.deepcopy(current)
    invalid["models"] = [
        m for m in invalid["models"] if not (m["provider"] == "gemini" and m["tier"] == "lite")
    ]
    with pytest.raises(ValueError, match="empty"):
        validator.validate_config(invalid)


def test_shared_json_edits_reach_real_full_engine_without_private_code(tmp_path):
    sandbox = tmp_path / "runtime"
    shutil.copytree(CODE / "src", sandbox / "src")
    shutil.copy2(CODE / "model-config.schema.json", sandbox / "model-config.schema.json")
    config = json.loads((CODE / "model-config.json").read_text())
    for provider, tier, new_id in (
        ("claude", "flash", "claude-fixture-flash"),
        ("gemini", "flash", "gemini-fixture-flash"),
        ("claude", "pro", "claude-fixture-pro"),
    ):
        target = next(
            m for m in config["models"] if m["provider"] == provider and m["tier"] == tier
        )
        previous = target["id"]
        target["id"] = new_id
        for chain in [
            *config["dialog_tiers"].values(),
            config["low_tier_pool"],
            config["summary_models"],
        ]:
            chain[:] = [new_id if model == previous else model for model in chain]
    config["dialog_tiers"]["medium"] = ["claude-fixture-flash"]
    config["low_tier_pool"] = ["gemini-fixture-flash"]
    config["summary_models"] = ["claude-fixture-pro"]
    config["environment"]["GPT_REASONING_EFFORT"] = "high"
    (sandbox / "model-config.json").write_text(json.dumps(config))
    card = sandbox / "character.json"
    card.write_text(
        json.dumps({"id": "synthetic", "name": "Example", "instructions": "Synthetic test."})
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(sandbox / "src"),
        "RP_RUNTIME_ROOT": str(sandbox),
        "RP_CHARACTER_PATH": str(card),
    }
    script = """
import json
from config import models, fixed
print(json.dumps({"medium":models.dialog_chain_from_tier("medium")[0],
 "low":models.low_tier_flash_models(), "summary":models.claude_summary_model_chain(),
 "reasoning":fixed.GPT_REASONING_EFFORT}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {
        "medium": "claude-fixture-flash",
        "low": ["gemini-fixture-flash"],
        "summary": ["claude-fixture-pro"],
        "reasoning": "high",
    }


def test_health_publisher_drops_credentials_urls_bodies_and_freeform_errors():
    publisher = module(ROOT / "scripts/probe_public_health.py", "health_publisher_test")
    config = (CODE / "model-config.json").read_bytes()
    model_id = json.loads(config)["models"][0]["id"]
    raw = {
        "providers": {
            "claude": {
                "models_total": 1,
                "models_ok": 1,
                "paths": {
                    "Official": {
                        "ok": True,
                        "severity": "green",
                        "verdict": "ok",
                        "status": 200,
                        "latency_ms": 5,
                        "base_url": "https://sensitive.example/secret-path",
                        "key_masked": "not-for-publication",
                        "verdict_text": "upstream body and diagnostic errors",
                        "models": {
                            model_id: {"ok": True, "verdict": "ok", "error": "sensitive details"}
                        },
                        "checks": {"auth": {"ok": True, "verdict": "ok", "body": "sensitive body"}},
                    }
                },
            }
        }
    }
    result = publisher.sanitize(raw, "example", "123", config)
    text = json.dumps(result)
    for forbidden in ("sensitive", "base_url", "key_masked", "verdict_text", "upstream", "body"):
        assert forbidden not in text
    assert result["credential_source"] == "rp-core-actions:example"
    assert result["config_sha256"] == hashlib.sha256(config).hexdigest()
    assert result["providers"]["claude"]["paths"]["Official"]["models"][model_id]["ok"] is True


def test_public_probe_has_no_private_checkout_or_telegram_credentials():
    text = (ROOT / ".github/workflows/provider-health.yml").read_text()
    assert "repository:" not in text
    assert "TG_TOKEN" not in text
    assert "GH_TOKEN" not in text
    assert "environment: ${{ matrix.environment }}" in text
    assert "CLAUDE_PROXY_BASE_URL" in text
