# SPDX-License-Identifier: AGPL-3.0-only



















from __future__ import annotations

from typing import Any












_HTTP_MODULE_ATTRS = (
    ("_base_client", "httpx"),
    ("_base_client", "httpx2"),
    ("_client", "httpx"),
    ("_client", "httpx2"),
)


def sdk_http_module(sdk_module: Any):
    """取 SDK **自己使用**的 http 客户端模块；全都取不到才按「新版优先」猜。"""
    for holder_attr, module_attr in _HTTP_MODULE_ATTRS:
        holder = getattr(sdk_module, holder_attr, None)
        http_module = getattr(holder, module_attr, None)
        if http_module is not None and hasattr(http_module, "Timeout"):
            return http_module
    try:
        import httpx2

        return httpx2
    except Exception:
        import httpx

        return httpx


def build_sdk_timeout(sdk_module: Any, *, connect: float, read: float):
    """按 SDK 自己的 http 模块构造 timeout；拿不到就退回**秒数**（两代 SDK 都接受）。"""
    connect_sec = float(connect)
    read_sec = float(read)
    module = sdk_http_module(sdk_module)
    try:
        return module.Timeout(connect=connect_sec, read=read_sec, write=read_sec, pool=read_sec)
    except Exception:
        return max(connect_sec, read_sec)
