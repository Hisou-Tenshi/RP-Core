# SPDX-License-Identifier: AGPL-3.0-only




























from __future__ import annotations

import importlib
import os
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (_HERE, os.path.join(os.path.dirname(_HERE), "src")):
    if os.path.exists(os.path.join(candidate, "sdk_compat.py")) and candidate not in sys.path:
        sys.path.insert(0, candidate)

import sdk_compat

FAILURES: list[str] = []


def show_versions() -> None:
    print("=" * 72)
    print("镜像依赖版本")
    print(f"  python       : {sys.version.split()[0]}")
    for name in ("anthropic", "openai", "zai", "httpx", "httpx2", "google.genai"):
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", "?")


            extra = ""
            if name in ("anthropic", "openai", "zai"):


                http_mod = sdk_compat.sdk_http_module(module)
                if http_mod is not None:
                    extra = f"  (SDK 使用 {http_mod.__name__})"
            print(f"  {name:<13}: {version}{extra}")
        except Exception as exc:
            print(f"  {name:<13}: 不可用（{type(exc).__name__}: {exc}）")
    print("=" * 72)


def sdk_http_module(sdk_module):
    """（薄封装）与运行时同一份实现，见 src/sdk_compat.py。"""
    return sdk_compat.sdk_http_module(sdk_module)


def _is_local_rejection(exc: BaseException) -> bool:
    """区分「本地构造被拒」与「正常的网络失败」。"""
    if isinstance(exc, TypeError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return "invalid `timeout`" in text or "httpx2" in text and "timeout" in text


def check_sdk(name: str, call_create, call_stream, sdk_module) -> None:
    module = sdk_http_module(sdk_module)
    print(f"\n[{name}] SDK 使用的 http 模块：{module.__name__}")

    candidates = []
    try:
        candidates.append(
            (
                "sdk_compat 产物",
                sdk_compat.build_sdk_timeout(sdk_module, connect=5.0, read=10.0),
            )
        )
    except Exception as exc:
        FAILURES.append(f"{name}: sdk_compat.build_sdk_timeout 构造失败：{type(exc).__name__}: {exc}")
    candidates.append(("纯秒数（兜底路径）", 10.0))

    for label, timeout in candidates:
        for path, call in (("create", call_create), ("stream", call_stream)):
            try:
                call(timeout)
                print(f"  ✅ {label:<18} + {path:<6} 请求构造通过")
            except Exception as exc:
                if _is_local_rejection(exc):
                    FAILURES.append(
                        f"{name}: {label} + {path} 被 SDK 本地拒绝 → "
                        f"{type(exc).__name__}: {exc}"
                    )
                    print(f"  ❌ {label:<18} + {path:<6} 被本地拒绝：{type(exc).__name__}: {exc}")
                else:

                    print(f"  ✅ {label:<18} + {path:<6} 走到网络层（{type(exc).__name__}）")


def main() -> int:
    show_versions()

    try:
        import anthropic
    except Exception as exc:
        FAILURES.append(f"anthropic 不可导入：{type(exc).__name__}: {exc}")
        anthropic = None

    try:
        import openai
    except Exception as exc:
        FAILURES.append(f"openai 不可导入：{type(exc).__name__}: {exc}")
        openai = None

    if anthropic is not None:

        def _anthropic_client(timeout):
            return anthropic.Anthropic(
                api_key="sk-ant-selfcheck", base_url="https://selfcheck.invalid", max_retries=0, timeout=timeout
            )

        def _create(timeout):
            _anthropic_client(timeout).messages.create(
                model="claude-haiku-4-5", max_tokens=1, messages=[{"role": "user", "content": "ping"}]
            )

        def _stream(timeout):
            with _anthropic_client(timeout).messages.stream(
                model="claude-haiku-4-5", max_tokens=1, messages=[{"role": "user", "content": "ping"}]
            ) as stream:
                for _ in stream.text_stream:
                    break

        check_sdk("anthropic", _create, _stream, anthropic)

    if openai is not None:

        def _openai_client(timeout):
            return openai.OpenAI(
                api_key="sk-selfcheck", base_url="https://selfcheck.invalid/v1", max_retries=0, timeout=timeout
            )

        def _o_create(timeout):
            _openai_client(timeout).chat.completions.create(
                model="gpt-5.4", messages=[{"role": "user", "content": "ping"}]
            )

        def _o_stream(timeout):
            list(
                _openai_client(timeout).chat.completions.create(
                    model="gpt-5.4", messages=[{"role": "user", "content": "ping"}], stream=True
                )
            )

        check_sdk("openai", _o_create, _o_stream, openai)

    try:
        import zai
    except Exception as exc:
        FAILURES.append(f"zai-sdk 不可导入：{type(exc).__name__}: {exc}")
        zai = None

    if zai is not None:


        def _zai_client(timeout):
            return zai.ZhipuAiClient(
                api_key="selfcheck",
                base_url="https://selfcheck.invalid/api/paas/v4",
                max_retries=0,
                timeout=timeout,
            )

        def _z_create(timeout):
            _zai_client(timeout).chat.completions.create(
                model="glm-5.3", messages=[{"role": "user", "content": "ping"}]
            )

        def _z_stream(timeout):
            list(
                _zai_client(timeout).chat.completions.create(
                    model="glm-5.3", messages=[{"role": "user", "content": "ping"}], stream=True
                )
            )

        check_sdk("zai", _z_create, _z_stream, zai)

    print("\n" + "=" * 72)
    if FAILURES:
        print("依赖自检失败（镜像与代码的 timeout 约定不一致）：")
        for item in FAILURES:
            print(f"  - {item}")
        print("=" * 72)
        return 1
    print("依赖自检通过：SDK 接受按其自身 http 模块构造的 timeout，也接受纯秒数兜底。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
