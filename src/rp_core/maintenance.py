# SPDX-License-Identifier: AGPL-3.0-only
"""Private runtime housekeeping and independent private GitHub Release backups."""

from __future__ import annotations

import gzip
import json
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from rp_core.protocol import RPCError


def _attachment_references(root: Path) -> set[Path]:
    """Keep any attachment still referenced by live or archived structured data."""
    references = set()

    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            # Legacy entries contain absolute temporary-view paths as well as
            # relative paths. Their downloads suffix is the stable data binding.
            suffix = value.replace("\\", "/").split("downloads/", 1)
            if len(suffix) == 2:
                references.add((root / "downloads" / suffix[1]).resolve())

    for path in (root / "data").rglob("*"):
        if not path.is_file() or not (
            path.name.endswith(".json") or path.name.endswith(".json.gz")
        ):
            continue
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as source:
                visit(json.load(source))
        except (OSError, ValueError):
            # An unreadable reference source cannot justify deleting attachments.
            raise RPCError("cleanup_failed", "Attachment references could not be read.") from None
    return references


def rotate_logs(root: Path, *, now: float | None = None, min_bytes=1) -> int:
    now = time.time() if now is None else now
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    date = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    archive = logs / "archive" / date
    rotated = 0
    for path in logs.iterdir():
        if (
            path.is_file()
            and path.suffix in (".log", ".jsonl", ".txt")
            and path.stat().st_size >= min_bytes
        ):
            archive.mkdir(parents=True, exist_ok=True)
            # Multiple same-day runs do not overwrite an earlier archive.
            target = archive / f"{path.name}.{time.time_ns()}.gz"
            with path.open("rb") as source, gzip.open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
            path.write_bytes(b"")
            rotated += 1
    archive_root = logs / "archive"
    if archive_root.is_dir():
        dates = sorted(
            path
            for path in archive_root.iterdir()
            if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
        )
        for old in dates[:-3]:
            shutil.rmtree(old)
    return rotated


def cleanup(root: Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    references = _attachment_references(root)
    rotated = rotate_logs(root, now=now)
    removed = 0
    for path in (root / "downloads").rglob("*"):
        if (
            path.is_file()
            and path.name != ".gitkeep"
            and path.resolve() not in references
            and now - path.stat().st_mtime >= 3 * 86400
        ):
            path.unlink()
            removed += 1
    return {"rotated": rotated, "attachments_removed": removed}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github(env: dict, path: str, *, method="GET", payload=None, content_type=None):
    token = env.get("GITHUB_TOKEN")
    if not token:
        raise RPCError("backup_failed", "Private backup credentials are missing.")
    # Paths are constructed here; never accept an upload host from a response.
    base = "https://uploads.github.com/" if content_type else "https://api.github.com/"
    body = (
        payload if content_type else (json.dumps(payload).encode() if payload is not None else None)
    )
    request = Request(  # noqa: S310 -- fixed HTTPS hosts
        base + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": content_type or "application/json",
            "User-Agent": "RP-Core-private-backup",
        },
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=120) as response:
            data = response.read()
            return json.loads(data) if data else None
    except HTTPError as error:
        if error.code == 404 and method == "GET":
            return None
        raise RPCError(
            "backup_failed", "The private backup operation failed.", retryable=True
        ) from None
    except (URLError, OSError, ValueError):
        raise RPCError(
            "backup_failed", "The private backup operation failed.", retryable=True
        ) from None


def build_backup(root: Path, destination: Path):
    """Package private state, using SQLite's snapshot API rather than copying a live DB."""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        paths = [
            root / name for name in ("character.json", "identity.json", "io.json", "Tenshi.json")
        ]
        for directory in ("data", "logs", "downloads", ".rp", "inbox"):
            paths.extend(path for path in (root / directory).rglob("*") if path.is_file())
        for path in paths:
            if not path.is_file() or path.is_symlink() or path.name.endswith(("-wal", "-shm")):
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix == ".db":
                with tempfile.TemporaryDirectory(prefix="rp-db-snapshot-") as temporary:
                    snapshot = Path(temporary) / path.name
                    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    target = sqlite3.connect(snapshot)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                        source.close()
                    archive.write(snapshot, relative)
            else:
                archive.write(path, relative)


def backup(root: Path, env: dict, *, weekly=False, now=None) -> dict:
    repository = env.get("REPO_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RPCError("backup_failed", "The selected private repository is invalid.")
    prefix = "repos/" + repository
    metadata = _github(env, prefix)
    if (
        not metadata
        or metadata.get("private") is not True
        or metadata.get("full_name", "").lower() != repository.lower()
    ):
        raise RPCError(
            "backup_failed", "Backup destination must be the selected private repository."
        )
    today = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    kind = "weekly" if weekly else "daily"
    stamp = today.strftime("%G-W%V" if weekly else "%Y-%m-%d")
    tag = f"rp-core-{kind}-{stamp}"
    release = _github(env, prefix + "/releases/tags/" + tag)
    asset_name = "rp-private-state.zip"
    if release and any(
        asset.get("name") == asset_name and asset.get("state") == "uploaded"
        for asset in release.get("assets", [])
    ):
        return {"status": "already_completed", "kind": kind}
    with tempfile.TemporaryDirectory(prefix="rp-private-backup-") as temporary:
        bundle = Path(temporary) / asset_name
        build_backup(root, bundle)
        if release is None:
            release = _github(
                env,
                prefix + "/releases",
                method="POST",
                payload={
                    "tag_name": tag,
                    "name": f"RP {kind} private backup {stamp}",
                    "target_commitish": env.get("RP_DATA_BRANCH") or "main",
                    "draft": False,
                    "prerelease": False,
                },
            )
        for asset in release.get("assets", []):
            if asset.get("name") == asset_name:
                _github(env, prefix + f"/releases/assets/{int(asset['id'])}", method="DELETE")
        uploaded = _github(
            env,
            prefix + f"/releases/{int(release['id'])}/assets?name=" + quote(asset_name),
            method="POST",
            payload=bundle.read_bytes(),
            content_type="application/zip",
        )
        if not uploaded or uploaded.get("state") != "uploaded":
            raise RPCError("backup_failed", "The private backup upload was not confirmed.")
    # Delete only this framework's old backup releases after a confirmed upload.
    retained = 4 if weekly else 7
    releases = []
    page = 1
    while True:
        batch = _github(env, prefix + f"/releases?per_page=100&page={page}") or []
        releases.extend(
            item
            for item in batch
            if re.fullmatch(
                rf"rp-core-{kind}-" + (r"\d{4}-W\d{2}" if weekly else r"\d{4}-\d{2}-\d{2}"),
                item.get("tag_name", ""),
            )
        )
        if len(batch) < 100:
            break
        page += 1
    for old in sorted(releases, key=lambda item: item["tag_name"], reverse=True)[retained:]:
        _github(env, prefix + f"/releases/{int(old['id'])}", method="DELETE")
    return {"status": "completed", "kind": kind}
