# SPDX-License-Identifier: AGPL-3.0-only































from __future__ import annotations

import traceback
from typing import Iterable

MAX_CHAIN = 8


def exception_chain(exc: BaseException) -> list[BaseException]:
    """展开异常的 cause/context 链（去重、限深），第 0 个就是传入的异常本身。"""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < MAX_CHAIN and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        nxt = current.__cause__
        if nxt is None and current.__context__ is not None and not current.__suppress_context__:
            nxt = current.__context__
        current = nxt
    return chain


def _origin(exc: BaseException) -> str:
    """异常最后一次抛出的位置：`文件:行 in 函数`（拿不到就留空）。"""
    tb = exc.__traceback__
    if tb is None:
        return ""
    try:
        extracted = traceback.extract_tb(tb)
        if not extracted:
            return ""
        last = extracted[-1]
        return f"{last.filename}:{last.lineno} in {last.name}"
    except Exception:
        return ""


def describe_exception(exc: BaseException) -> str:
    """单个链节：`类型: 完整原文`（原文为空时只留类型）。"""
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def format_exception(exc: BaseException | None) -> str:
    """多行渲染整条链；每个链节都带完整原文与抛出位置。"""
    if exc is None:
        return "<无异常>"
    lines: list[str] = []
    for index, item in enumerate(exception_chain(exc)):
        prefix = "" if index == 0 else f"↳ caused by: "
        lines.append(f"{prefix}{describe_exception(item)}")
        origin = _origin(item)
        if origin:
            lines.append(f"    at {origin}")
    return "\n".join(lines)


def format_exception_inline(exc: BaseException | None) -> str:
    """单行渲染整条链（完整原文，不截断）；适合直接塞进日志 f-string。"""
    if exc is None:
        return "<无异常>"
    parts: list[str] = []
    for index, item in enumerate(exception_chain(exc)):
        label = describe_exception(item)
        origin = _origin(item)
        if origin:
            label = f"{label} (at {origin})"
        parts.append(label if index == 0 else f"caused by {label}")
    return " ← ".join(parts)


def format_errors(items: Iterable[tuple[str, BaseException | None]]) -> str:
    """把多个 (标签, 异常) 聚合成一段原文，便于「全链失败」时一次看全。"""
    rendered: list[str] = []
    for label, exc in items:
        if exc is None:
            continue
        rendered.append(f"[{label}] {format_exception_inline(exc)}")
    return " ; ".join(rendered) if rendered else "<无可用错误信息>"
