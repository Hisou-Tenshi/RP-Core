# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "src/rp_core/compat_runtime"


def runner():
    spec = importlib.util.spec_from_file_location(
        "compat_catalog_runner", ROOT / "scripts/run_compat_tests.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sandbox(tmp_path, catalog):
    shutil.copytree(CODE / "src", tmp_path / "src")
    shutil.copy2(CODE / "model-config.schema.json", tmp_path / "model-config.schema.json")
    (tmp_path / "model-config.json").write_text(json.dumps(catalog))
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(tmp_path / "src"),
        "RP_RUNTIME_ROOT": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_actual_editable_catalog_loads_through_real_runtime(tmp_path):
    current = json.loads((CODE / "model-config.json").read_text())
    env = sandbox(tmp_path, current)
    result = runner().check_current_catalog(tmp_path, env)
    assert result.returncode == 0, result.stderr


def test_renamed_model_and_explicit_tier_are_not_tied_to_historical_names(tmp_path):
    current = json.loads((CODE / "model-config.json").read_text())
    customized = copy.deepcopy(current)
    target = next(
        m for m in customized["models"] if m["provider"] == "gemini" and m["tier"] == "max"
    )
    previous = target["id"]
    target.update(
        id="gemini-configurable-max-fixture",
        slug="google/gemini-configurable-max-fixture",
        generation=99.1,
    )
    for chain in [
        *customized["dialog_tiers"].values(),
        customized["low_tier_pool"],
        customized["summary_models"],
    ]:
        chain[:] = [target["id"] if model == previous else model for model in chain]
    customized["dialog_tiers"]["high"] = [target["id"]]
    env = sandbox(tmp_path, customized)
    result = runner().check_current_catalog(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "model-config.json").read_text()) == customized


def test_current_catalog_gate_rejects_valid_data_with_broken_runtime_mapping(tmp_path):
    current = json.loads((CODE / "model-config.json").read_text())
    env = sandbox(tmp_path, current)
    model_code = tmp_path / "src/config/models.py"
    model_code.write_text(model_code.read_text() + '\nDIALOG_TIER_MODELS["high"] = []\n')
    result = runner().check_current_catalog(tmp_path, env)
    assert result.returncode != 0
    assert "Dialog tier does not use the configured models" in result.stderr


def test_current_catalog_gate_rejects_dangling_references(tmp_path):
    current = json.loads((CODE / "model-config.json").read_text())
    current["dialog_tiers"]["high"] = ["gemini-not-registered-fixture"]
    env = sandbox(tmp_path, current)
    result = runner().check_current_catalog(tmp_path, env)
    assert result.returncode != 0
    assert "Routing chains must reference registered models" in result.stderr


def test_historical_fixture_is_not_the_production_catalog():
    historical = runner().COMPAT_CATALOG
    assert historical.is_relative_to(ROOT / "validation/fixtures")
    assert historical.resolve() != (CODE / "model-config.json").resolve()
    data = json.loads(historical.read_text())
    assert data["dialog_tiers"]["high"] == ["claude-opus-5-5"]
