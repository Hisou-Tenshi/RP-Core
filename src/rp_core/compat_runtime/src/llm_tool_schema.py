# SPDX-License-Identifier: AGPL-3.0-only


























from __future__ import annotations

import inspect
import re
from typing import Any, Iterable


MAX_ERROR_PLACEHOLDER_CHARS = 300


_ERROR_PLACEHOLDER_PREFIXES = (
    "error:",
    "error -",
    "exception:",
    "[tool result:",
)


_ERROR_PLACEHOLDER_MARKERS = (
    "no api keys configured",
    "no claude clients available",
    "no api paths configured",
)





_GATEWAY_ERROR_MARKERS = (
    "上游模型未返回任何内容",
    "触发了安全策略、上游限流",
    "请重试或简化输入后再试",
)


class ToolDeclarationError(RuntimeError):
    """工具声明构建失败（属于配置/代码错误，不是运行时偶发）。"""


def is_error_placeholder_response(text: str | None) -> bool:
    """判断文本是否为「错误占位」而非真正的模型回复。

    命中条件（任一）：
      - 去掉空白后以 Error: / Exception: 等前缀开头（且整体不长，避免误伤正常长文）
      - 含 no api keys configured / no claude clients available 等窒息标记
      - 含上游网关的中文占位提示（「上游模型未返回任何内容」等，且整体不长）

    正常回复里可以出现 "error" 这个词（例如中间某句），但不会 *开头* 就是
    "Error: "，因此前缀匹配 + 长度阈值足够安全。
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False

    lowered = s.lower()
    if any(marker in lowered for marker in _ERROR_PLACEHOLDER_MARKERS):
        return True

    if len(s) <= MAX_ERROR_PLACEHOLDER_CHARS:
        if any(marker in s for marker in _GATEWAY_ERROR_MARKERS):
            return True
        if lowered.startswith(_ERROR_PLACEHOLDER_PREFIXES):
            return True

    return False



























_DETERMINISTIC_ERROR_MARKERS = (
    "failed to parse the parameter",
    "automatic function calling",
    "invalid_argument",
    "invalid argument",
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "permission_denied",
    "unauthenticated",
    "no available accounts",
    "upstream request failed",
    "unsupported tool",
    "tool declaration",
    "function declaration",
    "temperature must be",
    "max_output_tokens",
    "request contains an invalid argument",
)


_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "readtimeout",
    "connecttimeout",
    "apitimeout",
    "apiconnection",
    "connection error",
    "connection reset",
    "temporarily unavailable",
    "overloaded",
    "rate limit",
    "resource_exhausted",
    "too many requests",
    "deadline exceeded",
)


_DETERMINISTIC_STATUS_CODES = frozenset({400, 401, 403, 502, 503})


def _extract_status_code(*candidates: Any) -> int | None:
    """从异常对象/文本里尽力提取 HTTP 状态码。"""
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            if 100 <= candidate <= 599:
                return candidate
        if isinstance(candidate, str):
            m = re.search(r"(?:http[ _-]?|status[ _-]?code[\"'=: ]*|code[\"'=: ]*)([1-5]\d{2})", candidate, re.I)
            if m:
                return int(m.group(1))
            m = re.search(r"^\s*([1-5]\d{2})\s+[A-Z_]{3,}", candidate)
            if m:
                return int(m.group(1))
    return None


def should_skip_provider_on_error(exc: BaseException) -> tuple[bool, str]:
    """判断某个 provider 的错误是否「确定性」到可以直接跳过该 provider 剩余候选。

    返回 (是否跳过, 原因)。纯函数，不依赖 SDK，便于单测。

    - 本地 ValueError / TypeError（工具声明、参数解析）→ 跳过。
      这类错误对同 provider 的所有模型完全一样，重试无意义。
    - HTTP 400/401/403/502/503 → 跳过。
    - 超时 / 连接失败 / 429 / 500 / 504 / 524 → 不跳过（瞬时，值得换通路再试）。
    """
    if exc is None:
        return False, ""


    if isinstance(exc, (ValueError, TypeError)) and not isinstance(exc, UnicodeError):
        return True, f"local {type(exc).__name__}"

    text = ""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:
        text = type(exc).__name__
    lowered = text.lower()

    if any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS):
        return False, "transient"

    status = _extract_status_code(
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        text,
    )
    if status is not None and status in _DETERMINISTIC_STATUS_CODES:
        return True, f"HTTP {status}"

    for marker in _DETERMINISTIC_ERROR_MARKERS:
        if marker in lowered:
            return True, f"marker:{marker}"

    return False, ""


def assert_tool_params_annotated(
    tool_funcs: Iterable[Any],
    *,
    strict: bool = True,
) -> list[str]:
    """自检：所有工具函数的形参都必须有类型注解。

    返回违规描述列表（形如 "note_patch.operations"）。
    strict=True 时，一旦有违规立即抛 ToolDeclarationError；
    便于在模块导入期就把问题炸出来，而不是等用户轮次里全部通路失败。

    背景见模块 docstring：漏写注解会让 google-genai 的反射式声明生成整体失败。
    """
    offenders: list[str] = []
    for fn in tool_funcs or []:
        name = getattr(fn, "__name__", None) or repr(fn)
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        for pname, param in sig.parameters.items():
            if param.annotation is inspect.Parameter.empty:
                offenders.append(f"{name}.{pname}")

    if offenders and strict:
        raise ToolDeclarationError(
            "以下工具形参缺少类型注解，会让 Gemini 的自动函数声明生成整体失败："
            + ", ".join(offenders)
            + "。请补上注解（例如 operations: list），或改用显式 JSON Schema 声明。"
        )
    return offenders





_JSON_SCHEMA_TYPE_TO_GEMINI = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
    "null": "NULL",
}


_NUMERIC_CONSTRAINT_KEYS = (
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
)


def _to_gemini_type(raw_type: Any) -> str | None:
    if isinstance(raw_type, str):
        return _JSON_SCHEMA_TYPE_TO_GEMINI.get(raw_type.strip().lower())
    if isinstance(raw_type, (list, tuple)):

        for item in raw_type:
            mapped = _to_gemini_type(item)
            if mapped and mapped != "NULL":
                return mapped
    return None


def _to_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def json_schema_to_gemini_schema(schema: dict | None) -> dict:
    """把 Claude 风格的 input_schema（JSON Schema 子集）转成 Gemini Schema dict。

    保持纯字典形态：genai SDK 的 `Tool.model_validate` 会自行归一到 types.Schema，
    这样本函数无需 import SDK，可离线单测。
    """
    if not isinstance(schema, dict):
        return {"type": "OBJECT", "properties": {}}

    out: dict[str, Any] = {}

    mapped_type = _to_gemini_type(schema.get("type"))
    out["type"] = mapped_type or "OBJECT"

    desc = schema.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc

    if schema.get("nullable") is True:
        out["nullable"] = True

    enum_values = schema.get("enum")
    if isinstance(enum_values, (list, tuple)) and enum_values:
        out["enum"] = [v if isinstance(v, str) else str(v) for v in enum_values]

    for key in _NUMERIC_CONSTRAINT_KEYS:
        if key in schema:
            converted = _to_number(schema.get(key))
            if converted is not None:
                out[key] = converted

    props = schema.get("properties")
    if isinstance(props, dict) and props:
        out["properties"] = {
            str(name): json_schema_to_gemini_schema(sub)
            for name, sub in props.items()
            if isinstance(sub, dict)
        }

    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        clean_required = [str(r) for r in required if isinstance(r, str) and r]
        if clean_required:
            out["required"] = clean_required

    items = schema.get("items")
    if isinstance(items, dict):
        out["items"] = json_schema_to_gemini_schema(items)

    return out


def build_gemini_tool_declarations(claude_tools: Iterable[dict]) -> dict:
    """把 Claude 风格工具定义转成单个 Gemini Tool dict。

    返回 {"function_declarations": [{"name", "description", "parameters"}, ...]}，
    可直接交给 `types.Tool.model_validate(...)` 或 `GenerateContentConfig(tools=[...])`。
    这样 Gemini 侧的 schema 与 Claude/OpenAI 侧同源，不再各自漂移。
    """
    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()

    for tool in claude_tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if name in seen:
            continue
        seen.add(name)

        decl: dict[str, Any] = {
            "name": name,
            "parameters": json_schema_to_gemini_schema(tool.get("input_schema")),
        }
        description = tool.get("description")
        if isinstance(description, str) and description.strip():
            decl["description"] = description
        declarations.append(decl)

    if not declarations:
        raise ToolDeclarationError("工具声明为空：Claude 工具定义集合不可用。")

    return {"function_declarations": declarations}
