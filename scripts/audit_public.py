# SPDX-License-Identifier: AGPL-3.0-only
"""Basic public-tree checks. Report filenames/categories, never matching secrets."""

from __future__ import annotations

import hashlib
import json
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
DIAGRAM_IMAGES = {
    f"docs/diagrams/{kind}.{language}.png"
    for kind in ("architecture", "execution")
    for language in ("zh", "en", "ja")
}


def verified_diagram(path: Path, relative: Path) -> bool:
    """Only the six reviewed, source-bound documentation PNGs may be binary."""
    if relative.as_posix() not in DIAGRAM_IMAGES or path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size > 2_000_000:
            return False
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return False
        manifest = json.loads((path.parent / "manifest.json").read_text())
        entries = [item for item in manifest["diagrams"] if item["image"] == relative.as_posix()]
        if manifest["schema_version"] != 1 or len(entries) != 1:
            return False
        entry = entries[0]
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return (
            0 < width <= 12000
            and 0 < height <= 12000
            and width * height <= 30_000_000
            and entry["width"] == width
            and entry["height"] == height
            and entry["bytes"] == len(data)
            and entry["image_sha256"] == hashlib.sha256(data).hexdigest()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def inspect(path: Path, relative: Path) -> list[str]:
    if verified_diagram(path, relative):
        return []
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
