# SPDX-License-Identifier: AGPL-3.0-only
"""Reproducible, code-only compatibility import; never imports data or persona."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import tokenize
from pathlib import Path

HELPERS = (
    "tenshi_git_sync.py",
    "weekly_archive_tenshi_log.py",
    "probe_provider_paths.py",
    "build_provider_health_dashboard.py",
    "clean_tenshi_log.py",
    "deps_selfcheck.py",
    "sticker_catalog_build.py",
    "tenshi_notes_crypto.py",
)


def constants(source):
    values = {}

    def evaluate(node):
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return evaluate(node.left) + evaluate(node.right)
        return ast.literal_eval(node)

    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = evaluate(node.value)
    return values


def code_only(text):
    lines = text.splitlines(keepends=True)
    for node in ast.parse(text).body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for index in range(node.lineno - 1, node.end_lineno):
                lines[index] = "\n"
    text = "".join(lines)
    tokens = [
        token._replace(string="") if token.type == tokenize.COMMENT else token
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
    ]
    return "# SPDX-License-Identifier: AGPL-3.0-only\n" + tokenize.untokenize(tokens)


def normalize_code(text):
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    protected = {
        line
        for token in tokens
        if token.type == tokenize.STRING and token.start[0] != token.end[0]
        for line in range(token.start[0], token.end[0])
    }
    lines = text.splitlines(keepends=True)
    return "".join(
        line if index in protected else line.rstrip() + chr(10)
        for index, line in enumerate(lines, 1)
    )


def function_map(text):
    result = {}

    def collect(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[prefix + node.name] = ast.dump(node, include_attributes=False)
            elif isinstance(node, ast.ClassDef):
                collect(node.body, prefix + node.name + ".")

    collect(ast.parse(text).body)
    return result


def import_runtime(source: Path, target: Path):
    from rp_core.persona_export import export_persona

    weather = export_persona(source / "src/config/persona.py", source / "src/config/fixed.py").get(
        "weather_defaults", {}
    )
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").write_text("# SPDX-License-Identifier: AGPL-3.0-only\n")
    manifest = {"schema_version": 1, "basis": "Tenshi runtime; code only", "files": []}
    paths = sorted((source / "src").rglob("*.py"))
    paths = [p for p in paths if p.name != "persona.py"]
    paths += [source / "scripts" / name for name in HELPERS]
    for path in paths:
        original = path.read_text(encoding="utf-8-sig")
        relative = path.relative_to(source)
        text = normalize_code(code_only(original))
        if path.name == "rag_modules.py":
            text = text.replace("pickle.load(f)", '__import__("_rp_cache").load(f)')
        # Remove the original user's proper name from procedural instructions.
        text = __import__("re").sub(r"他=[^）) ，,]+", "他=用户", text)
        for old, generic in {
            "天子在翻项目文件": "角色在翻项目文件",
            "地上之人的行踪": "对方的行踪",
            "天子的回忆录": "我的回忆录",
            "之前和这地上之人的琐事": "之前和对方的琐事",
        }.items():
            text = text.replace(old, generic)
        if relative.as_posix() == "src/config/env_boot.py":
            start = text.index("from dotenv import load_dotenv")
            end = text.index("_claude_key =", start)
            text = text[:start] + 'PROJECT_ROOT = os.environ["RP_RUNTIME_ROOT"]\n\n' + text[end:]
        elif path.name in ("tenshi_reply.py", "log_update.py"):
            if path.name == "tenshi_reply.py":
                text = text.replace("os.walk(base_dir)", "os.walk(base_dir, followlinks=True)")
            text = text.replace(
                "load_dotenv()", "None  # Environment is supplied explicitly by the public host."
            )
        if path.name == "tenshi_memory_manager.py":
            text = text.replace("tokenize='porter' ", "tokenize='porter'")
            text = text.replace(
                "data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')",
                'data_dir = os.path.join(os.environ["RP_RUNTIME_ROOT"], "data")',
            )
        if path.name == "stickers.py":
            text = text.replace(
                'LIBRARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "stickers.json")',
                'LIBRARY_PATH = os.path.join(os.environ["RP_RUNTIME_ROOT"], "data", "stickers.json")',
            )
        if path.name == "tenshi_skills.py":
            text = text.replace(
                'os.path.join(os.path.dirname(__file__), "..", "skills")',
                'os.path.join(os.environ["RP_RUNTIME_ROOT"], "skills")',
            )
        if path.name == "fixed.py":
            tree = ast.parse(text)
            lines = text.splitlines(keepends=True)
            replacements = {}
            for node in tree.body:
                if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
                    continue
                name = node.targets[0].id
                if name in {
                    "WEATHER_DEFAULT_LAT",
                    "WEATHER_DEFAULT_LON",
                    "WEATHER_DEFAULT_PLACE_LABEL",
                }:
                    replacements[node.lineno - 1] = (
                        node.end_lineno,
                        name + ' = __import__("_rp_role").weather_default(' + repr(name) + ")\n",
                    )
                if name == "MASTER_KEY_SOURCE":
                    replacements[node.lineno - 1] = (
                        node.end_lineno,
                        'MASTER_KEY_SOURCE = os.getenv("MASTER_KEY_SOURCE") or ""\n',
                    )
            for start, (end, replacement) in sorted(replacements.items(), reverse=True):
                lines[start:end] = [replacement]
            text = "".join(lines)
        if path.name == "log_update.py":
            for name in ("commit_memory_changes", "push_knowledge_base_and_check_success"):
                tree = ast.parse(text)
                node = next(
                    n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name
                )
                point = (
                    node.body[1].lineno - 1
                    if isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    else node.body[0].lineno - 1
                )
                guard = '    if os.getenv("RP_EXTERNAL_PERSISTENCE") == "1":\n'
                guard += (
                    "        return True\n"
                    if name == "commit_memory_changes"
                    else '        import hashlib\n        from pathlib import Path\n        _p = Path(tenshi_config.KB_FILE_PATH)\n        return _p.is_file() and hashlib.sha256(_p.read_bytes()).hexdigest() != os.getenv("RP_KB_BASE_SHA", "")\n'
                )
                lines = text.splitlines(keepends=True)
                lines.insert(point, guard)
                text = "".join(lines)
        if path.name in ("probe_provider_paths.py", "build_provider_health_dashboard.py"):
            text = text.replace(
                "pathlib.Path(__file__).resolve().parent.parent",
                'pathlib.Path(__import__("os").environ["RP_RUNTIME_ROOT"])',
            )
        if path.name == "tenshi_notes_crypto.py":
            text = text.replace(
                "return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
                'return os.environ["RP_RUNTIME_ROOT"]',
            )
        if path.name == "perception_layer.py":
            text = text.replace(
                "if not _called_from_perception_workflow():",
                'if not (os.getenv("RP_ACTIVE_TASK", "").endswith("perception") or _called_from_perception_workflow()):',
            )
            text = text.replace(
                'current_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))',
                "current_repo_root = tenshi_config.PROJECT_ROOT",
            )
            text = text.replace(
                'for repo_name in ("Tenshi", "Elaina"):',
                "for repo_name in (os.path.basename(current_repo_root),):",
            )
            text = text.replace(
                "cwd=tenshi_config.PROJECT_ROOT,",
                'cwd=os.getenv("RP_CODE_REPO_ROOT") or tenshi_config.PROJECT_ROOT,',
                1,
            )
        if path.name == "tenshi_git_sync.py":
            text = __import__("re").sub(
                r"DEFAULT_GIT_NAME = .*",
                'DEFAULT_GIT_NAME = os.getenv("RP_ROLE_GIT_NAME", "RP character")',
                text,
            )
            text = __import__("re").sub(
                r"DEFAULT_GIT_EMAIL = .*",
                'DEFAULT_GIT_EMAIL = os.getenv("RP_ROLE_GIT_EMAIL", "rp-character@example.invalid")',
                text,
            )
        if relative.as_posix() in ("src/config/fixed.py", "src/config/prompts.py"):
            text += """
from _rp_role import role_text as _role_text
for _k, _v in list(globals().items()):
    if isinstance(_v, str) and any(_s in _k for _s in ("PROMPT", "PROTOCOL", "TEMPLATE", "CUE")):
        globals()[_k] = _role_text(_v)
"""
        if path.name in ("weather_utils.py", "user_location.py"):
            for key, value in weather.items():
                text = text.replace(
                    str(value), "未设置默认位置" if key.endswith("LABEL") else "0.0"
                )
        if path.name == "fetch_tools.py":
            text = text.replace(
                "TenshiBot/1.0 (+https://github.com/Hisou-Tenshi; GitHub-Actions)",
                "RoleplayCore/1.0",
            )
        text = normalize_code(text).rstrip() + chr(10)
        ast.parse(text)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        old, new = function_map(original), function_map(text)
        manifest["files"].append(
            {
                "path": relative.as_posix(),
                "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "published_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "functions": len(old),
                "unchanged_functions": sum(new.get(k) == value for k, value in old.items()),
                "changed_functions": [name for name, body in old.items() if new.get(name) != body],
            }
        )
    persona = constants((source / "src/config/persona.py").read_text())
    kept = {
        k: v
        for k, v in persona.items()
        if not k.startswith("ROLE_")
        and k not in {"PERSONA_CARD", "SYSTEM_PROMPT_TEMPLATE", "CHAT_SCORE_BOT_LABEL"}
    }
    text = "# SPDX-License-Identifier: AGPL-3.0-only\nfrom _rp_role import role_values, role_text\nglobals().update(role_values())\n"
    for name, value in kept.items():
        text += name + " = role_text(" + repr(value) + ")\n"
    text += "SYSTEM_PROMPT_TEMPLATE = PERSONA_CARD + '\\n' + _PERSONA_TOOLS + '\\n' + _PERSONA_DYNAMIC\n"
    ast.parse(text)
    (target / "src/config/persona.py").write_text(text)
    for path in (source / "skills").glob("*.md"):
        destination = target / "skills" / path.name
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(path.read_text())
    output = target / "source-manifest.json"
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "imported_modules": len(paths),
                "functions": sum(x["functions"] for x in manifest["files"]),
                "unchanged_functions": sum(x["unchanged_functions"] for x in manifest["files"]),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path("src/rp_core/compat_runtime"))
    args = parser.parse_args()
    import_runtime(args.source.resolve(), args.target.resolve())
