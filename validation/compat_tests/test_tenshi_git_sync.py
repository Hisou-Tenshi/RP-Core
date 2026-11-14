# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "tenshi_git_sync",
    Path(__file__).resolve().parent.parent / "scripts" / "tenshi_git_sync.py",
)
assert _SPEC and _SPEC.loader
sync = importlib.util.module_from_spec(_SPEC)



sys.modules[_SPEC.name] = sync
_SPEC.loader.exec_module(sync)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _must(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr}"
    return proc.stdout or ""


@pytest.fixture()
def repos(tmp_path: Path):
    """造一个 bare remote + 本分支检出 + 一个「另一个流程」的检出。"""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _must(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed.mkdir()
    (seed / "data").mkdir()
    (seed / "logs").mkdir()
    (seed / "data" / "tenshi_log.json").write_text('{\n  "entries": [\n    {"id": 1}\n  ]\n}\n', encoding="utf-8")
    (seed / "logs" / "llm_trace.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    (seed / "notes.txt").write_text("base\n", encoding="utf-8")
    _must(seed, "init", "-q", "-b", "main")
    _must(seed, "config", "user.email", "seed@x.y")
    _must(seed, "config", "user.name", "seed")
    _must(seed, "add", "-A")
    _must(seed, "commit", "-q", "-m", "seed")
    _must(seed, "push", "-q", str(remote), "main")

    url = remote.resolve().as_uri()
    work = tmp_path / "work"
    other = tmp_path / "other"
    _must(tmp_path, "clone", "-q", url, str(work))
    _must(tmp_path, "clone", "-q", url, str(other))
    for repo_dir in (work, other):
        _must(repo_dir, "config", "user.email", "bot@x.y")
        _must(repo_dir, "config", "user.name", "bot")
    return {"url": url, "work": work, "other": other}


def _repo(path: Path) -> "sync.Repo":
    repo = sync.Repo(path=path)
    repo.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", check=False)
    return repo


def test_append_only_conflict_keeps_both_sides(repos):
    """两个 run 同时往 JSONL 追加 → 两边内容都必须留在 main 上。"""
    work, other = repos["work"], repos["other"]
    repo = _repo(work)

    (work / "logs" / "llm_trace.jsonl").write_text('{"id": 1}\n{"id": 2, "who": "ours"}\n', encoding="utf-8")
    sync.commit_unit(repo, ["logs/llm_trace.jsonl"], "ours")

    (other / "logs" / "llm_trace.jsonl").write_text('{"id": 1}\n{"id": 2, "who": "theirs"}\n', encoding="utf-8")
    _must(other, "add", "-A")
    _must(other, "commit", "-q", "-m", "theirs")
    _must(other, "push", "-q", "origin", "main")

    assert sync.push_direct(repo, "main", tier="reply", attempts=3, base_delay=0.01, max_delay=0.02)

    _must(other, "fetch", "-q", "origin", "main")
    merged = _must(other, "show", "origin/main:logs/llm_trace.jsonl")
    assert '"who": "ours"' in merged, "本分支的追加被丢了"
    assert '"who": "theirs"' in merged, "远端的追加被丢了"


def test_json_edit_conflict_is_merged_not_dropped(repos):
    """两侧各往同一个 JSON 数组里追加一条 → 合并后两条都在，且仍是合法 JSON。"""
    work, other = repos["work"], repos["other"]
    repo = _repo(work)

    (work / "data" / "tenshi_log.json").write_text(
        '{\n  "entries": [\n    {"id": 1},\n    {"id": 2, "who": "ours"}\n  ]\n}\n', encoding="utf-8"
    )
    sync.commit_unit(repo, ["data/tenshi_log.json"], "ours")

    (other / "data" / "tenshi_log.json").write_text(
        '{\n  "entries": [\n    {"id": 1},\n    {"id": 3, "who": "theirs"}\n  ]\n}\n', encoding="utf-8"
    )
    _must(other, "add", "-A")
    _must(other, "commit", "-q", "-m", "theirs")
    _must(other, "push", "-q", "origin", "main")

    assert sync.push_direct(repo, "main", tier="reply", attempts=3, base_delay=0.01, max_delay=0.02)

    _must(other, "fetch", "-q", "origin", "main")
    merged = _must(other, "show", "origin/main:data/tenshi_log.json")
    payload = json.loads(merged)
    ids = {entry.get("id") for entry in payload["entries"]}
    assert 2 in ids, "本分支新增的条目被丢了"
    assert 3 in ids, "远端新增的条目被丢了"


def test_recovery_leaves_no_stuck_rebase(repos):
    """回归 #3221：撞车后绝不能留下 .git/rebase-merge，也不能静默失败。"""
    work, other = repos["work"], repos["other"]
    repo = _repo(work)

    (work / "notes.txt").write_text("base\nours\n", encoding="utf-8")
    sync.commit_unit(repo, ["notes.txt"], "ours")

    (other / "notes.txt").write_text("base\ntheirs\n", encoding="utf-8")
    _must(other, "add", "-A")
    _must(other, "commit", "-q", "-m", "theirs")
    _must(other, "push", "-q", "origin", "main")

    assert sync.push_direct(repo, "main", tier="reply", attempts=3, base_delay=0.01, max_delay=0.02)
    assert not (work / ".git" / "rebase-merge").exists(), "rebase 卡住了"
    assert not (work / ".git" / "rebase-apply").exists(), "rebase 卡住了"


def test_direct_push_reports_failure_loudly(repos, capsys):
    """远端被别的东西推到不可达时应返回失败（而不是假装成功）。"""
    work = repos["work"]
    repo = _repo(work)
    _must(work, "remote", "set-url", "origin", "https://127.0.0.1:1/nope.git")
    (work / "notes.txt").write_text("changed\n", encoding="utf-8")
    sync.commit_unit(repo, ["notes.txt"], "ours")
    assert sync.push_direct(repo, "main", tier="edge", attempts=1) is False


def test_edge_tier_uses_staging_branch(repos):
    """边缘档必须推独立分支，不碰 main。"""
    work = repos["work"]
    repo = _repo(work)
    (work / "notes.txt").write_text("edge change\n", encoding="utf-8")
    assert sync.commit_unit(repo, ["notes.txt"], "edge")
    assert sync.push_branch(repo, "main", "12345", "Runtime Log Cleanup")

    local_main = _must(repos["other"], "rev-parse", "origin/main").strip()
    _must(repos["other"], "fetch", "-q", "origin")
    remote_main = _must(repos["other"], "rev-parse", "origin/main").strip()
    assert remote_main == local_main, "边缘档不应该动 main"

    branches = sync.list_staging_branches(repo)
    assert any(b.startswith("bot-staging/") for b in branches)


def test_merge_staging_lands_on_main(repos):
    """汇流器把 staging 分支的内容并回 main 并清理分支。"""
    work, other = repos["work"], repos["other"]
    repo = _repo(work)
    (work / "notes.txt").write_text("staged change\n", encoding="utf-8")
    assert sync.commit_unit(repo, ["notes.txt"], "edge")
    assert sync.push_branch(repo, "main", "999", "log_cleanup")

    merger = _repo(other)
    _must(other, "fetch", "origin")
    assert sync.merge_staging(merger) == 0

    _must(other, "fetch", "-q", "origin", "main")
    assert "staged change" in _must(other, "show", "origin/main:notes.txt")
    assert sync.list_staging_branches(merger) == []


def test_sparse_checkout_paths_are_committable(repos):
    """白名单外的路径必须能提交（secure_failures 那个 bug 的回归）。"""
    work = repos["work"]
    repo = _repo(work)
    (work / "secure_failures").mkdir()
    (work / "secure_failures" / "bundle.enc").write_text("ciphertext\n", encoding="utf-8")

    (work / ".git" / "info" / "sparse-checkout").write_text("data/\nlogs/\n", encoding="utf-8")
    _must(work, "config", "core.sparseCheckout", "true")
    assert sync.commit_unit(repo, ["secure_failures/bundle.enc"], "bundle")
    assert "bundle.enc" in _must(work, "show", "--name-only", "--pretty=format:", "HEAD")


def test_json_result_is_never_corrupted(repos):
    """退一步也必须保住「合法 JSON」这条底线（合并失败时只保留本分支）。"""
    work, other = repos["work"], repos["other"]
    repo = _repo(work)


    (work / "data" / "tenshi_log.json").write_text(
        '{\n  "entries": [\n    {"id": 1},\n    {"id": 2}\n  ]\n}\n', encoding="utf-8"
    )
    sync.commit_unit(repo, ["data/tenshi_log.json"], "ours")

    (other / "data" / "tenshi_log.json").write_text('{\n  "entries": [\n    {"id": 9}\n  ]\n}\n', encoding="utf-8")
    _must(other, "add", "-A")
    _must(other, "commit", "-q", "-m", "theirs")
    _must(other, "push", "-q", "origin", "main")

    assert sync.push_direct(repo, "main", tier="reply", attempts=4, base_delay=0.01, max_delay=0.02)

    _must(other, "fetch", "-q", "origin", "main")
    merged = _must(other, "show", "origin/main:data/tenshi_log.json")
    json.loads(merged)


class _NS:
    """最小的 args 替身，用来直接测 _git_identity 的各项分支。"""

    git_name = ""
    git_email = ""


def _conflict_via_rebase(repos, path: str, local_text: str, remote_text: str):
    """造一个真实的 rebase 冲突：远端先改并 push，本分支改同一文件后 rebase 上去。"""
    work, other = repos["work"], repos["other"]
    repo = _repo(work)
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(local_text, encoding="utf-8")
    assert sync.commit_unit(repo, [path], "local")

    remote_target = other / path
    remote_target.parent.mkdir(parents=True, exist_ok=True)
    remote_target.write_text(remote_text, encoding="utf-8")
    _must(other, "add", "-A")
    _must(other, "commit", "-q", "-m", "remote")
    _must(other, "push", "-q", "origin", "main")

    repo.git("fetch", "origin", "main")
    rebase = repo.git("rebase", "origin/main", check=False)
    assert rebase.returncode != 0, "这个场景应当产生 rebase 冲突"
    assert (_git(work, "ls-files", "-u").stdout or "").strip(), "索引里应有未合并阶段"
    return repo, target


def test_conflict_fallback_never_commits_conflict_markers(repos):
    """回归：兜底「保留本分支」时不能把 git 写的冲突标记提交进仓库。

    2026-10-28 探活产物（latest.md / dashboard.html / history.jsonl）实际踩到过：
    `_keep_branch_version` 直接 add 了带 `<<<<<<<` 的工作区文件。
    """
    repo, target = _conflict_via_rebase(
        repos,
        "logs/llm_trace.jsonl",
        local_text='{"id": 1}\n{"id": 2, "who": "local"}\n',
        remote_text='{"id": 1}\n{"id": 2, "who": "remote"}\n',
    )
    assert "<<<<<<<" in target.read_text(encoding="utf-8"), "工作区里应已有 git 冲突标记"

    sync._keep_branch_version(repo, "logs/llm_trace.jsonl")

    content = target.read_text(encoding="utf-8")
    assert "<<<<<<<" not in content
    assert ">>>>>>>" not in content
    assert "=======" not in content
    assert '"who": "local"' in content, "应保留本分支（stage#3）的干净版本"
    assert (_git(repo.path, "ls-files", "-u").stdout or "").strip() == "", "索引里不能残留未合并阶段"


def test_snapshot_path_keeps_local_side_not_json_union(repos):
    """探活快照冲突时取最新一侧，不能把两次 run 的 paths 合并成混合体。"""
    local = '{\n  "run_id": "new",\n  "paths": ["A", "B", "C"]\n}\n'
    repo, target = _conflict_via_rebase(
        repos,
        "data/provider_health.json",
        local_text=local,
        remote_text='{\n  "run_id": "old",\n  "paths": ["A"]\n}\n',
    )

    assert sync._resolve_conflicts(repo, set()) is True
    content = target.read_text(encoding="utf-8")
    assert "<<<<<<<" not in content
    assert content == local, "快照路径应整份保留本分支（最新 run）版本，而不是 JSON union"


def test_identity_is_required_for_commit(repos, monkeypatch, tmp_path):
    """回归：helper 必须自己下发 git 身份。

    runner 容器里 HOME=/github/home，没有全局 gitconfig；workflow 里原来的
    `git config --global user.name/email` 被统一入口取代后，如果 helper 不设身份，
    git commit 会直接以 128 失败（"Author identity unknown"）。
    """
    work = repos["work"]


    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    for key in ("user.name", "user.email"):
        _git(work, "config", "--unset-all", "--local", key)
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key, raising=False)

    (work / "notes.txt").write_text("identity check\n", encoding="utf-8")
    _git(work, "add", "--sparse", "--", "notes.txt")
    bare = _git(work, "-c", "user.useConfigOnly=true", "commit", "-m", "should fail")
    assert bare.returncode != 0, "没有身份时 commit 竟然成功了，测试前提不成立"


    name, email = sync._git_identity(_NS())
    repo = sync._setup_repo(work, identity=(name, email))
    assert _must(work, "config", "--get", "user.name").strip() == name
    assert sync.commit_unit(repo, ["notes.txt"], "with identity")
    assert _must(work, "log", "-1", "--pretty=format:%an <%ae>") == f"{name} <{email}>"


def test_identity_defaults_to_role_and_env_overrides(monkeypatch):
    monkeypatch.delenv("TENSHI_GIT_NAME", raising=False)
    monkeypatch.delenv("TENSHI_GIT_EMAIL", raising=False)

    assert sync._git_identity(_NS()) == (sync.DEFAULT_GIT_NAME, sync.DEFAULT_GIT_EMAIL)
    assert sync.DEFAULT_GIT_NAME == "RP character"

    monkeypatch.setenv("TENSHI_GIT_NAME", "Synthetic maintainer")
    monkeypatch.setenv("TENSHI_GIT_EMAIL", "maintainer@example.invalid")
    assert sync._git_identity(_NS()) == ("Synthetic maintainer", "maintainer@example.invalid")


    ns = _NS()
    ns.git_name = "命令行角色"
    ns.git_email = "cli@example.invalid"
    assert sync._git_identity(ns) == ("命令行角色", "cli@example.invalid")


def test_identity_rejects_empty(monkeypatch):
    monkeypatch.setenv("TENSHI_GIT_NAME", "   ")
    monkeypatch.setenv("TENSHI_GIT_EMAIL", "  ")
    ns = _NS()
    ns.git_name = " "
    ns.git_email = " "
    with pytest.raises(sync.SyncError):
        sync._git_identity(ns)


@pytest.mark.parametrize("tier", ["reply", "summary", "perceive", "edge"])
def test_tier_budgets_are_defined(tier):
    assert tier in sync.TIER_BUDGET
    assert tier in sync.DIRECT_TIERS or tier == "edge"


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(pytest.main([__file__, "-v"]))
