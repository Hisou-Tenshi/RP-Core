# SPDX-License-Identifier: AGPL-3.0-only














import argparse
import json
import os
import re
from typing import Dict, List


def _project_root() -> str:
    return os.environ["RP_RUNTIME_ROOT"]


def _note_json_path(root: str) -> str:
    return os.path.join(root, "Tenshi.json")


def _load_entries_from_json(path: str) -> List[Dict]:
    """读取运行时的明文 Tenshi.json，返回条目列表。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            "Tenshi.json 不是合法的明文 JSON，请先修复该文件（本工具只处理明文 JSON）。"
        ) from e
    if not isinstance(obj, list):
        raise ValueError("Tenshi.json 的 payload 必须是 JSON list。")
    out: List[Dict] = []
    for it in obj:
        if isinstance(it, dict):
            out.append(it)
    return out


_MD_SEPARATOR = "---"


def _escape_md_body_line(line: str) -> str:
    """正文中恰好是分隔符的行需要转义，否则导入时会被误当成条目结束标记。"""
    if line.strip() == _MD_SEPARATOR:
        return line.replace(_MD_SEPARATOR, "\\" + _MD_SEPARATOR, 1)
    return line


def _unescape_md_body_line(line: str) -> str:
    if line.strip() == "\\" + _MD_SEPARATOR:
        return line.replace("\\" + _MD_SEPARATOR, _MD_SEPARATOR, 1)
    return line


def export_to_md(temp_md_path: str) -> None:
    """把 Tenshi.json 明文导出为可读 Markdown（id + 空行 + 正文 + ---）。"""
    root = _project_root()
    json_path = _note_json_path(root)
    entries = _load_entries_from_json(json_path)

    lines: List[str] = []

    entries_sorted = sorted(entries, key=lambda e: e.get("id", ""), reverse=False)
    for ent in entries_sorted:
        _id = ent.get("id", "")
        txt = (ent.get("text", "") or "").strip()
        lines.append(_id)
        lines.append("")
        body_lines = txt.splitlines()
        if not body_lines:
            body_lines = [""]
        for body_line in body_lines:
            lines.append(_escape_md_body_line(body_line))
        lines.append(_MD_SEPARATOR)
    content = "\n".join(lines).rstrip() + "\n"
    with open(temp_md_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"已导出 {len(entries_sorted)} 条记录到：{temp_md_path}")


def import_from_md(temp_md_path: str) -> None:
    """把可读 Markdown 写回 Tenshi.json 明文（可读缩进 JSON，与运行时格式一致）。"""
    root = _project_root()
    json_path = _note_json_path(root)

    if not os.path.exists(temp_md_path):
        raise FileNotFoundError(temp_md_path)
    with open(temp_md_path, "r", encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]

    blocks: List[List[str]] = []
    cur: List[str] = []
    for line in lines:
        if line.strip() == _MD_SEPARATOR:
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    entries: List[Dict] = []
    for b in blocks:
        if not b:
            continue

        day = (b[0] or "").strip()
        body = "\n".join(_unescape_md_body_line(l) for l in b[2:]).strip()
        if not day or not body:
            continue

        m = re.match(r"^(\d{4}-\d{2}-\d{2})", day)
        if not m:
            continue
        entries.append({"id": m.group(1), "text": body})


    entries.sort(key=lambda e: e["id"], reverse=True)

    payload = json.dumps(entries, ensure_ascii=False, indent=2)
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(payload)
    print(f"已将 {len(entries)} 条记录从 {temp_md_path} 写入：{json_path}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="在明文 Tenshi.json 与可读 Markdown 之间互转 Tenshi 的私人碎碎念。"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="将 Tenshi.json 导出为可读 markdown 文件。")
    p_exp.add_argument(
        "--out",
        default="Tenshi_temp.md",
        help="输出 markdown 路径（默认：仓库根目录下 Tenshi_temp.md）。",
    )

    p_imp = sub.add_parser("import", help="将 markdown 文件写回 Tenshi.json。")
    p_imp.add_argument(
        "src",
        help="包含 note blocks 的源 markdown 路径（id + 空行 + 正文 + ---）。",
    )

    args = ap.parse_args()
    if args.cmd == "export":
        out_path = args.out
        if not os.path.isabs(out_path):
            out_path = os.path.join(_project_root(), out_path)
        export_to_md(out_path)
    elif args.cmd == "import":
        src_path = args.src
        if not os.path.isabs(src_path):
            src_path = os.path.join(_project_root(), src_path)
        import_from_md(src_path)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
