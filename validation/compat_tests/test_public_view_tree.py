# SPDX-License-Identifier: AGPL-3.0-only
"""The public-code/private-data mounts remain inspectable by the RP file tool."""
from pathlib import Path
import tenshi_reply


def test_file_tool_walks_public_code_mount_without_git_or_env(tmp_path, monkeypatch):
    code = Path(tenshi_reply.__file__).parent
    (tmp_path / "src").symlink_to(code, target_is_directory=True)
    monkeypatch.setattr(tenshi_reply.tenshi_config, "PROJECT_ROOT", str(tmp_path))
    tree = tenshi_reply.read_config_file()
    assert "tenshi_reply.py" in tree
    assert "SEARCH_AGENT_PROMPT" in tenshi_reply.read_config_file("src/config/fixed.py")
    assert "File not found" in tenshi_reply.read_config_file(".env")
    assert "File not found" in tenshi_reply.read_config_file(".git/config")
