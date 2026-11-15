# SPDX-License-Identifier: AGPL-3.0-only
"""Run the audited upstream regression suite against public code and fake data."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPAT_CATALOG = ROOT / "validation/fixtures/compat-model-config.json"


def check_current_catalog(sandbox: Path, env: dict[str, str]):
    """Exercise the editable catalog before running fixed historical scenarios."""
    script = """
import json
from dataclasses import asdict
from pathlib import Path
from config import models

configured = json.loads(Path('model-config.json').read_text())
assert [asdict(model) for model in models.MODELS] == configured['models'], 'Model registry differs from the configured catalog'
assert json.loads(json.dumps([asdict(provider) for provider in models.PROVIDERS])) == configured['providers'], 'Provider registry differs from the configured catalog'
ids = {item['id'] for item in configured['models']}
assert set(models.MODEL_REGISTRY) == ids, 'Registered model IDs differ'
for tier, expected in configured['dialog_tiers'].items():
    assert models.dialog_tier_models(tier) == expected, 'Dialog tier does not use the configured models: ' + tier
    chain = models.dialog_chain_from_tier(tier)
    assert chain, 'Empty dialog chain: ' + tier
    assert all(item.split('#', 1)[0] in ids for item in chain), 'Unknown model in computed dialog chain: ' + tier
assert models.low_tier_flash_models() == configured['low_tier_pool'], 'Low-tier pool differs'
for spec in configured['models']:
    assert models.openrouter_fallback_slug(spec['id']) == (spec['slug'] or None), 'OpenRouter mapping differs for: ' + spec['id']
assert models.EMBEDDING_MODEL == configured['embedding']['model'], 'Embedding model differs'
assert models.EMBEDDING_DIM == configured['embedding']['dimensions'], 'Embedding dimension differs'
print('Current editable catalog: schema, runtime registry, routes and model mappings passed.')
"""
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def main():
    CODE = ROOT / "src/rp_core/compat_runtime"
    with tempfile.TemporaryDirectory(prefix="rp-compat-tests-") as temporary:
        sandbox = Path(temporary)
        for name in ("src", "scripts", "skills"):
            shutil.copytree(CODE / name, sandbox / name)
        for name in ("model-config.json", "model-config.schema.json"):
            shutil.copy2(CODE / name, sandbox / name)
        shutil.copytree(ROOT / "validation/compat_tests", sandbox / "tests")
        (sandbox / "tests/__init__.py").write_text("")
        (sandbox / "data").mkdir()
        (sandbox / "logs").mkdir()
        card = {
            "id": "tenshi",
            "name": "比那名居天子",
            "instructions": "你是用于自动回归的合成天子示例，不含真实个人经历。",
            "role": {
                "ROLE_NAME_FULL": "比那名居天子",
                "ROLE_NAME_SHORT": "天子",
                "ROLE_NAME_EN": "Tenshi",
                "ROLE_SELF_REFERENCE": "本小姐",
                "ROLE_USER_REFERENCE": "地上之人",
            },
        }
        (sandbox / "character.json").write_text(json.dumps(card, ensure_ascii=False))
        (sandbox / "Tenshi.json").write_text('[{"id":"2000-01-01","text":"synthetic diary"}]')
        (sandbox / "data/knowledge_base.json").write_text("{}")
        (sandbox / "data/tenshi_log.json").write_text("[]")
        (sandbox / "data/stickers.json").write_text("[]")
        (sandbox / "data/bond_state.json").write_text(
            json.dumps(
                {
                    "settled": [
                        {
                            "id": "synthetic",
                            "text": "我们一起聊过天。",
                            "since": "2000-01-01",
                            "src": "synthetic",
                            "evidence": [{"quote": "一起聊聊天", "msg_id": "synthetic-1"}],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        (sandbox / "data/perception_state.json").write_text(
            json.dumps({"last_silence_at": 946684800})
        )
        (sandbox / "src/config/stickers.json").write_text("[]")
        env = {
            key: value
            for key, value in os.environ.items()
            if not any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        }
        env.update(
            {
                "RP_RUNTIME_ROOT": str(sandbox),
                "RP_CHARACTER_PATH": str(sandbox / "character.json"),
                "RP_EXTERNAL_PERSISTENCE": "1",
                "RP_KB_BASE_SHA": hashlib.sha256(b"{}").hexdigest(),
                "RP_SHADOW": "1",
                "RP_UNIT_TESTS": "1",
                "PYTHONPATH": str(sandbox / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "ADMIN_ID": "1",
            }
        )
        current = check_current_catalog(sandbox, env)
        print(current.stdout + current.stderr)
        if current.returncode:
            return current.returncode
        # Imported regression cases assert historical model IDs and mocked API
        # catalogs. Keep those scenarios reproducible without constraining the
        # independently validated, user-editable production catalog.
        shutil.copy2(COMPAT_CATALOG, sandbox / "model-config.json")
        print(
            "Historical compatibility scenarios use validation/fixtures/compat-model-config.json."
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(sandbox / "tests"), "-q", "--tb=short"],
            cwd=sandbox,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        log = Path(tempfile.gettempdir()) / "rp-compat-regression.log"
        log.write_text(result.stdout + result.stderr)
        print((result.stdout + result.stderr)[-18000:])
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
