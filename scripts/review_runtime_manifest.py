# SPDX-License-Identifier: AGPL-3.0-only
"""Check/update public review hashes without access to any private source repo."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/rp_core/compat_runtime"


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument(
        "--refresh", action="store_true", help="Record explicitly reviewed public edits"
    )
    args = parser.parse_args()
    manifest_path = ROOT / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    changed = []
    for item in manifest["files"]:
        path = (ROOT / item["path"]).resolve()
        if not path.is_relative_to(ROOT.resolve()) or path.suffix != ".py":
            raise ValueError("Invalid public review path")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("reviewed_sha256", item["published_sha256"]):
            changed.append(item["path"])
            if args.refresh:
                item["reviewed_sha256"] = digest
    if changed and args.check:
        raise SystemExit("Public runtime edits need review: " + ", ".join(changed))
    if changed:
        manifest["function_metrics_scope"] = (
            "Original code-only import snapshot; later public edits use reviewed_sha256."
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10))
    print(f"Verified {len(manifest['files'])} public modules; reviewed changes: {len(changed)}.")


if __name__ == "__main__":
    main()
