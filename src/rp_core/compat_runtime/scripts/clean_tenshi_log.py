# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Tuple


_DELETE = object()


def _is_empty_or_none_string(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return True
        if s.lower() == "none":
            return True
    return False


def _clean(obj: Any, *, drop_empty_containers: bool) -> Tuple[Any, int]:
    """
    递归清理：
    - dict: 删除值为空/None/'none' 的键；对子元素递归清理
    - list: 清理并删除空/None/'none' 元素；对子元素递归清理
    - 其它: 原样返回
    返回 (cleaned_obj 或 _DELETE, removed_count)
    """
    removed = 0

    if _is_empty_or_none_string(obj):

        return _DELETE, 0

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            cleaned_v, r = _clean(v, drop_empty_containers=drop_empty_containers)
            removed += r
            if cleaned_v is _DELETE:
                removed += 1
                continue
            out[k] = cleaned_v

        if drop_empty_containers and len(out) == 0:
            return _DELETE, removed
        return out, removed

    if isinstance(obj, list):
        out_list: list[Any] = []
        for item in obj:
            cleaned_item, r = _clean(item, drop_empty_containers=drop_empty_containers)
            removed += r
            if cleaned_item is _DELETE:
                removed += 1
                continue
            out_list.append(cleaned_item)

        if drop_empty_containers and len(out_list) == 0:
            return _DELETE, removed
        return out_list, removed

    return obj, 0


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理 tenshi_log.json：删除值为空/None/'none' 的字段（递归处理）。"
    )
    parser.add_argument(
        "--in",
        dest="in_path",
        default="data/tenshi_log.json",
        help="输入 JSON 文件路径（默认：data/tenshi_log.json）",
    )
    parser.add_argument(
        "--out",
        dest="out_path",
        default="data/tenshi_log.json",
        help="输出 JSON 文件路径（默认：覆盖原文件）",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="不生成备份（默认会为原文件生成 .bak-时间戳 备份）",
    )
    parser.add_argument(
        "--keep-empty-containers",
        action="store_true",
        help="保留清理后为空的 dict/list（默认会一并删除为空的容器字段）",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="输出 JSON 缩进（默认 2；设为 0 表示无缩进）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将要删除的字段数量，不写文件",
    )
    args = parser.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    drop_empty_containers = not args.keep_empty_containers

    raw = in_path.read_text(encoding="utf-8")
    data = json.loads(raw)

    cleaned, removed = _clean(data, drop_empty_containers=drop_empty_containers)
    if cleaned is _DELETE:
        cleaned = [] if isinstance(data, list) else {}

    if args.dry_run:
        print(f"[dry-run] 将删除（字段/元素）计数：{removed}")
        return 0

    if (not args.no_backup) and out_path.resolve() == in_path.resolve():
        ts = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
        backup = in_path.with_suffix(in_path.suffix + f".bak-{ts}")
        backup.write_text(raw, encoding="utf-8")
        print(f"已备份：{backup}")

    indent = None if args.indent == 0 else args.indent
    out_text = json.dumps(cleaned, ensure_ascii=False, indent=indent) + "\n"
    _atomic_write_text(out_path, out_text)
    print(f"已写入：{out_path}")
    print(f"已删除（字段/元素）计数：{removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
