# SPDX-License-Identifier: AGPL-3.0-only


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fetch_tools import FETCH_MAX_CALLS_PER_TURN, fetch_url_tool
from tenshi_skills import read_skill


from config.prompts import (
    SEARCH_MEMORY_NO_RESULTS_NOTICE,
    SEARCH_MEMORY_TRUNCATION_NOTE,
    TOOL_ERROR_TOOL_NOT_FOUND_TEMPLATE,
    TOOL_NOTICE_FETCH_URL_LIMIT_TEMPLATE,
    TOOL_NOTICE_READ_CONFIG_LIMIT_TEMPLATE,
    TOOL_NOTICE_SEARCH_WEB_LIMIT_TEMPLATE,
)

MAX_WEB_SEARCH_CALLS = 2
MAX_READ_CONFIG_CALLS = 3
MAX_FETCH_CALLS = FETCH_MAX_CALLS_PER_TURN
MAX_TOOL_CALLS = 5


@dataclass
class ToolLoopState:
    read_config_count: int = 0
    web_search_count: int = 0
    fetch_count: int = 0
    tool_calls_count: int = 0
    note_write_ids_collector: set | None = None


def execute_tenshi_tool(
    name: str,
    args: dict[str, Any] | None,
    state: ToolLoopState,
    *,
    search_memory_fn: Callable,
    search_web_fn: Callable,
    read_config_file_fn: Callable,
    read_note_fn: Callable,
    note_patch_fn: Callable,
    schedule_perception_fn: Callable,
    extract_note_ids_fn: Callable | None = None,
) -> str:
    """Dispatch a single tool call. Raises no exceptions — returns error strings."""
    args = args if isinstance(args, dict) else {}
    name = (name or "").strip()

    if name == "search_memory":
        query = args.get("query")
        result = search_memory_fn(query or "")
        if isinstance(result, str) and len(result) > 3000:
            result = result[:3000] + SEARCH_MEMORY_TRUNCATION_NOTE
        if not result:
            return SEARCH_MEMORY_NO_RESULTS_NOTICE
        return result

    if name == "search_web":
        if state.web_search_count >= MAX_WEB_SEARCH_CALLS:
            return TOOL_NOTICE_SEARCH_WEB_LIMIT_TEMPLATE.format(max_calls=MAX_WEB_SEARCH_CALLS)
        state.web_search_count += 1
        return search_web_fn(args.get("query") or "")

    if name == "fetch_url":
        if state.fetch_count >= MAX_FETCH_CALLS:
            return TOOL_NOTICE_FETCH_URL_LIMIT_TEMPLATE.format(max_calls=MAX_FETCH_CALLS)
        state.fetch_count += 1
        return fetch_url_tool(
            url=args.get("url") or "",
            format=args.get("output_format") or args.get("format") or "auto",
            max_chars=args.get("max_chars"),
        )

    if name == "read_config_file":
        if state.read_config_count >= MAX_READ_CONFIG_CALLS:
            return TOOL_NOTICE_READ_CONFIG_LIMIT_TEMPLATE.format(max_calls=MAX_READ_CONFIG_CALLS)
        state.read_config_count += 1
        return read_config_file_fn(path=args.get("path"), truncate=False)

    if name == "read_note":
        n = args.get("n") or 7
        return read_note_fn(n=n)

    if name == "note_patch":
        ops = args.get("operations") or []
        read_n = args.get("read_n") or 3
        result = note_patch_fn(operations=ops, read_n=read_n)
        if state.note_write_ids_collector is not None and extract_note_ids_fn:
            state.note_write_ids_collector.update(extract_note_ids_fn(result))
        return result

    if name == "schedule_perception":
        return schedule_perception_fn(
            note=args.get("note") or "",
            trigger_at=args.get("trigger_at") or "",
            interval_minutes=args.get("interval_minutes") or 0,
            interval_hours=args.get("interval_hours") or 0,
            entries=args.get("entries"),
        )

    if name == "read_skill":
        return read_skill(args.get("skill_id") or args.get("name") or "")

    return TOOL_ERROR_TOOL_NOT_FOUND_TEMPLATE.format(name=name)
