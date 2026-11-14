# SPDX-License-Identifier: AGPL-3.0-only
"""One-time AST-only data export; never executes the private source file."""

from __future__ import annotations

import ast
from pathlib import Path

from rp_core.protocol import RPCError


def export_persona(source: Path, fixed: Path | None = None) -> dict:
    text = source.read_text(encoding="utf-8-sig")
    values = {}

    def literal(node):
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal(node.left) + literal(node.right)
        return ast.literal_eval(node)

    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        values[target.id] = literal(node.value)
                    except (ValueError, KeyError, TypeError):
                        continue
    if not isinstance(values.get("PERSONA_CARD"), str):
        raise RPCError("invalid_character", "The legacy source has no literal character card.")
    result = {
        "id": values.get("ROLE_NAME_EN", "character").lower(),
        "name": values.get("ROLE_NAME_FULL", "角色"),
        "instructions": values["PERSONA_CARD"],
        "role": {
            key: value
            for key, value in values.items()
            if key.startswith("ROLE_") and isinstance(value, str)
        },
    }
    if fixed and fixed.is_file():
        weather = {}
        for node in ast.parse(fixed.read_text(encoding="utf-8-sig")).body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("WEATHER_DEFAULT_")
            ):
                name = node.targets[0].id
                expression = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
                if isinstance(expression, ast.BoolOp):
                    value = ast.literal_eval(expression.values[-1])
                    weather[name] = str(value) if name.endswith("LABEL") else float(value)
        result["weather_defaults"] = weather
    return result
