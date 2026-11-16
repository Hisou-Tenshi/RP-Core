# SPDX-License-Identifier: AGPL-3.0-only
"""Private history must say who changed what, without ever granting anything."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from rp_core import full_service as service
from rp_core import identity
from rp_core.data_init import initialize_data
from rp_core.legacy import write_json

OWNER = {"name": "Synthetic owner", "email": "97383295+synthetic-owner@users.invalid"}
CHARACTER = {"name": "Synthetic character", "email": "268836736+synthetic-character@users.invalid"}


@pytest.fixture
def root(tmp_path):
    initialize_data(tmp_path, driver="tenshi-v1")
    write_json(tmp_path / "identity.json", {"allowed_user_ids": []})
    return tmp_path


def test_defaults_stay_neutral_and_unpersonal(root):
    assert identity.resolve(root, "owner") == identity.DEFAULT_OWNER
    assert identity.resolve(root, "character") == identity.DEFAULT_CHARACTER
    for role in identity.SYSTEM_ROLES:
        assert identity.resolve(root, f"system:{role}") == identity.DEFAULT_SYSTEMS[role]
    assert identity.resolve(root, "system:unknown") == identity.DEFAULT_SYSTEMS["maintenance"]
    assert identity.resolve(root, "unrecognized") == identity.DEFAULT_SYSTEMS["maintenance"]


def test_private_identity_names_each_role(root):
    write_json(
        root / "identity.json",
        {
            "allowed_user_ids": [],
            "git": {
                "owner": OWNER,
                "character": CHARACTER,
                "systems": {"probe": {"name": "synthetic-probe", "email": "probe@systems.invalid"}},
            },
        },
    )
    assert identity.resolve(root, "owner") == (OWNER["name"], OWNER["email"])
    assert identity.resolve(root, "character") == (CHARACTER["name"], CHARACTER["email"])
    assert identity.resolve(root, "system:probe") == (
        "synthetic-probe",
        "probe@systems.invalid",
    )
    # A role the private file does not mention keeps its neutral placeholder.
    assert identity.resolve(root, "system:sync") == identity.DEFAULT_SYSTEMS["sync"]


@pytest.mark.parametrize(
    "git",
    [
        {"owner": "not-an-object"},
        {"character": {"name": "", "email": "valid@example.invalid"}},
        {"character": {"name": "Synthetic", "email": "not-an-email"}},
        {"systems": {"sync": {"email": "missing-name@example.invalid"}}},
    ],
)
def test_malformed_authorship_falls_back_instead_of_blocking_a_write(root, git):
    write_json(root / "identity.json", {"allowed_user_ids": [], "git": git})
    assert identity.resolve(root, "owner") == identity.DEFAULT_OWNER
    assert identity.resolve(root, "character") == identity.DEFAULT_CHARACTER


def test_title_uses_the_instance_clock():
    moment = datetime(2026, 11, 16, 1, 2, 3, tzinfo=timezone(timedelta(hours=8)))
    assert identity.title("感知", "世界观测已入档", moment) == (
        "感知｜世界观测已入档 · 11-16 01:02:03 [skip ci]"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [("8", 8), ("0", 0), ("-5", -5), ("", 8), ("nonsense", 8), ("99", 8)],
)
def test_timezone_offset_is_parsed_defensively(monkeypatch, raw, expected):
    monkeypatch.setenv("TIMEZONE_OFFSET", raw)
    assert identity.offset_hours() == expected


def test_every_queued_task_gets_its_own_readable_line():
    titles = set()
    for task in service.TASKS:
        role, category, event = identity.plan(task)
        assert role == "owner" or role == "character" or role.startswith("system:")
        assert category and event
        line = f"{category}｜{event}"
        assert "天子" not in line
        titles.add(line)
    # No two tasks may collapse into the same commit title again.
    assert len(titles) == len(service.TASKS)


@pytest.mark.parametrize(
    "task,expected",
    [
        ("queue", (OWNER, "收讯｜消息已入队")),
        ("reply", (CHARACTER, "回复｜回音已落档")),
        ("perception", (CHARACTER, "感知｜世界观测已入档")),
        ("probe", (None, "探活｜通路健康快照")),
        ("cleanup", (None, "维护｜运行时日志已清理")),
        ("recovery", (None, "恢复｜私有状态已保全")),
    ],
)
def test_persist_signs_and_titles_each_task(root, monkeypatch, task, expected):
    person, line = expected
    write_json(
        root / "identity.json",
        {"allowed_user_ids": [], "git": {"owner": OWNER, "character": CHARACTER}},
    )
    (root / "data").mkdir(exist_ok=True)
    (root / "data/tenshi_log.json").write_text("[]", encoding="utf-8")
    commands = []

    def fake(command, **kwargs):
        if command[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=str(root).encode())
        if command[:3] == ["git", "status", "--porcelain=v1"]:
            return SimpleNamespace(returncode=0, stdout=b" M data/tenshi_log.json\0")
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout=b"")
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(service.subprocess, "run", fake)
    service.persist(root, task=task)

    command = commands[-1]
    name = command[command.index("--git-name") + 1]
    email = command[command.index("--git-email") + 1]
    message = command[command.index("--message") + 1]
    if task == "probe":
        assert (name, email) == identity.DEFAULT_SYSTEMS["probe"]
    elif task == "cleanup":
        assert (name, email) == identity.DEFAULT_SYSTEMS["maintenance"]
    elif task == "recovery":
        assert (name, email) == identity.DEFAULT_SYSTEMS["sync"]
    else:
        assert (name, email) == (person["name"], person["email"])
    assert message.startswith(f"{line} · ")
    assert message.endswith(" [skip ci]")
    # Identity flags must precede the subcommand for argparse to accept them.
    assert command.index("--git-name") < command.index("push")


def test_already_isolated_events_no_longer_re_alarm_every_run(root, monkeypatch):
    monkeypatch.setenv("RP_ALLOW_LIVE", "1")
    write_json(
        root / "inbox/a.json",
        {
            "full_task": "perception",
            "legacy_payload": {"text": "synthetic", "user_id": 1, "chat_id": 1, "msg_id": 1},
        },
    )
    import hashlib

    receipt = root / ".rp/full-receipts" / (hashlib.sha256(b"a").hexdigest() + ".json")
    write_json(
        receipt,
        {"event_id": "a", "task": "perception", "status": "quarantined", "attempts": 1},
    )

    def explode(*args, **kwargs):
        raise AssertionError("an isolated event must never be resent")

    monkeypatch.setattr(service, "run_full", explode)
    counts = service.run_queue(root, {"persistence": {"driver": "local"}})
    assert counts["skipped"] == 1
    assert counts["quarantined"] == 0
    assert counts["failed"] == 0
