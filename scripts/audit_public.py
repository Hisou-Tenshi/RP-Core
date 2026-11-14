# SPDX-License-Identifier: AGPL-3.0-only
"""Basic public-tree checks. Report filenames/categories, never matching secrets."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

DENIED_ROOTS = {"data", "logs", "downloads", "secure_failures", "reports", "instance"}
DENIED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pkl",
    ".enc",
    ".gz",
    ".bak",
    ".jpg",
    ".png",
    ".zip",
    ".bundle",
}
SECRETS = [
    re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{24,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"[0-9]{8,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def inspect(path: Path, relative: Path) -> list[str]:
    problems = []
    if (
        relative.parts[0] in DENIED_ROOTS
        or path.suffix in DENIED_SUFFIXES
        or path.name == "Tenshi.json"
    ):
        problems.append("private-data-path")
    if path.is_symlink():
        return [*problems, "symlink-not-reviewed"]
    if not path.is_file():
        return problems
    if path.stat().st_size > 2_000_000:
        return [*problems, "oversized-public-file"]
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeError:
        return [*problems, "binary-public-file"]
    if any(pattern.search(content) for pattern in SECRETS):
        problems.append("possible-credential")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    files = sorted(set(name for name in listing.decode().split(chr(0)) if name))
    failures = []
    for name in files:
        for category in inspect(root / name, Path(name)):
            failures.append(f"{name}: {category}")
    if failures:
        print("Public-tree review failed:\n" + "\n".join(failures))
        return 1
    print(
        f"Reviewed {len(files)} public paths; no configured private-data or credential patterns found."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
