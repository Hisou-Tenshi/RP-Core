# SPDX-License-Identifier: AGPL-3.0-only

























































































from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__import__("os").environ["RP_RUNTIME_ROOT"])
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tenshi_config
from llm_routing import (
    build_claude_proxy_configs_for_model,
    build_deepseek_paths,
    build_gemini_paths,
    build_glm_paths,
    build_gpt_paths,
    build_grok_paths,
    build_magmarouter_paths,
    build_openrouter_paths,
)

ANTHROPIC_VERSION = "2023-06-01"
GEMINI_DEFAULT_BASE = "https://generativelanguage.googleapis.com"
ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
USER_AGENT = "Tenshi-provider-health/1.0"
BODY_EXCERPT_LIMIT = 600


HTTP_BODY_READ_LIMIT = 8 * 1024 * 1024

VERDICTS: dict[str, str] = {
    "ok": "✅ 可用",
    "not_configured": "➖ 未配置",
    "dns_fail": "❌ DNS 失败",
    "tcp_fail": "❌ 连接失败",
    "tls_fail": "❌ TLS 失败",
    "tls_cert_invalid": "⚠️ 证书不受信",
    "auth_denied": "❌ key 被拒",
    "blocked": "❌ 被拦截",
    "model_not_found": "❌ 模型不可用（模型级 ban）",
    "rate_limited": "⚠️ 限流",
    "quota_exhausted": "❌ 额度问题",
    "bad_request": "❌ 请求被拒",
    "upstream_error": "❌ 上游错误",
    "timeout": "❌ 超时",
    "http_error": "❌ 请求异常",
    "unknown": "❓ 未判定",
}









WARN_VERDICTS = {"rate_limited"}


def severity_of(verdict: str) -> str:
    if verdict == "ok":
        return "green"
    if verdict == "not_configured":
        return "not_configured"
    if verdict in WARN_VERDICTS:
        return "warn"
    return "red"

_REDACT: list[str] = []


def label(verdict: str) -> str:
    return VERDICTS.get(verdict, verdict)


def redact(text: str) -> str:
    out = str(text)
    for secret in _REDACT:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "<redacted>")
    return out


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.replace(microsecond=0).isoformat()


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 1)







def _target(provider: str, name: str, base_url: str | None, api_key: str | None, protocol: str, model: str) -> dict:
    return {
        "provider": provider,
        "name": name or (base_url or "?"),
        "base_url": (base_url or "").rstrip("/"),
        "api_key": (api_key or "").strip(),
        "protocol": protocol,
        "model": model,
    }


def _first_model_with(models: list, keyword: str) -> str | None:
    for name in models or []:
        if keyword in str(name).lower():
            return str(name)
    return None


def _dedupe_models(models: list[str]) -> list[str]:
    out: list[str] = []
    for m in models or []:
        if m and m not in out:
            out.append(m)
    return out







def _provider_model_ids(provider: str) -> list[str]:
    """某 provider 在 Tenshi 模型注册表里的**全部**模型（顺序 = 业务档位顺序）。"""
    groups: dict[str, tuple[str, ...]] = {
        "claude": ("CLAUDE_PRO_MODELS", "CLAUDE_FLASH_MODELS", "CLAUDE_LITE_MODELS"),
        "gemini": ("GEMINI_PRO_MODELS", "GEMINI_FLASH_MODELS", "GEMINI_LITE_MODELS"),
        "deepseek": ("DEEPSEEK_PRO_MODELS", "DEEPSEEK_FLASH_MODELS"),
        "gpt": ("GPT_MODELS",),
        "glm": ("GLM_PRO_MODELS", "GLM_FLASH_MODELS"),
        "magmarouter": ("MAGMAROUTER_MODELS",),
        "openrouter": ("OPENROUTER_MODELS",),
        "grok": ("GROK_MODELS",),
    }
    models: list[str] = []
    for name in groups.get(provider, ()):
        models.extend(str(m) for m in (getattr(tenshi_config, name, []) or []) if m)
    return _dedupe_models(models)


def _fireworks_model_id(native_model: str) -> str:
    """DeepSeek 原生模型 id → Fireworks 上的命名空间（accounts/fireworks/models/...）。"""
    spec = tenshi_config.model_spec(native_model)
    tier = getattr(spec, "tier", "") if spec is not None else ""
    low = (native_model or "").lower()
    if tier == "pro" or (not tier and "pro" in low):
        return tenshi_config.FIREWORKS_DEEPSEEK_V4_PRO_MODEL
    if tier == "flash" or (not tier and "flash" in low):
        return tenshi_config.FIREWORKS_DEEPSEEK_V4_FLASH_MODEL
    return native_model


def _path_model_id(
    native_model: str,
    *,
    openrouter: bool = False,
    fireworks: bool = False,
    vendor_prefix: str = "",
) -> str:
    """原生模型 id → 该通路上真正发送的模型名（Fireworks 命名空间 / OpenRouter slug）。"""
    if fireworks:
        return _fireworks_model_id(native_model)
    if openrouter:
        slug = tenshi_config.openrouter_fallback_slug(native_model)
        if slug:
            return slug


        return f"{vendor_prefix}{native_model}" if vendor_prefix else native_model
    return native_model


def _with_all_models(
    target: dict,
    *,
    openrouter: bool = False,
    fireworks: bool = False,
    vendor_prefix: str = "",
) -> dict:
    """给一条通路挂上「该 provider 的全部模型」以及它们在本通路的实际模型 id。

    `target["models"]`  = 原生模型 id 列表（JSON 里 models 字典的 key，dashboard 的矩阵列）
    `target["path_models"]` = 原生 id → 本通路实际发送/比对的模型名
    """
    native_models = _provider_model_ids(target["provider"])
    target["models"] = native_models
    target["path_models"] = {
        native: _path_model_id(
            native,
            openrouter=openrouter,
            fireworks=fireworks,
            vendor_prefix=vendor_prefix,
        )
        for native in native_models
    }
    return target


def _claude_probe_models(path_name: str) -> list[str]:
    """按档位挑**该档真正会服务**的模型候选来探。

    Tenshi 的分档规则（llm_routing.build_claude_proxy_configs_for_model）：
    Fable → Prime/Max；Opus/Sonnet → Base→Prime→Max。
    代理当前普遍没有 5.5，拿 opus-5-5 去探 Max/Prime 档会 404，把好代理探成
    「模型不可用」；因此优先用代理普遍支持的 haiku-4-5 与其日期快照，
    再回落到该档自己的旗舰模型。探活时逐个候选尝试，任一成功即算通路可用。
    """
    name = (path_name or "").lower()
    lite = list(getattr(tenshi_config, "CLAUDE_LITE_MODELS", []) or [])
    pro_models = list(getattr(tenshi_config, "CLAUDE_PRO_MODELS", []) or [])
    flash_models = list(getattr(tenshi_config, "CLAUDE_FLASH_MODELS", []) or [])

    proxy_friendly = [m for m in lite if "haiku-4-5" in m]
    if "max" in name or "prime" in name:
        candidates = proxy_friendly + pro_models[:1]
    elif "base" in name:
        candidates = proxy_friendly + flash_models[:1]
    else:
        candidates = proxy_friendly + lite[:1]
    out: list[str] = []
    for m in candidates:
        if m and m not in out:
            out.append(m)
    return out or [tenshi_config.claude_lite_primary()]


def collect_targets(providers: list[str]) -> list[dict]:
    """按类别收集运行时真正会走的通路。"""
    targets: list[dict] = []

    if "claude" in providers:
        proxy_confs = build_claude_proxy_configs_for_model(tenshi_config.claude_lite_primary())
        for conf in proxy_confs:
            name = conf.get("name") or "Proxy"
            candidates = _claude_probe_models(name)
            target = _target(
                "claude", name, conf.get("base_url"), conf.get("api_key"), "anthropic", candidates[0]
            )
            target["model_candidates"] = candidates
            targets.append(_with_all_models(target))


        if tenshi_config.OPENROUTER_API_KEY:
            _lite = tenshi_config.claude_lite_primary()
            or_target = _target(
                "claude",
                "OpenRouter",
                tenshi_config.OPENROUTER_BASE_URL,
                tenshi_config.OPENROUTER_API_KEY,
                "openai",
                tenshi_config.openrouter_fallback_slug(_lite) or f"anthropic/{_lite}",
            )
            targets.append(_with_all_models(or_target, openrouter=True, vendor_prefix="anthropic/"))


        configured_names = {str(c.get("name") or "") for c in proxy_confs}
        _proxy1_url = getattr(tenshi_config, "CLAUDE_PROXY_BASE_URL", None)
        if _proxy1_url:
            for _tier_name in ("Proxy 1 Base", "Proxy 1 Prime", "Proxy 1 Max"):
                if _tier_name in configured_names:
                    continue
                targets.append(
                    _with_all_models(
                        _target("claude", _tier_name, _proxy1_url, None, "anthropic", tenshi_config.claude_lite_primary())
                    )
                )
        _proxy2_url = getattr(tenshi_config, "CLAUDE_PROXY_BASE_URL_2", None)
        if _proxy2_url and getattr(tenshi_config, "CLAUDE_PROXY2", None) is None and "Proxy 2" not in configured_names:
            targets.append(
                _with_all_models(
                    _target("claude", "Proxy 2", _proxy2_url, None, "anthropic", tenshi_config.claude_lite_primary())
                )
            )
        target = _target(
            "claude",
            "Official",
            getattr(tenshi_config, "CLAUDE_OFFICIAL_BASE_URL", ANTHROPIC_DEFAULT_BASE),
            tenshi_config.CLAUDE_API_KEY,
            "anthropic",
            tenshi_config.claude_lite_primary(),
        )
        target["model_candidates"] = _dedupe_models(
            [tenshi_config.claude_lite_primary(), *list(getattr(tenshi_config, "CLAUDE_LITE_MODELS", []) or [])]
        ) or [tenshi_config.claude_lite_primary()]
        targets.append(_with_all_models(target))

    if "gemini" in providers:
        model = tenshi_config.gemini_lite_primary()



        for path in build_gemini_paths(tenshi_config.GEMINI_API_KEY, tenshi_config.GEMINI_PROXIES):
            is_openai = path.get("protocol") == "openai"
            vendor_prefix = str(path.get("vendor_prefix") or "")
            target = _target(
                "gemini",
                path.get("name") or "Proxy",
                path.get("base_url") or GEMINI_DEFAULT_BASE,
                path.get("api_key"),
                "openai" if is_openai else "gemini",
                (
                    tenshi_config.openrouter_fallback_slug(model)
                    or f"{vendor_prefix}{model}"
                )
                if is_openai
                else model,
            )
            targets.append(
                _with_all_models(
                    target, openrouter=is_openai, vendor_prefix=vendor_prefix
                )
            )

    if "deepseek" in providers:
        base_model = tenshi_config.deepseek_preprocess_primary()
        for path in build_deepseek_paths(
            tenshi_config.DEEPSEEK_API_KEY, tenshi_config.DEEPSEEK_BASE_URL, tenshi_config.DEEPSEEK_PROXIES
        ):
            base_url = (path.get("base_url") or "").rstrip("/")
            is_fireworks = "fireworks" in base_url
            is_openrouter = bool(path.get("openrouter")) or path.get("name") == "OpenRouter"

            model = base_model
            if is_fireworks:
                model = tenshi_config.FIREWORKS_DEEPSEEK_V4_FLASH_MODEL
            elif is_openrouter:
                model = tenshi_config.openrouter_fallback_slug(base_model) or base_model
            target = _target(
                "deepseek", path.get("name") or "Proxy", base_url, path.get("api_key"), "openai", model
            )
            targets.append(
                _with_all_models(target, openrouter=is_openrouter, fireworks=is_fireworks)
            )

    if "gpt" in providers:

        configured_names = {str(p.get("name") or "") for p in tenshi_config.GPT_PROXIES}
        for path in build_gpt_paths(
            tenshi_config.GPT_API_KEY,
            tenshi_config.GPT_BASE_URL,
            tenshi_config.GPT_PROXIES,
        ):
            is_openrouter = bool(path.get("openrouter")) or path.get("name") == "OpenRouter"
            model = (
                tenshi_config.openrouter_fallback_slug(tenshi_config.gpt_flash_primary())
                if is_openrouter
                else tenshi_config.gpt_flash_primary()
            )
            target = _target(
                "gpt",
                path.get("name") or "OpenAI",
                path.get("base_url") or tenshi_config.GPT_BASE_URL,
                path.get("api_key"),
                "openai",
                model or tenshi_config.gpt_flash_primary(),
            )
            targets.append(_with_all_models(target, openrouter=is_openrouter))


        for _proxy_name, _proxy_url in (
            ("GPT Proxy", getattr(tenshi_config, "GPT_PROXY_BASE_URL", None)),
            ("GPT Proxy 2", getattr(tenshi_config, "GPT_PROXY2_BASE_URL", None)),
        ):
            if _proxy_url and _proxy_name not in configured_names:
                targets.append(
                    _with_all_models(
                        _target(
                            "gpt",
                            _proxy_name,
                            _proxy_url,
                            None,
                            "openai",
                            tenshi_config.gpt_flash_primary(),
                        )
                    )
                )

    if "glm" in providers:




        for path in build_glm_paths(tenshi_config.GLM_API_KEY, tenshi_config.GLM_BASE_URL):
            is_openrouter = bool(path.get("openrouter")) or path.get("name") == "OpenRouter"
            _glm_model = tenshi_config.glm_pro_primary()
            if is_openrouter:
                _glm_model = tenshi_config.openrouter_fallback_slug(_glm_model) or _glm_model
            target = _target(
                "glm",
                path.get("name") or "Official",
                path.get("base_url") or tenshi_config.GLM_BASE_URL,
                path.get("api_key"),
                "openai",
                _glm_model,
            )
            targets.append(_with_all_models(target, openrouter=is_openrouter))

    if "magmarouter" in providers:


        for path in build_magmarouter_paths(
            tenshi_config.MAGMAROUTER_API_KEY, tenshi_config.MAGMAROUTER_BASE_URL
        ):
            target = _target(
                "magmarouter",
                path.get("name") or "MagmaRouter",
                path.get("base_url") or tenshi_config.MAGMAROUTER_BASE_URL,
                path.get("api_key"),
                "openai",
                tenshi_config.magmarouter_primary(),
            )
            targets.append(_with_all_models(target))

    if "openrouter" in providers:
        for path in build_openrouter_paths(tenshi_config.OPENROUTER_API_KEY, tenshi_config.OPENROUTER_BASE_URL):
            target = _target(
                "openrouter",
                path.get("name") or "OpenRouter",
                path.get("base_url"),
                path.get("api_key"),
                "openai",
                tenshi_config.openrouter_primary(),
            )
            targets.append(_with_all_models(target))

    if "grok" in providers:
        for path in build_grok_paths(
            tenshi_config.GROK_API_KEY,
            tenshi_config.GROK_BASE_URL,
            tenshi_config.GROK_PROXIES,
        ):
            is_openrouter = bool(path.get("openrouter")) or path.get("name") == "OpenRouter"
            model = (
                tenshi_config.openrouter_fallback_slug(tenshi_config.grok_primary())
                if is_openrouter
                else tenshi_config.grok_primary()
            )
            target = _target(
                "grok",
                path.get("name") or "Official",
                path.get("base_url"),
                path.get("api_key"),
                "openai",
                model or tenshi_config.grok_primary(),
            )
            targets.append(_with_all_models(target, openrouter=is_openrouter))


    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for item in targets:
        key = (item["provider"], item["name"], item["base_url"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique







def probe_dns(host: str, port: int) -> dict:
    start = time.perf_counter()
    out: dict = {"ok": False, "addresses": [], "error": None}
    try:
        seen: list[str] = []
        for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
            family = "IPv6" if info[0] == socket.AF_INET6 else "IPv4"
            entry = f"{info[4][0]} ({family})"
            if entry not in seen:
                seen.append(entry)
        out["addresses"] = seen
        out["ok"] = bool(seen)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["latency_ms"] = elapsed_ms(start)
    return out


def probe_tcp(host: str, port: int, timeout: float) -> dict:
    start = time.perf_counter()
    out: dict = {"ok": False, "peer": None, "error": None}
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        peer = sock.getpeername()
        out["ok"] = True
        out["peer"] = f"{peer[0]}:{peer[1]}"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    out["latency_ms"] = elapsed_ms(start)
    return out


def probe_tls(host: str, port: int, timeout: float) -> dict:
    out: dict = {"ok": False, "error": None, "version": None, "cert_cn": None, "issuer": None, "days_left": None, "cert_error": None, "unverified_ok": None}
    start = time.perf_counter()

    def _handshake(ctx: ssl.SSLContext):
        sock = None
        tls = None
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            tls = ctx.wrap_socket(sock, server_hostname=host)
            return True, None, tls
        except ssl.SSLCertVerificationError as exc:
            return False, f"证书校验失败：{exc.verify_message or exc}", None
        except ssl.SSLError as exc:
            return False, f"TLS 握手失败：{exc}", None
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}", None
        finally:
            if tls is None and sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _fill(tls) -> None:
        out["version"] = tls.version()
        cert = tls.getpeercert() or {}
        subject = {k: v for part in cert.get("subject", ()) for k, v in part}
        issuer = {k: v for part in cert.get("issuer", ()) for k, v in part}
        out["cert_cn"] = subject.get("commonName")
        out["issuer"] = issuer.get("organizationName") or issuer.get("commonName")
        not_after = cert.get("notAfter")
        if not_after:
            try:
                expiry = dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
                out["days_left"] = (expiry - now_utc()).days
            except ValueError:
                pass

    strict = ssl.create_default_context()
    ok, err, tls = _handshake(strict)
    if ok and tls is not None:
        out["ok"] = True
        try:
            _fill(tls)
        finally:
            try:
                tls.close()
            except OSError:
                pass
    else:
        out["error"] = err
        loose = ssl.create_default_context()
        loose.check_hostname = False
        loose.verify_mode = ssl.CERT_NONE
        ok_loose, _e, tls_loose = _handshake(loose)
        out["unverified_ok"] = bool(ok_loose)
        if ok_loose and tls_loose is not None:
            out["cert_error"] = err
            try:
                _fill(tls_loose)
            finally:
                try:
                    tls_loose.close()
                except OSError:
                    pass
    out["latency_ms"] = elapsed_ms(start)
    return out


def http_json(url: str, *, headers: dict, payload: dict | None = None, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    req.add_header("User-Agent", USER_AGENT)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for key, value in headers.items():
        if value:
            req.add_header(key, value)

    start = time.perf_counter()
    raw = b""
    out: dict = {"status": None, "ok": False, "latency_ms": None, "body": "", "json": None, "error": None}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:


            raw = resp.read(HTTP_BODY_READ_LIMIT)
            out["status"] = resp.status
            out["ok"] = 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        raw = exc.read(HTTP_BODY_READ_LIMIT) if hasattr(exc, "read") else b""
        out["status"] = exc.code
        out["error"] = f"HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        out["error"] = f"URLError: {exc.reason}"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["latency_ms"] = elapsed_ms(start)

    text = raw.decode("utf-8", errors="replace") if raw else ""
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            out["json"] = parsed
    if len(text) > BODY_EXCERPT_LIMIT:
        text = text[:BODY_EXCERPT_LIMIT] + "…"
    out["body"] = redact(text)
    return out


def _error_message(result: dict) -> str:
    data = result.get("json")
    if not isinstance(data, dict):
        try:
            data = json.loads(result.get("body") or "{}")
        except (ValueError, TypeError):
            return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    if isinstance(err, str):
        return err
    return ""


def count_tokens_anthropic(target: dict, timeout: float) -> dict:
    """官方 token 计数端点：校验 key + 模型 + 请求格式，**不创建消息、不产出 token**。"""
    return http_json(
        f"{target['base_url'] or ANTHROPIC_DEFAULT_BASE}/v1/messages/count_tokens",
        headers={
            "x-api-key": target["api_key"],
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": "token-counting-2024-11-01",
        },
        payload={"model": target["model"], "messages": [{"role": "user", "content": "ping"}]},
        timeout=timeout,
    )


def count_tokens_gemini(target: dict, timeout: float) -> dict:
    """官方 token 计数端点：校验 key + 模型 + 请求格式，**不产出 token**。"""
    base = target["base_url"] or GEMINI_DEFAULT_BASE
    return http_json(
        f"{base}/v1beta/models/{target['model']}:countTokens",
        headers={"x-goog-api-key": target["api_key"]},
        payload={"contents": [{"parts": [{"text": "ping"}]}]},
        timeout=timeout,
    )




COUNT_TOKEN_CALLERS = {
    "anthropic": count_tokens_anthropic,
    "gemini": count_tokens_gemini,
}


_NO_GENERATIVE_CALL_NOTE = "（只做非生成校验，不发真实调用）"










def auth_anthropic(target: dict, timeout: float) -> dict:
    return http_json(
        f"{target['base_url'] or ANTHROPIC_DEFAULT_BASE}/v1/models",
        headers={"x-api-key": target["api_key"], "anthropic-version": ANTHROPIC_VERSION},
        timeout=timeout,
    )


def auth_gemini(target: dict, timeout: float) -> dict:
    base = target["base_url"] or GEMINI_DEFAULT_BASE
    return http_json(f"{base}/v1beta/models", headers={"x-goog-api-key": target["api_key"]}, timeout=timeout)


def auth_openai_compatible(target: dict, timeout: float) -> dict:
    return http_json(
        f"{target['base_url']}/models",
        headers={"Authorization": f"Bearer {target['api_key']}"},
        timeout=timeout,
    )


AUTH_CALLERS = {
    "anthropic": auth_anthropic,
    "gemini": auth_gemini,
    "openai": auth_openai_compatible,
}


def classify_network(target: dict, layers: dict) -> tuple[str, str]:
    """第一层：网络是否通。"""
    if layers.get("dns") and not layers["dns"]["ok"]:
        return "dns_fail", f"DNS 解析失败：{layers['dns'].get('error')}"
    if layers.get("tcp") and not layers["tcp"]["ok"]:
        return "tcp_fail", f"TCP 建连失败：{layers['tcp'].get('error')}"
    if layers.get("tls") and not layers["tls"]["ok"]:
        if layers["tls"].get("unverified_ok"):
            return "tls_cert_invalid", f"TLS 可握手但证书不受信：{layers['tls'].get('cert_error')}"
        return "tls_fail", f"TLS 握手失败：{layers['tls'].get('error')}"
    return "ok", "网络可达"


def classify_auth(target: dict, result: dict | None) -> tuple[str, str]:
    """第二层：key 能不能过（只列模型，不调用）。"""
    if not target["api_key"]:
        return "not_configured", "未配置 key"
    if result is None:
        return "unknown", "未执行鉴权探测"
    status = result.get("status")
    message = _error_message(result)
    detail = f"HTTP {status}" + (f"：{message[:200]}" if message else (f"：{result.get('error')}" if result.get("error") else ""))
    if result.get("ok"):
        return "ok", f"鉴权通过（{detail}）"
    if status == 404:


        return "unknown", f"该通路未实现列模型端点（404），本次不下结论：{detail}"
    if status in (401, 403):
        low = (result.get("body") or "").lower()
        if status == 403 and any(w in low for w in ("cloudflare", "<html", "access denied", "forbidden by", "waf")):
            return "blocked", f"被代理侧 / WAF 拒绝（403）：{detail}"
        return "auth_denied", f"key 被拒（{status}）：{detail}"
    if status == 429:
        return "rate_limited", f"限流（429）：{detail}"
    if status in (402, 451):
        return "quota_exhausted", f"额度 / 风控（{status}）：{detail}"
    if status and status >= 500:
        return "upstream_error", f"上游错误（{status}）：{detail}"
    if result.get("error") and "timeout" in str(result["error"]).lower():
        return "timeout", f"请求超时：{detail}"
    if result.get("error"):
        return "http_error", f"{result['error']}：{detail}"
    return "unknown", detail












_MODEL_MISSING_MARKERS = (
    "model_not_found",
    "not found",
    "not_found",
    "not supported",
    "not_supported",
    "does not exist",
    "no such model",
    "invalid model",
    "unknown model",
    "is not found",
)


def _is_model_missing(message: str, body: str) -> bool:
    """错误是「这个模型在该通路上不可用」，而不是「端点没实现」。"""
    low = f"{message} {body}".lower()
    if "model" not in low:
        return False
    return any(marker in low for marker in _MODEL_MISSING_MARKERS)


def _extract_model_ids(payload: dict | list | str | None) -> list[str] | None:
    """从 GET /models 的响应里取模型 id；解析不出来返回 None（不下结论）。"""
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data or "")
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    ids: list[str] = []
    for key in ("data", "models"):
        raw = data.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = item.get("id") or item.get("name")
            if not value:
                continue
            value = str(value)
            if value.startswith("models/"):
                value = value[len("models/"):]
            ids.append(value)
    return ids or None


def _match_model(candidates: list[str], ids: list[str]) -> str | None:
    """在模型列表里找探测模型；支持精确匹配与日期快照别名（xxx ↔ xxx-20251001）。"""
    by_lower = {mid.lower(): mid for mid in ids}
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in ids:
            return candidate
        hit = by_lower.get(candidate.lower())
        if hit:
            return hit
    for candidate in candidates:
        if not candidate:
            continue
        prefix = candidate.lower() + "-"
        for mid in ids:
            low = mid.lower()
            if low.startswith(prefix) and low[len(prefix):].isdigit():
                return mid
    return None


def classify_model_list(target: dict, auth_result: dict | None) -> tuple[str, str]:
    """模型层判据之一：鉴权响应的模型列表里有没有探测模型（不额外发请求）。"""
    if not target["api_key"]:
        return "not_configured", "未配置 key"
    if auth_result is None:
        return "unknown", "未执行鉴权探测，无法核对模型列表"
    if not auth_result.get("ok"):
        return "unknown", "鉴权未通过，本次不核对模型列表（由鉴权层下结论）"
    ids = _extract_model_ids(
        auth_result.get("json") if auth_result.get("json") is not None else (auth_result.get("body") or "")
    )
    if ids is None:
        return "unknown", "该通路的列模型响应不可解析，本次不核对模型"
    candidates = [c for c in (target.get("model_candidates") or [target["model"]]) if c]
    hit = _match_model(candidates, ids)
    if hit:
        return "ok", f"模型在官方模型列表里（{hit}）{_NO_GENERATIVE_CALL_NOTE}"
    return "model_not_found", (
        f"模型不在该通路的模型列表里（探测 {candidates[0]}，列表 {len(ids)} 个）"
        f"{_NO_GENERATIVE_CALL_NOTE}；该「通路×模型」组合按红处理，运行时模型级 ban"
    )


def classify_count_tokens(target: dict, result: dict | None) -> tuple[str, str]:
    """模型层判据之二：官方 count tokens 端点（不创建消息、不产出 token）。"""
    if not target["api_key"]:
        return "not_configured", "未配置 key"
    if result is None:
        return "unknown", "未执行 token 计数探测"
    status = result.get("status")
    message = _error_message(result)
    body = result.get("body") or ""
    detail = f"HTTP {status}" + (f"：{message[:200]}" if message else (f"：{result.get('error')}" if result.get("error") else ""))
    if result.get("ok"):
        return "ok", f"官方 count tokens 通过{_NO_GENERATIVE_CALL_NOTE}"
    if status in (401, 403):
        low = body.lower()
        if status == 403 and any(w in low for w in ("cloudflare", "<html", "access denied", "forbidden by", "waf")):
            return "blocked", f"被代理侧 / WAF 拒绝（403）：{detail}"
        return "auth_denied", f"key 被拒（{status}）：{detail}"
    if status == 404:
        if _is_model_missing(message, body):
            return "model_not_found", f"模型不可用（404）：{detail}"
        return "unknown", f"该通路未实现 token 计数端点（404），本次不下结论：{detail}"
    if status in (405, 501):
        return "unknown", f"该通路不支持 token 计数（{status}），本次不下结论：{detail}"
    if status == 429:
        return "rate_limited", f"限流（429）：{detail}"
    if status in (402, 451):
        return "quota_exhausted", f"额度 / 风控（{status}）：{detail}"
    if status and status >= 500:
        return "upstream_error", f"上游错误（{status}）：{detail}"
    if status == 400:

        return "unknown", f"该通路不接受 token 计数请求（400），本次不下结论：{detail}"
    if result.get("error") and "timeout" in str(result["error"]).lower():
        return "timeout", f"请求超时：{detail}"
    if result.get("error"):
        return "http_error", f"{result['error']}：{detail}"
    return "unknown", detail













def _model_method_label(method: str | None) -> str:
    return {"models_list": "模型列表", "count_tokens": "count tokens", "none": "未校验"}.get(
        method or "none", str(method)
    )


def _model_entry(
    verdict: str,
    detail: str,
    *,
    method: str = "none",
    status=None,
    latency_ms=None,
) -> dict:
    """一条逐模型结论（JSON 路径级 models 字典的值；字段名固定给 dashboard 用）。"""
    check = _check(verdict, detail, latency_ms=latency_ms, status=status)
    return {
        "ok": check["ok"],
        "verdict": verdict,
        "severity": check["severity"],
        "detail": detail,
        "method": method,
        "status": status,
        "latency_ms": latency_ms,
    }


def _model_available(entry: dict | None) -> bool:
    """模型是否**真正可用**（报告里的 ✅）。

    注意 warn 档（model_not_found / rate_limited）的 `ok` 也是 True —— 那是给运行时
    gate 用的「不 ban」语义；但统计「模型覆盖」（models_ok / 矩阵表头）时必须只算
    真正 ok 的模型，否则代理只服务 haiku 时，7 个模型里 4 个 404 也会显示 7/7。
    """
    return bool(entry) and entry.get("ok") is True and entry.get("verdict") == "ok"


def _probe_model_plan(target: dict, all_models: bool) -> list[tuple[str, str]]:
    """本通路要探的模型：[(原生模型 id, 本通路实际模型名)]。

    `--no-all-models` 时退回旧口径：只探代表模型（Claude 代理仍按候选顺序逐个试，
    任一命中即算可用），用于本地调试、避免一次跑满全模型。
    """
    path_models: dict[str, str] = target.get("path_models") or {}
    if all_models:
        models = [m for m in (target.get("models") or []) if m]
        if models:
            return [(m, path_models.get(m, m)) for m in models]
        return [(target["model"], target["model"])]
    candidates = [m for m in (target.get("model_candidates") or []) if m]
    if not candidates:
        return [(target["model"], target["model"])]
    return [(m, path_models.get(m, m)) for m in candidates]


def _probe_one_model(
    target: dict,
    native_model: str,
    path_model: str,
    auth_result: dict | None,
    timeout: float,
) -> dict:
    """单模型判定（不生成、不产出 token）。

    先核对鉴权响应的模型列表；列表里没有该模型，再对该模型发官方 count tokens 复核
    （有些代理的列表不全 / 日期快照对不上，以 count tokens 的实际结论为准）。
    列表响应不可解析 → count tokens；count tokens 端点也没实现 → unknown（fail-open）。
    """
    probe_target_dict = dict(target)
    probe_target_dict["model"] = path_model
    probe_target_dict["model_candidates"] = [path_model]

    verdict, detail = classify_model_list(probe_target_dict, auth_result)
    method = "models_list"
    result: dict | None = None
    status = (auth_result or {}).get("status")
    latency_ms = (auth_result or {}).get("latency_ms")

    count_caller = COUNT_TOKEN_CALLERS.get(target.get("protocol"))
    if count_caller is not None and verdict in ("unknown", "model_not_found"):
        result = count_caller(probe_target_dict, timeout)
        count_verdict, count_detail = classify_count_tokens(probe_target_dict, result)
        method = "count_tokens"
        status = result.get("status")
        latency_ms = result.get("latency_ms")


        if count_verdict != "unknown":
            verdict, detail = count_verdict, count_detail
        elif verdict == "unknown":
            detail = count_detail

    return _model_entry(verdict, detail, method=method, status=status, latency_ms=latency_ms)


def probe_models(
    target: dict,
    auth_result: dict | None,
    *,
    timeout: float,
    all_models: bool = True,
) -> dict[str, dict]:
    """逐模型判定，返回 {原生模型 id: 逐模型结论}。"""
    checks: dict[str, dict] = {}
    for native_model, path_model in _probe_model_plan(target, all_models):
        checks[native_model] = _probe_one_model(target, native_model, path_model, auth_result, timeout)

        if not all_models and checks[native_model].get("ok") is True:
            break
    return checks


def _mark_models(
    target: dict,
    verdict: str,
    detail: str,
    *,
    all_models: bool = True,
) -> dict[str, dict]:
    """没到发请求阶段就整条通路同一个结论（未配置 key / 网络不通 / 非 HTTPS）。"""
    checks: dict[str, dict] = {}
    for native_model, _path_model in _probe_model_plan(target, all_models):
        checks[native_model] = _model_entry(verdict, detail, method="none")
    return checks


def _aggregate_models(checks: dict[str, dict]) -> tuple[str, str]:
    """逐模型结论 → 通路级模型层结论。

    聚合规则（与 task 约定一致）：任一模型 ok → ok；无 ok 且有 warn → warn；
    全 unknown → unknown；全红 → 红。not_configured 只有「全部未配置」时才保留。
    """
    entries = list((checks or {}).values())
    if not entries:
        return "unknown", "没有可探测的模型"
    total = len(entries)
    if all(e.get("verdict") == "not_configured" for e in entries):
        return "not_configured", "未配置 key"

    oks = [e for e in entries if e.get("ok") is True and e.get("verdict") not in WARN_VERDICTS]
    if oks:
        reds = [e for e in entries if e.get("ok") is False]
        banned_note = (
            f"，{len(reds)} 个模型在该通路 ban（model_not_found，运行时按「通路×模型」跳过）"
            if reds
            else ""
        )
        return "ok", (
            f"{len(oks)}/{total} 个模型可用"
            f"（{_model_method_label(oks[0].get('method'))}，只做非生成校验）{banned_note}"
        )
    warns = [e for e in entries if e.get("verdict") in WARN_VERDICTS]
    if warns:
        return warns[0]["verdict"], (
            f"没有模型通过校验（{len(warns)}/{total} 个模型未通过）：{warns[0]['detail'][:160]}"
        )
    reds = [e for e in entries if e.get("ok") is False]
    if reds:
        return reds[0]["verdict"], f"{len(reds)}/{total} 个模型不可用：{reds[0]['detail'][:160]}"
    return "unknown", f"{total} 个模型均未下结论：{entries[0]['detail'][:160]}"


def _aggregate_model_method(checks: dict[str, dict]) -> str:
    """通路级 model_check_method：取真正产出结论的那个 method。"""
    entries = list((checks or {}).values())
    if not entries:
        return "none"
    groups = (
        [e for e in entries if e.get("ok") is True and e.get("verdict") not in WARN_VERDICTS],
        [e for e in entries if e.get("verdict") in WARN_VERDICTS],
        [e for e in entries if e.get("ok") is False],
        entries,
    )
    for group in groups:
        for entry in group:
            if entry.get("method") in ("models_list", "count_tokens"):
                return entry["method"]
    return "none"


def _check_cell(check: dict | None) -> str:
    """检查结果的展示符号：绿 ✅ / 黄 ⚠️（可用但不理想）/ 红 ❌ / 没结论 —。"""
    c = check or {}
    if c.get("verdict") is None or c.get("ok") is None:
        return "—"
    if c.get("ok"):
        return "⚠️" if c.get("severity") == "warn" else "✅"
    return "❌"


def _check(verdict: str | None, detail: str, *, latency_ms=None, status=None, extra: dict | None = None) -> dict:
    """一条检查记录。ok=None 表示「没下结论」——不参与外层 ban（fail-open）。

    注意 `unknown` 也算「没结论」：例如某些代理不实现「列模型」/ count tokens 端点，
    这不能说明通路坏（真实调用可能完全正常），不能因此 ban 掉。
    """
    if verdict is None or verdict in ("unknown", "not_configured"):
        ok: bool | None = None
        severity = "unknown"
    elif verdict == "ok":
        ok = True
        severity = "green"
    elif verdict in WARN_VERDICTS:

        ok = True
        severity = "warn"
    else:
        ok = False
        severity = "red"
    out = {
        "ok": ok,
        "verdict": verdict,
        "verdict_text": detail,
        "severity": severity,
        "latency_ms": latency_ms,
        "status": status,
    }
    if extra:
        out.update(extra)
    return out


def probe_target(target: dict, timeout: float, *, all_models: bool = True) -> dict:
    """一条通路跑三层检查；三层全部走官方「非生成」端点，不发真实生成请求。

    `all_models=True`（默认）对该 provider 的全部模型逐一判定；`False` 是调试口径，
    只探代表模型（行为与旧版一致）。
    """
    parsed = urllib.parse.urlsplit(target["base_url"] or "")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme != "http" else 80)
    layers: dict = {"dns": None, "tcp": None, "tls": None}
    auth_result: dict | None = None
    now = iso(now_utc())

    if host:
        layers["dns"] = probe_dns(host, port)
        if layers["dns"]["ok"]:
            layers["tcp"] = probe_tcp(host, port, timeout)
        if layers["tcp"] and layers["tcp"]["ok"]:
            layers["tls"] = probe_tls(host, port, timeout)

    network_ok = bool(host and layers["dns"] and layers["dns"]["ok"] and layers["tcp"] and layers["tcp"]["ok"])
    https_ok = parsed.scheme == "https"

    if network_ok and target["api_key"] and https_ok:
        auth_result = AUTH_CALLERS[target["protocol"]](target, timeout)






    if not target["api_key"]:
        model_checks = _mark_models(target, "not_configured", "未配置 key", all_models=all_models)
    elif not network_ok:
        model_checks = _mark_models(target, "unknown", "网络不可达，未做模型校验", all_models=all_models)
    elif not https_ok:
        model_checks = _mark_models(target, "unknown", "非 HTTPS 通路，跳过模型校验", all_models=all_models)
    else:
        model_checks = probe_models(target, auth_result, timeout=timeout, all_models=all_models)

    mv, md = _aggregate_models(model_checks)
    model_method = _aggregate_model_method(model_checks)

    nv, nd = classify_network(target, layers)
    av, ad = classify_auth(target, auth_result)

    model_latencies = [
        entry.get("latency_ms") for entry in model_checks.values() if entry.get("latency_ms") is not None
    ]
    model_status = next(
        (entry.get("status") for entry in model_checks.values() if entry.get("status") is not None),
        (auth_result or {}).get("status"),
    )
    checks = {
        "network": _check(
            nv,
            nd,
            latency_ms=(layers["tls"] or {}).get("latency_ms") or (layers["tcp"] or {}).get("latency_ms"),
        ),
        "auth": _check(
            av,
            ad,
            latency_ms=auth_result.get("latency_ms") if auth_result else None,
            status=auth_result.get("status") if auth_result else None,
        ),
        "model": _check(
            mv,
            md,
            latency_ms=min(model_latencies) if model_latencies else None,
            status=model_status,
            extra={"checked_at_utc": now, "method": model_method},
        ),
    }



    reds = [name for name, c in checks.items() if c["ok"] is False]
    if not target["api_key"]:



        severity = "not_configured"
        verdict = "not_configured"
        detail = "未配置 key：运行时不构建该通路（探活占位，用于报告展示覆盖范围）"
    elif reds:
        severity = "red"
        verdict = checks[reds[0]]["verdict"]
        detail = "；".join(f"{name}: {checks[name]['verdict_text']}" for name in reds)
    else:
        warnings = [name for name, c in checks.items() if c["ok"] is True and c["verdict"] in WARN_VERDICTS]
        severity = "warn" if warnings else "green"
        verdict = checks[warnings[0]]["verdict"] if warnings else "ok"
        if warnings:
            detail = checks[warnings[0]]["verdict_text"]
        else:
            unknown = [name for name, c in checks.items() if c["ok"] is None]
            detail = "三层检查均通过" + (f"（{'、'.join(unknown)} 未下结论，按可用处理）" if unknown else "")

    latencies = [
        result.get("latency_ms")
        for result in (auth_result,)
        if result is not None and result.get("latency_ms") is not None
    ] + model_latencies
    models_ok = sum(1 for entry in model_checks.values() if _model_available(entry))
    return {
        "provider": target["provider"],
        "name": target["name"],
        "base_url": target["base_url"],
        "protocol": target["protocol"],
        "model": target["model"],

        "key_masked": (target["api_key"][:4] + "…") if len(target["api_key"]) > 10 else ("***" if target["api_key"] else ""),
        "ok": severity in ("green", "warn"),
        "severity": severity,
        "verdict": verdict,
        "verdict_text": detail,
        "checks": checks,
        "model_check_method": model_method,
        "status": model_status,
        "latency_ms": min(latencies) if latencies else (layers["tls"] or {}).get("latency_ms"),
        "models_total": len(model_checks),
        "models_ok": models_ok,
        "models": model_checks,
        "dns": layers["dns"],
        "tcp": layers["tcp"],
        "tls": layers["tls"],
        "api_attempts": [auth_result] if auth_result is not None else [],
    }







def _short_model_label(model: str) -> str:
    """报告表头用的模型短名：去掉 Fireworks 命名空间前缀，过长时截断但保留可读性。"""
    label = str(model or "")
    prefix = "accounts/fireworks/models/"
    if label.startswith(prefix):
        label = label[len(prefix):]
    if len(label) > 38:
        label = label[:37] + "…"
    return label


def build_markdown(context: dict, results: list[dict]) -> str:
    by_provider: dict[str, list[dict]] = {}
    for item in results:
        by_provider.setdefault(item["provider"], []).append(item)

    def _provider_models(items: list[dict]) -> list[str]:
        models: list[str] = []
        for item in items:
            for model_id in (item.get("models") or {}):
                if model_id not in models:
                    models.append(model_id)
        return models

    lines: list[str] = ["# 全通路探活报告", ""]
    lines.append(f"- 时间：`{context['generated_at_bj']}`（北京时间 UTC+8） / `{context['generated_at_utc']}`（UTC）")
    lines.append(f"- 环境：{context['environment']} · run `{context.get('run_id') or '本地'}`")
    if context.get("deployment") or context.get("deployment_label"):
        lines.append(f"- 部署：`{context.get('deployment') or 'unknown'}` · {context.get('deployment_label') or '—'}")
    lines.append(
        "- 方式：**不真实调用模型**（官方非生成端点：DNS/TCP/TLS → 列模型鉴权 → "
        "全模型 模型列表 / count tokens 校验）"
    )
    lines.append(
        "- 覆盖："
        + ", ".join(
            f"{k}（{len(v)} 条 / {len(_provider_models(v))} 个模型）" for k, v in by_provider.items()
        )
    )
    lines.append("")
    lines.append("## 类别结论")
    lines.append("")
    lines.append("| 类别 | 通路可用/总数 | 模型可用/总 | 结论 | 红通路 |")
    lines.append("|---|---|---|---|---|")
    for provider, items in by_provider.items():
        ok = [i for i in items if i["ok"]]
        red = [i for i in items if not i["ok"] and i["verdict"] != "not_configured"]
        configured = [i for i in items if i["verdict"] != "not_configured"]
        if ok:
            verdict_cell = "✅ 绿（照常调用）"
        elif not configured:
            verdict_cell = "➖ 未配置（无可用 key，运行时不构建任何通路）"
        else:
            verdict_cell = "❌ 红（外层 ban 掉）"
        model_ids = _provider_models(items)
        models_ok = sum(
            1
            for model_id in model_ids
            if any(_model_available((i.get("models") or {}).get(model_id)) for i in items)
        )
        lines.append(
            f"| `{provider}` | {len(ok)}/{len(items)} | {models_ok}/{len(model_ids)} | "
            f"{verdict_cell} | "
            + ("、".join(f"`{i['name']}`" for i in red) or "—")
            + " |"
        )
    lines.append("")
    warn = [i for i in results if i.get("severity") == "warn"]
    if warn:
        lines.append(
            "> ⚠️ 标黄的通路**不会**被 ban（端点通/活，只是这次探测请求不理想，"
            "例如刚好碰上 429 限流；模型不可用自 2026-10-29 起已属红档，不在此列）："
            + "、".join(f"`{i['provider']}/{i['name']}`（{i['verdict_text'][:60]}）" for i in warn)
        )
        lines.append("")
    nc = [i for i in results if i.get("verdict") == "not_configured"]
    if nc:
        lines.append(
            "> ➖ 未配置的通路不会构建（探活占位，仅展示覆盖缺口）："
            + "、".join(f"`{i['provider']}/{i['name']}`" for i in nc)
        )
        lines.append("")
    lines.append("## 逐通路")
    lines.append("")
    lines.append("| 类别 | 通路 | 代表模型 | 结论 | 网络 | 鉴权 | 模型校验 | 模型 | 延迟 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    method_labels = {"models_list": "列表", "count_tokens": "count"}
    for item in results:
        checks = item.get("checks") or {}
        def _cell(name: str) -> str:
            return _check_cell(checks.get(name))
        model_method = (checks.get("model") or {}).get("method")
        model_cell = _cell("model")
        if model_cell != "—" and model_method in method_labels:
            model_cell += f"（{method_labels[model_method]}）"
        models_cell = f"{item.get('models_ok', 0)}/{item.get('models_total', 0)}"
        lines.append(
            f"| `{item['provider']}` | {item['name']} | `{item['model']}` | {label(item['verdict'])} | "
            f"{_cell('network')} | {_cell('auth')} | {model_cell} | {models_cell}"
            f" | {item['latency_ms'] if item['latency_ms'] is not None else '—'} ms | "
            f"{item['verdict_text'][:140]} |"
        )
    lines.append("")


    lines.append("## 全模型矩阵")
    lines.append("")
    lines.append(
        "> 行 = 运行时通路，列 = Tenshi 模型注册表（`src/config/models.py`）里该 provider 的**全部**模型；"
        "格：✅ 可用 / ⚠️ 限流（不 ban）/ ❌ 不可用（**模型级 ban**：运行时跳过该「通路×模型」组合）"
        " / — 未下结论（fail-open）/ ➖ 未配置。"
    )
    lines.append("> 逐模型的 verdict / HTTP 状态 / 延迟见 JSON 的 `paths.<通路>.models`。")
    lines.append("")
    for provider, items in by_provider.items():
        model_ids = _provider_models(items)
        if not model_ids:
            continue
        models_ok = sum(
            1
            for model_id in model_ids
            if any(_model_available((i.get("models") or {}).get(model_id)) for i in items)
        )
        lines.append(f"### `{provider}`（模型 {models_ok}/{len(model_ids)} 可用）")
        lines.append("")
        header = "| 通路 | " + " | ".join(f"`{_short_model_label(m)}`" for m in model_ids) + " |"
        lines.append(header)
        lines.append("|---|" + "---|" * len(model_ids))
        for item in items:
            model_map = item.get("models") or {}
            cells = [_check_cell(model_map.get(model_id)) for model_id in model_ids]
            lines.append(f"| {item['name']} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)



HISTORY_LIMIT = 400


def write_outputs(
    json_path: pathlib.Path,
    report_path: pathlib.Path | None,
    context: dict,
    results: list[dict],
) -> dict:
    """写 schema 4 健康 JSON（+ 可选 Markdown 报告），返回 payload。"""
    providers: dict[str, dict] = {}
    provider_model_state: dict[str, dict[str, bool]] = {}
    for item in results:
        entry = providers.setdefault(
            item["provider"],
            {
                "enabled": False,
                "paths": {},
                "probe_model": item["model"],
                "models_total": 0,
                "models_ok": 0,
            },
        )
        item_models = item.get("models") or {}
        entry["paths"][item["name"]] = {
            "ok": item["ok"],
            "severity": item.get("severity"),
            "verdict": item["verdict"],
            "verdict_text": item["verdict_text"],

            "checks": item.get("checks"),
            "model_check_method": item.get("model_check_method"),
            "status": item["status"],
            "latency_ms": item["latency_ms"],
            "base_url": item["base_url"],
            "model": item["model"],
            "key_masked": item["key_masked"],

            "models_total": item.get("models_total", len(item_models)),
            "models_ok": item.get("models_ok", sum(1 for m in item_models.values() if _model_available(m))),
            "models": item_models,
        }
        state = provider_model_state.setdefault(item["provider"], {})
        for model_id, model_entry in item_models.items():

            state[model_id] = state.get(model_id, False) or _model_available(model_entry)
        if item["ok"]:
            entry["enabled"] = True




    coverage: dict[str, int] = {}
    for provider, state in provider_model_state.items():
        entry = providers[provider]
        registry_total = len(_provider_model_ids(provider)) or len(state)
        entry["models_total"] = registry_total
        entry["models_ok"] = sum(1 for ok in state.values() if ok)
        coverage[provider] = registry_total

    payload = {
        "schema": 4,
        **context,
        "coverage": coverage,
        "providers": providers,
        "summary": {
            "green": [f"{i['provider']}/{i['name']}" for i in results if i.get("severity") == "green"],
            "warn": [f"{i['provider']}/{i['name']}" for i in results if i.get("severity") == "warn"],
            "red": [f"{i['provider']}/{i['name']}" for i in results if i.get("severity") == "red"],
            "not_configured": [f"{i['provider']}/{i['name']}" for i in results if i["verdict"] == "not_configured"],
        },
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(build_markdown(context, results), encoding="utf-8")
    return payload










def build_history_entry(context: dict, payload: dict) -> dict:
    """从本次 payload 提炼一行历史记录（字段固定，dashboard 依赖）。"""
    paths: dict[str, dict] = {}
    providers: dict[str, dict] = {}
    for provider, entry in (payload.get("providers") or {}).items():
        provider_paths = entry.get("paths") or {}
        for name, item in provider_paths.items():
            paths[f"{provider}/{name}"] = {
                "severity": item.get("severity"),
                "ok": item.get("ok"),
                "verdict": item.get("verdict"),
                "latency_ms": item.get("latency_ms"),
                "models_ok": item.get("models_ok"),
                "models_total": item.get("models_total"),
            }
        providers[provider] = {
            "ok": sum(1 for p in provider_paths.values() if p.get("ok")),
            "total": len(provider_paths),
            "models_ok": entry.get("models_ok"),
            "models_total": entry.get("models_total"),
        }
    return {
        "generated_at_utc": context.get("generated_at_utc"),
        "generated_at_bj": context.get("generated_at_bj"),
        "environment": context.get("environment"),
        "deployment": context.get("deployment"),
        "deployment_label": context.get("deployment_label"),
        "run_id": context.get("run_id"),
        "probe_method": context.get("probe_method"),
        "paths": paths,
        "providers": providers,
    }


def append_history(history_path: pathlib.Path, entry: dict, *, limit: int = HISTORY_LIMIT) -> int:
    """追加一行历史 JSONL，只保留最近 limit 行；原子写（tmp + replace）。

    读取时过滤掉无法解析的行：历史文件是跨运行累积的，万一被并发合并写进了冲突
    标记 / 半截内容（2026-10-28 实际发生过），下一次运行必须能自动清洗掉，而不是
    让坏行永久留在仓库里。
    """
    history_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if history_path.exists():
        try:
            raw_lines = history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            raw_lines = []
        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                json.loads(stripped)
            except (ValueError, TypeError):
                continue
            lines.append(stripped)
    lines.append(json.dumps(entry, ensure_ascii=False))
    lines = lines[-limit:]
    tmp_path = history_path.with_name(history_path.name + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, history_path)
    return len(lines)









DEPLOYMENT_LABELS = {
    "calling": "Calling 运行时（Calling secrets）",
    "tenshi": "Tenshi 备用运行时（Tenshi secrets）",
    "local": "本地（local）",
}


def deployment_label_for(deployment: str) -> str:
    return DEPLOYMENT_LABELS.get(deployment, deployment)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="全 provider / 全通路 / 全模型探活（不真实调用模型）")
    parser.add_argument("--providers", default="claude,gemini,deepseek,gpt,glm,magmarouter,openrouter,grok", help="要探测的类别（逗号分隔）")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次网络操作超时（秒）")
    parser.add_argument("--json", default=str(ROOT / "data" / "provider_health.json"), help="健康 JSON 输出路径")
    parser.add_argument("--report", default=None, help="Markdown 报告输出路径（可选）")
    parser.add_argument("--history", default=None, help="追加历史 JSONL 路径（可选；默认不写）")
    parser.add_argument(
        "--deployment",
        choices=("calling", "tenshi", "local"),
        default="local",
        help="部署身份（写进 JSON / 历史；Calling 与 Tenshi 两套运行时各自独立探活）",
    )
    parser.add_argument("--deployment-label", default=None, help="部署展示名（默认按 --deployment 生成）")
    parser.add_argument(
        "--no-all-models",
        action="store_true",
        help="调试用：每条通路只探代表模型（默认探该 provider 的全部模型）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])



    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    all_models = not args.no_all_models
    deployment_label = args.deployment_label or deployment_label_for(args.deployment)

    for value in (
        tenshi_config.CLAUDE_API_KEY,
        tenshi_config.CLAUDE_PROXY_KEY_BASE,
        tenshi_config.CLAUDE_PROXY_KEY_PRIME,
        tenshi_config.CLAUDE_PROXY_KEY_MAX,
        tenshi_config.CLAUDE_PROXY_KEY_2,
        tenshi_config.GEMINI_API_KEY,
        tenshi_config.GEMINI_PROXY_KEY,
        tenshi_config.GPT_PROXY_KEY,
        tenshi_config.GPT_PROXY2_KEY,
        tenshi_config.DEEPSEEK_API_KEY,
        tenshi_config.FIREWORKS_API_KEY,
        tenshi_config.GPT_API_KEY,
        tenshi_config.GLM_API_KEY,
        tenshi_config.MAGMAROUTER_API_KEY,
        tenshi_config.OPENROUTER_API_KEY,
        tenshi_config.GROK_API_KEY,
    ):
        if value:
            _REDACT.append(str(value))

    json_path = pathlib.Path(args.json)
    started = now_utc()
    targets = collect_targets(providers)
    context = {
        "generated_at_utc": iso(started),
        "generated_at_bj": (started + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
        "environment": "容器" if pathlib.Path("/.dockerenv").exists() else "非容器",
        "deployment": args.deployment,
        "deployment_label": deployment_label,
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "script": "scripts/probe_provider_paths.py",

        "probe_method": "official_non_call",
    }

    model_count = sum(len(t.get("models") or [t["model"]]) for t in targets) if all_models else len(targets)
    print(
        f"[探活] 方式=不真实调用（官方非生成端点）· 部署={args.deployment}（{deployment_label}）"
        f" · 共 {len(targets)} 条通路 / {model_count} 个模型点，类别 {providers}"
    )
    results: list[dict] = []
    for target in targets:
        try:
            item = probe_target(target, args.timeout, all_models=all_models)
        except Exception as exc:
            item = {
                "provider": target["provider"],
                "name": target["name"],
                "base_url": target["base_url"],
                "protocol": target["protocol"],
                "model": target["model"],
                "key_masked": "",
                "ok": False,
                "severity": "red",
                "verdict": "http_error",
                "verdict_text": f"探测异常：{type(exc).__name__}: {exc}",
                "checks": {},
                "model_check_method": None,
                "status": None,
                "latency_ms": None,
                "models_total": 0,
                "models_ok": 0,
                "models": {},
                "dns": None,
                "tcp": None,
                "tls": None,
                "api_attempts": [],
            }
        results.append(item)
        checks = item.get("checks") or {}
        summary = " ".join(f"{name}={_check_cell(c)}" for name, c in checks.items())
        print(
            f"  [{item['provider']:<10}] {item['name']:<18} {label(item['verdict']):<12}"
            f" {summary} 模型 {item.get('models_ok', 0)}/{item.get('models_total', 0)}"
            f" {item['latency_ms']}ms"
        )

    payload = write_outputs(json_path, pathlib.Path(args.report) if args.report else None, context, results)

    if args.history:
        history_path = pathlib.Path(args.history)
        kept = append_history(history_path, build_history_entry(context, payload))
        print(f"  历史已追加 {history_path}（保留最近 {kept} 行）")

    print("\n[汇总]")
    for provider, entry in payload["providers"].items():
        good = sum(1 for p in entry["paths"].values() if p["ok"])
        configured = sum(
            1 for p in entry["paths"].values() if p.get("verdict") != "not_configured"
        )
        if entry["enabled"]:
            state = "✅ 绿"
        elif configured == 0:

            state = "➖ 未配置"
        else:
            state = "❌ 红"
        print(
            f"  {provider:<10} {state}"
            f"  {good}/{len(entry['paths'])} 条通路可用"
            f" · 模型 {entry.get('models_ok', 0)}/{entry.get('models_total', 0)}"
        )
    print(f"  已写入 {args.json}")
    if args.report:
        print(f"  报告 {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
