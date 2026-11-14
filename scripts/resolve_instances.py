# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve a trusted public registry into a metadata-only Actions matrix."""

import argparse
import json
import os
import re
from pathlib import Path


def resolve(path: Path, selected: str = "all", variables: dict | None = None):
    registry = json.loads(path.read_text())
    if registry.get("schema_version") != 1:
        raise ValueError("Unsupported public registry version")
    seen, result = set(), []
    root = path.resolve().parent.parent
    for item in registry["instances"]:
        identifier = item["id"]
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", identifier) or identifier in seen:
            raise ValueError("Invalid or duplicate instance ID")
        seen.add(identifier)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", item["repository_variable"]):
            raise ValueError("Invalid repository variable")
        runtime = (root / item["runtime"]).resolve()
        if not runtime.is_relative_to(root / "config") or not runtime.is_file():
            raise ValueError("Runtime configuration must be in the public config directory")
        environment = item.get("environment", identifier)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", environment):
            raise ValueError("Invalid deployment environment")
        if selected not in ("all", identifier):
            continue
        if variables is not None and not variables.get(item["repository_variable"]):
            continue
        result.append(
            {
                "id": identifier,
                "environment": environment,
                "repository_variable": item["repository_variable"],
                "runtime": item["runtime"],
            }
        )
    if selected != "all" and selected not in seen:
        raise ValueError("Unknown instance ID")
    return {"include": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=Path("config/instances.json"))
    parser.add_argument("--instance", default="all")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        if os.environ.get("RP_DEFAULT_REPO_AVAILABLE") == "true":
            configured = json.loads(os.environ.get("RP_REPOSITORY_VARIABLES", "{}"))
            configured.setdefault("RP_TENSHI_REPOSITORY", "configured")
            os.environ["RP_REPOSITORY_VARIABLES"] = json.dumps(configured)
        variables = (
            json.loads(os.environ["RP_REPOSITORY_VARIABLES"])
            if "RP_REPOSITORY_VARIABLES" in os.environ
            else None
        )
        matrix = resolve(args.registry, args.instance, variables)
    except (ValueError, KeyError, OSError, TypeError):
        raise SystemExit("Invalid public instance registry or selection.") from None
    encoded = json.dumps(matrix, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a") as stream:
            stream.write(
                "matrix="
                + encoded
                + "\nhas_instances="
                + str(bool(matrix["include"])).lower()
                + "\n"
            )
    else:
        print(encoded)


if __name__ == "__main__":
    main()
