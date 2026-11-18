# SPDX-License-Identifier: AGPL-3.0-only
"""One public, validated model catalog for the full runtime and console."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from jsonschema import Draft7Validator

BASE = Path(__file__).resolve().parents[1]


def validate_config(value):
    schema = json.loads((BASE / "model-config.schema.json").read_text())
    Draft7Validator(schema).validate(value)
    if re.search(
        r"(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,}|[0-9]{8,12}:[A-Za-z0-9_-]{30,})",
        json.dumps(value),
    ):
        raise ValueError("Credential-like values must not be stored in the public catalog.")
    providers = [p["key"] for p in value["providers"]]
    if len(set(providers)) != len(providers):
        raise ValueError("Provider keys must be unique.")
    ids = [m["id"] for m in value["models"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Model IDs must be unique.")
    referenced = value["low_tier_pool"] + value["summary_models"]
    for chain in value["dialog_tiers"].values():
        referenced += chain
    if any(model not in ids for model in referenced):
        raise ValueError("Routing chains must reference registered models.")
    required = {
        "claude": {"pro", "flash", "lite"},
        "gemini": {"pro", "flash", "lite"},
        "deepseek": {"pro", "flash"},
        "gpt": {None},
        "glm": {"pro", "flash"},
        "magmarouter": {None},
        "openrouter": {None},
        "grok": {None},
    }
    for provider, tiers in required.items():
        for tier in tiers:
            if not any(
                m["provider"] == provider and (tier is None or m["tier"] == tier)
                for m in value["models"]
            ):
                raise ValueError(f"Required runtime pool is empty: {provider}/{tier}.")
    if not any(m["provider"] == "gemini" and m["vision"] for m in value["models"]):
        raise ValueError("A Gemini vision model is required by the full engine.")
    if any(
        next(m for m in value["models"] if m["id"] == mid)["provider"] != "claude"
        for mid in value["summary_models"]
    ):
        raise ValueError("The summary client requires Claude model IDs.")
    return value


CONFIG = validate_config(json.loads((BASE / "model-config.json").read_text()))
ROUTE_CATALOG = {
    "claude": {
        "totokens",
        "codeyu_base",
        "codeyu_prime",
        "codeyu_max",
        "coderplan",
        "77",
        "dragon",
        "openrouter",
        "official",
    },
    "gemini": {"totokens", "codeyu", "coderplan", "dragon", "openrouter", "official"},
    "gpt": {"totokens", "codeyu", "coderplan", "77", "dragon", "openrouter", "official"},
    "grok": {"codeyu", "totokens", "coderplan", "dragon", "openrouter", "official"},
    "deepseek": {"official", "dragon", "fireworks_pro", "openrouter"},
    "glm": {"official", "dragon", "openrouter"},
    "kimi": {"dragon", "official"},
    "qwen": {"official"},
    "magmarouter": {"official"},
    "openrouter": {"official"},
}
for provider, policy in CONFIG["path_policies"].items():
    allowed = ROUTE_CATALOG[provider]
    if (
        len(policy["order"]) != len(set(policy["order"]))
        or len(policy["disabled"]) != len(set(policy["disabled"]))
        or not set(policy["order"] + policy["disabled"]).issubset(allowed)
    ):
        raise ValueError("Invalid provider path settings.")


def apply_environment():
    # These names are strictly allowlisted by the schema; credentials cannot be set here.
    # Explicit public settings override legacy environment defaults. Missing names keep them.
    for name, value in CONFIG["environment"].items():
        os.environ[name] = value
    os.environ["RP_PROVIDER_PATH_POLICIES"] = json.dumps(
        CONFIG["path_policies"], separators=(",", ":")
    )
    for name, key in (
        ("EMBEDDING_MODEL", "model"),
        ("EMBEDDING_DIM", "dimensions"),
        ("EMBEDDING_MAX_INPUT_CHARS", "max_input_chars"),
    ):
        os.environ[name] = str(CONFIG["embedding"][key])
