# SPDX-License-Identifier: AGPL-3.0-only

























































from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence






MAX_TEXT_TOOL_CALLS = 5

MAX_NATIVE_TOOL_CALLS = 8

TEXT_TOOL_CALL_ID_PREFIX = "textcall_"

SYNTHETIC_TOOL_CALL_ID_PREFIX = "callfix_"


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,63}$")


_NAME_KEYS = (
    "name",
    "tool",
    "tool_name",
    "toolName",
    "function_name",
    "functionName",
    "recipient_name",
)
_ARGS_KEYS = (
    "arguments",
    "parameters",
    "input",
    "args",
    "tool_input",
    "tool_args",
    "arguments_json",
    "parameters_json",
)
_CONTAINER_KEYS = ("tool_calls", "tool_call", "function_calls", "function_call", "calls")







_OUTER_TAGS = (
    "function_calls",
    "functioncalls",
    "tool_calls",
    "toolcalls",
    "tool_call",
    "toolcall",
    "function_call",
    "functioncall",
    "tool_use",
    "tooluse",
)

_CALL_TAGS = ("invoke", "tool_use", "tooluse", "tool_call", "toolcall", "function_call", "functioncall")

_TAG = r"(?:\w+:)?(?:%s)"
_OUTER_TAG_ALT = "|".join(_OUTER_TAGS)
_CALL_TAG_ALT = "|".join(_CALL_TAGS)

_OUTER_BLOCK_RE = re.compile(
    rf"<\s*{_TAG % _OUTER_TAG_ALT}\b[^>]*>",
    re.IGNORECASE,
)
_CALL_BLOCK_RE = re.compile(
    rf"<\s*(?P<tag>{_TAG % _CALL_TAG_ALT})\b(?P<attrs>[^>]*?)>"
    rf"(?P<body>.*?)"
    rf"(?:<\s*/\s*(?P=tag)\s*>|(?=<\s*{_TAG % _CALL_TAG_ALT}\b)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CLOSED_CALL_RE = re.compile(r"<\s*/\s*(?:\w+:)?(?:%s)\s*>\s*\Z" % _CALL_TAG_ALT, re.IGNORECASE)
_PARAMETER_BLOCK_RE = re.compile(
    r"<\s*(?:\w+:)?parameter\s+name\s*=\s*(?P<q>[\"'])(?P<name>.+?)(?P=q)"
    r"(?P<attrs>[^>]*)>"
    r"(?P<value>.*?)"
    r"(?:<\s*/\s*(?:\w+:)?parameter\s*>|(?=<\s*(?:\w+:)?parameter\b)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CHILD_NAME_RE = re.compile(
    r"<\s*(?:tool_name|tool|name)\s*>(?P<name>.*?)<\s*/\s*(?:tool_name|tool|name)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_CHILD_ARGS_RE = re.compile(
    r"<\s*(?:input|parameters|arguments|tool_input|tool_args|args)\s*>(?P<body>.*?)"
    r"<\s*/\s*(?:input|parameters|arguments|tool_input|tool_args|args)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ATTR_NAME_RE = re.compile(
    r"\b(?:name|tool|tool_name|recipient)\s*=\s*(?P<q>[\"'])(?P<name>.+?)(?P=q)",
    re.IGNORECASE,
)
_ATTR_ARGS_RE = re.compile(
    r"\b(?:arguments|parameters|input|args|tool_input|arguments_json)\s*=\s*"
    r"(?P<q>[\"'])(?P<body>.*?)(?P=q)",
    re.IGNORECASE | re.DOTALL,
)


_SPECIAL_MARKER_RE = re.compile(
    r"<\|\s*/?\s*(?:tool_calls?|function_calls?)\s*\|>"
    r"|<｜\s*/?\s*(?:tool_calls?|function_calls?)\s*｜>"
    r"|\[\s*/?\s*TOOL_CALLS?\s*\]"
    r"|\[\s*/?\s*FUNCTION_CALLS?\s*\]",
    re.IGNORECASE,
)
_CLOSING_MARKER_RE = re.compile(r"<\|\s*/|\[\s*/")








_NAMESPACED_CALL_RE = re.compile(
    rf"<\s*\w+\s*:\s*(?:{_OUTER_TAG_ALT}|{_CALL_TAG_ALT}|parameter)\b",
    re.IGNORECASE,
)

_SELF_EVIDENT_JSON_FMT = frozenset({"tag-json", "marker"})


_FENCE_RE = re.compile(
    r"```[ \t]*(?P<info>[A-Za-z0-9_\-+.]*)[ \t]*\r?\n(?P<body>.*?)(?:```|\Z)",
    re.DOTALL,
)
_FENCE_TOOL_HINTS = ("tool", "function", "json", "invoke", "xml")


_STRAY_TAG_RE = re.compile(
    rf"<\s*/?\s*(?:\w+:)?(?:{_OUTER_TAG_ALT}|{_CALL_TAG_ALT}|parameter)\b[^>]*>",
    re.IGNORECASE,
)

_STRING_ATTR_RE = re.compile(r"string\s*=\s*[\"']?\s*true", re.IGNORECASE)
_NUMBER_LIKE_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\Z")
_MULTI_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_TRAILING_COMMA_RE = re.compile(r",\s*(?P<close>[}\]])")
_FUNC_CALL_TEXT_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*[:(]?\s*(?P<args>[\[{].*[\]}])\s*\)?\s*$",
    re.DOTALL,
)

_MISS = object()
_MISSING = object()







def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _unescape_xml(value: str) -> str:
    """还原 XML 实体（`&amp;` 必须最后处理，否则会把 `&amp;lt;` 二次解码）。"""
    return (
        value.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&#39;", "'")
        .replace("&amp;", "&")
    )


def normalize_tool_name(name: Any) -> str:
    """把模型写出来的各种「工具名」收敛成声明里的样子。

    覆盖：首尾空白 / 反引号引号、`functions.note_patch`（OpenAI legacy recipient）、
    `note_patch()`（多写括号）、`note-patch`、`note.patch`（分隔符漂移）。
    """
    text = _as_text(name).strip().strip("`'\"")
    if not text:
        return ""
    if text.lower().startswith("functions."):
        text = text.split(".", 1)[1].strip()
    paren = text.find("(")
    if paren > 0:
        text = text[:paren]
    text = text.strip().rstrip(":").strip()
    return re.sub(r"[\s\-.]+", "_", text)


def tool_names_from_declarations(tools: Any) -> set[str]:
    """从任意形态的工具声明里取出工具名集合。

    同时支持 OpenAI 形态（`{"type":"function","function":{"name":...}}`）、
    Claude 形态（`{"name":..., "input_schema":...}`）、以及 google-genai 的
    `Tool` / `FunctionDeclaration` 对象（`function_declarations[].name`）。
    """
    names: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            names.add(value.strip())

    for tool in tools or []:
        if isinstance(tool, dict):
            _add(tool.get("name"))
            fn = tool.get("function")
            if isinstance(fn, dict):
                _add(fn.get("name"))
            decls = tool.get("function_declarations")
            if isinstance(decls, (list, tuple)):
                for decl in decls:
                    got = getattr(decl, "name", None)
                    if got is None and isinstance(decl, dict):
                        got = decl.get("name")
                    _add(got)
            continue
        _add(getattr(tool, "name", None))
        for decl in getattr(tool, "function_declarations", None) or []:
            _add(getattr(decl, "name", None))
    return names


def _match_allowed(name: Any, allowed_names: Sequence[str] | set[str] | None) -> str | None:
    """把候选名匹配到声明里的名字；匹配不上返回 None。"""
    candidate = normalize_tool_name(name)
    if not candidate:
        return None
    if not allowed_names:
        return candidate if _TOOL_NAME_RE.match(candidate) else None
    lookup = {normalize_tool_name(n).lower(): n for n in allowed_names if _as_text(n).strip()}
    return lookup.get(candidate.lower())







def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.fullmatch(stripped)
    if match:
        return match.group("body").strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
        return stripped[3:-3].strip()
    return stripped


def _complete_truncated_json(text: str) -> str | None:
    """给被流式截断的 JSON 补齐引号与括号；补不上返回 None。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((stack[-1] == "{" and ch == "}") or (stack[-1] == "[" and ch == "]")):
                stack.pop()
            else:
                return None
    if not stack and not in_string:
        return None
    repaired = text + ('"' if in_string else "")
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


def parse_tool_arguments(raw: Any) -> tuple[dict, str | None]:
    """尽最大努力把工具参数还原成 dict。

    返回 `(args, error)`：成功时 error 为 None；失败时 args 为 `{}` 且 error 说明原因。
    **调用方不应在 error 非空时执行工具**——旧代码在那里静默用了空参数。

    依次尝试：已是 dict → `json.loads` → `ast.literal_eval`（Python 字面量/单引号）
    → 去尾逗号 → 补齐被截断的括号。
    """
    if isinstance(raw, dict):
        return raw, None
    if raw is None:
        return {}, None

    if hasattr(raw, "items") and not isinstance(raw, (str, bytes)):
        for build in (lambda: dict(raw), lambda: dict(raw.items())):
            try:
                return build(), None
            except Exception:
                continue

    text = (raw if isinstance(raw, str) else _as_text(raw)).strip()
    if not text:
        return {}, None
    if text in ("{}", "[]"):
        return {}, None

    fenced = _strip_code_fence(text)
    candidates: list[str] = [fenced] if fenced != text else []
    candidates.append(text)
    for base in list(candidates):
        if _TRAILING_COMMA_RE.search(base):
            candidates.append(_TRAILING_COMMA_RE.sub(r"\g<close>", base))
        completed = _complete_truncated_json(base)
        if completed:
            candidates.append(completed)
            if _TRAILING_COMMA_RE.search(completed):
                candidates.append(_TRAILING_COMMA_RE.sub(r"\g<close>", completed))

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except Exception:
            loaded = _MISS
        if loaded is _MISS:
            continue
        if isinstance(loaded, dict):
            return loaded, None
        if isinstance(loaded, list) and len(loaded) == 1 and isinstance(loaded[0], dict):
            return loaded[0], None

    try:
        literal = ast.literal_eval(text)
    except Exception:
        literal = None
    if isinstance(literal, dict):
        return literal, None

    preview = text[:160].replace("\n", " ")
    return {}, f"工具参数不是合法 JSON（预览：{preview}）"







@dataclass
class NativeRepair:
    """原生 tool_calls 的修复结果。"""

    calls: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    repaired: bool = False


def _raw_tool_call_to_dict(tc: Any) -> dict:
    if isinstance(tc, dict):
        return dict(tc)
    fn = getattr(tc, "function", None)
    if fn is not None:
        name = getattr(fn, "name", None)
        arguments = getattr(fn, "arguments", None)
    else:
        name = getattr(tc, "name", None)
        arguments = getattr(tc, "arguments", None)
    return {
        "id": getattr(tc, "id", None),
        "type": getattr(tc, "type", "function") or "function",
        "function": {"name": name, "arguments": arguments},
    }


def repair_native_tool_calls(
    raw_tool_calls: Any,
    *,
    allowed_names: Sequence[str] | set[str] | None = None,
    max_calls: int = MAX_NATIVE_TOOL_CALLS,
) -> NativeRepair:
    """规范化 + 修复上游返回的结构化 tool_calls。

    与旧 `llm_compat.normalize_tool_calls` 的关键差别：**不再因为缺 id/name 就整条丢弃**
    （那会让一次工具调用凭空消失，且与「模型选择了不调用工具」无法区分）。

    * 缺 `id` → 补 `callfix_<n>`；缺 `name` → 跳过并记录；
    * 名字归一化（大小写 / `functions.` 前缀 / 多余括号 / 分隔符漂移）；
    * 参数宽松解析；解析失败**不执行**，把错误文本交给模型重试；
    * 同一轮里完全相同的 (name, args) 去重；
    * 超出 `max_calls` 的调用丢弃并记录。
    """
    result = NativeRepair()
    if not raw_tool_calls or isinstance(raw_tool_calls, (str, bytes)):
        return result

    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(list(raw_tool_calls)):
        try:
            item = _raw_tool_call_to_dict(raw)
        except Exception as exc:
            result.problems.append(f"第 {index + 1} 条 tool_call 无法读取：{exc}")
            result.repaired = True
            continue

        fn = item.get("function")
        fn = dict(fn) if isinstance(fn, dict) else {}
        raw_name = fn.get("name") or item.get("name")
        raw_args = fn.get("arguments")
        if raw_args is None:
            raw_args = item.get("arguments")

        call_id = _as_text(item.get("id")).strip()
        if not call_id:
            call_id = f"{SYNTHETIC_TOOL_CALL_ID_PREFIX}{index + 1}"
            result.problems.append(f"第 {index + 1} 条 tool_call 缺 id，已补 {call_id}")
            result.repaired = True

        name = normalize_tool_name(raw_name)
        if not name:
            result.problems.append(f"第 {index + 1} 条 tool_call 缺工具名，已跳过")
            result.repaired = True
            continue
        matched = _match_allowed(name, allowed_names)
        if matched is not None:
            if matched != name:
                result.repaired = True
            name = matched
        elif allowed_names:

            result.problems.append(f"工具名 {name!r} 不在本次声明里，交给工具层报错")
            result.repaired = True

        args, args_error = parse_tool_arguments(raw_args)
        if args_error:
            result.problems.append(f"{name} 参数解析失败：{args_error}")
            result.repaired = True

        key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))
        if key in seen:
            result.problems.append(f"{name} 同一轮重复调用已去重")
            result.repaired = True
            continue
        seen.add(key)

        if len(result.calls) >= max(1, int(max_calls)):
            result.problems.append(f"超过本轮调用上限 {max_calls}，丢弃 {name}")
            result.repaired = True
            continue

        result.calls.append(
            {
                "id": call_id,
                "type": item.get("type") or "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                "_args_error": args_error,
            }
        )

    return result







@dataclass
class _Region:
    """一段可以被安全删除的 markup 区域。"""

    start: int
    end: int
    body_start: int
    body_end: int
    fmt: str
    kind: str
    attrs: str = ""
    note: str | None = None
    allowed_names: Sequence[str] | set[str] | None = None

    def span(self) -> tuple[int, int]:
        return self.start, self.end

    def overlaps(self, other: tuple[int, int]) -> bool:
        return self.start < other[1] and other[0] < self.end


@dataclass
class _Parsed:
    """一个区域内解析出来的候选调用。"""

    name: str | None
    args: dict[str, Any] = field(default_factory=dict)
    args_error: str | None = None
    note: str | None = None


def _balanced_json_span(text: str, start: int) -> tuple[int, int] | None:
    """从 `start` 起（允许前面有空白）找出配平 JSON 片段的范围。"""
    idx = start
    while idx < len(text) and text[idx] in " \t\r\n":
        idx += 1
    if idx >= len(text) or text[idx] not in "{[":
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for pos in range(idx, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            opener = stack.pop()
            if (opener == "{" and ch != "}") or (opener == "[" and ch != "]"):
                return None
            if not stack:
                return idx, pos + 1
    return None


def _iter_markup_regions(
    text: str, *, allowed_names: Sequence[str] | set[str] | None = None
) -> Iterator[_Region]:
    """扫描出所有可安全删除的 markup 区域。

    关键约束：**只删有明确边界的片段**。未闭合的 `<invoke>` / 未闭合的围栏只吃到
    「最后一个可识别内层 markup」为止，绝不把后面的正文一起吞掉。
    """

    for match in _CALL_BLOCK_RE.finditer(text):
        tag = match.group("tag").split(":")[-1].lower()
        body_start = match.start("body")
        body = match.group("body") or ""
        body_end = body_start + len(body)
        closed = bool(_CLOSED_CALL_RE.search(match.group(0)))
        attrs = match.group("attrs") or ""

        if closed:
            yield _Region(
                match.start(), match.end(), body_start, body_end,
                f"xml:{tag}", "xml", attrs=attrs, allowed_names=allowed_names,
            )
            continue


        end = body_start
        for param in _PARAMETER_BLOCK_RE.finditer(body):
            end = max(end, body_start + param.end())
        balanced = _balanced_json_span(text, body_start)
        if balanced and balanced[1] <= match.end():
            end = max(end, balanced[1])
        yield _Region(
            match.start(), max(end, body_start), body_start, max(end, body_start),
            f"xml:{tag}", "xml", attrs=attrs, allowed_names=allowed_names,
        )




    if allowed_names:
        for match in _FENCE_RE.finditer(text):
            info = (match.group("info") or "").strip().lower()
            if not any(hint in info for hint in _FENCE_TOOL_HINTS):
                continue
            raw = match.group(0)
            body_start, body_end = match.start("body"), match.end("body")
            if raw.rstrip().endswith("```"):
                yield _Region(
                    match.start(), match.end(), body_start, body_end, "fence", "json",
                    allowed_names=allowed_names,
                )
                continue

            balanced = _balanced_json_span(text, body_start)
            if balanced:
                yield _Region(
                    balanced[0], balanced[1], balanced[0], balanced[1], "fence", "json",
                    allowed_names=allowed_names,
                )


    for match in _SPECIAL_MARKER_RE.finditer(text):
        if _CLOSING_MARKER_RE.match(match.group(0)):
            continue
        balanced = _balanced_json_span(text, match.end())
        if balanced:
            yield _Region(
                match.start(), balanced[1], balanced[0], balanced[1], "marker", "json",
                allowed_names=allowed_names,
            )
        else:
            yield _Region(
                match.start(), match.end(), match.end(), match.end(), "marker", "json",
                note="标记后面没有可解析的 JSON", allowed_names=allowed_names,
            )


    for match in _OUTER_BLOCK_RE.finditer(text):
        balanced = _balanced_json_span(text, match.end())
        if balanced:
            yield _Region(
                balanced[0], balanced[1], balanced[0], balanced[1], "tag-json", "json",
                allowed_names=allowed_names,
            )


    if allowed_names:
        stripped = text.strip()
        if stripped[:1] in ("{", "[") and stripped[-1:] in ("}", "]"):
            lead = len(text) - len(text.lstrip())
            balanced = _balanced_json_span(text, lead)
            if balanced and balanced[0] == lead and balanced[1] == lead + len(stripped):
                yield _Region(
                    balanced[0], balanced[1], balanced[0], balanced[1], "bare-json", "json",
                    allowed_names=allowed_names,
                )


def _looks_like_declaration(obj: dict) -> bool:
    """排除「模型把工具声明 / JSON Schema 回显了一遍」的误判。"""
    if "input_schema" in obj or "inputSchema" in obj:
        return True
    for key in _ARGS_KEYS:
        value = obj.get(key)
        if isinstance(value, dict) and "properties" in value and "type" in value:
            return True
    return False


def _extract_calls_from_json(
    payload: Any,
    *,
    allowed_names: Sequence[str] | set[str] | None,
) -> list[tuple[str, Any]]:
    """从 JSON 结构里挖出 `(工具名, 原始参数)`；名字必须命中声明集合。"""
    found: list[tuple[str, Any]] = []

    def _visit(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, list):
            for item in node:
                _visit(item, depth + 1)
            return
        if not isinstance(node, dict) or _looks_like_declaration(node):
            return

        for key in _CONTAINER_KEYS:
            if key in node:
                _visit(node[key], depth + 1)

        fn = node.get("function")
        if isinstance(fn, dict):
            _visit(fn, depth + 1)
            return

        name = None
        for key in _NAME_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                name = value
                break
        if not name:
            return

        matched = _match_allowed(name, allowed_names)
        if matched is None:
            return

        raw_args: Any = _MISSING
        for key in _ARGS_KEYS:
            if key in node:
                raw_args = node[key]
                break
        found.append((matched, None if raw_args is _MISSING else raw_args))

    _visit(payload)
    return found


def _coerce_parameter_value(raw: str, attrs: str) -> Any:
    """还原一个 `<parameter>` 的文本值。

    * `string="true"` → 强制字符串（不做 JSON 解码）；
    * 形如 JSON（`{...}` / `[...]` / `"..."` / `true|false|null` / 数字）→ 解成对应类型
      （`operations`/`entries` 这类数组参数、`read_n`/`n` 这类数字参数的关键）；
    * 其余（`2026-09-13 09:15`、中文 note）→ 原样字符串。
    """
    text = (raw or "").strip()
    if _STRING_ATTR_RE.search(attrs or ""):
        return _unescape_xml(text)
    if not text:
        return ""
    looks_structured = (
        text[0] in "{["
        or text[0] in "\"'"
        or text in ("true", "false", "null")
        or bool(_NUMBER_LIKE_RE.match(text))
    )
    if looks_structured:
        try:
            return json.loads(text)
        except Exception:
            pass
    return _unescape_xml(text)


def _parse_parameter_children(body: str) -> dict:
    args: dict[str, Any] = {}
    for match in _PARAMETER_BLOCK_RE.finditer(body or ""):
        key = (match.group("name") or "").strip()
        if key:
            args[key] = _coerce_parameter_value(match.group("value"), match.group("attrs"))
    return args


def _parse_xml_region(region: _Region, text: str) -> list[_Parsed]:
    """解析一个 XML 调用元素。"""
    body = text[region.body_start : region.body_end]
    attrs = region.attrs

    name: Any = None
    attr_name = _ATTR_NAME_RE.search(attrs)
    if attr_name:
        name = attr_name.group("name")
    if not name:
        child_name = _CHILD_NAME_RE.search(body)
        if child_name:
            name = child_name.group("name")
    name = _as_text(name).strip() or None

    param_args = _parse_parameter_children(body)
    if param_args:
        return [_Parsed(name=name, args=param_args)]

    child_args = _CHILD_ARGS_RE.search(body)
    if child_args:
        args, err = parse_tool_arguments(child_args.group("body"))
        return [_Parsed(name=name, args=args, args_error=err)]

    attr_args = _ATTR_ARGS_RE.search(attrs)
    if attr_args:
        args, err = parse_tool_arguments(_unescape_xml(attr_args.group("body")))
        return [_Parsed(name=name, args=args, args_error=err)]

    if name is None:
        return [_Parsed(name=None, note="认得出工具调用标签，但既没有工具名也没有参数")]
    return [_Parsed(name=name, args={})]


def _parse_json_region(region: _Region, text: str) -> list[_Parsed]:
    """解析一个 JSON 区域。"""
    body = text[region.body_start : region.body_end].strip()
    if not body:
        return []

    payload: Any = _MISS
    try:
        payload = json.loads(body)
    except Exception:
        payload = _MISS

    if payload is _MISS:
        func = _FUNC_CALL_TEXT_RE.match(body)
        if func:
            matched = _match_allowed(func.group("name"), region.allowed_names)
            if matched:
                args, err = parse_tool_arguments(func.group("args"))
                return [_Parsed(name=matched, args=args, args_error=err)]
        parsed, err = parse_tool_arguments(body)
        if err or not parsed:
            return [_Parsed(name=None, note=f"{region.fmt} 区域里的内容解析不出工具调用")]
        payload = parsed

    found = _extract_calls_from_json(payload, allowed_names=region.allowed_names)
    if not found:
        return [_Parsed(name=None, note=f"{region.fmt} 区域里的 JSON 不像工具调用")]

    parsed_calls: list[_Parsed] = []
    for name, raw_args in found:
        args, err = parse_tool_arguments(raw_args)
        parsed_calls.append(_Parsed(name=name, args=args, args_error=err))
    return parsed_calls


def _parse_region(region: _Region, text: str) -> list[_Parsed]:
    if region.kind == "xml":
        return _parse_xml_region(region, text)
    if region.note:
        return [_Parsed(name=None, note=region.note)]
    return _parse_json_region(region, text)







def has_tool_call_markup(text: Any) -> bool:
    """正文里是否含工具调用标记（任意已知格式）。

    含「下划线被吞掉」的变体（`<toolcalls>` / `<functioncalls>` / `notepatch` 那种写法）——
    2026-09-15 线上泄漏的就是这种写法，老判断漏掉它会导致空壳标签留在正文里外发。
    """
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()

    if _NAMESPACED_CALL_RE.search(text):
        return True
    if "function_calls" in lowered or "functioncalls" in lowered or "<invoke" in lowered:
        return True

    if "tool_call" in lowered or "toolcall" in lowered:
        return True
    if "tool_use" in lowered or "tooluse" in lowered:
        return True
    if "function_call" in lowered or "functioncall" in lowered:
        return True
    return bool(_SPECIAL_MARKER_RE.search(text))


@dataclass
class ToolCallRecovery:
    """正文抢救结果。"""

    text: str
    tool_calls: list[dict] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    markup_found: bool = False

    @property
    def recovered(self) -> bool:
        return bool(self.tool_calls)


def recover_tool_calls(
    text: Any,
    *,
    allowed_names: Sequence[str] | set[str] | None = None,
    max_calls: int = MAX_TEXT_TOOL_CALLS,
) -> ToolCallRecovery:
    """从正文里抢救工具调用，并返回剥干净 markup 的可见正文。

    注意这里**不做** `has_tool_call_markup` 快速预判：围栏 / 裸 JSON 这类形态本身
    不带工具调用标签，只有结合 `allowed_names` 才能确认是不是调用。
    """
    original = text if isinstance(text, str) else ""
    if not original:
        return ToolCallRecovery(text=original)

    regions = list(_iter_markup_regions(original, allowed_names=allowed_names))
    if not regions:


        cleaned = strip_tool_call_markup(original)
        if cleaned == original:
            return ToolCallRecovery(text=original)
        return ToolCallRecovery(
            text=cleaned,
            problems=["认得出工具调用标记，但没有任何有边界的可执行片段，已只做剥离"],
            markup_found=True,
        )
    parsed_per_region = [(region, _parse_region(region, original)) for region in regions]




    named_regions = [
        region for region, parsed in parsed_per_region if any(item.name for item in parsed)
    ]

    def _covered_by_named(region: _Region) -> bool:
        return any(other.overlaps(region.span()) for other in named_regions)

    def _is_soft(fmt: str) -> bool:
        """「软」区域：本身只是普通 JSON，不能仅凭它认定发生了工具调用事故。"""
        return fmt in ("fence", "bare-json")

    tool_calls: list[dict] = []
    formats: list[str] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()

    for region, parsed in parsed_per_region:
        named_here = any(item.name for item in parsed)
        if named_here or not _is_soft(region.fmt):
            if region.fmt not in formats:
                formats.append(region.fmt)
        for item in parsed:
            if item.name is None:
                if _covered_by_named(region) or _is_soft(region.fmt):
                    continue
                problems.append(item.note or "认得出工具调用标记，但没能解析出可执行调用")
                continue
            key = (item.name, json.dumps(item.args, ensure_ascii=False, sort_keys=True, default=str))
            if key in seen:
                problems.append(f"{item.name} 重复出现，已去重")
                continue
            seen.add(key)
            if len(tool_calls) >= max(1, int(max_calls)):
                problems.append(f"超过本轮抢救上限 {max_calls}，丢弃 {item.name}")
                continue
            if item.args_error:
                problems.append(f"{item.name} 参数解析失败：{item.args_error}")
            tool_calls.append(
                {
                    "id": f"{TEXT_TOOL_CALL_ID_PREFIX}{len(tool_calls) + 1}",
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": json.dumps(item.args, ensure_ascii=False),
                    },
                    "_args_error": item.args_error,
                }
            )


    hard_markup = any(not _is_soft(region.fmt) for region, _ in parsed_per_region)
    if not tool_calls and not hard_markup:
        return ToolCallRecovery(text=original)

    cleaned = _strip_spans(
        original, [region.span() for region in _regions_to_strip(parsed_per_region)]
    )
    return ToolCallRecovery(
        text=cleaned,
        tool_calls=tool_calls,
        formats=formats,
        problems=problems,
        markup_found=True,
    )


def _regions_to_strip(parsed_per_region: Sequence[tuple[_Region, list[_Parsed]]]) -> list[_Region]:
    """决定哪些区域要真的从正文里删掉。

    显式工具调用 markup（XML 元素 / `[TOOL_CALLS]` 标记 / `<tool_calls>` 外壳）一律删；
    但**围栏与裸 JSON 只有真解析出调用才删**——否则模型只是正常贴了一段 JSON，
    删掉它等于篡改回复（而且会连「把工具声明回显一遍」这种无害情况也误伤）。
    """
    keep: list[_Region] = []
    for region, parsed in parsed_per_region:
        if any(item.name for item in parsed) or region.fmt not in ("fence", "bare-json"):
            keep.append(region)
    return keep


def _strip_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda span: span[0]):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    cleaned = text
    for start, end in reversed(merged):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = _STRAY_TAG_RE.sub("", cleaned)
    cleaned = _SPECIAL_MARKER_RE.sub("", cleaned)
    cleaned = _MULTI_BLANK_LINE_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def strip_tool_call_markup(
    text: Any,
    *,
    allowed_names: Sequence[str] | set[str] | None = None,
) -> str:
    """只剥离、不解析（最后防线）。

    不含 markup 的文本原样返回。默认（`allowed_names=None`）**只认显式的工具调用标记**，
    不会去动一段正常的 ```json 代码块；传上 `allowed_names` 时才会连围栏/裸 JSON 形态
    一起剥离（供 `ingest_model_tool_output` 在已确认是调用的前提下使用）。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    regions = list(_iter_markup_regions(text, allowed_names=allowed_names))
    if not regions:
        if not has_tool_call_markup(text):
            return text

        cleaned = _STRAY_TAG_RE.sub("", text)
        cleaned = _SPECIAL_MARKER_RE.sub("", cleaned)
        return _MULTI_BLANK_LINE_RE.sub("\n\n", cleaned).strip()
    return _strip_spans(text, [region.span() for region in regions])


def sanitize_model_text(
    text: Any, *, allowed_names: Sequence[str] | set[str] | None = None
) -> str:
    """**绝不外发**的最后防线：不执行，只剥离所有已知工具调用标记。

    换气标记 `<br>` 不在这里清:它要活着传到流式收尾的切分器
    (显示/落盘的清理分别在 reply_streaming 与落盘锚点完成)。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    if not allowed_names and not has_tool_call_markup(text):
        return text
    return strip_tool_call_markup(text, allowed_names=allowed_names)


def extract_text_tool_calls(text: Any) -> tuple[str, list[dict]]:
    """兼容旧接口：`(cleaned_text, tool_calls)`。"""
    result = recover_tool_calls(text)
    return result.text, result.tool_calls







@dataclass
class ModelToolOutput:
    """一次模型输出的「已经收拾干净」的结论。"""

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    from_text: bool = False
    upstream_message_unusable: bool = False
    formats: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    markup_found: bool = False

    @property
    def has_calls(self) -> bool:
        return bool(self.tool_calls)


def ingest_model_tool_output(
    *,
    provider: str,
    content: Any,
    native_tool_calls: Any = None,
    allowed_names: Sequence[str] | set[str] | None = None,
    max_native_calls: int = MAX_NATIVE_TOOL_CALLS,
    max_text_calls: int = MAX_TEXT_TOOL_CALLS,
) -> ModelToolOutput:
    """把一次模型输出统一收拾成：干净正文 + 待执行调用 + 问题清单。

    这是各个 provider 循环唯一需要调用的入口。无论上游是「结构化 tool_calls」、
    「正文里塞 XML」还是「两者都有」，都能得到同一形状的结论。
    """
    raw_text = _as_text(content)
    out = ModelToolOutput(text=raw_text)

    repair = repair_native_tool_calls(
        native_tool_calls, allowed_names=allowed_names, max_calls=max_native_calls
    )
    out.problems.extend(repair.problems)

    if repair.calls:
        out.tool_calls = repair.calls
        out.text = sanitize_model_text(raw_text, allowed_names=allowed_names)
        out.markup_found = out.text != raw_text
        out.upstream_message_unusable = repair.repaired or out.markup_found
        if out.markup_found:
            out.problems.append("模型同时返回了结构化 tool_calls 和正文标记，已剥离正文标记")
        return out

    recovery = recover_tool_calls(
        raw_text, allowed_names=allowed_names, max_calls=max_text_calls
    )
    out.text = recovery.text
    out.markup_found = recovery.markup_found
    out.formats = recovery.formats
    out.problems.extend(recovery.problems)
    if not recovery.markup_found:
        out.upstream_message_unusable = repair.repaired
        return out

    out.upstream_message_unusable = True
    if recovery.tool_calls:
        out.tool_calls = recovery.tool_calls
        out.from_text = True
    else:
        out.problems.append(f"[{provider}] 正文只有工具调用标记、没能抢救出可执行调用，已剥离")
    return out


def clean_tool_calls_for_message(tool_calls: Sequence[dict]) -> list[dict]:
    """去掉内部记账字段（`_args_error` 等），得到可以直接回灌给上游的 tool_calls。"""
    cleaned: list[dict] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        fn = fn if isinstance(fn, dict) else {}
        cleaned.append(
            {
                "id": _as_text(call.get("id")),
                "type": _as_text(call.get("type")) or "function",
                "function": {
                    "name": _as_text(fn.get("name")),
                    "arguments": _as_text(fn.get("arguments")) or "{}",
                },
            }
        )
    return cleaned


def format_recovered_tool_results_notice(results: Sequence[tuple[str, str]]) -> str:
    """把抢救执行出来的结果写成一段可回灌给模型的通知（用于 Gemini / Claude）。

    `results` 是 `(tool_name, tool_result_text)` 列表。
    """
    lines = [
        "[SYSTEM NOTICE] 你刚才没有用结构化 tool_calls 调用工具，而是把调用写成了正文。",
        "系统已按你的意图代为执行，结果如下——**不要再重复调用**：",
        "",
    ]
    for name, output in results:
        lines.append(f"--- {name}")
        lines.append(_as_text(output))
        lines.append("")
    lines.append("请基于以上结果继续输出最终回复。不要再输出任何工具调用标记或调用格式。")
    return "\n".join(lines)
