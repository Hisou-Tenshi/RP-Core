# SPDX-License-Identifier: AGPL-3.0-only
"""Export the executable contracts; --check prevents schema drift in CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rp_core.protocol import SCHEMAS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "schemas"
    stale = []
    for name, schema in SCHEMAS.items():
        target = root / f"{name}.schema.json"
        value = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
        rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        if args.check:
            if not target.is_file() or target.read_text() != rendered:
                stale.append(name)
        else:
            root.mkdir(exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
    if stale:
        print("Schema exports are missing or stale: " + ", ".join(stale))
        return 1
    print(f"Validated {len(SCHEMAS)} schema exports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
