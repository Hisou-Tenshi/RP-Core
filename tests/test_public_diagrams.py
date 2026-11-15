# SPDX-License-Identifier: AGPL-3.0-only
"""The documentation-image exception must not admit arbitrary binary data."""

from __future__ import annotations

import runpy
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = runpy.run_path(str(ROOT / "scripts/audit_public.py"))


def test_only_the_six_source_bound_readme_images_are_allowed():
    expected = AUDIT["DIAGRAM_IMAGES"]
    assert len(expected) == 6
    for name in expected:
        assert AUDIT["inspect"](ROOT / name, Path(name)) == []


def test_unlisted_png_remains_forbidden(tmp_path):
    image = tmp_path / "unreviewed.png"
    shutil.copy2(ROOT / "docs/diagrams/architecture.zh.png", image)
    problems = AUDIT["inspect"](image, Path("docs/diagrams/unreviewed.png"))
    assert "private-data-path" in problems
    assert "binary-public-file" in problems


def test_modified_allowed_image_fails_its_manifest(tmp_path):
    image = tmp_path / "architecture.zh.png"
    shutil.copy2(ROOT / "docs/diagrams/architecture.zh.png", image)
    shutil.copy2(ROOT / "docs/diagrams/manifest.json", tmp_path / "manifest.json")
    relative = Path("docs/diagrams/architecture.zh.png")
    assert AUDIT["inspect"](image, relative) == []
    image.write_bytes(image.read_bytes() + b"unreviewed modification")
    assert "private-data-path" in AUDIT["inspect"](image, relative)
